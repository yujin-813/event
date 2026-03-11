from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import os
import platform
from pathlib import Path
import re
import threading
import time
from typing import Dict, List, Optional
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from src.test_log_db import append_event as append_event_to_db
from src.test_log_db import get_session, init_test_log_db, upsert_session


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
    run_settings: Dict[str, object]
    stop_event: threading.Event
    thread: threading.Thread | None
    matched_definition_events: set[str] = field(default_factory=set)


_SESSIONS: Dict[str, DebugSession] = {}
_LOCK = threading.Lock()
_SESSION_ID_PATTERN = re.compile(r"^dbg_\d{8}_\d{6}(?:_\d{6})?_[0-9a-f]{4,16}$")


def _max_running_debug_sessions() -> int:
    raw = str(os.getenv("QA_MAX_RUNNING_DEBUG_SESSIONS", "1")).strip()
    try:
        val = int(raw)
    except Exception:
        val = 1
    if val < 1:
        return 1
    if val > 20:
        return 20
    return val


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


def infer_collect_session_id(request_url: str, request_method: str = "GET", request_body: str = "") -> str:
    return _infer_session_id_from_collect(
        url=str(request_url or "").strip(),
        method=str(request_method or "GET").strip().upper(),
        post_data=str(request_body or ""),
    )


def is_collect_session_allowed(session_id: str, db_path: Path | None = None) -> bool:
    sid = str(session_id or "").strip()
    if not sid or not _SESSION_ID_PATTERN.match(sid):
        return False

    with _LOCK:
        sess = _SESSIONS.get(sid)
        if sess and str(sess.status).strip() in {"running", "stopping"}:
            return True

    effective_db = Path(db_path) if db_path is not None else Path("data/test_logs/qa_runs.db")
    try:
        db_row = get_session(effective_db, sid)
        db_status = str(db_row.get("status", "")).strip()
        if db_status in {"running", "stopping"}:
            return True
    except Exception:
        pass

    # 런타임 메모리가 사라진 경우 파일 기준 복구 세션도 허용하되, 오래된 세션은 차단한다.
    try:
        p = Path(f"data/debug_stream/{sid}.jsonl")
        if p.exists():
            age_sec = max(0.0, time.time() - p.stat().st_mtime)
            if age_sec <= 2 * 60 * 60:
                return True
    except Exception:
        return False
    return False


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


def _is_truthy(value: object) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on", "y"}


def _env_int(key: str, default: int) -> int:
    raw = str(os.getenv(key, "")).strip()
    if not raw:
        return default
    try:
        return int(raw)
    except Exception:
        return default


def _clamp_int(value: object, default: int, minimum: int, maximum: int) -> int:
    try:
        num = int(value)
    except Exception:
        num = default
    return max(minimum, min(maximum, num))


