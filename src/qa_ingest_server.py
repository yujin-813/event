from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from threading import Lock, Thread
import time
from typing import Dict
from urllib import error as urllib_error
from urllib import request as urllib_request
from urllib.parse import parse_qs, urlparse

from src.debug_runtime import (
    infer_collect_session_id,
    ingest_collect_request,
    is_collect_session_allowed,
)


_SERVER_STATE: Dict[str, object] = {"started": False, "host": "", "port": 0}
_SERVER_LOCK = Lock()
_RATE_LIMIT_LOCK = Lock()
_RATE_LIMIT_STATE: Dict[str, Dict[str, float]] = {}
_UPSTREAM_GA_HOSTS = {
    "www.google-analytics.com",
    "region1.google-analytics.com",
    "analytics.google.com",
}


def _truthy(value: object) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on", "y"}


def _analytics_proxy_enabled() -> bool:
    return _truthy(os.getenv("QA_ANALYTICS_PROXY_ENABLED", "0"))


def _analytics_proxy_allow_any() -> bool:
    return _truthy(os.getenv("QA_ANALYTICS_PROXY_ALLOW_ANY", "0"))


def _analytics_proxy_timeout_sec() -> float:
    raw = str(os.getenv("QA_ANALYTICS_PROXY_TIMEOUT_SEC", "2.5")).strip()
    try:
        val = float(raw)
    except Exception:
        val = 2.5
    if val < 0.3:
        return 0.3
    if val > 10:
        return 10.0
    return val


def _collect_rate_limit_enabled() -> bool:
    return _truthy(os.getenv("QA_COLLECT_RATE_LIMIT_ENABLED", "1"))


def _collect_rate_limit_rps() -> float:
    raw = str(os.getenv("QA_COLLECT_RATE_LIMIT_RPS", "8")).strip()
    try:
        val = float(raw)
    except Exception:
        val = 8.0
    if val < 1:
        return 1.0
    if val > 500:
        return 500.0
    return val


def _collect_rate_limit_burst() -> float:
    raw = str(os.getenv("QA_COLLECT_RATE_LIMIT_BURST", "24")).strip()
    try:
        val = float(raw)
    except Exception:
        val = 24.0
    if val < 1:
        return 1.0
    if val > 2000:
        return 2000.0
    return val


def _collect_require_active_session() -> bool:
    return _truthy(os.getenv("QA_COLLECT_REQUIRE_ACTIVE_SESSION", "1"))


def _extract_client_ip(handler: BaseHTTPRequestHandler) -> str:
    xff = str(handler.headers.get("X-Forwarded-For", "")).strip()
    if xff:
        return xff.split(",")[0].strip()
    xr = str(handler.headers.get("X-Real-IP", "")).strip()
    if xr:
        return xr
    try:
        return str(handler.client_address[0]).strip()
    except Exception:
        return ""


def _allow_rate_limited_request(key: str) -> tuple[bool, float]:
    if not _collect_rate_limit_enabled():
        return True, 0.0
    now = time.monotonic()
    rate = _collect_rate_limit_rps()
    burst = _collect_rate_limit_burst()
    with _RATE_LIMIT_LOCK:
        state = _RATE_LIMIT_STATE.get(key, {"tokens": burst, "updated_at": now})
        elapsed = max(0.0, now - float(state.get("updated_at", now)))
        tokens = min(burst, float(state.get("tokens", burst)) + elapsed * rate)
        allowed = tokens >= 1.0
        retry_after = 0.0
        if allowed:
            tokens -= 1.0
        else:
            retry_after = max(0.1, (1.0 - tokens) / rate)
        _RATE_LIMIT_STATE[key] = {"tokens": tokens, "updated_at": now}
        if len(_RATE_LIMIT_STATE) > 20000:
            stale_cut = now - 3600
            for rk, rv in list(_RATE_LIMIT_STATE.items()):
                if float(rv.get("updated_at", now)) < stale_cut:
                    _RATE_LIMIT_STATE.pop(rk, None)
    return allowed, retry_after


