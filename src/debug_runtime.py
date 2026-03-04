from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import platform
from pathlib import Path
import threading
from typing import Dict, List, Optional
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from src.test_log_db import append_event as append_event_to_db
from src.test_log_db import init_test_log_db, upsert_session


@dataclass
class DebugSession:
    session_id: str
    target_url: str
    output_file: Path
    status: str
    started_at: str
    ended_at: str
    captured_events: int
    last_error: str
    tester_name: str
    tester_note: str
    db_path: Path
    launch_browser: bool
    stop_event: threading.Event
    thread: threading.Thread | None


_SESSIONS: Dict[str, DebugSession] = {}
_LOCK = threading.Lock()


def _parse_form_encoded(text: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    if not text:
        return out
    for key, value in parse_qsl(text, keep_blank_values=True):
        out[key] = value
    return out


def _is_ga_like_request(url: str, method: str, post_data: str) -> bool:
    parsed = urlparse(url)
    path = parsed.path.lower().rstrip("/")
    query = _parse_form_encoded(parsed.query)
    body = _parse_form_encoded(post_data) if method.upper() == "POST" else {}
    merged = dict(query)
    merged.update(body)

    if path.endswith("/g/collect") or "/g/collect" in path:
        return True
    if path.endswith("/mp/collect") or "/mp/collect" in path:
        return True
    if path.endswith("/collect") and (
        merged.get("v") == "2"
        or "en" in merged
        or "tid" in merged
        or "measurement_id" in query
    ):
        return True
    if (
        method.upper() == "POST"
        and post_data
        and '"events"' in post_data
        and "measurement_id" in query
    ):
        return True
    return False


def _decode_collect_event_params(payload: Dict[str, str]) -> Dict[str, object]:
    params: Dict[str, object] = {}
    for key, value in payload.items():
        if key.startswith("ep."):
            params[key[3:]] = value
        elif key.startswith("epn."):
            try:
                num = float(value)
                params[key[4:]] = int(num) if num.is_integer() else num
            except ValueError:
                params[key[4:]] = value
    if "qa_debug_session_id" not in params and payload.get("ep.qa_debug_session_id"):
        params["qa_debug_session_id"] = payload.get("ep.qa_debug_session_id", "")
    return params


def _extract_debug_sid_from_page_url(page_url: str) -> str:
    raw = str(page_url or "").strip()
    if not raw:
        return ""
    try:
        parsed = urlparse(raw)
        q = _parse_form_encoded(parsed.query)
        return str(q.get("qa_debug_session_id", "")).strip()
    except Exception:
        return ""


def _infer_session_id_from_collect(url: str, method: str, post_data: str) -> str:
    parsed = urlparse(url or "")
    query = _parse_form_encoded(parsed.query)
    body = _parse_form_encoded(post_data) if method.upper() == "POST" else {}
    merged = dict(query)
    merged.update(body)

    for key in ("qa_debug_session_id", "ep.qa_debug_session_id"):
        sid = str(merged.get(key, "")).strip()
        if sid:
            return sid

    for page_key in ("dl", "ep.page_location"):
        sid = _extract_debug_sid_from_page_url(str(merged.get(page_key, "")).strip())
        if sid:
            return sid
    return ""


def _infer_single_running_session_id() -> str:
    with _LOCK:
        running = [sid for sid, sess in _SESSIONS.items() if str(getattr(sess, "status", "")).strip() == "running"]
    if len(running) == 1:
        return running[0]
    return ""


def _extract_ga_hit_payloads(url: str, method: str, post_data: str, session_id: str) -> List[Dict[str, object]]:
    if not _is_ga_like_request(url, method, post_data):
        return []

    parsed = urlparse(url)
    query = _parse_form_encoded(parsed.query)
    path = parsed.path.lower().rstrip("/")
    hits: List[Dict[str, object]] = []

    if path.endswith("/g/collect") or "/g/collect" in path or "en" in query:
        body = _parse_form_encoded(post_data) if method.upper() == "POST" else {}
        merged = dict(query)
        merged.update(body)
        event_name = str(merged.get("en", "")).strip()
        if not event_name:
            return []
        event_params = _decode_collect_event_params(merged)
        hits.append(
            {
                "source": "ga_hit",
                "event_name": event_name,
                "params": event_params,
                "session_id": session_id,
                "page_url": merged.get("dl", ""),
                "measurement_id": merged.get("tid", ""),
                "client_id": merged.get("cid", ""),
                "request_method": method.upper(),
                "captured_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        return hits

    if path.endswith("/mp/collect") or "/mp/collect" in path or ("measurement_id" in query and post_data):
        event_params: Dict[str, object] = {}
        event_name = ""
        client_id = ""
        if post_data:
            try:
                payload = json.loads(post_data)
                if isinstance(payload, dict):
                    client_id = str(payload.get("client_id", "")).strip()
                    events = payload.get("events")
                    if isinstance(events, list):
                        for ev in events:
                            if not isinstance(ev, dict):
                                continue
                            event_name = str(ev.get("name", "")).strip()
                            if not event_name:
                                continue
                            raw_params = ev.get("params")
                            event_params = raw_params if isinstance(raw_params, dict) else {}
                            hits.append(
                                {
                                    "source": "ga_hit",
                                    "event_name": event_name,
                                    "params": event_params,
                                    "session_id": session_id,
                                    "page_url": str(event_params.get("page_location", "")).strip(),
                                    "measurement_id": query.get("measurement_id", ""),
                                    "client_id": client_id,
                                    "request_method": method.upper(),
                                    "captured_at": datetime.now(timezone.utc).isoformat(),
                                }
                            )
            except Exception:
                return []
    return hits


def _append_query(url: str, extra: Dict[str, str]) -> str:
    parsed = urlparse(url)
    q = dict(parse_qsl(parsed.query, keep_blank_values=True))
    q.update({k: v for k, v in extra.items() if str(v).strip()})
    return urlunparse(
        (
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            parsed.params,
            urlencode(q),
            parsed.fragment,
        )
    )


def _launch_browser(playwright):
    last_err: Exception | None = None
    attempt_errors: List[str] = []
    # EC2(무GUI) 환경에서도 동작하도록 headed -> headless 순으로 폴백한다.
    for launch_kwargs in (
        {"headless": False, "channel": "chrome"},
        {"headless": False},
        {"headless": True, "channel": "chrome"},
        {"headless": True},
        # 일부 EC2/컨테이너 환경에서 sandbox 관련 실패를 우회하기 위한 최후 폴백
        {"headless": True, "channel": "chrome", "args": ["--no-sandbox", "--disable-dev-shm-usage"]},
        {"headless": True, "args": ["--no-sandbox", "--disable-dev-shm-usage"]},
    ):
        if platform.system().lower() != "linux" and "args" in launch_kwargs:
            continue
        try:
            return playwright.chromium.launch(**launch_kwargs)
        except Exception as exc:  # pragma: no cover - runtime environment dependent
            last_err = exc
            attempt_errors.append(f"{launch_kwargs}: {exc}")
    detail = attempt_errors[-1] if attempt_errors else str(last_err or "")
    raise RuntimeError(
        "디버깅 브라우저 실행에 실패했습니다. "
        "Chrome/Chromium 설치 여부와 playwright 브라우저 설치 상태를 확인하세요. "
        "EC2에서는 `/opt/ga4-qa-mvp/.venv/bin/playwright install --with-deps chromium` 실행 후 서비스 재시작이 필요할 수 있습니다. "
        f"(last_error: {detail})"
    ) from last_err


def _build_init_script(session_id: str) -> str:
    sid = json.dumps(session_id)
    return f"""
(() => {{
  const SID = {sid};
  const MODE_KEY = "qa_debug_mode";
  const SID_KEY = "qa_debug_session_id";

  try {{
    sessionStorage.setItem(MODE_KEY, "1");
    if (SID) {{
      sessionStorage.setItem(SID_KEY, SID);
    }}
  }} catch (e) {{}}

  const safeClone = (obj) => {{
    try {{
      return JSON.parse(JSON.stringify(obj));
    }} catch (e) {{
      return {{}};
    }}
  }};

  const emit = (source, eventName, params) => {{
    const payload = {{
      source,
      event_name: eventName || "",
      params: params && typeof params === "object" ? params : {{}},
      session_id: SID || "",
      page_url: location.href,
      captured_at: new Date().toISOString()
    }};
    try {{
      if (typeof window.__qaDebugEmit === "function") {{
        window.__qaDebugEmit(payload);
      }}
    }} catch (e) {{}}
    try {{
      window.postMessage({{ type: "QA_DEBUG_EVENT", payload }}, "*");
    }} catch (e) {{}}
  }};

  const attachSession = (obj) => {{
    if (!obj || typeof obj !== "object" || Array.isArray(obj)) return obj;
    if (SID && !obj[SID_KEY]) {{
      obj[SID_KEY] = SID;
    }}
    return obj;
  }};

  const ensureHitIndicator = () => {{
    let el = document.getElementById("__qaHitIndicator");
    if (el) return el;
    el = document.createElement("div");
    el.id = "__qaHitIndicator";
    el.textContent = "QA HIT 0";
    el.style.position = "fixed";
    el.style.top = "14px";
    el.style.right = "14px";
    el.style.zIndex = "2147483647";
    el.style.padding = "8px 10px";
    el.style.borderRadius = "10px";
    el.style.background = "#111827";
    el.style.color = "#ffffff";
    el.style.border = "2px solid transparent";
    el.style.font = "600 12px/1.2 -apple-system, BlinkMacSystemFont, Segoe UI, sans-serif";
    el.style.boxShadow = "0 6px 18px rgba(0,0,0,0.28)";
    el.style.transition = "transform .16s ease, background-color .16s ease, border-color .16s ease, box-shadow .16s ease";
    document.documentElement.appendChild(el);
    return el;
  }};

  const ensureHitFlashLayer = () => {{
    let layer = document.getElementById("__qaHitFlashLayer");
    if (layer) return layer;
    layer = document.createElement("div");
    layer.id = "__qaHitFlashLayer";
    layer.style.position = "fixed";
    layer.style.inset = "0";
    layer.style.zIndex = "2147483646";
    layer.style.pointerEvents = "none";
    layer.style.opacity = "0";
    layer.style.background = "rgba(34,197,94,0)";
    layer.style.transition = "opacity .14s ease, background-color .14s ease";
    document.documentElement.appendChild(layer);
    return layer;
  }};

  const flashHit = (url, transport) => {{
    try {{
      const count = Number(window.__qaHitCount || 0) + 1;
      window.__qaHitCount = count;
      const badge = ensureHitIndicator();
      const flashLayer = ensureHitFlashLayer();
      const eventName = (() => {{
        try {{
          const u = new URL(String(url || ""), location.href);
          return String(u.searchParams.get("en") || "").trim();
        }} catch (e) {{
          return "";
        }}
      }})();
      badge.textContent = `QA HIT ${{count}}`;
      badge.title = eventName ? `collect event=${{eventName}} / ${{transport || "unknown"}}` : `collect / ${{transport || "unknown"}}`;
      badge.style.background = "#16a34a";
      badge.style.transform = "scale(1.08)";
      badge.style.borderColor = "rgba(255,255,255,.85)";
      badge.style.boxShadow = "0 0 0 4px rgba(34,197,94,.35), 0 10px 22px rgba(0,0,0,.38)";
      clearTimeout(window.__qaHitBadgeTimer);
      window.__qaHitBadgeTimer = setTimeout(() => {{
        badge.style.background = "#111827";
        badge.style.transform = "scale(1)";
        badge.style.borderColor = "transparent";
        badge.style.boxShadow = "0 6px 18px rgba(0,0,0,0.28)";
      }}, 360);

      flashLayer.style.opacity = "1";
      flashLayer.style.backgroundColor = "rgba(34,197,94,.16)";
      clearTimeout(window.__qaHitFlashTimer);
      window.__qaHitFlashTimer = setTimeout(() => {{
        flashLayer.style.opacity = "0";
        flashLayer.style.backgroundColor = "rgba(34,197,94,0)";
      }}, 190);

      document.documentElement.style.boxShadow = "inset 0 0 0 5px rgba(34,197,94,.95)";
      clearTimeout(window.__qaHitBorderTimer);
      window.__qaHitBorderTimer = setTimeout(() => {{
        document.documentElement.style.boxShadow = "";
      }}, 420);

      try {{
        window.postMessage({{
          type: "QA_DEBUG_HIT",
          payload: {{
            transport: transport || "",
            collect_url: String(url || ""),
            hit_count: count
          }}
        }}, "*");
      }} catch (e) {{}}
    }} catch (e) {{}}
  }};

  const isCollectUrl = (rawUrl) => {{
    try {{
      const u = new URL(String(rawUrl || ""), location.href);
      const path = (u.pathname || "").toLowerCase();
      const q = u.searchParams;
      if (path.includes("/g/collect") || path.includes("/mp/collect")) {{
        return true;
      }}
      if (path.endsWith("/collect")) {{
        if (q.get("v") === "2" || q.has("en") || q.has("tid") || q.has("measurement_id")) {{
          return true;
        }}
      }}
      return false;
    }} catch (e) {{
      return false;
    }}
  }};

  const wrapDataLayer = () => {{
    window.dataLayer = window.dataLayer || [];
    if (window.__qaDataLayerWrapped || typeof window.dataLayer.push !== "function") {{
      return;
    }}
    const originalPush = window.dataLayer.push.bind(window.dataLayer);
    window.dataLayer.push = function () {{
      const args = Array.prototype.slice.call(arguments);
      const obj = args[0];
      if (obj && typeof obj === "object" && !Array.isArray(obj)) {{
        attachSession(obj);
        const eventName = obj.event || "";
        emit("dataLayer.push", eventName, safeClone(obj));
      }}
      return originalPush.apply(window.dataLayer, args);
    }};
    window.__qaDataLayerWrapped = true;
  }};

  const wrapGtag = () => {{
    if (window.__qaGtagWrapped || typeof window.gtag !== "function") {{
      return;
    }}
    const originalGtag = window.gtag;
    window.gtag = function () {{
      const args = Array.prototype.slice.call(arguments);
      if (args[0] === "event") {{
        const eventName = args[1] || "";
        const params = (args[2] && typeof args[2] === "object") ? args[2] : {{}};
        attachSession(params);
        args[2] = params;
        emit("gtag", eventName, safeClone(params));
      }}
      return originalGtag.apply(window, args);
    }};
    window.__qaGtagWrapped = true;
  }};

  const wrapBeacon = () => {{
    if (window.__qaBeaconWrapped) return;
    if (!navigator || typeof navigator.sendBeacon !== "function") {{
      window.__qaBeaconWrapped = true;
      return;
    }}
    const originalBeacon = navigator.sendBeacon.bind(navigator);
    navigator.sendBeacon = function (url, data) {{
      if (isCollectUrl(url)) {{
        flashHit(url, "sendBeacon");
      }}
      return originalBeacon(url, data);
    }};
    window.__qaBeaconWrapped = true;
  }};

  const wrapFetch = () => {{
    if (window.__qaFetchWrapped || typeof window.fetch !== "function") {{
      return;
    }}
    const originalFetch = window.fetch.bind(window);
    window.fetch = function (input, init) {{
      const url = typeof input === "string"
        ? input
        : (input && typeof input.url === "string" ? input.url : "");
      if (isCollectUrl(url)) {{
        flashHit(url, "fetch");
      }}
      return originalFetch(input, init);
    }};
    window.__qaFetchWrapped = true;
  }};

  const wrapXHR = () => {{
    if (window.__qaXHRWrapped || typeof window.XMLHttpRequest === "undefined") {{
      return;
    }}
    const originalOpen = window.XMLHttpRequest.prototype.open;
    const originalSend = window.XMLHttpRequest.prototype.send;
    window.XMLHttpRequest.prototype.open = function (method, url) {{
      this.__qaUrl = url;
      return originalOpen.apply(this, arguments);
    }};
    window.XMLHttpRequest.prototype.send = function (body) {{
      if (isCollectUrl(this.__qaUrl)) {{
        flashHit(this.__qaUrl, "xhr");
      }}
      return originalSend.apply(this, arguments);
    }};
    window.__qaXHRWrapped = true;
  }};

  ensureHitIndicator();
  wrapDataLayer();
  wrapGtag();
  wrapBeacon();
  wrapFetch();
  wrapXHR();
  setInterval(() => {{
    wrapDataLayer();
    wrapGtag();
    wrapBeacon();
    wrapFetch();
    wrapXHR();
  }}, 1000);
}})();
"""


def _append_event(output_file: Path, payload: Dict[str, object]) -> None:
    output_file.parent.mkdir(parents=True, exist_ok=True)
    with output_file.open("a", encoding="utf-8") as fp:
        fp.write(json.dumps(payload, ensure_ascii=False) + "\n")


def _append_event_with_db(output_file: Path, payload: Dict[str, object], db_path: Path) -> None:
    _append_event(output_file, payload)
    try:
        append_event_to_db(db_path, payload)
    except Exception:
        return


def _session_to_payload(session: DebugSession) -> Dict[str, object]:
    return {
        "session_id": session.session_id,
        "target_url": session.target_url,
        "status": session.status,
        "started_at": session.started_at,
        "ended_at": session.ended_at,
        "captured_events": session.captured_events,
        "last_error": session.last_error,
        "tester_name": session.tester_name,
        "tester_note": session.tester_note,
    }


def _sync_session_db(session_id: str) -> None:
    with _LOCK:
        session = _SESSIONS.get(session_id)
        if not session:
            return
        payload = _session_to_payload(session)
        db_path = session.db_path
    try:
        init_test_log_db(db_path)
        upsert_session(db_path, payload)
    except Exception:
        return


def _set_session_fields(session_id: str, **kwargs) -> None:
    with _LOCK:
        session = _SESSIONS.get(session_id)
        if not session:
            return
        for k, v in kwargs.items():
            setattr(session, k, v)


def _get_session(session_id: str) -> Optional[DebugSession]:
    with _LOCK:
        return _SESSIONS.get(session_id)


def _run_debug_session(session_id: str) -> None:
    session = _get_session(session_id)
    if not session:
        return

    try:
        from playwright.sync_api import sync_playwright
    except ModuleNotFoundError as exc:
        _set_session_fields(
            session_id,
            status="error",
            last_error=(
                "playwright가 설치되어 있지 않습니다. "
                "pip install playwright && playwright install chromium 실행 후 재시도하세요."
            ),
            ended_at=datetime.now(timezone.utc).isoformat(),
        )
        return

    try:
        output_path = session.output_file
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text("", encoding="utf-8")
        init_test_log_db(session.db_path)
        _sync_session_db(session_id)

        with sync_playwright() as p:
            browser = _launch_browser(p)
            context = browser.new_context()

            def on_emit(source, payload):  # noqa: ANN001
                try:
                    if not isinstance(payload, dict):
                        return
                    payload.setdefault("captured_at", datetime.now(timezone.utc).isoformat())
                    payload.setdefault("session_id", session_id)
                    payload.setdefault("source", "browser")
                    _append_event_with_db(output_path, payload, session.db_path)
                    with _LOCK:
                        s = _SESSIONS.get(session_id)
                        if s:
                            s.captured_events += 1
                            should_sync = (s.captured_events % 10 == 0)
                        else:
                            should_sync = False
                    if should_sync:
                        _sync_session_db(session_id)
                except Exception:
                    return

            context.expose_binding("__qaDebugEmit", on_emit)
            context.add_init_script(_build_init_script(session_id))

            def on_request(request):  # noqa: ANN001
                try:
                    # Playwright request hook -> Event Collector core path
                    # (/qa/collect과 동일한 ingest 로직을 재사용)
                    captured = ingest_collect_request(
                        session_id=session_id,
                        request_url=str(request.url),
                        request_method=str(request.method),
                        request_body=str(request.post_data or ""),
                    )
                    if not captured:
                        return
                    with _LOCK:
                        s = _SESSIONS.get(session_id)
                        if s:
                            # ingest_collect_request 내부에서 누적되므로 중복 증가 방지
                            should_sync = (s.captured_events % 10 == 0)
                        else:
                            should_sync = False
                    if should_sync:
                        _sync_session_db(session_id)
                except Exception:
                    return

            context.on("request", on_request)

            test_url = _append_query(
                session.target_url,
                {"qa_debug_mode": "1", "qa_debug_session_id": session_id},
            )
            page = context.new_page()
            page.goto(test_url, wait_until="domcontentloaded")
            page.bring_to_front()

            while not session.stop_event.is_set():
                page.wait_for_timeout(500)

            context.close()
            browser.close()

        _set_session_fields(
            session_id,
            status="stopped",
            ended_at=datetime.now(timezone.utc).isoformat(),
        )
        _sync_session_db(session_id)
    except Exception as exc:  # pragma: no cover - runtime dependent
        _set_session_fields(
            session_id,
            status="error",
            last_error=str(exc),
            ended_at=datetime.now(timezone.utc).isoformat(),
        )
        _sync_session_db(session_id)


def ingest_collect_request(
    session_id: str,
    request_url: str,
    request_method: str = "GET",
    request_body: str = "",
) -> int:
    sid = session_id.strip()
    if not sid:
        sid = _infer_session_id_from_collect(
            url=request_url.strip(),
            method=(request_method or "GET").strip().upper(),
            post_data=request_body or "",
        )
    if not sid:
        sid = _infer_single_running_session_id()
    if not sid or not request_url.strip():
        return 0

    fallback_output_path = Path(f"data/debug_stream/{sid}.jsonl")
    fallback_db_path = Path("data/test_logs/qa_runs.db")
    with _LOCK:
        session = _SESSIONS.get(sid)
        output_path = session.output_file if session else fallback_output_path
        db_path = session.db_path if session else fallback_db_path

    if session is None:
        try:
            init_test_log_db(db_path)
            upsert_session(
                db_path,
                {
                    "session_id": sid,
                    "target_url": "",
                    "status": "running",
                    "started_at": datetime.now(timezone.utc).isoformat(),
                    "ended_at": "",
                    "captured_events": 0,
                    "last_error": "",
                    "tester_name": "",
                    "tester_note": "extension_collect",
                },
            )
        except Exception:
            pass

    hits = _extract_ga_hit_payloads(
        url=request_url.strip(),
        method=(request_method or "GET").strip().upper(),
        post_data=request_body or "",
        session_id=sid,
    )
    if not hits:
        return 0

    for payload in hits:
        _append_event_with_db(output_path, payload, db_path)

    with _LOCK:
        current = _SESSIONS.get(sid)
        if current:
            current.captured_events += len(hits)
            should_sync = (current.captured_events % 10 == 0)
        else:
            should_sync = False
    if should_sync:
        _sync_session_db(sid)
    return len(hits)


def start_debug_session(
    session_id: str,
    target_url: str,
    output_file: Path,
    tester_name: str = "",
    tester_note: str = "",
    db_path: Path | None = None,
    launch_browser: bool = True,
) -> Dict[str, object]:
    sid = session_id.strip()
    url = target_url.strip()
    if not sid:
        raise ValueError("session_id가 비어 있습니다.")
    if not url:
        raise ValueError("디버깅 대상 URL을 입력하세요.")
    effective_db_path = Path(db_path) if db_path is not None else Path("data/test_logs/qa_runs.db")

    with _LOCK:
        existing = _SESSIONS.get(sid)
        if existing and existing.status == "running":
            return get_debug_session_snapshot(sid)

        stop_event = threading.Event()
        session = DebugSession(
            session_id=sid,
            target_url=url,
            output_file=Path(output_file),
            status="running",
            started_at=datetime.now(timezone.utc).isoformat(),
            ended_at="",
            captured_events=0,
            last_error="",
            tester_name=tester_name.strip(),
            tester_note=tester_note.strip(),
            db_path=effective_db_path,
            launch_browser=bool(launch_browser),
            stop_event=stop_event,
            thread=None,
        )
        _SESSIONS[sid] = session

    _sync_session_db(sid)

    if bool(launch_browser):
        th = threading.Thread(target=_run_debug_session, args=(sid,), daemon=True)
        session.thread = th
        th.start()
    return get_debug_session_snapshot(sid)


def stop_debug_session(session_id: str) -> Dict[str, object]:
    sid = session_id.strip()
    if not sid:
        return {}
    with _LOCK:
        session = _SESSIONS.get(sid)
        if not session:
            return {}
        session.stop_event.set()
        if session.status == "running":
            session.status = "stopping" if session.launch_browser else "stopped"
            if not session.launch_browser:
                session.ended_at = datetime.now(timezone.utc).isoformat()
    _sync_session_db(sid)
    return get_debug_session_snapshot(sid)


def get_debug_session_snapshot(session_id: str) -> Dict[str, object]:
    sid = session_id.strip()
    if not sid:
        return {}
    with _LOCK:
        session = _SESSIONS.get(sid)
        if not session:
            return {}
        return {
            "session_id": session.session_id,
            "target_url": session.target_url,
            "output_file": str(session.output_file),
            "status": session.status,
            "started_at": session.started_at,
            "ended_at": session.ended_at,
            "captured_events": session.captured_events,
            "last_error": session.last_error,
            "tester_name": session.tester_name,
            "tester_note": session.tester_note,
            "db_path": str(session.db_path),
            "launch_browser": bool(session.launch_browser),
        }


def load_debug_events(output_file: Path, limit: int = 200):
    import pandas as pd

    path = Path(output_file)
    if not path.exists():
        return pd.DataFrame()

    # 실시간 append 중에는 마지막 줄이 부분적으로 쓰여 JSON parse가 깨질 수 있어
    # 라인 단위로 안전 파싱(깨진 라인은 건너뜀)한다.
    records: List[Dict[str, object]] = []
    try:
        with path.open("r", encoding="utf-8") as fp:
            for raw_line in fp:
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except Exception:
                    continue
                if isinstance(row, dict):
                    records.append(row)
    except Exception:
        return pd.DataFrame()

    if not records:
        return pd.DataFrame()

    df = pd.DataFrame(records)
    if "captured_at" in df.columns:
        df["captured_at"] = pd.to_datetime(df["captured_at"], errors="coerce")
        df = df.sort_values("captured_at", ascending=False)
    return df.head(int(limit)).copy()