def _normalize_run_settings(run_settings: Optional[Dict[str, object]] = None) -> Dict[str, object]:
    raw = dict(run_settings or {})
    browser_name = str(raw.get("browser_name", "chrome") or "chrome").strip().lower()
    if browser_name not in {"chrome", "chromium"}:
        browser_name = "chrome"
    definition_event_names: List[str] = []
    raw_definition_events = raw.get("definition_event_names", [])
    if isinstance(raw_definition_events, (list, tuple, set)):
        for item in raw_definition_events:
            name = str(item or "").strip()
            if name and name not in definition_event_names:
                definition_event_names.append(name)
    elif str(raw_definition_events or "").strip():
        definition_event_names.append(str(raw_definition_events).strip())
    raw_definition_hints = raw.get("definition_runtime_hints", {})
    definition_runtime_hints = {
        "target_ids": [],
        "section_names": [],
        "page_ids": [],
        "text_hints": [],
    }
    if isinstance(raw_definition_hints, dict):
        for key in definition_runtime_hints.keys():
            raw_values = raw_definition_hints.get(key, [])
            if isinstance(raw_values, (list, tuple, set)):
                for item in raw_values:
                    value_text = str(item or "").strip()
                    if value_text and value_text not in definition_runtime_hints[key]:
                        definition_runtime_hints[key].append(value_text)
    settings = {
        "environment": str(raw.get("environment", "prod") or "prod").strip() or "prod",
        "browser_name": browser_name,
        "viewport_width": _clamp_int(raw.get("viewport_width"), 1440, 320, 2560),
        "viewport_height": _clamp_int(raw.get("viewport_height"), 900, 320, 2560),
        "auto_crawl_enabled": bool(raw.get("auto_crawl_enabled", True)),
        "auto_stop_after_crawl": bool(raw.get("auto_stop_after_crawl", True)),
        "max_auto_clicks": _clamp_int(
            raw.get("max_auto_clicks", _env_int("QA_AUTO_CRAWL_MAX_CLICKS", 80)),
            80,
            1,
            500,
        ),
        "click_interval_ms": _clamp_int(
            raw.get("click_interval_ms", _env_int("QA_AUTO_CRAWL_CLICK_INTERVAL_MS", 1200)),
            1200,
            100,
            10000,
        ),
        "wait_after_click_ms": _clamp_int(
            raw.get("wait_after_click_ms", _env_int("QA_AUTO_CRAWL_WAIT_AFTER_CLICK_MS", 1200)),
            1200,
            100,
            10000,
        ),
        "block_link_navigation": bool(
            raw.get("block_link_navigation", _is_truthy(os.getenv("QA_AUTO_BLOCK_LINK_NAV", "1")))
        ),
        "single_page_only": bool(
            raw.get("single_page_only", _is_truthy(os.getenv("QA_SINGLE_PAGE_ONLY", "1")))
        ),
        "mobile_mode": bool(raw.get("mobile_mode", _is_truthy(os.getenv("QA_DEBUG_MOBILE_MODE", "0")))),
        "mobile_device": str(raw.get("mobile_device", "iPhone 13") or "iPhone 13").strip() or "iPhone 13",
        "qa_mode": str(raw.get("qa_mode", "전체 이벤트 테스트") or "전체 이벤트 테스트").strip(),
        "scenario_template": str(raw.get("scenario_template", "") or "").strip(),
        "definition_event_names": definition_event_names,
        "definition_runtime_hints": definition_runtime_hints,
    }
    return settings


def _get_definition_event_targets(run_settings: Optional[Dict[str, object]] = None) -> List[str]:
    raw_settings = _normalize_run_settings(run_settings)
    values = raw_settings.get("definition_event_names", [])
    if not isinstance(values, list):
        return []
    return [str(name).strip() for name in values if str(name).strip()]


def _get_definition_runtime_hints(run_settings: Optional[Dict[str, object]] = None) -> Dict[str, List[str]]:
    raw_settings = _normalize_run_settings(run_settings)
    hints = raw_settings.get("definition_runtime_hints", {})
    if not isinstance(hints, dict):
        return {"target_ids": [], "section_names": [], "page_ids": [], "text_hints": []}
    out = {"target_ids": [], "section_names": [], "page_ids": [], "text_hints": []}
    for key in out.keys():
        raw_values = hints.get(key, [])
        if isinstance(raw_values, list):
            out[key] = [str(v).strip() for v in raw_values if str(v).strip()]
    return out


def _candidate_matches_definition(meta: Dict[str, object], hints: Dict[str, List[str]]) -> bool:
    if not isinstance(meta, dict):
        return False
    target_ids = {str(v).strip() for v in hints.get("target_ids", []) if str(v).strip()}
    section_names = {str(v).strip() for v in hints.get("section_names", []) if str(v).strip()}
    page_ids = {str(v).strip() for v in hints.get("page_ids", []) if str(v).strip()}
    text_hints = [str(v).strip().lower() for v in hints.get("text_hints", []) if str(v).strip()]
    strong_hint_exists = bool(target_ids or section_names or text_hints)
    target_id = str(meta.get("target_id", "")).strip()
    section_name = str(meta.get("section_name", "")).strip()
    page_id = str(meta.get("page_id", "")).strip()
    text = str(meta.get("text", "")).strip().lower()
    if page_ids and page_id and page_id not in page_ids:
        return False
    if target_ids and target_id in target_ids:
        return True
    if section_names and section_name in section_names:
        return True
    if text_hints and text:
        for hint in text_hints:
            if hint and hint in text:
                return True
    return not strong_hint_exists


