from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from threading import Lock, Thread
from typing import Dict
from urllib.parse import parse_qs, urlparse

from src.debug_runtime import ingest_collect_request


_SERVER_STATE: Dict[str, object] = {
    "started": False,
    "host": "",
    "port": 0,
}
_SERVER_LOCK = Lock()


def _to_text(value: object) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _pick(payload: Dict[str, object], *keys: str) -> str:
    for key in keys:
        value = _to_text(payload.get(key))
        if value:
            return value
    return ""


def _normalize_request_payload(payload: Dict[str, object]) -> Dict[str, str]:
    return {
        "session_id": _pick(payload, "session_id", "qa_debug_session_id", "sid"),
        "request_url": _pick(payload, "request_url", "url", "collect_url"),
        "request_method": _pick(payload, "request_method", "method") or "GET",
        "request_body": _pick(payload, "request_body", "body", "post_data"),
    }


class _CollectHandler(BaseHTTPRequestHandler):
    server_version = "QAIngest/1.0"

    def _set_headers(self, code: int = 200, content_type: str = "application/json") -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
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

    def _handle_collect(self, payload: Dict[str, object]) -> None:
        norm = _normalize_request_payload(payload)
        cnt = ingest_collect_request(
            session_id=norm["session_id"],
            request_url=norm["request_url"],
            request_method=norm["request_method"] or "GET",
            request_body=norm["request_body"],
        )
        self._set_headers(200, "application/json")
        self.wfile.write(json.dumps({"ok": True, "captured": int(cnt)}).encode("utf-8"))

    def do_OPTIONS(self) -> None:  # noqa: N802
        self._set_headers(204, "text/plain")

    def do_GET(self) -> None:  # noqa: N802
        path = (self.path or "").split("?", 1)[0]
        if path == "/qa/health":
            self._set_headers(200, "application/json")
            self.wfile.write(b'{"ok":true}')
            return
        if path not in {"/qa/collect", "/qa/ingest"}:
            self._set_headers(404, "application/json")
            self.wfile.write(b'{"ok":false,"error":"not_found"}')
            return
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query, keep_blank_values=True)
        payload: Dict[str, object] = {
            "session_id": query.get("session_id", query.get("sid", [""]))[0],
            "request_url": query.get("request_url", query.get("url", [""]))[0],
            "request_method": query.get("request_method", query.get("method", ["GET"]))[0],
            "request_body": query.get("request_body", query.get("body", [""]))[0],
        }
        self._handle_collect(payload)

    def do_POST(self) -> None:  # noqa: N802
        path = (self.path or "").split("?", 1)[0]
        if path not in {"/qa/collect", "/qa/ingest"}:
            self._set_headers(404, "application/json")
            self.wfile.write(b'{"ok":false,"error":"not_found"}')
            return
        payload = self._read_json_body()
        self._handle_collect(payload)

    def log_message(self, format: str, *args) -> None:  # noqa: A003
        return


def ensure_ingest_server(host: str = "127.0.0.1", port: int = 8600) -> Dict[str, object]:
    with _SERVER_LOCK:
        if bool(_SERVER_STATE.get("started")):
            return dict(_SERVER_STATE)

        httpd = ThreadingHTTPServer((host, int(port)), _CollectHandler)
        th = Thread(target=httpd.serve_forever, daemon=True)
        th.start()
        _SERVER_STATE["started"] = True
        _SERVER_STATE["host"] = host
        _SERVER_STATE["port"] = int(port)
        _SERVER_STATE["thread"] = th
        _SERVER_STATE["httpd"] = httpd
        return dict(_SERVER_STATE)