def _allowed_upstream(url: str) -> bool:
    target = str(url or "").strip()
    if not target:
        return False
    try:
        parsed = urlparse(target)
    except Exception:
        return False

    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return False
    if _analytics_proxy_allow_any():
        return True
    host = (parsed.hostname or "").strip().lower()
    return host in _UPSTREAM_GA_HOSTS


def _forward_collect_request(request_url: str, request_method: str, request_body: str) -> Dict[str, object]:
    url = str(request_url or "").strip()
    method = str(request_method or "GET").strip().upper()
    body_text = str(request_body or "")
    if not url:
        return {"forwarded": False, "status": 0, "reason": "missing_request_url"}
    if method not in {"GET", "POST"}:
        method = "GET"
    if not _allowed_upstream(url):
        return {"forwarded": False, "status": 0, "reason": "upstream_not_allowed"}

    data = body_text.encode("utf-8") if (method == "POST" and body_text) else None
    headers = {"User-Agent": "GA4-QA-Reporter/1.0"}
    if data is not None:
        headers["Content-Type"] = "application/x-www-form-urlencoded; charset=utf-8"
    req = urllib_request.Request(url, method=method, data=data, headers=headers)

    try:
        with urllib_request.urlopen(req, timeout=_analytics_proxy_timeout_sec()) as resp:
            # 응답 바디는 판정에 사용하지 않고 상태코드만 확인한다.
            _ = resp.read(1)
            return {"forwarded": True, "status": int(resp.status), "reason": "ok"}
    except urllib_error.HTTPError as exc:
        try:
            _ = exc.read(1)
        except Exception:
            pass
        return {"forwarded": False, "status": int(exc.code), "reason": f"http_{exc.code}"}
    except Exception as exc:
        return {"forwarded": False, "status": 0, "reason": f"{type(exc).__name__}: {exc}"}