def _mark_definition_hits(session_id: str, hits: List[Dict[str, object]]) -> List[str]:
    sid = str(session_id or "").strip()
    if not sid or not hits:
        return []
    with _LOCK:
        session = _SESSIONS.get(sid)
        if not session:
            return []
        target_names = set(_get_definition_event_targets(session.run_settings))
        if not target_names:
            return []
        for payload in hits:
            event_name = str(
                payload.get("event_name")
                or payload.get("event")
                or payload.get("en")
                or ""
            ).strip()
            if event_name and event_name in target_names:
                session.matched_definition_events.add(event_name)
        return sorted(session.matched_definition_events)


def _get_definition_progress(session_id: str, run_settings: Optional[Dict[str, object]] = None) -> tuple[List[str], List[str], bool]:
    sid = str(session_id or "").strip()
    target_names = _get_definition_event_targets(run_settings)
    matched_names: List[str] = []
    with _LOCK:
        session = _SESSIONS.get(sid)
        if session:
            if not target_names:
                target_names = _get_definition_event_targets(session.run_settings)
            matched_names = sorted(session.matched_definition_events)
    target_set = set(target_names)
    matched = [name for name in matched_names if name in target_set] if target_set else []
    completed = bool(target_set and target_set.issubset(set(matched)))
    return sorted(target_set), matched, completed


