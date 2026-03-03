from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from threading import Lock, Thread
from typing import Dict
from urllib import request as urllib_request
from urllib.parse import parse_qs, urlparse

from src.debug_runtime import ingest_collect_request


_SERVER_STATE: Dict[str, object] = {"started": False, "host": "", "port": 0}
_SERVER_LOCK = Lock()


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
            self._set_headers(200)
            self.wfile.write(b'{"ok":true}')
            return
        if path != "/qa/collect":
            self._set_headers(404)
            self.wfile.write(b'{"ok":false,"error":"not_found"}')
            return
        parsed = urlparse(self.path)
        q = parse_qs(parsed.query, keep_blank_values=True)
        captured = ingest_collect_request(
            session_id=str(q.get("session_id", [""])[0]).strip(),
            request_url=str(q.get("request_url", [""])[0]).strip(),
            request_method=str(q.get("request_method", ["GET"])[0]).strip().upper(),
            request_body=str(q.get("request_body", [""])[0]),
        )
        self._set_headers(200)
        self.wfile.write(json.dumps({"ok": True, "captured": int(captured)}).encode("utf-8"))

    def do_POST(self) -> None:  # noqa: N802
        path = (self.path or "").split("?", 1)[0]
        if path != "/qa/collect":
            self._set_headers(404)
            self.wfile.write(b'{"ok":false,"error":"not_found"}')
            return
        payload = self._read_json_body()
        captured = ingest_collect_request(
            session_id=str(payload.get("session_id", payload.get("qa_debug_session_id", ""))).strip(),
            request_url=str(payload.get("request_url", payload.get("collect_url", ""))).strip(),
            request_method=str(payload.get("request_method", payload.get("method", "GET"))).strip().upper(),
            request_body=str(payload.get("request_body", payload.get("body", ""))),
        )
        self._set_headers(200)
        self.wfile.write(json.dumps({"ok": True, "captured": int(captured)}).encode("utf-8"))

    def log_message(self, format: str, *args) -> None:  # noqa: A003
        return


class _ReusableThreadingHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True


def _health_ok(host: str, port: int, timeout: float = 0.8) -> bool:
    url = f"http://{host}:{int(port)}/qa/health"
    try:
        with urllib_request.urlopen(url, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="ignore").strip()
            if resp.status != 200:
                return False
            try:
                parsed = json.loads(body) if body else {}
                if isinstance(parsed, dict):
                    return bool(parsed.get("ok", False))
            except Exception:
                pass
            normalized = body.lower().replace(" ", "")
            return '"ok":true' in normalized
    except Exception:
        return False


def ensure_ingest_server(host: str = "127.0.0.1", port: int = 8600) -> Dict[str, object]:
    with _SERVER_LOCK:
        if bool(_SERVER_STATE.get("started")):
            return dict(_SERVER_STATE)
        # 이미 동일 포트에서 정상 동작 중이면 외부 프로세스를 재사용한다.
        for health_host in (host, "127.0.0.1", "localhost"):
            if _health_ok(health_host, int(port)):
                _SERVER_STATE.update(
                    {"started": True, "host": health_host, "port": int(port), "external": True}
                )
                return dict(_SERVER_STATE)

        last_err: Exception | None = None
        for bind_host in (host, "0.0.0.0"):
            try:
                httpd = _ReusableThreadingHTTPServer((bind_host, int(port)), _CollectHandler)
                th = Thread(target=httpd.serve_forever, daemon=True)
                th.start()
                _SERVER_STATE.update(
                    {
                        "started": True,
                        "host": bind_host,
                        "port": int(port),
                        "thread": th,
                        "httpd": httpd,
                        "external": False,
                    }
                )
                return dict(_SERVER_STATE)
            except OSError as exc:
                last_err = exc
                # 포트 점유 상태면 기존 프로세스 헬스체크 후 재사용 처리
                if exc.errno in {98, 48}:  # Linux EADDRINUSE / macOS EADDRINUSE
                    for health_host in (host, "127.0.0.1", "localhost"):
                        if _health_ok(health_host, int(port)):
                            _SERVER_STATE.update(
                                {"started": True, "host": health_host, "port": int(port), "external": True}
                            )
                            return dict(_SERVER_STATE)
                continue
            except Exception as exc:
                last_err = exc
                continue

        if last_err is not None:
            raise RuntimeError(f"ingest server start failed: {last_err}") from last_err
        raise RuntimeError("ingest server start failed: unknown")