class _CollectHandler(BaseHTTPRequestHandler):
    server_version = "QAIngest/1.0"

    def _set_headers(self, code: int = 200) -> None:
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def _write_json(self, code: int, payload: Dict[str, object]) -> None:
        self._set_headers(code)
        self.wfile.write(json.dumps(payload, separators=(",", ":")).encode("utf-8"))

    def _read_json_body(self) -> Dict[str, object]:
        try:
            length = int(self.headers.get("Content-Length", "0") or 0)
        except Exception:
            length = 0
        if length <= 0:
            return {}
        raw = self.rfile.read(min(length, 262144))
        if not raw:
            return {}
        try:
            data = json.loads(raw.decode("utf-8", errors="ignore"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def do_OPTIONS(self) -> None:  # noqa: N802
        self._set_headers(204)

    def do_GET(self) -> None:  # noqa: N802
        path = (self.path or "").split("?", 1)[0]
        if path == "/qa/health":
            payload: Dict[str, object] = {
                "ok": True,
                "analytics_proxy_enabled": _analytics_proxy_enabled(),
                "analytics_proxy_allow_any": _analytics_proxy_allow_any(),
                "collect_require_active_session": _collect_require_active_session(),
                "collect_rate_limit_enabled": _collect_rate_limit_enabled(),
                "collect_rate_limit_rps": _collect_rate_limit_rps(),
                "collect_rate_limit_burst": _collect_rate_limit_burst(),
            }
            self._write_json(200, payload)
            return
        if path != "/qa/collect":
            self._write_json(404, {"ok": False, "error": "not_found"})
            return
        client_ip = _extract_client_ip(self)
        allow, retry_after = _allow_rate_limited_request(f"ip:{client_ip}")
        if not allow:
            self._write_json(
                429,
                {
                    "ok": False,
                    "error": "rate_limited",
                    "retry_after_sec": round(float(retry_after), 3),
                },
            )
            return
        parsed = urlparse(self.path)
        q = parse_qs(parsed.query, keep_blank_values=True)
        req_url = str(q.get("request_url", [""])[0]).strip()
        req_method = str(q.get("request_method", ["GET"])[0]).strip().upper()
        req_body = str(q.get("request_body", [""])[0])
        sid = str(q.get("session_id", [""])[0]).strip() or infer_collect_session_id(req_url, req_method, req_body)
        if _collect_require_active_session():
            if not sid or not is_collect_session_allowed(sid):
                self._write_json(403, {"ok": False, "error": "invalid_or_inactive_session"})
                return
        captured = ingest_collect_request(
            session_id=sid,
            request_url=req_url,
            request_method=req_method,
            request_body=req_body,
        )
        forward_requested = _truthy(q.get("forward_to_ga", [""])[0]) or _truthy(q.get("forward", [""])[0])
        should_forward = _analytics_proxy_enabled() or forward_requested
        proxy_result = (
            _forward_collect_request(req_url, req_method, req_body)
            if should_forward
            else {"forwarded": False, "status": 0, "reason": "disabled"}
        )
        self._write_json(
            200,
            {
                "ok": True,
                "captured": int(captured),
                "session_id": sid,
                "analytics_proxy": {
                    "enabled": bool(_analytics_proxy_enabled()),
                    "requested": bool(forward_requested),
                    **proxy_result,
                },
            },
        )

    def do_POST(self) -> None:  # noqa: N802
        path = (self.path or "").split("?", 1)[0]
        if path != "/qa/collect":
            self._write_json(404, {"ok": False, "error": "not_found"})
            return
        client_ip = _extract_client_ip(self)
        allow, retry_after = _allow_rate_limited_request(f"ip:{client_ip}")
        if not allow:
            self._write_json(
                429,
                {
                    "ok": False,
                    "error": "rate_limited",
                    "retry_after_sec": round(float(retry_after), 3),
                },
            )
            return
        payload = self._read_json_body()
        req_url = str(payload.get("request_url", payload.get("collect_url", ""))).strip()
        req_method = str(payload.get("request_method", payload.get("method", "GET"))).strip().upper()
        req_body = str(payload.get("request_body", payload.get("body", "")))
        sid = str(payload.get("session_id", payload.get("qa_debug_session_id", ""))).strip() or infer_collect_session_id(
            req_url, req_method, req_body
        )
        if _collect_require_active_session():
            if not sid or not is_collect_session_allowed(sid):
                self._write_json(403, {"ok": False, "error": "invalid_or_inactive_session"})
                return
        captured = ingest_collect_request(
            session_id=sid,
            request_url=req_url,
            request_method=req_method,
            request_body=req_body,
        )
        forward_requested = _truthy(payload.get("forward_to_ga", payload.get("forward", "")))
        should_forward = _analytics_proxy_enabled() or forward_requested
        proxy_result = (
            _forward_collect_request(req_url, req_method, req_body)
            if should_forward
            else {"forwarded": False, "status": 0, "reason": "disabled"}
        )
        self._write_json(
            200,
            {
                "ok": True,
                "captured": int(captured),
                "session_id": sid,
                "analytics_proxy": {
                    "enabled": bool(_analytics_proxy_enabled()),
                    "requested": bool(forward_requested),
                    **proxy_result,
                },
            },
        )

    def log_message(self, format: str, *args) -> None:  # noqa: A003
        return


class _ReusableThreadingHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True


def ensure_ingest_server(host: str = "127.0.0.1", port: int = 8600) -> Dict[str, object]:
    with _SERVER_LOCK:
        if bool(_SERVER_STATE.get("started")):
            return dict(_SERVER_STATE)
        httpd = _ReusableThreadingHTTPServer((host, int(port)), _CollectHandler)
        th = Thread(target=httpd.serve_forever, daemon=True)
        th.start()
        _SERVER_STATE.update(
            {
                "started": True,
                "host": host,
                "port": int(port),
                "thread": th,
                "httpd": httpd,
            }
        )
        return dict(_SERVER_STATE)


def run_ingest_server_forever(host: str = "127.0.0.1", port: int = 8600) -> None:
    httpd = _ReusableThreadingHTTPServer((host, int(port)), _CollectHandler)
    try:
        httpd.serve_forever()
    finally:
        httpd.server_close()


if __name__ == "__main__":
    host = str(os.getenv("QA_INGEST_HOST", "127.0.0.1")).strip() or "127.0.0.1"
    port_raw = str(os.getenv("QA_INGEST_PORT", "8600")).strip() or "8600"
    try:
        port = int(port_raw)
    except Exception:
        port = 8600
    run_ingest_server_forever(host=host, port=port)