def _launch_browser(playwright):
    last_err: Exception | None = None
    attempt_errors: List[str] = []
    browser_name = "chrome"
    if isinstance(playwright, tuple):
        playwright, browser_name = playwright
    # EC2(무GUI) 환경에서도 동작하도록 headed -> headless 순으로 폴백한다.
    for launch_kwargs in (
        {"headless": False, "channel": "chrome"} if browser_name == "chrome" else {"headless": False},
        {"headless": False},
        {"headless": True, "channel": "chrome"} if browser_name == "chrome" else {"headless": True},
        {"headless": True},
        # 일부 EC2/컨테이너 환경에서 sandbox 관련 실패를 우회하기 위한 최후 폴백
        {"headless": True, "channel": "chrome", "args": ["--no-sandbox", "--disable-dev-shm-usage"]}
        if browser_name == "chrome"
        else {"headless": True, "args": ["--no-sandbox", "--disable-dev-shm-usage"]},
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


def _build_context_kwargs(playwright, run_settings: Dict[str, object]) -> Dict[str, object]:
    settings = _normalize_run_settings(run_settings)
    if settings.get("mobile_mode", False):
        device_profile = playwright.devices.get(str(settings.get("mobile_device", "iPhone 13")))
        if isinstance(device_profile, dict) and device_profile:
            return dict(device_profile)
    return {
        "viewport": {
            "width": int(settings.get("viewport_width", 1440)),
            "height": int(settings.get("viewport_height", 900)),
        }
    }


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


def _record_runtime_payload(session_id: str, payload: Dict[str, object]) -> None:
    session = _get_session(session_id)
    if not session:
        return
    payload.setdefault("captured_at", datetime.now(timezone.utc).isoformat())
    payload.setdefault("session_id", session_id)
    payload.setdefault("page_url", session.target_url)
    _append_event_with_db(session.output_file, payload, session.db_path)
    with _LOCK:
        current = _SESSIONS.get(session_id)
        if current:
            current.captured_events += 1
            should_sync = current.captured_events % 10 == 0
        else:
            should_sync = False
    if should_sync:
        _sync_session_db(session_id)


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


def _build_compare_key(url: str) -> str:
    parsed = urlparse(str(url or "").strip())
    host = parsed.netloc.lower().strip()
    path = parsed.path or "/"
    return f"{host}{path}"


def _capture_annotated_screenshot(page, screenshot_path: Path, bbox: Dict[str, int], marker_index: int) -> tuple[str, Dict[str, int]]:
    screenshot_path.parent.mkdir(parents=True, exist_ok=True)
    image_bytes = page.screenshot(full_page=False)
    raw_path = screenshot_path.with_name(f"{screenshot_path.stem}_raw{screenshot_path.suffix}")
    raw_path.write_bytes(image_bytes)
    normalized_bbox = {
        "bbox_x": int(max(0, bbox.get("bbox_x", 0))),
        "bbox_y": int(max(0, bbox.get("bbox_y", 0))),
        "bbox_width": int(max(0, bbox.get("bbox_width", 0))),
        "bbox_height": int(max(0, bbox.get("bbox_height", 0))),
    }
    try:
        from io import BytesIO
        from PIL import Image, ImageDraw, ImageFont

        image = Image.open(BytesIO(image_bytes)).convert("RGB")
        img_width, img_height = image.size
        draw = ImageDraw.Draw(image)
        font = ImageFont.load_default()
        x1 = int(min(max(0, normalized_bbox.get("bbox_x", 0)), max(0, img_width - 1)))
        y1 = int(min(max(0, normalized_bbox.get("bbox_y", 0)), max(0, img_height - 1)))
        x2 = int(min(max(x1, x1 + normalized_bbox.get("bbox_width", 0)), max(0, img_width - 1)))
        y2 = int(min(max(y1, y1 + normalized_bbox.get("bbox_height", 0)), max(0, img_height - 1)))
        normalized_bbox = {
            "bbox_x": x1,
            "bbox_y": y1,
            "bbox_width": max(0, x2 - x1),
            "bbox_height": max(0, y2 - y1),
        }
        draw.rectangle([x1, y1, x2, y2], outline=(220, 38, 38), width=4)
        marker_text = str(marker_index)
        text_bbox = draw.textbbox((0, 0), marker_text, font=font)
        text_width = int(text_bbox[2] - text_bbox[0])
        text_height = int(text_bbox[3] - text_bbox[1])
        label_padding_x = 8
        label_padding_y = 5
        label_width = text_width + (label_padding_x * 2)
        label_height = text_height + (label_padding_y * 2)
        label_x = x2 + 8
        if label_x + label_width > img_width:
            label_x = max(0, x1 - label_width - 8)
        label_y = max(0, y1)
        if label_y + label_height > img_height:
            label_y = max(0, img_height - label_height)
        draw.rectangle(
            [label_x, label_y, label_x + label_width, label_y + label_height],
            fill=(220, 38, 38),
            outline=(220, 38, 38),
            width=2,
        )
        draw.text(
            (label_x + label_padding_x, label_y + label_padding_y),
            marker_text,
            fill=(255, 255, 255),
            font=font,
        )
        image.save(screenshot_path, format="PNG")
    except Exception:
        screenshot_path.write_bytes(image_bytes)
    return str(raw_path), normalized_bbox


def _extract_candidate_metadata(handle) -> Dict[str, object]:
    return handle.evaluate(
        """
        (el) => {
          const readAttr = (node, name) => (node && node.getAttribute ? (node.getAttribute(name) || "") : "");
          const normalizeText = (value) => String(value || "").replace(/\\s+/g, " ").trim();
          const toPattern = (value) => String(value || "").replace(/\\d+/g, "#");
          const buildSelector = (node) => {
            const parts = [];
            let current = node;
            let depth = 0;
            while (current && current.nodeType === 1 && depth < 6) {
              let part = current.tagName.toLowerCase();
              if (current.id) {
                part += `#${current.id}`;
                parts.unshift(part);
                break;
              }
              const className = normalizeText(current.className || "").split(" ").filter(Boolean).slice(0, 2).join(".");
              if (className) {
                part += `.${className}`;
              }
              let nth = 1;
              let sibling = current;
              while ((sibling = sibling.previousElementSibling)) {
                if (sibling.tagName === current.tagName) nth += 1;
              }
              part += `:nth-of-type(${nth})`;
              parts.unshift(part);
              current = current.parentElement;
              depth += 1;
            }
            return parts.join(" > ");
          };

          const sectionHost = el.closest("[data-section-name]");
          const sectionName = readAttr(el, "data-section-name") || readAttr(sectionHost, "data-section-name") || "";
          const text = normalizeText(el.innerText || el.textContent || readAttr(el, "aria-label") || readAttr(el, "title"));
          const href = readAttr(el, "href");
          const tag = (el.tagName || "").toLowerCase();
          const classAttribute = normalizeText(readAttr(el, "class"));
          const role = readAttr(el, "role");
          const dataQa = readAttr(el, "data-qa");
          const dataButtonId = readAttr(el, "data-button-id");
          const dataSectionName = readAttr(el, "data-section-name");
          const targetId = dataQa
            ? `data-qa:${dataQa}`
            : (dataButtonId
              ? `data-button-id:${dataButtonId}`
              : (dataSectionName
                ? `data-section-name:${dataSectionName}`
                : (el.id ? `id:${el.id}` : "")));
          const uiRole = role || (tag === "a" ? "link" : (tag === "button" || tag === "input" ? "button_like" : "clickable"));
          const selector = buildSelector(el);
          const selectorPattern = toPattern(selector);
          const screenState = `modal:${document.querySelectorAll('[role="dialog"], [aria-modal="true"], .modal, [class*="modal"]').length > 0 ? 1 : 0}|expanded:${document.querySelectorAll('[aria-expanded="true"]').length > 0 ? 1 : 0}|tooltip:${document.querySelectorAll('[role="tooltip"], [class*="tooltip"]').length > 0 ? 1 : 0}|self_expanded:${readAttr(el, 'aria-expanded') || 'na'}`;
          const pageId = location.pathname || "/";
          const classPattern = toPattern(classAttribute.split(" ").slice(0, 2).join("."));
          const key = [pageId, tag, targetId || selector, text.slice(0, 80), href].join("|");
          const structureKey = [pageId, sectionName || "section", uiRole, tag, classPattern || selectorPattern, screenState].join("|");
          const hasStableControlId = Boolean(dataButtonId) || (Boolean(dataQa) && !/(content|banner|card|item|product|goods)/i.test(dataQa));
          const patternKey = hasStableControlId
            ? [structureKey, targetId || "na"].join("|")
            : structureKey;
          return {
            key,
            pattern_key: patternKey,
            structure_key: structureKey,
            text,
            href,
            target_id: targetId || selector,
            selector,
            selector_pattern: selectorPattern,
            section_name: sectionName,
            screen_state: screenState,
            class_attribute: classAttribute,
            ui_role: uiRole,
            tag,
            page_id: pageId
          };
        }
        """
    )


def _emit_auto_crawl_event(session_id: str, event_name: str, page_url: str, params: Dict[str, object]) -> None:
    _record_runtime_payload(
        session_id,
        {
            "source": "auto_crawl",
            "event_name": event_name,
            "params": params,
            "page_url": page_url,
            "request_method": "AUTO",
        },
    )


def _run_auto_crawl(page, session_id: str, run_settings: Dict[str, object], stop_event: threading.Event) -> None:
    settings = _normalize_run_settings(run_settings)
    definition_targets = _get_definition_event_targets(settings)
    definition_hints = _get_definition_runtime_hints(settings)
    max_auto_clicks = int(settings.get("max_auto_clicks", 80))
    wait_after_click_ms = int(settings.get("wait_after_click_ms", 1200))
    click_interval_ms = int(settings.get("click_interval_ms", 1200))
    block_link_navigation = bool(settings.get("block_link_navigation", True))
    single_page_only = bool(settings.get("single_page_only", True))
    start_compare_key = _build_compare_key(page.url)
    start_url = page.url
    screenshots_dir = Path("data/debug_screens") / session_id
    candidate_selectors = [
        "[data-qa]",
        "[data-button-id]",
        "button",
        "a.gtm-click-button",
        "a.gtm-click-content",
        "a[href]",
        "[role='button']",
        "input[type='button']",
        "input[type='submit']",
        "[onclick]",
    ]
    seen_pattern_keys: set[str] = set()
    clicked_count = 0

    while not stop_event.is_set() and clicked_count < max_auto_clicks:
        target_names, matched_names, definition_done = _get_definition_progress(session_id, settings)
        if definition_done:
            _emit_auto_crawl_event(
                session_id,
                "definition_targets_reached",
                page.url,
                {
                    "reason": "all_definition_events_captured",
                    "matched_definition_events": matched_names,
                    "definition_event_names": target_names,
                    "run_mode": "Auto Crawl",
                },
            )
            break
        try:
            page.wait_for_load_state("domcontentloaded", timeout=5000)
        except Exception:
            pass

        selected_handle = None
        selected_meta: Dict[str, object] = {}
        selected_bbox: Dict[str, int] = {}
        for selector in candidate_selectors:
            handles = page.locator(selector).element_handles()
            for handle in handles:
                try:
                    bbox = handle.bounding_box()
                    if not bbox:
                        continue
                    if float(bbox.get("width", 0)) < 6 or float(bbox.get("height", 0)) < 6:
                        continue
                    meta = _extract_candidate_metadata(handle)
                    if not meta:
                        continue
                    if definition_targets and not _candidate_matches_definition(meta, definition_hints):
                        continue
                    text = str(meta.get("text", "")).strip()
                    if not text and str(meta.get("tag", "")) not in {"button", "input"}:
                        continue
                    pattern_key = str(meta.get("pattern_key", "")).strip()
                    if not pattern_key or pattern_key in seen_pattern_keys:
                        continue
                    selected_handle = handle
                    selected_meta = meta
                    selected_bbox = {
                        "bbox_x": int(max(0, bbox.get("x", 0))),
                        "bbox_y": int(max(0, bbox.get("y", 0))),
                        "bbox_width": int(max(0, bbox.get("width", 0))),
                        "bbox_height": int(max(0, bbox.get("height", 0))),
                    }
                    break
                except Exception:
                    continue
            if selected_handle is not None:
                break

        if selected_handle is None:
            _emit_auto_crawl_event(
                session_id,
                "auto_crawl_idle",
                page.url,
                {
                    "reason": "no_new_candidates",
                    "auto_click_index": clicked_count,
                    "max_auto_clicks": max_auto_clicks,
                    "run_mode": "Auto Crawl",
                },
            )
            break

        clicked_count += 1
        seen_pattern_keys.add(str(selected_meta.get("pattern_key", "")).strip())
        screenshot_name = f"{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S_%f')}_auto_click.png"
        screenshot_path = screenshots_dir / screenshot_name
        screenshot_file = str(screenshot_path).strip()
        raw_screenshot_file = ""
        navigation_blocked = False
        current_url = page.url

        try:
            selected_handle.scroll_into_view_if_needed(timeout=1500)
        except Exception:
            pass

        try:
            raw_screenshot_file, selected_bbox = _capture_annotated_screenshot(
                page,
                screenshot_path,
                selected_bbox,
                clicked_count,
            )
        except Exception:
            screenshot_file = ""
            raw_screenshot_file = ""

        click_reason = "clicked"
        try:
            href = str(selected_meta.get("href", "")).strip()
            if href and block_link_navigation:
                navigation_blocked = True
                selected_handle.evaluate(
                    """
                    (el) => {
                      const blocker = (event) => event.preventDefault();
                      el.addEventListener("click", blocker, { capture: true, once: true });
                      el.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true, view: window }));
                    }
                    """
                )
            else:
                selected_handle.click(timeout=2500, force=True)
        except Exception as exc:
            click_reason = f"error:{exc}"

        _emit_auto_crawl_event(
            session_id,
            "auto_click",
            current_url,
            {
                "clicked": click_reason == "clicked",
                "reason": click_reason,
                "key": str(selected_meta.get("key", "")).strip(),
                "pattern_key": str(selected_meta.get("pattern_key", "")).strip(),
                "structure_key": str(selected_meta.get("structure_key", "")).strip(),
                "text": str(selected_meta.get("text", "")).strip(),
                "href": str(selected_meta.get("href", "")).strip(),
                "target_id": str(selected_meta.get("target_id", "")).strip(),
                "selector": str(selected_meta.get("selector", "")).strip(),
                "section_name": str(selected_meta.get("section_name", "")).strip(),
                "screen_state": str(selected_meta.get("screen_state", "")).strip(),
                "class_attribute": str(selected_meta.get("class_attribute", "")).strip(),
                "ui_role": str(selected_meta.get("ui_role", "")).strip(),
                "selector_pattern": str(selected_meta.get("selector_pattern", "")).strip(),
                "tag": str(selected_meta.get("tag", "")).strip(),
                "navigation_blocked": navigation_blocked,
                "url": current_url,
                "auto_click_index": clicked_count,
                "annotation_no": clicked_count,
                "max_auto_clicks": max_auto_clicks,
                "run_mode": "Auto Crawl",
                "screen_name": str(selected_meta.get("page_id", "")).strip() or "/",
                "screenshot_path": screenshot_file,
                "selector_screenshot": screenshot_file,
                "raw_screenshot_file": raw_screenshot_file,
                "bounding_box": {
                    "x": int(selected_bbox.get("bbox_x", 0)),
                    "y": int(selected_bbox.get("bbox_y", 0)),
                    "width": int(selected_bbox.get("bbox_width", 0)),
                    "height": int(selected_bbox.get("bbox_height", 0)),
                },
                **selected_bbox,
            },
        )

        try:
            page.wait_for_timeout(wait_after_click_ms)
            page.wait_for_load_state("networkidle", timeout=2500)
        except Exception:
            pass

        if definition_targets:
            target_names, matched_names, definition_done = _get_definition_progress(session_id, settings)
            if definition_done:
                _emit_auto_crawl_event(
                    session_id,
                    "definition_targets_reached",
                    page.url,
                    {
                        "reason": "all_definition_events_captured",
                        "matched_definition_events": matched_names,
                        "definition_event_names": target_names,
                        "run_mode": "Auto Crawl",
                        "auto_click_index": clicked_count,
                    },
                )
                break

        if single_page_only and _build_compare_key(page.url) != start_compare_key:
            try:
                page.goto(start_url, wait_until="domcontentloaded")
                page.wait_for_timeout(click_interval_ms)
            except Exception:
                pass
        else:
            page.wait_for_timeout(click_interval_ms)

    if clicked_count >= max_auto_clicks:
        _emit_auto_crawl_event(
            session_id,
            "max_auto_clicks_reached",
            page.url,
            {
                "auto_click_index": clicked_count,
                "max_auto_clicks": max_auto_clicks,
                "run_mode": "Auto Crawl",
            },
        )


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
            run_settings = _normalize_run_settings(session.run_settings)
            browser = _launch_browser((p, str(run_settings.get("browser_name", "chrome"))))
            context = browser.new_context(**_build_context_kwargs(p, run_settings))

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

            if run_settings.get("auto_crawl_enabled", True):
                try:
                    page.wait_for_timeout(1200)
                    _run_auto_crawl(page, session_id, run_settings, session.stop_event)
                except Exception as exc:
                    _emit_auto_crawl_event(
                        session_id,
                        "auto_crawl_error",
                        page.url,
                        {"reason": str(exc), "run_mode": "Auto Crawl"},
                    )
                if run_settings.get("auto_stop_after_crawl", True):
                    session.stop_event.set()

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
    if hits:
        with _LOCK:
            current = _SESSIONS.get(sid)
            target_names = set(_get_definition_event_targets(current.run_settings if current else None))
        if target_names:
            hits = [
                payload
                for payload in hits
                if str(
                    payload.get("event_name")
                    or payload.get("event")
                    or payload.get("en")
                    or ""
                ).strip() in target_names
            ]
    if not hits:
        return 0

    for payload in hits:
        _append_event_with_db(output_path, payload, db_path)
    _mark_definition_hits(sid, hits)

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
    run_settings: Optional[Dict[str, object]] = None,
) -> Dict[str, object]:
    sid = session_id.strip()
    url = target_url.strip()
    if not sid:
        raise ValueError("session_id가 비어 있습니다.")
    if not url:
        raise ValueError("디버깅 대상 URL을 입력하세요.")
    effective_db_path = Path(db_path) if db_path is not None else Path("data/test_logs/qa_runs.db")

    with _LOCK:
        running_count = sum(1 for s in _SESSIONS.values() if str(getattr(s, "status", "")).strip() == "running")
        if running_count >= _max_running_debug_sessions():
            raise RuntimeError(
                f"동시 디버깅 세션 제한을 초과했습니다. "
                f"(running={running_count}, limit={_max_running_debug_sessions()})"
            )
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
            run_settings=_normalize_run_settings(run_settings),
            matched_definition_events=set(),
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
            "run_settings": dict(session.run_settings),
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
