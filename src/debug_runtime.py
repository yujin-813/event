from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import html
import json
import os
import platform
from pathlib import Path
import re
import threading
import time
from typing import Dict, List, Optional, Tuple
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

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
    matched_definition_rows: set[str] = field(default_factory=set)
    skipped_definition_rows: set[str] = field(default_factory=set)
    current_definition_row_id: str = ""
    auto_crawl_paused: bool = False
    auto_crawl_pause_reason: str = ""
    hit_screenshot_count: int = 0
    preconditions_done: set[str] = field(default_factory=set)
    preconditions_attempts: Dict[str, int] = field(default_factory=dict)
    current_scenario_group: str = ""
    definition_targets_reached: bool = False
    annotation_counters: Dict[str, int] = field(default_factory=dict)
    manual_recording_enabled: bool = False
    manual_profile_selected: str = "default"
    manual_flow_replay_enabled: bool = True
    manual_recording_selected: str = ""
    manual_recording_name: str = ""
    manual_replay_running: bool = False
    manual_record_buffer: List[Dict[str, object]] = field(default_factory=list)
    manual_clicked_keys: set[str] = field(default_factory=set)
    analytics_probe_count: int = 0
    analytics_probe_keys: set[str] = field(default_factory=set)
    last_ingested_hit_event: str = ""


_SESSIONS: Dict[str, DebugSession] = {}
_LOCK = threading.RLock()
_SESSION_ID_PATTERN = re.compile(r"^dbg_\d{8}_\d{6}(?:_\d{6})?_[0-9a-f]{4,16}$")
_MANUAL_FLOW_LIBRARY_PATH = (
    Path("data/workspace/projects/default-project/artifacts/manual_flow_library.json")
)


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


def _page_id_pattern_matches(expected: str, actual: str) -> bool:
    """page_id 동적 패턴 매칭: /main/{스토어코드}/sale → /main/musinsa/sale"""
    if not expected or not actual:
        return False
    if "{" not in expected:
        return expected == actual
    parts = re.split(r"\{[^}]+\}", expected)
    pattern = ".+".join(re.escape(p) for p in parts)
    return bool(re.fullmatch(pattern, actual))


def _normalize_target_id(value: object) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    return raw


def _extract_payload_event_name(payload: Dict[str, object]) -> str:
    if not isinstance(payload, dict):
        return ""
    return str(
        payload.get("event_name") or payload.get("event") or payload.get("en") or ""
    ).strip()


def _normalize_section_name(value: object) -> str:
    raw = str(value or "").strip().lower()
    if not raw:
        return ""
    return re.sub(r"[^0-9a-z가-힣]+", "", raw)


def _section_name_matches(expected: str, actual: str) -> bool:
    exp = str(expected or "").strip()
    act = str(actual or "").strip()
    if not exp:
        return True
    if not act:
        return False
    if exp == act:
        return True
    exp_norm = _normalize_section_name(exp)
    act_norm = _normalize_section_name(act)
    if exp_norm and act_norm and exp_norm == act_norm:
        return True
    return False


def _normalize_manual_recording_catalog(raw: object) -> List[Dict[str, object]]:
    if not isinstance(raw, list):
        return []
    out: List[Dict[str, object]] = []
    for item in raw[:12]:
        if not isinstance(item, dict):
            continue
        rid = str(item.get("id", "")).strip()
        if not rid:
            continue
        steps_raw = item.get("steps", [])
        steps: List[Dict[str, object]] = []
        if isinstance(steps_raw, list):
            for step in steps_raw[:40]:
                if not isinstance(step, dict):
                    continue
                steps.append(
                    {
                        "action": str(step.get("action", "click") or "click")
                        .strip()
                        .lower(),
                        "target_hint": str(step.get("target_hint", "")).strip(),
                        "target_id": str(step.get("target_id", "")).strip(),
                        "selector": str(step.get("selector", "")).strip(),
                        "wait_ms": _safe_int(step.get("wait_ms", 600), fallback=600),
                        "row_id": str(step.get("row_id", "")).strip(),
                    }
                )
        out.append(
            {
                "id": rid,
                "name": str(item.get("name", "")).strip() or rid,
                "created_at": str(item.get("created_at", "")).strip(),
                "updated_at": str(item.get("updated_at", "")).strip(),
                "step_count": _safe_int(item.get("step_count", len(steps)), fallback=len(steps)),
                "ui_signature_hash": str(item.get("ui_signature_hash", "")).strip(),
                "ui_signature_text": str(item.get("ui_signature_text", "")).strip(),
                "ui_signature_version": str(item.get("ui_signature_version", "v1")).strip() or "v1",
                "steps": steps,
            }
        )
    return out


def _collect_ui_structure_snapshot(page) -> Dict[str, object]:
    try:
        payload = page.evaluate(
            """
            () => {
              try {
                const sectionNames = Array.from(document.querySelectorAll("[data-section-name]"))
                  .map((el) => String(el.getAttribute("data-section-name") || "").trim())
                  .filter(Boolean)
                  .slice(0, 40);
                const uniqSections = Array.from(new Set(sectionNames)).sort();
                const tabCount =
                  document.querySelectorAll("[role='tab'], .tab, [class*='tab-'], [class*='tabs']").length;
                const filterCount =
                  document.querySelectorAll("[data-filter], [class*='filter'], [aria-label*='필터'], [aria-label*='filter']").length;
                const moreCount = Array.from(document.querySelectorAll("button, a, [role='button']"))
                  .filter((el) => {
                    const txt = String((el.innerText || el.textContent || "")).replace(/\\s+/g, " ").trim().toLowerCase();
                    return txt.includes("더보기") || txt.includes("more");
                  }).length;
                const repeatCandidates = [
                  document.querySelectorAll("ul li").length,
                  document.querySelectorAll("[class*='list'] > *").length,
                  document.querySelectorAll("[class*='grid'] > *").length,
                  document.querySelectorAll("[data-section-name] li").length,
                ];
                const repeatMax = Math.max(0, ...repeatCandidates);
                const dataQaCount = document.querySelectorAll("[data-qa]").length;
                const dataButtonCount = document.querySelectorAll("[data-button-id]").length;
                const dataSectionCount = document.querySelectorAll("[data-section-name]").length;
                const path = String(location.pathname || "").trim();
                const signatureText = [
                  `path=${path}`,
                  `sections=${uniqSections.join("|")}`,
                  `data_qa=${dataQaCount}`,
                  `data_button_id=${dataButtonCount}`,
                  `data_section_name=${dataSectionCount}`,
                  `tabs=${tabCount}`,
                  `filters=${filterCount}`,
                  `more=${moreCount}`,
                  `repeat_max=${repeatMax}`,
                ].join(";");
                return {
                  path,
                  sections: uniqSections,
                  data_qa_count: dataQaCount,
                  data_button_id_count: dataButtonCount,
                  data_section_name_count: dataSectionCount,
                  tab_count: tabCount,
                  filter_count: filterCount,
                  more_count: moreCount,
                  repeat_max: repeatMax,
                  signature_text: signatureText,
                };
              } catch (e) {
                return {};
              }
            }
            """
        )
    except Exception:
        return {}
    payload = payload if isinstance(payload, dict) else {}
    signature_text = str(payload.get("signature_text", "")).strip()
    if signature_text:
        payload["signature_hash"] = hashlib.sha1(signature_text.encode("utf-8")).hexdigest()[:16]
    else:
        payload["signature_hash"] = ""
    return payload


def _evaluate_ui_structure_change(
    expected_snapshot: Dict[str, object],
    current_snapshot: Dict[str, object],
) -> Dict[str, object]:
    expected_sections = {
        str(v).strip()
        for v in (expected_snapshot.get("sections", []) if isinstance(expected_snapshot, dict) else [])
        if str(v).strip()
    }
    current_sections = {
        str(v).strip()
        for v in (current_snapshot.get("sections", []) if isinstance(current_snapshot, dict) else [])
        if str(v).strip()
    }
    intersection = expected_sections & current_sections
    section_overlap_ratio = (
        (len(intersection) / max(1, len(expected_sections)))
        if expected_sections
        else 1.0
    )

    expected_path = str(expected_snapshot.get("path", "")).strip()
    current_path = str(current_snapshot.get("path", "")).strip()
    expected_data_button = _safe_int(expected_snapshot.get("data_button_id_count", 0), fallback=0)
    current_data_button = _safe_int(current_snapshot.get("data_button_id_count", 0), fallback=0)
    expected_tab = _safe_int(expected_snapshot.get("tab_count", 0), fallback=0)
    current_tab = _safe_int(current_snapshot.get("tab_count", 0), fallback=0)
    expected_repeat = _safe_int(expected_snapshot.get("repeat_max", 0), fallback=0)
    current_repeat = _safe_int(current_snapshot.get("repeat_max", 0), fallback=0)

    change_reasons: List[str] = []
    severity = 0
    if expected_path and current_path and expected_path != current_path:
        change_reasons.append("path_changed")
        severity += 1
    if len(expected_sections) >= 3 and section_overlap_ratio < 0.45:
        change_reasons.append("section_overlap_low")
        severity += 2
    if max(expected_data_button, current_data_button) >= 10:
        gap = abs(expected_data_button - current_data_button)
        if gap >= 8 and (gap / max(1, expected_data_button)) >= 0.65:
            change_reasons.append("button_id_density_changed")
            severity += 1
    if abs(expected_tab - current_tab) >= 4:
        change_reasons.append("tab_structure_changed")
        severity += 1
    if max(expected_repeat, current_repeat) >= 12:
        r_gap = abs(expected_repeat - current_repeat)
        if r_gap >= 10 and (r_gap / max(1, expected_repeat)) >= 0.7:
            change_reasons.append("repeat_list_structure_changed")
            severity += 1

    return {
        "compatible": severity < 2,
        "severity": int(severity),
        "reasons": change_reasons,
        "section_overlap_ratio": float(round(section_overlap_ratio, 4)),
        "expected_section_count": int(len(expected_sections)),
        "runtime_section_count": int(len(current_sections)),
        "expected_path": expected_path,
        "runtime_path": current_path,
    }


def _ensure_manual_replay_compatibility(page, session_id: str, run_settings: Dict[str, object]) -> None:
    sid = str(session_id or "").strip()
    if not sid:
        return
    replay_enabled = bool(run_settings.get("manual_flow_replay_enabled", True))
    selected_id = str(run_settings.get("manual_recording_selected", "")).strip()
    if (not replay_enabled) or (not selected_id):
        return
    catalog = _normalize_manual_recording_catalog(run_settings.get("manual_recording_catalog", []))
    selected = next((row for row in catalog if str(row.get("id", "")).strip() == selected_id), {})
    if not isinstance(selected, dict) or not selected:
        return
    current_snapshot = _collect_ui_structure_snapshot(page)
    current_hash = str(current_snapshot.get("signature_hash", "")).strip()
    current_text = str(current_snapshot.get("signature_text", "")).strip()
    expected_hash = str(selected.get("ui_signature_hash", "")).strip()
    expected_text = str(selected.get("ui_signature_text", "")).strip()
    expected_snapshot: Dict[str, object] = {}
    if expected_text:
        try:
            # signature text format: key=value;key=value...
            expected_snapshot = {}
            for token in expected_text.split(";"):
                t = str(token).strip()
                if "=" not in t:
                    continue
                k, v = t.split("=", 1)
                expected_snapshot[str(k).strip()] = str(v).strip()
            if "sections" in expected_snapshot:
                expected_snapshot["sections"] = [
                    s.strip()
                    for s in str(expected_snapshot.get("sections", "")).split("|")
                    if s.strip()
                ]
            for n_key in [
                "data_button_id",
                "tabs",
                "repeat_max",
            ]:
                if n_key in expected_snapshot:
                    expected_snapshot[n_key] = _safe_int(expected_snapshot.get(n_key, 0), fallback=0)
            expected_snapshot = {
                "path": str(expected_snapshot.get("path", "")).strip(),
                "sections": expected_snapshot.get("sections", []),
                "data_button_id_count": expected_snapshot.get("data_button_id", 0),
                "tab_count": expected_snapshot.get("tabs", 0),
                "repeat_max": expected_snapshot.get("repeat_max", 0),
            }
        except Exception:
            expected_snapshot = {}
    compatibility = (
        _evaluate_ui_structure_change(expected_snapshot, current_snapshot)
        if expected_snapshot
        else {
            "compatible": True,
            "severity": 0,
            "reasons": [],
            "section_overlap_ratio": 1.0,
            "expected_section_count": 0,
            "runtime_section_count": 0,
            "expected_path": "",
            "runtime_path": str(current_snapshot.get("path", "")).strip(),
        }
    )
    mismatch = bool(not compatibility.get("compatible", True))
    reason = "ui_structure_changed" if mismatch else "ok"
    if not expected_hash:
        reason = "missing_recording_signature"
    if not current_hash:
        reason = "missing_runtime_signature"
    _emit_auto_crawl_event(
        sid,
        "ui_structure_check",
        page.url,
        {
            "reason": reason,
            "manual_recording_id": selected_id,
            "manual_recording_name": str(selected.get("name", "")).strip(),
            "expected_signature_hash": expected_hash,
            "runtime_signature_hash": current_hash,
            "expected_signature_text": expected_text,
            "runtime_signature_text": current_text,
            "structure_change_severity": int(compatibility.get("severity", 0) or 0),
            "structure_change_reasons": list(compatibility.get("reasons", []) or []),
            "section_overlap_ratio": float(compatibility.get("section_overlap_ratio", 0.0) or 0.0),
            "run_mode": "Auto Crawl",
        },
    )
    if not mismatch:
        return
    with _LOCK:
        session = _SESSIONS.get(sid)
        if session:
            session.manual_flow_replay_enabled = False
            if isinstance(session.run_settings, dict):
                session.run_settings["manual_flow_replay_enabled"] = False
                session.run_settings["manual_replay_disabled_reason"] = "ui_structure_changed"
    _emit_auto_crawl_event(
        sid,
        "manual_replay_disabled",
        page.url,
        {
            "reason": "ui_structure_changed",
            "manual_recording_id": selected_id,
            "manual_recording_name": str(selected.get("name", "")).strip(),
            "expected_signature_hash": expected_hash,
            "runtime_signature_hash": current_hash,
            "run_mode": "Auto Crawl",
        },
    )


def _norm_token(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().lower())


def _selector_pattern(value: object) -> str:
    return re.sub(r"\d+", "#", str(value or "").strip())


def _manual_payload_keys(payload: Dict[str, object]) -> set[str]:
    params = payload.get("params", {}) if isinstance(payload.get("params", {}), dict) else {}
    selector = str(params.get("selector", "")).strip()
    target_id = str(params.get("target_id", "")).strip()
    text = str(params.get("text", "")).strip()
    keys: set[str] = set()
    if selector:
        keys.add(f"selector:{_selector_pattern(selector)}")
    if target_id:
        keys.add(f"target:{_norm_token(target_id)}")
    if text:
        keys.add(f"text:{_norm_token(text)[:80]}")
    return {k for k in keys if k and not k.endswith(":")}


def _candidate_interaction_keys(meta: Dict[str, object]) -> set[str]:
    selector = str(meta.get("selector_pattern", "") or meta.get("selector", "")).strip()
    target_id = str(meta.get("target_id", "")).strip()
    text = str(meta.get("text", "")).strip()
    keys: set[str] = set()
    if selector:
        keys.add(f"selector:{_selector_pattern(selector)}")
    if target_id:
        keys.add(f"target:{_norm_token(target_id)}")
    if text:
        keys.add(f"text:{_norm_token(text)[:80]}")
    return {k for k in keys if k and not k.endswith(":")}


def _remember_manual_click(session_id: str, payload: Dict[str, object]) -> None:
    sid = str(session_id or "").strip()
    if not sid:
        return
    keys = _manual_payload_keys(payload)
    if not keys:
        return
    with _LOCK:
        session = _SESSIONS.get(sid)
        if not session:
            return
        if not isinstance(session.manual_clicked_keys, set):
            session.manual_clicked_keys = set()
        session.manual_clicked_keys.update(keys)
        if len(session.manual_clicked_keys) > 400:
            session.manual_clicked_keys = set(list(session.manual_clicked_keys)[-300:])


def _collect_manual_excluded_keys(
    session_id: str,
    run_settings: Dict[str, object],
) -> set[str]:
    excluded: set[str] = set()
    sid = str(session_id or "").strip()
    if sid:
        with _LOCK:
            session = _SESSIONS.get(sid)
            if session and isinstance(session.manual_clicked_keys, set):
                excluded.update(session.manual_clicked_keys)
    selected_rec_id = str(run_settings.get("manual_recording_selected", "")).strip()
    if selected_rec_id:
        catalog = _normalize_manual_recording_catalog(run_settings.get("manual_recording_catalog", []))
        selected = next((row for row in catalog if str(row.get("id", "")).strip() == selected_rec_id), {})
        if isinstance(selected, dict):
            for step in selected.get("steps", []):
                if not isinstance(step, dict):
                    continue
                selector = str(step.get("selector", "")).strip()
                target_id = str(step.get("target_id", "")).strip()
                target_hint = str(step.get("target_hint", "")).strip()
                if selector:
                    excluded.add(f"selector:{_selector_pattern(selector)}")
                if target_id:
                    excluded.add(f"target:{_norm_token(target_id)}")
                if target_hint:
                    excluded.add(f"text:{_norm_token(target_hint)[:80]}")
    return excluded


def _is_risky_candidate(meta: Dict[str, object]) -> bool:
    text = _norm_token(meta.get("text", ""))
    href = _norm_token(meta.get("href", ""))
    target_id = _norm_token(meta.get("target_id", ""))
    class_attr = _norm_token(meta.get("class_attribute", ""))
    risk_terms = [
        "logout", "log out", "signout", "탈퇴", "회원탈퇴", "삭제", "remove", "delete",
        "초기화", "reset", "unregister", "deactivate",
    ]
    haystack = " | ".join([text, href, target_id, class_attr])
    return any(term in haystack for term in risk_terms if term)


def _is_content_like_candidate(meta: Dict[str, object]) -> bool:
    button_id = _norm_token(meta.get("button_id", ""))
    ui_role = _norm_token(meta.get("ui_role", ""))
    target_id = str(meta.get("target_id", "")).strip().lower()
    data_qa = _norm_token(meta.get("data_qa", ""))
    cls = _norm_token(meta.get("class_attribute", ""))
    href = _norm_token(meta.get("href", ""))
    text = _norm_token(meta.get("text", ""))
    # 탭/필터/정렬류 컨트롤은 콘텐츠 반복 아이템으로 보지 않음
    if button_id in {"tab", "filter", "sort"}:
        return False
    if ui_role in {"tab"}:
        return False
    control_terms = ["정렬", "필터", "sort", "filter", "tab", "option"]
    if any(t in text for t in control_terms):
        return False
    if any(t in cls for t in ["tab", "filter", "sort"]):
        return False
    # 카드/상품/배너/콘텐츠성 타겟 판정
    if target_id.startswith("data-item-id:"):
        return True
    if target_id.startswith("data-content-id:") or target_id.startswith("data-banner-id:"):
        return True
    if data_qa and any(t in data_qa for t in ["content", "banner", "card", "item", "product", "goods"]):
        return True
    if any(t in cls for t in ["content", "banner", "card", "item", "product", "goods"]):
        return True
    if href and any(t in href for t in ["/products", "/goods", "/item", "/p/"]):
        return True
    return False


def _looks_like_tab_candidate(meta: Dict[str, object]) -> bool:
    button_id = str(meta.get("button_id", "")).strip().lower()
    section_name = str(meta.get("section_name", "")).strip().lower()
    ui_role = str(meta.get("ui_role", "")).strip().lower()
    if ui_role == "tab":
        return True
    if button_id == "tab" or "tab" in button_id:
        return True
    if button_id.startswith("theme_") or button_id.startswith("category_tab_"):
        return True
    if "tab" in section_name:
        return True
    return False


def _is_global_noise_section(section_name: str) -> bool:
    s = str(section_name or "").strip().lower()
    if not s:
        return False
    exact = {
        "footer",
        "pc_gnb",
        "mobile_gnb",
        "global_nav",
        "global_navigation",
        "header",
        "quick_menu",
        "sidemenu",
        "side_menu",
    }
    if s in exact:
        return True
    return any(token in s for token in ["footer", "gnb", "global_nav", "quick_menu", "side_menu"])


def _href_path_key(href: str) -> str:
    raw = str(href or "").strip()
    if not raw:
        return ""
    try:
        parsed = urlparse(raw)
        path = str(parsed.path or "").strip().lower()
        if path:
            return path
        return raw.lower()
    except Exception:
        return raw.lower()


def _candidate_kind(meta: Dict[str, object]) -> str:
    section = str(meta.get("section_name", "")).strip().lower()
    button_id = str(meta.get("button_id", "")).strip().lower()
    ui_role = str(meta.get("ui_role", "")).strip().lower()
    cls = str(meta.get("class_attribute", "")).strip().lower()
    target_id = str(meta.get("target_id", "")).strip().lower()
    data_qa = str(meta.get("data_qa", "")).strip().lower()
    text = str(meta.get("text", "")).strip().lower()
    hay = " | ".join([section, button_id, ui_role, cls, target_id, data_qa, text])
    if any(token in hay for token in ["tab", "theme_", "category_tab", "ranking_tab"]):
        return "tab"
    if any(token in hay for token in ["filter", "chip", "facet", "정렬", "필터"]):
        return "filter"
    if any(token in hay for token in ["tooltip", "guide", "help", "tip"]):
        return "tooltip"
    if (
        target_id.startswith("data-item-id:")
        or any(token in hay for token in ["product", "goods", "item", "card", "wishlist", "select_item"])
    ):
        return "product"
    if any(token in hay for token in ["banner", "hero", "display", "funnel"]):
        return "banner"
    return "generic"


def _infer_definition_row_kind(row: Dict[str, object]) -> str:
    if not isinstance(row, dict):
        return "generic"
    event_name = str(row.get("event_name", "")).strip().lower()
    action = str(row.get("action", "")).strip().lower()
    obj = str(row.get("object", "")).strip().lower()
    section = str(_get_expected_param(row, "section_name", fallback_row_key=True)).strip().lower()
    description = str(row.get("description", "")).strip().lower()
    hay = " | ".join([event_name, action, obj, section, description])
    if any(token in hay for token in ["tooltip", "tip", "guide", "도움말", "툴팁"]):
        return "tooltip"
    if any(token in hay for token in ["filter", "chip", "facet", "필터", "정렬"]):
        return "filter"
    if any(token in hay for token in ["tab", "category_tab", "theme", "카테고리탭", "테마칩"]):
        return "tab"
    if any(token in hay for token in ["banner", "funnel", "display", "hero"]):
        return "banner"
    if any(token in hay for token in ["select_item", "view_item_list", "wishlist", "goods", "product", "item"]):
        return "product"
    if obj in {"content", "goods", "item"}:
        return "product"
    return "generic"


def _candidate_selectors_for_row_kind(kind: str) -> List[str]:
    k = str(kind or "").strip().lower()
    if k == "tab":
        return [
            "[role='tab']",
            "[data-button-id*='tab']",
            "[data-qa*='tab']",
            "button[aria-selected]",
            "button",
            "a",
        ]
    if k == "filter":
        return [
            "[data-button-id*='filter']",
            "[data-qa*='filter']",
            "[data-qa*='chip']",
            "[role='button']",
            "button",
            "a",
        ]
    if k == "product":
        return [
            "[data-item-id]",
            "[data-product-id]",
            "[data-goods-no]",
            ".gtm-click-content",
            "a.gtm-click-content",
            "a[href]",
            "button",
        ]
    if k == "banner":
        return [
            "[data-banner-id]",
            "[data-content-id]",
            ".gtm-click-content",
            "a.gtm-click-content",
            "a[href]",
            "button",
        ]
    if k == "tooltip":
        return [
            "[role='tooltip'] button",
            "[class*='tooltip'] button",
            "[class*='tooltip'] [role='button']",
            "[data-qa*='tooltip']",
            "button",
            "a",
        ]
    return [
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


def _candidate_dedup_key(meta: Dict[str, object]) -> str:
    button_id = str(meta.get("button_id", "")).strip()
    section_name = str(meta.get("section_name", "")).strip()
    data_index = str(meta.get("data_index", "")).strip()
    button_name = _norm_token(meta.get("button_name", ""))
    text = _norm_token(meta.get("text", ""))
    if button_id:
        if _looks_like_tab_candidate(meta):
            stable_label = button_name[:40] or text[:28]
            return (
                f"tab:section:{section_name}|"
                f"idx:{data_index or 'na'}|"
                f"label:{stable_label or 'na'}"
            )
        # 가상 리스트/무한 스크롤형 콘텐츠는 index/href 변화가 커서
        # section + control id 기준으로 하나의 패턴으로 묶는다.
        if _is_content_like_candidate(meta) and section_name:
            return f"button_id:{button_id}|section:{section_name}"
        return f"button_id:{button_id}|section:{section_name}|index:{data_index}"
    pattern_key = str(meta.get("pattern_key", "")).strip()
    if pattern_key:
        if _is_content_like_candidate(meta):
            tag = str(meta.get("tag", "")).strip().lower()
            role = str(meta.get("ui_role", "")).strip().lower()
            if section_name:
                return f"content:{section_name}|{tag}|{role}"
        return f"pattern:{pattern_key}"
    return ""


def _read_page_hit_count(page) -> int:
    try:
        value = page.evaluate("() => Number(window.__qaHitCount || 0)")
        return int(max(0, int(value or 0)))
    except Exception:
        return -1


def _read_page_last_hit_event(page) -> str:
    try:
        value = page.evaluate("() => String(window.__qaLastHitEvent || '')")
        return str(value or "").strip()
    except Exception:
        return ""


def _read_session_hit_count(session_id: str) -> int:
    sid = str(session_id or "").strip()
    if not sid:
        return -1
    with _LOCK:
        session = _SESSIONS.get(sid)
        if not session:
            return -1
        try:
            return int(max(0, int(session.captured_events or 0)))
        except Exception:
            return -1


def _read_session_last_hit_event(session_id: str) -> str:
    sid = str(session_id or "").strip()
    if not sid:
        return ""
    with _LOCK:
        session = _SESSIONS.get(sid)
        if not session:
            return ""
        return str(getattr(session, "last_ingested_hit_event", "") or "").strip()


def _event_family_simple(name: str) -> str:
    raw = str(name or "").strip().lower()
    if not raw:
        return ""
    if raw.startswith("click_") or raw in {
        "select_item",
        "view_item",
        "purchase",
        "add_to_cart",
        "begin_checkout",
    }:
        return "click"
    if raw.startswith("impression_") or raw in {
        "view_item_list",
        "view_promotion",
    }:
        return "impression"
    if "wishlist" in raw:
        return "wishlist"
    if raw in {"page_view", "session_start", "first_visit", "scroll"}:
        return "navigation"
    return raw.split("_", 1)[0]


def _get_expected_param(
    row: Dict[str, object],
    key: str,
    fallback_row_key: bool = True,
) -> str:
    expected_params = row.get("expected_params", {})
    expected_params = expected_params if isinstance(expected_params, dict) else {}
    value = str(expected_params.get(key, "")).strip()
    if value:
        return value
    if fallback_row_key:
        return str(row.get(key, "")).strip()
    return ""


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
    if post_data:
        try:
            payload = json.loads(post_data)
            if isinstance(payload, dict):
                direct = str(
                    payload.get("qa_debug_session_id", "")
                    or payload.get("session_id", "")
                ).strip()
                if direct:
                    return direct
                events = payload.get("events")
                if isinstance(events, list):
                    for ev in events:
                        if not isinstance(ev, dict):
                            continue
                        event_props = ev.get("event_properties")
                        event_props = (
                            event_props if isinstance(event_props, dict) else {}
                        )
                        sid = str(
                            event_props.get("qa_debug_session_id", "")
                            or event_props.get("session_id", "")
                        ).strip()
                        if sid:
                            return sid
        except Exception:
            pass
    return ""


def infer_collect_session_id(
    request_url: str, request_method: str = "GET", request_body: str = ""
) -> str:
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

    effective_db = (
        Path(db_path) if db_path is not None else Path("data/test_logs/qa_runs.db")
    )
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
        running = [
            sid
            for sid, sess in _SESSIONS.items()
            if str(getattr(sess, "status", "")).strip() == "running"
        ]
    if len(running) == 1:
        return running[0]
    return ""


def _extract_ga_hit_payloads(
    url: str, method: str, post_data: str, session_id: str
) -> List[Dict[str, object]]:
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

    if (
        path.endswith("/mp/collect")
        or "/mp/collect" in path
        or ("measurement_id" in query and post_data)
    ):
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
                            event_params = (
                                raw_params if isinstance(raw_params, dict) else {}
                            )
                            hits.append(
                                {
                                    "source": "ga_hit",
                                    "event_name": event_name,
                                    "params": event_params,
                                    "session_id": session_id,
                                    "page_url": str(
                                        event_params.get("page_location", "")
                                    ).strip(),
                                    "measurement_id": query.get("measurement_id", ""),
                                    "client_id": client_id,
                                    "request_method": method.upper(),
                                    "captured_at": datetime.now(
                                        timezone.utc
                                    ).isoformat(),
                                }
                            )
            except Exception:
                return []
    return hits


def _is_amplitude_like_request(url: str, method: str, post_data: str) -> bool:
    parsed = urlparse(url or "")
    host = str(parsed.hostname or "").strip().lower()
    path = str(parsed.path or "").strip().lower()
    if "amplitude.com" not in host:
        return False
    if any(token in path for token in ["/2/httpapi", "/batch", "/httpapi"]):
        return True
    if method.upper() == "POST" and post_data and '"events"' in post_data:
        return True
    return False


def _extract_amplitude_hit_payloads(
    url: str, method: str, post_data: str, session_id: str
) -> List[Dict[str, object]]:
    if not _is_amplitude_like_request(url, method, post_data):
        return []

    parsed = urlparse(url)
    query = _parse_form_encoded(parsed.query)
    hits: List[Dict[str, object]] = []
    body_obj: Dict[str, object] = {}
    events: List[Dict[str, object]] = []
    if post_data:
        try:
            parsed_json = json.loads(post_data)
            if isinstance(parsed_json, dict):
                body_obj = parsed_json
        except Exception:
            pass
        if not body_obj:
            form = _parse_form_encoded(post_data)
            if isinstance(form, dict) and form:
                body_obj = dict(form)
                for key in ("events", "event"):
                    raw = str(form.get(key, "")).strip()
                    if not raw:
                        continue
                    try:
                        parsed_events = json.loads(raw)
                        if isinstance(parsed_events, list):
                            events.extend([e for e in parsed_events if isinstance(e, dict)])
                        elif isinstance(parsed_events, dict):
                            events.append(parsed_events)
                    except Exception:
                        continue
    if not events:
        payload_events = body_obj.get("events")
        if isinstance(payload_events, list):
            events = [e for e in payload_events if isinstance(e, dict)]
        elif isinstance(body_obj.get("event"), dict):
            events = [body_obj.get("event")]  # type: ignore[list-item]

    for ev in events:
        event_name = str(
            ev.get("event_type") or ev.get("event_name") or ev.get("event") or ""
        ).strip()
        if not event_name:
            continue
        event_props = ev.get("event_properties")
        event_props = event_props if isinstance(event_props, dict) else {}
        user_props = ev.get("user_properties")
        user_props = user_props if isinstance(user_props, dict) else {}
        params: Dict[str, object] = dict(event_props)
        if "qa_debug_session_id" not in params:
            params["qa_debug_session_id"] = str(
                event_props.get("qa_debug_session_id", "")
                or body_obj.get("qa_debug_session_id", "")
                or session_id
            ).strip()
        page_url = str(
            event_props.get("page_url", "")
            or event_props.get("page_location", "")
            or ev.get("page_url", "")
            or ""
        ).strip()
        if not page_url:
            page_url = str(query.get("url", "") or query.get("page_url", "")).strip()
        hits.append(
            {
                "source": "amplitude_hit",
                "event_name": event_name,
                "params": params,
                "user_properties": user_props,
                "session_id": session_id,
                "page_url": page_url,
                "measurement_id": str(query.get("api_key", "") or body_obj.get("api_key", "")).strip(),
                "client_id": str(
                    ev.get("device_id", "")
                    or ev.get("user_id", "")
                    or body_obj.get("device_id", "")
                    or ""
                ).strip(),
                "request_method": method.upper(),
                "captured_at": datetime.now(timezone.utc).isoformat(),
            }
        )
    return hits


def _extract_analytics_hit_payloads(
    url: str, method: str, post_data: str, session_id: str
) -> List[Dict[str, object]]:
    hits: List[Dict[str, object]] = []
    hits.extend(_extract_ga_hit_payloads(url, method, post_data, session_id))
    hits.extend(_extract_amplitude_hit_payloads(url, method, post_data, session_id))
    return hits


def _is_analytics_probe_candidate(url: str, method: str, post_data: str) -> bool:
    parsed = urlparse(url or "")
    host = str(parsed.hostname or "").strip().lower()
    path = str(parsed.path or "").strip().lower()
    if not host:
        return False
    if "amplitude" in host or "analytics" in host or "segment" in host:
        return True
    if any(token in path for token in ["/collect", "/track", "/batch", "/httpapi"]):
        return True
    if method.upper() == "POST" and post_data and '"events"' in post_data:
        return True
    return False


def _record_analytics_probe_if_needed(
    session_id: str,
    request_url: str,
    request_method: str,
    request_body: str,
    extracted_hits: List[Dict[str, object]],
) -> None:
    if not _is_analytics_probe_candidate(request_url, request_method, request_body):
        return
    parsed = urlparse(request_url or "")
    probe_key = f"{str(parsed.hostname or '').strip().lower()}|{str(parsed.path or '').strip().lower()}"
    with _LOCK:
        session = _SESSIONS.get(session_id)
        if not session:
            return
        if not isinstance(session.analytics_probe_keys, set):
            session.analytics_probe_keys = set()
        if probe_key in session.analytics_probe_keys:
            return
        if int(getattr(session, "analytics_probe_count", 0) or 0) >= 30:
            return
        session.analytics_probe_keys.add(probe_key)
        session.analytics_probe_count = int(
            getattr(session, "analytics_probe_count", 0) or 0
        ) + 1

    body_head = str(request_body or "").strip().replace("\n", " ").replace("\r", " ")[:220]
    _record_runtime_payload(
        session_id,
        {
            "source": "analytics_probe",
            "event_name": "analytics_probe_request",
            "page_url": str(request_url or "").strip(),
            "request_method": str(request_method or "GET").strip().upper(),
            "params": {
                "host": str(parsed.hostname or "").strip().lower(),
                "path": str(parsed.path or "").strip().lower(),
                "query": str(parsed.query or "").strip()[:180],
                "parsed_hit_count": int(len(extracted_hits or [])),
                "matched_sources": sorted(
                    list(
                        {
                            str(h.get("source", "")).strip()
                            for h in (extracted_hits or [])
                            if str(h.get("source", "")).strip()
                        }
                    )
                ),
                "body_snippet": body_head,
                "body_has_events_json": bool('"events"' in str(request_body or "")),
            },
        },
        count_as_event=False,
    )


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


def _normalize_run_settings(
    run_settings: Optional[Dict[str, object]] = None,
) -> Dict[str, object]:
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

    goal_event_names: List[str] = []
    raw_goal_events = raw.get("goal_event_names", [])
    if isinstance(raw_goal_events, (list, tuple, set)):
        for item in raw_goal_events:
            name = str(item or "").strip()
            if name and name not in goal_event_names:
                goal_event_names.append(name)
    elif str(raw_goal_events or "").strip():
        for token in re.split(r"[,\n]+", str(raw_goal_events or "").strip()):
            name = str(token or "").strip()
            if name and name not in goal_event_names:
                goal_event_names.append(name)

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

    definition_runtime_rows: List[Dict[str, object]] = []
    raw_definition_rows = raw.get("definition_runtime_rows", [])
    if isinstance(raw_definition_rows, (list, tuple)):
        for item in raw_definition_rows:
            if not isinstance(item, dict):
                continue
            row_id = str(item.get("definition_row_id", "")).strip()
            event_name = str(item.get("event_name", "")).strip()
            if not row_id or not event_name:
                continue
            expected_params = item.get("expected_params", {})
            expected_params = (
                expected_params if isinstance(expected_params, dict) else {}
            )

            definition_runtime_rows.append(
                {
                    "definition_row_id": row_id,
                    "no": str(item.get("no", "")).strip(),
                    "event_name": event_name,
                    "page_id": str(item.get("page_id", "")).strip(),
                    "section_name": str(item.get("section_name", "")).strip(),
                    "canonical_key": str(item.get("canonical_key", "")).strip(),
                    "section_index": str(item.get("section_index", "")).strip(),
                    "action": str(item.get("action", "")).strip(),
                    "object": str(item.get("object", "")).strip(),
                    "description": str(item.get("description", "")).strip(),
                    "target_ids": (
                        [
                            str(v).strip()
                            for v in item.get("target_ids", [])
                            if str(v).strip()
                        ]
                        if isinstance(item.get("target_ids", []), list)
                        else []
                    ),
                    "text_hints": (
                        [
                            str(v).strip()
                            for v in item.get("text_hints", [])
                            if str(v).strip()
                        ]
                        if isinstance(item.get("text_hints", []), list)
                        else []
                    ),
                    "expected_params": {
                        str(k).strip(): str(v).strip()
                        for k, v in expected_params.items()
                        if str(k).strip()
                    },
                }
            )

    raw_definition_event_types = raw.get("definition_event_types", {})
    definition_event_types: Dict[str, str] = {}
    if isinstance(raw_definition_event_types, dict):
        for key, value in raw_definition_event_types.items():
            key_text = str(key or "").strip()
            value_text = str(value or "").strip()
            if key_text and value_text and key_text not in definition_event_types:
                definition_event_types[key_text] = value_text

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
        "max_run_minutes": _clamp_int(
            raw.get("max_run_minutes", _env_int("QA_MAX_RUN_MINUTES", 25)),
            25,
            1,
            240,
        ),
        "click_interval_ms": _clamp_int(
            raw.get(
                "click_interval_ms", _env_int("QA_AUTO_CRAWL_CLICK_INTERVAL_MS", 400)
            ),
            400,
            100,
            10000,
        ),
        "wait_after_click_ms": _clamp_int(
            raw.get(
                "wait_after_click_ms",
                _env_int("QA_AUTO_CRAWL_WAIT_AFTER_CLICK_MS", 600),
            ),
            600,
            100,
            10000,
        ),
        "block_link_navigation": bool(
            raw.get(
                "block_link_navigation",
                _is_truthy(os.getenv("QA_AUTO_BLOCK_LINK_NAV", "1")),
            )
        ),
        "single_page_only": bool(
            raw.get(
                "single_page_only", _is_truthy(os.getenv("QA_SINGLE_PAGE_ONLY", "1"))
            )
        ),
        "restrict_to_start_url": bool(
            raw.get(
                "restrict_to_start_url",
                _is_truthy(os.getenv("QA_RESTRICT_TO_START_URL", "1")),
            )
        ),
        "mobile_mode": bool(
            raw.get("mobile_mode", _is_truthy(os.getenv("QA_DEBUG_MOBILE_MODE", "0")))
        ),
        "mobile_device": str(
            raw.get("mobile_device", "iPhone 13") or "iPhone 13"
        ).strip()
        or "iPhone 13",
        "qa_mode": str(
            raw.get("qa_mode", "전체 이벤트 테스트") or "전체 이벤트 테스트"
        ).strip(),
        "scenario_template": str(raw.get("scenario_template", "") or "").strip(),
        "definition_file_name": str(raw.get("definition_file_name", "") or "").strip(),
        "definition_event_names": definition_event_names,
        "goal_event_names": goal_event_names,
        "definition_runtime_hints": definition_runtime_hints,
        "definition_runtime_rows": definition_runtime_rows,
        "definition_event_types": definition_event_types,
        "manual_recording_enabled": bool(raw.get("manual_recording_enabled", False)),
        "manual_profile_selected": str(raw.get("manual_profile_selected", "default") or "default").strip() or "default",
        "manual_flow_replay_enabled": bool(raw.get("manual_flow_replay_enabled", True)),
        "manual_recording_selected": str(raw.get("manual_recording_selected", "") or "").strip(),
        "manual_recording_name": str(raw.get("manual_recording_name", "") or "").strip(),
        "auto_crawl_start_mode": (
            str(raw.get("auto_crawl_start_mode", "manual") or "manual").strip().lower()
            if str(raw.get("auto_crawl_start_mode", "manual") or "manual").strip().lower() in {"manual", "auto"}
            else "manual"
        ),
        "manual_page_scope_key": str(raw.get("manual_page_scope_key", "") or "").strip(),
        "manual_profile_options": [
            str(v).strip()
            for v in (
                raw.get("manual_profile_options", [])
                if isinstance(raw.get("manual_profile_options", []), list)
                else []
            )
            if str(v).strip()
        ],
        "manual_recording_catalog": _normalize_manual_recording_catalog(
            raw.get("manual_recording_catalog", [])
        ),
        "analytics_source_mode": (
            str(raw.get("analytics_source_mode", "both") or "both").strip().lower()
            if str(raw.get("analytics_source_mode", "both") or "both").strip().lower()
            in {"both", "ga4", "amplitude"}
            else "both"
        ),
        # added
        "save_raw_screenshot": bool(raw.get("save_raw_screenshot", False)),
        "save_probe_screenshot": bool(raw.get("save_probe_screenshot", False)),
        "screenshot_format": str(raw.get("screenshot_format", "png") or "png")
        .strip()
        .lower(),
        "screenshot_quality": _clamp_int(
            raw.get("screenshot_quality", 85), 85, 30, 100
        ),
        "overlay_only_capture": bool(raw.get("overlay_only_capture", True)),
        "content_section_click_budget": _clamp_int(
            raw.get("content_section_click_budget", 1), 1, 1, 10
        ),
        "no_novelty_limit": _clamp_int(
            raw.get("no_novelty_limit", 10), 10, 2, 80
        ),
    }
    return settings


def _get_definition_event_targets(
    run_settings: Optional[Dict[str, object]] = None,
) -> List[str]:
    raw_settings = _normalize_run_settings(run_settings)
    values = raw_settings.get("definition_event_names", [])
    if not isinstance(values, list):
        return []
    return [str(name).strip() for name in values if str(name).strip()]


def _get_collection_goal_event_names(
    run_settings: Optional[Dict[str, object]] = None,
) -> List[str]:
    raw_settings = _normalize_run_settings(run_settings)
    values = raw_settings.get("goal_event_names", [])
    if not isinstance(values, list):
        return []
    return [str(name).strip() for name in values if str(name).strip()]


def _get_definition_event_type_map(
    run_settings: Optional[Dict[str, object]] = None,
) -> Dict[str, str]:
    raw_settings = _normalize_run_settings(run_settings)
    values = raw_settings.get("definition_event_types", {})
    if not isinstance(values, dict):
        return {}
    out: Dict[str, str] = {}
    for key, value in values.items():
        key_text = str(key or "").strip()
        value_text = str(value or "").strip()
        if key_text and value_text:
            out[key_text] = value_text
    return out


def _get_definition_runtime_hints(
    run_settings: Optional[Dict[str, object]] = None,
) -> Dict[str, List[str]]:
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


def _get_definition_runtime_rows(
    run_settings: Optional[Dict[str, object]] = None,
) -> List[Dict[str, object]]:
    raw_settings = _normalize_run_settings(run_settings)
    rows = raw_settings.get("definition_runtime_rows", [])
    if not isinstance(rows, list):
        return []
    return [dict(row) for row in rows if isinstance(row, dict)]


def _safe_int(value: object, fallback: int = 10**9) -> int:
    try:
        return int(str(value).strip())
    except Exception:
        return fallback


def _manual_definition_key(run_settings: Dict[str, object]) -> str:
    return str(run_settings.get("definition_file_name", "")).strip() or "__default__"


def _manual_page_scope_key(run_settings: Dict[str, object]) -> str:
    explicit = str(run_settings.get("manual_page_scope_key", "")).strip()
    if explicit:
        return explicit
    target = str(run_settings.get("target_url", "")).strip()
    return _build_compare_key(target) if target else "__page__"


def _load_manual_flow_store() -> Dict[str, object]:
    try:
        if not _MANUAL_FLOW_LIBRARY_PATH.exists():
            return {"version": 1, "definitions": {}}
        payload = json.loads(_MANUAL_FLOW_LIBRARY_PATH.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            return {"version": 1, "definitions": {}}
        payload.setdefault("version", 1)
        payload.setdefault("definitions", {})
        return payload
    except Exception:
        return {"version": 1, "definitions": {}}


def _save_manual_flow_store(payload: Dict[str, object]) -> None:
    try:
        _MANUAL_FLOW_LIBRARY_PATH.parent.mkdir(parents=True, exist_ok=True)
        _MANUAL_FLOW_LIBRARY_PATH.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception:
        return


def _record_manual_flow_step(
    session_id: str,
    payload: Dict[str, object],
    run_settings: Dict[str, object],
    force_save: bool = False,
) -> None:
    if str(run_settings.get("qa_mode", "")).strip() != "정의서 검증":
        return
    params = payload.get("params", {}) if isinstance(payload.get("params", {}), dict) else {}
    with _LOCK:
        session = _SESSIONS.get(str(session_id or "").strip())
        if (not force_save) and session and not bool(session.manual_recording_enabled):
            return
    row_id = str(params.get("current_definition_row_id", "")).strip()
    if not row_id:
        return
    selector = str(params.get("selector", "")).strip()
    target_hint = (
        str(params.get("text", "")).strip()
        or str(params.get("target_id", "")).strip()
        or selector
    )
    if not selector and not target_hint:
        return
    wait_ms = _safe_int(params.get("wait_ms", 600), fallback=600)
    if wait_ms < 100:
        wait_ms = 100
    if wait_ms > 5000:
        wait_ms = 5000
    step = {
        "action": "click",
        "target_hint": target_hint,
        "target_id": str(params.get("target_id", "")).strip(),
        "selector": selector,
        "wait_ms": wait_ms,
        "event_name": str(params.get("current_definition_event_name", "")).strip(),
        "section_name": str(params.get("current_definition_section_name", "")).strip(),
        "canonical_key": str(
            params.get("current_definition_canonical_key", "")
        ).strip(),
    }

    with _LOCK:
        store = _load_manual_flow_store()
        defs = store.get("definitions", {})
        defs = defs if isinstance(defs, dict) else {}
        definition_key = _manual_definition_key(run_settings)
        page_scope_key = _manual_page_scope_key(run_settings)
        profile_selected = (
            str(params.get("manual_profile", "")).strip()
            or (
                str(session.manual_profile_selected).strip()
                if session
                else str(run_settings.get("manual_profile_selected", "default")).strip()
            )
            or "default"
        )
        per_definition_root = defs.get(definition_key, {})
        per_definition_root = (
            per_definition_root if isinstance(per_definition_root, dict) else {}
        )
        per_profile_root = per_definition_root.get(profile_selected, {})
        per_profile_root = (
            per_profile_root if isinstance(per_profile_root, dict) else {}
        )
        per_definition = per_profile_root.get(page_scope_key, {})
        per_definition = per_definition if isinstance(per_definition, dict) else {}
        row_state = per_definition.get(row_id, {})
        row_state = row_state if isinstance(row_state, dict) else {}
        steps = row_state.get("steps", [])
        steps = steps if isinstance(steps, list) else []

        dedup = {
            (
                str(item.get("action", "click")).strip().lower(),
                str(item.get("selector", "")).strip(),
                str(item.get("target_hint", "")).strip(),
            )
            for item in steps
            if isinstance(item, dict)
        }
        step_key = (
            "click",
            str(step.get("selector", "")).strip(),
            str(step.get("target_hint", "")).strip(),
        )
        if step_key in dedup:
            return

        steps.append(step)
        row_state["steps"] = steps[-12:]
        row_state["updated_at"] = datetime.now(timezone.utc).isoformat()
        row_state["session_id"] = str(session_id or "").strip()
        row_state["profile"] = profile_selected
        per_definition[row_id] = row_state
        per_profile_root[page_scope_key] = per_definition
        per_definition_root[profile_selected] = per_profile_root
        defs[definition_key] = per_definition_root
        store["definitions"] = defs
        _save_manual_flow_store(store)


def _normalize_manual_recording_step(
    payload: Dict[str, object],
    require_row_id: bool = False,
) -> Dict[str, object] | None:
    params = payload.get("params", {}) if isinstance(payload.get("params", {}), dict) else {}
    row_id = str(params.get("current_definition_row_id", "")).strip()
    if require_row_id and not row_id:
        return None
    selector = str(params.get("selector", "")).strip()
    target_hint = (
        str(params.get("text", "")).strip()
        or str(params.get("target_id", "")).strip()
        or selector
    )
    if not selector and not target_hint:
        return None
    wait_ms = _safe_int(params.get("wait_ms", 600), fallback=600)
    wait_ms = max(100, min(wait_ms, 5000))
    return {
        "action": "click",
        "target_hint": target_hint,
        "target_id": str(params.get("target_id", "")).strip(),
        "selector": selector,
        "wait_ms": wait_ms,
        "row_id": row_id,
        "event_name": str(params.get("current_definition_event_name", "")).strip(),
        "section_name": str(params.get("current_definition_section_name", "")).strip(),
        "canonical_key": str(params.get("current_definition_canonical_key", "")).strip(),
    }


def _buffer_manual_flow_step(
    session_id: str,
    payload: Dict[str, object],
) -> None:
    sid = str(session_id or "").strip()
    if not sid:
        return
    with _LOCK:
        session = _SESSIONS.get(sid)
        if not session or not bool(session.manual_recording_enabled):
            return
        if not isinstance(session.manual_record_buffer, list):
            session.manual_record_buffer = []
        session.manual_record_buffer.append(dict(payload))
        session.manual_record_buffer = session.manual_record_buffer[-120:]


def _flush_manual_recording_buffer(session_id: str, reason: str = "") -> None:
    sid = str(session_id or "").strip()
    if not sid:
        return
    with _LOCK:
        session = _SESSIONS.get(sid)
        if not session:
            return
        buffered_payloads = (
            list(session.manual_record_buffer)
            if isinstance(session.manual_record_buffer, list)
            else []
        )
        session.manual_record_buffer = []
        run_settings = (
            dict(session.run_settings) if isinstance(session.run_settings, dict) else {}
        )
        selected_profile = str(session.manual_profile_selected or "default").strip() or "default"
        existing_selected_id = str(session.manual_recording_selected or "").strip()
        custom_name = str(session.manual_recording_name or "").strip()
    if not buffered_payloads:
        return

    for payload in buffered_payloads:
        if isinstance(payload, dict):
            _record_manual_flow_step(
                sid,
                payload,
                run_settings,
                force_save=True,
            )

    normalized_steps: List[Dict[str, object]] = []
    seen_keys: set[tuple[str, str, str]] = set()
    for payload in buffered_payloads:
        if not isinstance(payload, dict):
            continue
        step = _normalize_manual_recording_step(payload, require_row_id=False)
        if not isinstance(step, dict):
            continue
        step_key = (
            str(step.get("action", "click")).strip().lower(),
            str(step.get("selector", "")).strip(),
            str(step.get("target_hint", "")).strip(),
        )
        if step_key in seen_keys:
            continue
        seen_keys.add(step_key)
        normalized_steps.append(step)
    if not normalized_steps:
        return

    created_at = datetime.now(timezone.utc).isoformat()
    rec_id = f"rec_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S_%f')}_{os.urandom(2).hex()}"
    rec_name = custom_name or str(run_settings.get("manual_recording_name", "")).strip()
    if not rec_name:
        rec_name = f"Manual Recording {datetime.now().strftime('%m-%d %H:%M')}"
    definition_key = _manual_definition_key(run_settings)
    page_scope_key = _manual_page_scope_key(run_settings)
    step_payloads = [
        {
            "action": str(step.get("action", "click")).strip().lower() or "click",
            "target_hint": str(step.get("target_hint", "")).strip(),
            "target_id": str(step.get("target_id", "")).strip(),
            "selector": str(step.get("selector", "")).strip(),
            "wait_ms": _safe_int(step.get("wait_ms", 600), fallback=600),
            "row_id": str(step.get("row_id", "")).strip(),
            "event_name": str(step.get("event_name", "")).strip(),
            "section_name": str(step.get("section_name", "")).strip(),
            "canonical_key": str(step.get("canonical_key", "")).strip(),
        }
        for step in normalized_steps
    ][:80]
    signature_text_candidates = [
        str(payload.get("ui_signature_text", "")).strip()
        for payload in buffered_payloads
        if isinstance(payload, dict) and str(payload.get("ui_signature_text", "")).strip()
    ]
    signature_text = signature_text_candidates[-1] if signature_text_candidates else ""
    signature_hash = hashlib.sha1(signature_text.encode("utf-8")).hexdigest()[:16] if signature_text else ""

    with _LOCK:
        store = _load_manual_flow_store()
        rec_root = store.get("recordings", {})
        rec_root = rec_root if isinstance(rec_root, dict) else {}
        def_root = rec_root.get(definition_key, {})
        def_root = def_root if isinstance(def_root, dict) else {}
        profile_root = def_root.get(selected_profile, {})
        profile_root = profile_root if isinstance(profile_root, dict) else {}
        page_entries = profile_root.get(page_scope_key, [])
        page_entries = page_entries if isinstance(page_entries, list) else []
        page_entries.insert(
            0,
            {
                "id": rec_id,
                "name": rec_name,
                "created_at": created_at,
                "updated_at": created_at,
                "session_id": sid,
                "source_reason": str(reason or "").strip(),
                "step_count": len(step_payloads),
                "ui_signature_hash": signature_hash,
                "ui_signature_text": signature_text,
                "ui_signature_version": "v1",
                "steps": step_payloads,
            },
        )
        profile_root[page_scope_key] = page_entries[:20]
        def_root[selected_profile] = profile_root
        rec_root[definition_key] = def_root
        store["recordings"] = rec_root
        _save_manual_flow_store(store)

        session = _SESSIONS.get(sid)
        if session:
            session.manual_recording_selected = rec_id
            if isinstance(session.run_settings, dict):
                session.run_settings["manual_recording_selected"] = rec_id
                session.run_settings["manual_recording_name"] = rec_name
                current_catalog = _normalize_manual_recording_catalog(
                    session.run_settings.get("manual_recording_catalog", [])
                )
                newest = {
                    "id": rec_id,
                    "name": rec_name,
                    "created_at": created_at,
                    "updated_at": created_at,
                    "step_count": len(step_payloads),
                    "ui_signature_hash": signature_hash,
                    "ui_signature_text": signature_text,
                    "ui_signature_version": "v1",
                    "steps": step_payloads,
                }
                session.run_settings["manual_recording_catalog"] = [newest] + current_catalog[:29]
                session.run_settings["manual_profile_selected"] = selected_profile
    if existing_selected_id != rec_id:
        _sync_session_db(sid)


def _load_manual_recording_catalog(
    run_settings: Dict[str, object],
    profile_selected: str,
) -> List[Dict[str, object]]:
    definition_key = _manual_definition_key(run_settings)
    page_scope_key = _manual_page_scope_key(run_settings)
    profile = str(profile_selected or "default").strip() or "default"
    try:
        store = _load_manual_flow_store()
        rec_root = store.get("recordings", {})
        rec_root = rec_root if isinstance(rec_root, dict) else {}
        def_root = rec_root.get(definition_key, {})
        def_root = def_root if isinstance(def_root, dict) else {}
        profile_root = def_root.get(profile, {})
        profile_root = profile_root if isinstance(profile_root, dict) else {}
        entries = profile_root.get(page_scope_key, [])
        entries = entries if isinstance(entries, list) else []
        out: List[Dict[str, object]] = []
        for item in entries[:12]:
            if not isinstance(item, dict):
                continue
            rid = str(item.get("id", "")).strip()
            if not rid:
                continue
            steps = item.get("steps", [])
            if not isinstance(steps, list):
                steps = []
            out.append(
                {
                    "id": rid,
                    "name": str(item.get("name", "")).strip() or rid,
                    "created_at": str(item.get("created_at", "")).strip(),
                    "updated_at": str(item.get("updated_at", "")).strip(),
                    "step_count": _safe_int(item.get("step_count", len(steps)), fallback=len(steps)),
                    "ui_signature_hash": str(item.get("ui_signature_hash", "")).strip(),
                    "ui_signature_text": str(item.get("ui_signature_text", "")).strip(),
                    "ui_signature_version": str(item.get("ui_signature_version", "v1")).strip() or "v1",
                    "steps": [
                        {
                            "action": str(step.get("action", "click") or "click").strip().lower(),
                            "target_hint": str(step.get("target_hint", "")).strip(),
                            "target_id": str(step.get("target_id", "")).strip(),
                            "selector": str(step.get("selector", "")).strip(),
                            "wait_ms": _safe_int(step.get("wait_ms", 600), fallback=600),
                            "row_id": str(step.get("row_id", "")).strip(),
                        }
                        for step in steps
                        if isinstance(step, dict)
                    ][:40],
                }
            )
        return out
    except Exception:
        return []


def _delete_manual_recording(
    run_settings: Dict[str, object],
    profile_selected: str,
    recording_id: str,
) -> bool:
    rid = str(recording_id or "").strip()
    if not rid:
        return False
    definition_key = _manual_definition_key(run_settings)
    page_scope_key = _manual_page_scope_key(run_settings)
    profile = str(profile_selected or "default").strip() or "default"
    try:
        store = _load_manual_flow_store()
        rec_root = store.get("recordings", {})
        if not isinstance(rec_root, dict):
            return False
        def_root = rec_root.get(definition_key, {})
        if not isinstance(def_root, dict):
            return False
        profile_root = def_root.get(profile, {})
        if not isinstance(profile_root, dict):
            return False
        rows = profile_root.get(page_scope_key, [])
        if not isinstance(rows, list):
            return False
        kept = [row for row in rows if not (isinstance(row, dict) and str(row.get("id", "")).strip() == rid)]
        if len(kept) == len(rows):
            return False
        profile_root[page_scope_key] = kept
        def_root[profile] = profile_root
        rec_root[definition_key] = def_root
        store["recordings"] = rec_root
        _save_manual_flow_store(store)
        return True
    except Exception:
        return False


def _load_manual_steps_for_row(
    run_settings: Dict[str, object],
    profile_selected: str,
    row_id: str,
) -> List[Dict[str, object]]:
    definition_key = _manual_definition_key(run_settings)
    page_scope_key = _manual_page_scope_key(run_settings)
    rid = str(row_id or "").strip()
    if not rid:
        return []
    try:
        store = _load_manual_flow_store()
        defs = store.get("definitions", {})
        defs = defs if isinstance(defs, dict) else {}
        root = defs.get(definition_key, {})
        root = root if isinstance(root, dict) else {}
        profile = str(profile_selected or "default").strip() or "default"
        profile_root = root.get(profile, {})
        if not isinstance(profile_root, dict):
            # backward compatibility (old shape)
            profile_root = root
        block = profile_root.get(page_scope_key, {})
        if not isinstance(block, dict):
            block = profile_root
        row_state = block.get(rid, {}) if isinstance(block, dict) else {}
        steps = row_state.get("steps", []) if isinstance(row_state, dict) else []
        if not isinstance(steps, list):
            return []
        out: List[Dict[str, object]] = []
        for step in steps:
            if not isinstance(step, dict):
                continue
            out.append(
                {
                    "action": str(step.get("action", "click") or "click").strip().lower(),
                    "target_hint": str(step.get("target_hint", "")).strip(),
                    "target_id": str(step.get("target_id", "")).strip(),
                    "selector": str(step.get("selector", "")).strip(),
                    "wait_ms": _safe_int(step.get("wait_ms", 600), fallback=600),
                    "row_id": str(step.get("row_id", "")).strip(),
                }
            )
        return out
    except Exception:
        return []


def _select_next_definition_row(
    definition_rows: List[Dict[str, object]],
    matched_row_ids: set[str],
    skipped_row_ids: set[str],
) -> Dict[str, object]:
    candidates = [
        row
        for row in definition_rows
        if str(row.get("definition_row_id", "")).strip()
        and str(row.get("definition_row_id", "")).strip() not in matched_row_ids
        and str(row.get("definition_row_id", "")).strip() not in skipped_row_ids
    ]
    if not candidates:
        return {}

    def sort_key(row: Dict[str, object]):
        group = str(row.get("scenario_group", "")).strip()
        order = _safe_int(row.get("scenario_order", ""), fallback=10**9)
        no = _safe_int(row.get("no", ""), fallback=10**9)
        row_id = str(row.get("definition_row_id", "")).strip()
        if group:
            return (0, group, order, no, row_id)
        return (1, "", order, no, row_id)

    candidates.sort(key=sort_key)
    return candidates[0]


def _group_counts(
    definition_rows: List[Dict[str, object]],
    group_name: str,
    matched_row_ids: set[str],
    skipped_row_ids: set[str],
) -> Dict[str, int]:
    total = 0
    done = 0
    for row in definition_rows:
        if str(row.get("scenario_group", "")).strip() != group_name:
            continue
        row_id = str(row.get("definition_row_id", "")).strip()
        if not row_id:
            continue
        total += 1
        if row_id in matched_row_ids or row_id in skipped_row_ids:
            done += 1
    return {"total": total, "done": done}


def _get_processed_definition_row_ids(session: Optional[DebugSession]) -> set[str]:
    if not session:
        return set()
    processed = set(getattr(session, "matched_definition_rows", set()) or set())
    processed.update(getattr(session, "skipped_definition_rows", set()) or set())
    return {str(row_id).strip() for row_id in processed if str(row_id).strip()}


def _row_hints_from_definition_row(row: Dict[str, object]) -> Dict[str, List[str]]:
    if not isinstance(row, dict):
        return {
            "target_ids": [],
            "section_names": [],
            "page_ids": [],
            "text_hints": [],
            "screen_states": [],
        }

    expected_params = row.get("expected_params", {})
    expected_params = expected_params if isinstance(expected_params, dict) else {}

    section_name = str(
        expected_params.get("section_name") or row.get("section_name", "")
    ).strip()
    page_id = str(expected_params.get("page_id") or row.get("page_id", "")).strip()

    extra_target_ids = []
    for key in [
        "button_id",
        "target_id",
        "content_id",
        "banner_id",
        "brand_id",
        "category_id",
    ]:
        value = str(expected_params.get(key, "")).strip()
        if value:
            extra_target_ids.append(value)

    return {
        "target_ids": (
            [str(v).strip() for v in row.get("target_ids", []) if str(v).strip()]
            if isinstance(row.get("target_ids", []), list)
            else []
        )
        + extra_target_ids,
        "section_names": [section_name] if section_name else [],
        "page_ids": [page_id] if page_id else [],
        "text_hints": (
            [str(v).strip() for v in row.get("text_hints", []) if str(v).strip()]
            if isinstance(row.get("text_hints", []), list)
            else []
        ),
        "screen_states": (
            [str(row.get("screen_state", "")).strip()]
            if str(row.get("screen_state", "")).strip()
            else []
        ),
    }


def _normalize_candidate_target_values(meta: Dict[str, object]) -> set[str]:
    values: set[str] = set()
    target_id = str(meta.get("target_id", "")).strip()
    if target_id:
        values.add(target_id)
        if ":" in target_id:
            values.add(target_id.split(":", 1)[1].strip())
    selector = str(meta.get("selector", "")).strip()
    if selector:
        values.add(selector)
    return {value for value in values if value}


def _score_candidate_definition_match(
    meta: Dict[str, object], hints: Dict[str, List[str]]
) -> Dict[str, object]:
    target_ids = {str(v).strip() for v in hints.get("target_ids", []) if str(v).strip()}
    section_names = {str(v).strip() for v in hints.get("section_names", []) if str(v).strip()}
    section_names_norm = {v.lower() for v in section_names if v}
    page_ids = {str(v).strip() for v in hints.get("page_ids", []) if str(v).strip()}
    text_hints = [
        str(v).strip().lower() for v in hints.get("text_hints", []) if str(v).strip()
    ]
    target_values = _normalize_candidate_target_values(meta)
    target_id_match = bool(target_ids and target_values.intersection(target_ids))
    section_name = str(meta.get("section_name", "")).strip()
    section_name_norm = section_name.lower()
    section_match = bool(section_names_norm and section_name_norm in section_names_norm)
    page_id = str(meta.get("page_id", "")).strip()
    page_match = bool(page_ids and page_id in page_ids)
    text = str(meta.get("text", "")).strip().lower()
    text_match = bool(text_hints and text and any(hint in text for hint in text_hints))
    strong_hint_exists = bool(target_ids or text_hints)
    has_section_hint = bool(section_names_norm)
    if page_ids and not page_match:
        return {"accepted": False, "score": -999.0}
    # 정의서 section_name 힌트가 있는 경우, 다른 section(footer/gnb 등) 오탐은 조기 차단.
    if has_section_hint and section_name_norm and not section_match:
        return {"accepted": False, "score": -999.0}
    score = 0.0
    if page_match:
        score += 3.0
    if target_id_match:
        score += 6.0
    if text_match:
        score += 4.0
    if section_match:
        score += 4.0
    if has_section_hint:
        if section_match:
            accepted = bool(target_id_match or text_match or page_match or score > 0.0)
        else:
            # section 정보가 수집되지 않은 후보는 target/text 힌트가 강할 때만 허용.
            accepted = bool(
                (not section_name_norm)
                and (target_id_match or text_match)
                and (not page_ids or page_match)
            )
    elif strong_hint_exists:
        accepted = bool(
            target_id_match or text_match or (section_match and score >= 7.0)
        )
    else:
        accepted = bool(section_match or page_match or score > 0.0)
    return {"accepted": accepted, "score": score}


def _candidate_matches_definition(
    meta: Dict[str, object], hints: Dict[str, List[str]]
) -> bool:
    if not isinstance(meta, dict):
        return False
    return bool(_score_candidate_definition_match(meta, hints).get("accepted", False))


def _mark_definition_hits(session_id: str, hits: List[Dict[str, object]]) -> List[str]:
    sid = str(session_id or "").strip()
    if not sid or not hits:
        return []
    with _LOCK:
        session = _SESSIONS.get(sid)
        if not session:
            return []
        target_names = set(_get_definition_event_targets(session.run_settings))
        target_names.update(_get_collection_goal_event_names(session.run_settings))
        if not target_names:
            return []
        for payload in hits:
            event_name = _extract_payload_event_name(payload)
            if event_name and event_name in target_names:
                session.matched_definition_events.add(event_name)
        return sorted(session.matched_definition_events)


def _definition_hit_matches_row(
    target_row: Dict[str, object], payload: Dict[str, object]
) -> bool:
    if not isinstance(target_row, dict) or not isinstance(payload, dict):
        return False

    event_name = _extract_payload_event_name(payload)
    expected_event_name = str(target_row.get("event_name", "")).strip()
    if event_name != expected_event_name:
        return False

    params = payload.get("params", {})
    params = params if isinstance(params, dict) else {}

    # page_id: params.page_id 우선, 없으면 page_url path fallback
    page_url = str(payload.get("page_url", "")).strip()
    page_path = str(urlparse(page_url).path or "").strip()
    actual_page_id = str(params.get("page_id", "")).strip() or page_path
    expected_page_id = _get_expected_param(target_row, "page_id")

    if expected_page_id:
        if not actual_page_id:
            return False
        if not _page_id_pattern_matches(expected_page_id, actual_page_id):
            return False

    expected_section_name = _get_expected_param(target_row, "section_name")
    actual_section_name = str(params.get("section_name", "")).strip()
    if expected_section_name and not _section_name_matches(
        expected_section_name, actual_section_name
    ):
        return False

    expected_section_index = _get_expected_param(target_row, "section_index")
    actual_section_index = str(params.get("section_index", "")).strip()
    if expected_section_index and expected_section_index not in {
        "{index}",
        "{section_index}",
    }:
        if not actual_section_index:
            return False
        if actual_section_index != expected_section_index:
            return False

    expected_button_id = _get_expected_param(
        target_row, "button_id", fallback_row_key=True
    )
    actual_button_id = str(params.get("button_id", "")).strip()
    if expected_button_id:
        if not actual_button_id:
            return False
        if actual_button_id != expected_button_id:
            return False

    expected_target_id = _normalize_target_id(
        _get_expected_param(target_row, "target_id", fallback_row_key=True)
    )
    actual_target_id = _normalize_target_id(str(params.get("target_id", "")).strip())

    if expected_target_id:
        expected_target_tail = expected_target_id.split(":", 1)[-1].strip()
        actual_target_tail = actual_target_id.split(":", 1)[-1].strip()

        if not actual_target_id:
            return False

        if (
            actual_target_id != expected_target_id
            and actual_target_tail != expected_target_tail
        ):
            return False

    target_ids = (
        [str(v).strip() for v in target_row.get("target_ids", []) if str(v).strip()]
        if isinstance(target_row.get("target_ids", []), list)
        else []
    )
    if target_ids and not expected_button_id and not expected_target_id:
        candidate_actuals = [
            str(params.get(key, "")).strip()
            for key in [
                "button_id",
                "banner_id",
                "content_id",
                "category_id",
                "filter_value",
                "brand_id",
                "liked_item_id",
                "content_no",
                "item_list_id",
                "item_list_name",
            ]
        ]
        if not any(v in target_ids for v in candidate_actuals if v):
            return False

    return True


def _definition_relaxed_match_score(
    target_row: Dict[str, object],
    payload: Dict[str, object],
    qa_mode: str = "",
    allow_family_match: bool = False,
) -> Dict[str, object]:
    if not isinstance(target_row, dict) or not isinstance(payload, dict):
        return {"accepted": False, "score": -1.0, "reason": "invalid_payload"}

    def _event_family(name: str) -> str:
        n = str(name or "").strip().lower()
        if not n:
            return ""
        if n.startswith("click_") or n in {"select_item"}:
            return "click"
        if n.startswith("impression_") or n in {"view_item_list", "page_view"}:
            return "impression"
        if n in {"add_to_wishlist", "remove_from_wishlist"}:
            return "wishlist"
        return ""

    event_name = _extract_payload_event_name(payload)
    expected_event_name = str(target_row.get("event_name", "")).strip()
    if not event_name:
        return {"accepted": False, "score": -1.0, "reason": "event_name_missing"}
    event_exact_match = event_name == expected_event_name
    event_family_match = False
    if not event_exact_match:
        if allow_family_match:
            event_family_match = bool(
                _event_family(event_name)
                and _event_family(event_name) == _event_family(expected_event_name)
            )
        if not event_family_match:
            return {"accepted": False, "score": -1.0, "reason": "event_name_mismatch"}

    params = payload.get("params", {})
    params = params if isinstance(params, dict) else {}

    def _tokenize_text(value: object) -> List[str]:
        text = str(value or "").strip().lower()
        if not text:
            return []
        tokens = [t for t in re.split(r"[^0-9a-zA-Z가-힣_]+", text) if t]
        return [t for t in tokens if len(t) >= 2]

    def _build_payload_text(params_obj: Dict[str, object], payload_obj: Dict[str, object]) -> str:
        keys = [
            "section_name",
            "button_id",
            "button_name",
            "content_id",
            "content_name",
            "category_id",
            "category_name",
            "brand_id",
            "filter_type",
            "filter_value",
            "page_id",
            "page_link",
            "landing_url",
            "extra_info",
        ]
        parts: List[str] = []
        for key in keys:
            value = str(params_obj.get(key, "")).strip().lower()
            if value:
                parts.append(value)
        page_url = str(payload_obj.get("page_url", "")).strip().lower()
        if page_url:
            parts.append(page_url)
        return " ".join(parts)

    score = 0.0
    matched_signals: List[str] = []
    hard_signal_count = 0
    non_empty_params = 0
    for value in params.values():
        if str(value).strip():
            non_empty_params += 1
    if event_exact_match:
        score += 2.0
    elif event_family_match:
        score += 0.8
        matched_signals.append("event_family")
    if non_empty_params > 0:
        score += min(1.2, float(non_empty_params) * 0.03)

    expected_page_id = _get_expected_param(target_row, "page_id")
    if expected_page_id:
        page_url = str(payload.get("page_url", "")).strip()
        page_path = str(urlparse(page_url).path or "").strip()
        actual_page_id = str(params.get("page_id", "")).strip() or page_path
        if not actual_page_id:
            return {"accepted": False, "score": -1.0, "reason": "page_id_missing"}
        if not _page_id_pattern_matches(expected_page_id, actual_page_id):
            return {"accepted": False, "score": -1.0, "reason": "page_id_mismatch"}
        score += 3.0
        matched_signals.append("page_id")

    expected_section_name = _get_expected_param(target_row, "section_name")
    actual_section_name = str(params.get("section_name", "")).strip()
    if expected_section_name:
        if not actual_section_name:
            return {"accepted": False, "score": -1.0, "reason": "section_name_missing"}
        if _section_name_matches(expected_section_name, actual_section_name):
            score += 4.0
            hard_signal_count += 1
            matched_signals.append("section_name")
        else:
            return {"accepted": False, "score": -1.0, "reason": "section_name_mismatch"}

    expected_button_id = _get_expected_param(
        target_row, "button_id", fallback_row_key=True
    )
    if expected_button_id:
        actual_button_id = str(params.get("button_id", "")).strip()
        if actual_button_id == expected_button_id:
            score += 5.0
            hard_signal_count += 1
            matched_signals.append("button_id")
        elif actual_button_id:
            return {"accepted": False, "score": -1.0, "reason": "button_id_mismatch"}

    expected_target_id = _normalize_target_id(
        _get_expected_param(target_row, "target_id", fallback_row_key=True)
    )
    if expected_target_id:
        actual_target_id = _normalize_target_id(str(params.get("target_id", "")).strip())
        if not actual_target_id:
            return {"accepted": False, "score": -1.0, "reason": "target_id_missing"}
        expected_target_tail = expected_target_id.split(":", 1)[-1].strip()
        actual_target_tail = actual_target_id.split(":", 1)[-1].strip()
        if (
            actual_target_id == expected_target_id
            or actual_target_tail == expected_target_tail
        ):
            score += 5.0
            hard_signal_count += 1
            matched_signals.append("target_id")
        else:
            return {"accepted": False, "score": -1.0, "reason": "target_id_mismatch"}

    detail_keys = [
        "content_id",
        "banner_id",
        "brand_id",
        "category_id",
        "filter_value",
    ]
    for key in detail_keys:
        expected_val = _get_expected_param(target_row, key, fallback_row_key=True)
        if not expected_val:
            continue
        actual_val = str(params.get(key, "")).strip()
        if not actual_val:
            continue
        if actual_val == expected_val:
            score += 3.0
            hard_signal_count += 1
            matched_signals.append(key)
        else:
            return {"accepted": False, "score": -1.0, "reason": f"{key}_mismatch"}

    row_text_hints = (
        [str(v).strip().lower() for v in target_row.get("text_hints", []) if str(v).strip()]
        if isinstance(target_row.get("text_hints", []), list)
        else []
    )
    row_text_hints.extend(_tokenize_text(target_row.get("description", "")))
    # 중복 제거 (순서 유지)
    dedup_hints: List[str] = []
    for hint in row_text_hints:
        if hint and hint not in dedup_hints:
            dedup_hints.append(hint)
    row_text_hints = dedup_hints[:20]

    payload_text = _build_payload_text(params, payload)
    text_match_count = 0
    if payload_text and row_text_hints:
        for hint in row_text_hints:
            if hint in payload_text:
                text_match_count += 1
    if text_match_count > 0:
        score += min(5.0, float(text_match_count) * 1.5)
        matched_signals.append("text_hints")

    # expected 파라미터가 거의 없는 row는 완화 매칭으로 붙이지 않는다.
    expected_signals = 0
    for key in [
        "page_id",
        "section_name",
        "button_id",
        "target_id",
        "content_id",
        "banner_id",
        "brand_id",
        "category_id",
        "filter_value",
    ]:
        if _get_expected_param(target_row, key, fallback_row_key=True):
            expected_signals += 1
    if expected_signals == 0:
        # 정의서 row에 구조화된 기대값이 없으면 description/text_hints 기반으로 2차 매칭 허용
        is_definition_validation = str(qa_mode or "").strip() == "정의서 검증"
        if allow_family_match:
            min_score = 1.0 if is_definition_validation else 0.9
            accepted = bool(score >= min_score)
            reason = "ok_relaxed_family" if accepted else "family_score_too_low"
        else:
            min_text = 3 if is_definition_validation else 2
            accepted = bool(text_match_count >= min_text)
            reason = "ok_relaxed_text" if accepted else "text_signal_too_low"
        return {
            "accepted": accepted,
            "score": score,
            "reason": reason,
            "matched_signals": matched_signals,
            "hard_signal_count": hard_signal_count,
            "text_match_count": text_match_count,
        }

    is_definition_validation = str(qa_mode or "").strip() == "정의서 검증"
    min_score = 6.0 if is_definition_validation else 4.0
    if event_family_match and is_definition_validation:
        min_score += 1.0
    accepted = bool(score >= min_score and hard_signal_count >= 1)
    reason = "ok_relaxed" if accepted else "score_too_low"
    return {
        "accepted": accepted,
        "score": score,
        "reason": reason,
        "matched_signals": matched_signals,
        "hard_signal_count": hard_signal_count,
    }


def _definition_third_match_score(
    target_row: Dict[str, object],
    payload: Dict[str, object],
    qa_mode: str = "",
) -> Dict[str, object]:
    if not isinstance(target_row, dict) or not isinstance(payload, dict):
        return {"accepted": False, "score": -1.0, "reason": "invalid_payload"}

    def _event_family(name: str) -> str:
        n = str(name or "").strip().lower()
        if not n:
            return ""
        if n.startswith("click_") or n in {"select_item"}:
            return "click"
        if n.startswith("impression_") or n in {"view_item_list", "page_view"}:
            return "impression"
        if n in {"add_to_wishlist", "remove_from_wishlist"}:
            return "wishlist"
        return ""

    expected_event_name = str(target_row.get("event_name", "")).strip()
    actual_event_name = _extract_payload_event_name(payload)
    if not expected_event_name or not actual_event_name:
        return {"accepted": False, "score": -1.0, "reason": "event_name_missing"}
    event_exact = actual_event_name == expected_event_name
    if not event_exact:
        return {"accepted": False, "score": -1.0, "reason": "event_name_mismatch"}

    params = payload.get("params", {})
    params = params if isinstance(params, dict) else {}
    expected_section = _get_expected_param(target_row, "section_name")
    actual_section = str(params.get("section_name", "")).strip()
    expected_page = _get_expected_param(target_row, "page_id")
    actual_page = str(params.get("page_id", "")).strip() or str(
        urlparse(str(payload.get("page_url", "")).strip()).path or ""
    ).strip()

    score = 0.0
    signals: List[str] = []
    score += 1.8
    signals.append("event_exact")

    if expected_page and actual_page:
        if _page_id_pattern_matches(expected_page, actual_page):
            score += 2.4
            signals.append("page_id")
        else:
            return {"accepted": False, "score": -1.0, "reason": "page_id_mismatch"}

    if expected_section:
        if not actual_section:
            return {"accepted": False, "score": -1.0, "reason": "section_name_missing"}
        if _section_name_matches(expected_section, actual_section):
            score += 3.0
            signals.append("section_exact")
        else:
            return {"accepted": False, "score": -1.0, "reason": "section_name_mismatch"}

    row_text_hints = (
        [str(v).strip().lower() for v in target_row.get("text_hints", []) if str(v).strip()]
        if isinstance(target_row.get("text_hints", []), list)
        else []
    )
    row_text_hints.extend(
        [t for t in re.split(r"[^0-9a-zA-Z가-힣_]+", str(target_row.get("description", "")).strip().lower()) if len(t) >= 2]
    )
    row_text_hints = list(dict.fromkeys([h for h in row_text_hints if h]))[:16]
    payload_text = " ".join(
        [
            str(params.get("button_name", "")).strip().lower(),
            str(params.get("content_name", "")).strip().lower(),
            str(params.get("extra_info", "")).strip().lower(),
            str(params.get("section_name", "")).strip().lower(),
            str(payload.get("page_url", "")).strip().lower(),
        ]
    )
    text_match_count = 0
    if payload_text and row_text_hints:
        for hint in row_text_hints:
            if hint in payload_text:
                text_match_count += 1
    if text_match_count > 0:
        score += min(2.4, float(text_match_count) * 0.8)
        signals.append("text_hints")

    is_definition_validation = str(qa_mode or "").strip() == "정의서 검증"
    min_score = 4.6 if is_definition_validation else 3.8
    accepted = bool(score >= min_score and (event_exact or text_match_count >= 2))
    return {
        "accepted": accepted,
        "score": score,
        "reason": "ok_third" if accepted else "score_too_low_third",
        "matched_signals": signals,
        "text_match_count": text_match_count,
    }


def _mark_definition_rows(session_id: str, hits: List[Dict[str, object]]) -> List[str]:
    sid = str(session_id or "").strip()
    if not sid or not hits:
        return []

    with _LOCK:
        session = _SESSIONS.get(sid)
        if not session:
            return []

        runtime_rows = _get_definition_runtime_rows(session.run_settings)
        if not runtime_rows:
            return []

        processed = _get_processed_definition_row_ids(session)
        remaining_rows = [
            row
            for row in runtime_rows
            if str(row.get("definition_row_id", "")).strip()
            and str(row.get("definition_row_id", "")).strip() not in processed
        ]

        if not remaining_rows:
            session.current_definition_row_id = ""
            return []

        used_hit_indexes = set()
        qa_mode = str(session.run_settings.get("qa_mode", "")).strip()

        for row in remaining_rows:
            row_id = str(row.get("definition_row_id", "")).strip()
            row_no = str(row.get("no", "")).strip()
            row_event_name = str(row.get("event_name", "")).strip()
            row_expected_section = _get_expected_param(row, "section_name")

            matched = False
            same_event_indexes: List[int] = []

            for idx, payload in enumerate(hits):
                if idx in used_hit_indexes:
                    continue

                params = payload.get("params", {})
                params = params if isinstance(params, dict) else {}
                actual_event_name = _extract_payload_event_name(payload)
                if actual_event_name == row_event_name:
                    same_event_indexes.append(idx)

                _debug_match_log(
                    sid,
                    "row_candidate_check",
                    {
                        "definition_row_id": row_id,
                        "definition_no": row_no,
                        "definition_event_name": row_event_name,
                        "expected_section_name": row_expected_section,
                        "actual_event_name": _extract_payload_event_name(payload),
                        "actual_section_name": str(
                            params.get("section_name", "")
                        ).strip(),
                        "actual_button_id": str(params.get("button_id", "")).strip(),
                        "page_url": str(payload.get("page_url", "")).strip(),
                    },
                )

                if _definition_hit_matches_row(row, payload):
                    session.matched_definition_rows.add(row_id)
                    used_hit_indexes.add(idx)
                    matched = True

                    _debug_match_log(
                        sid,
                        "row_matched",
                        {
                            "definition_row_id": row_id,
                            "definition_no": row_no,
                            "definition_event_name": row_event_name,
                            "expected_section_name": row_expected_section,
                            "actual_event_name": _extract_payload_event_name(payload),
                            "actual_section_name": str(
                                params.get("section_name", "")
                            ).strip(),
                            "actual_button_id": str(
                                params.get("button_id", "")
                            ).strip(),
                            "page_url": str(payload.get("page_url", "")).strip(),
                        },
                    )
                    break

            # 2차 매칭: strict 실패 시 definition signal 기반 점수 매칭
            if not matched:
                if not same_event_indexes:
                    _debug_match_log(
                        sid,
                        "row_relaxed_skipped_no_event_candidates",
                        {
                            "definition_row_id": row_id,
                            "definition_no": row_no,
                            "definition_event_name": row_event_name,
                            "qa_mode": qa_mode,
                        },
                    )

                if same_event_indexes:
                    evaluated: List[Tuple[float, int, Dict[str, object], Dict[str, object]]] = []
                    accepted_candidates: List[
                        Tuple[float, int, Dict[str, object], Dict[str, object]]
                    ] = []
                    reject_reason_counts: Dict[str, int] = {}
                    allow_family_match = False
                    for idx in same_event_indexes:
                        payload = hits[idx]
                        score_info = _definition_relaxed_match_score(
                            row,
                            payload,
                            qa_mode=qa_mode,
                            allow_family_match=allow_family_match,
                        )
                        score_value = float(score_info.get("score", 0.0) or 0.0)
                        evaluated.append((score_value, idx, payload, score_info))
                        if bool(score_info.get("accepted", False)):
                            accepted_candidates.append((score_value, idx, payload, score_info))
                        else:
                            reason = str(score_info.get("reason", "rejected")).strip() or "rejected"
                            reject_reason_counts[reason] = int(
                                reject_reason_counts.get(reason, 0)
                            ) + 1

                    evaluated.sort(key=lambda x: x[0], reverse=True)
                    top_any_score = evaluated[0][0] if evaluated else -1.0
                    top_any_reason = (
                        str(evaluated[0][3].get("reason", "")).strip() if evaluated else ""
                    )
                    _debug_match_log(
                        sid,
                        "row_relaxed_candidates_evaluated",
                        {
                            "definition_row_id": row_id,
                            "definition_no": row_no,
                            "definition_event_name": row_event_name,
                            "qa_mode": qa_mode,
                            "candidate_count": len(same_event_indexes),
                            "accepted_candidate_count": len(accepted_candidates),
                            "top_score_any": top_any_score,
                            "top_reason_any": top_any_reason,
                            "reject_reason_counts": reject_reason_counts,
                            "allow_family_match": allow_family_match,
                        },
                    )

                    accepted_candidates.sort(key=lambda x: x[0], reverse=True)
                    if accepted_candidates:
                        top_score = accepted_candidates[0][0]
                        top_items = [
                            item for item in accepted_candidates if item[0] == top_score
                        ]
                        # 동점이면 오탐 방지를 위해 보류
                        if len(top_items) == 1:
                            chosen_score, chosen_idx, chosen_payload, chosen_info = top_items[0]
                            session.matched_definition_rows.add(row_id)
                            used_hit_indexes.add(chosen_idx)
                            matched = True

                            chosen_params = chosen_payload.get("params", {})
                            chosen_params = (
                                chosen_params if isinstance(chosen_params, dict) else {}
                            )
                            _debug_match_log(
                                sid,
                                "row_matched_relaxed",
                                {
                                    "definition_row_id": row_id,
                                    "definition_no": row_no,
                                    "definition_event_name": row_event_name,
                                    "qa_mode": qa_mode,
                                    "relaxed_score": chosen_score,
                                    "matched_signals": list(
                                        chosen_info.get("matched_signals", []) or []
                                    ),
                                    "actual_event_name": _extract_payload_event_name(
                                        chosen_payload
                                    ),
                                    "actual_section_name": str(
                                        chosen_params.get("section_name", "")
                                    ).strip(),
                                    "actual_button_id": str(
                                        chosen_params.get("button_id", "")
                                    ).strip(),
                                    "page_url": str(
                                        chosen_payload.get("page_url", "")
                                    ).strip(),
                                },
                            )
                        else:
                            _debug_match_log(
                                sid,
                                "row_relaxed_tie_rejected",
                                {
                                    "definition_row_id": row_id,
                                    "definition_no": row_no,
                                    "definition_event_name": row_event_name,
                                    "qa_mode": qa_mode,
                                    "top_score": top_score,
                                    "tie_count": len(top_items),
                                },
                            )

            # 3차 매칭: strict/relaxed 실패 시 확신도 기반 fallback 매칭
            if not matched:
                third_candidate_indexes = list(same_event_indexes)
                if third_candidate_indexes:
                    third_evaluated: List[Tuple[float, int, Dict[str, object], Dict[str, object]]] = []
                    for idx in third_candidate_indexes:
                        payload = hits[idx]
                        info = _definition_third_match_score(row, payload, qa_mode=qa_mode)
                        third_evaluated.append((float(info.get("score", 0.0) or 0.0), idx, payload, info))
                    third_evaluated.sort(key=lambda x: x[0], reverse=True)
                    accepted_third = [item for item in third_evaluated if bool(item[3].get("accepted", False))]
                    _debug_match_log(
                        sid,
                        "row_third_candidates_evaluated",
                        {
                            "definition_row_id": row_id,
                            "definition_no": row_no,
                            "definition_event_name": row_event_name,
                            "qa_mode": qa_mode,
                            "candidate_count": len(third_candidate_indexes),
                            "accepted_candidate_count": len(accepted_third),
                            "top_score_any": float(third_evaluated[0][0]) if third_evaluated else -1.0,
                            "top_reason_any": str(third_evaluated[0][3].get("reason", "")).strip() if third_evaluated else "",
                        },
                    )
                    if accepted_third:
                        top_score = accepted_third[0][0]
                        top_items = [item for item in accepted_third if item[0] == top_score]
                        if len(top_items) == 1:
                            chosen_score, chosen_idx, chosen_payload, chosen_info = top_items[0]
                            session.matched_definition_rows.add(row_id)
                            used_hit_indexes.add(chosen_idx)
                            matched = True
                            chosen_params = chosen_payload.get("params", {})
                            chosen_params = chosen_params if isinstance(chosen_params, dict) else {}
                            _debug_match_log(
                                sid,
                                "row_matched_third",
                                {
                                    "definition_row_id": row_id,
                                    "definition_no": row_no,
                                    "definition_event_name": row_event_name,
                                    "qa_mode": qa_mode,
                                    "third_score": chosen_score,
                                    "matched_signals": list(chosen_info.get("matched_signals", []) or []),
                                    "actual_event_name": _extract_payload_event_name(
                                        chosen_payload
                                    ),
                                    "actual_section_name": str(chosen_params.get("section_name", "")).strip(),
                                    "actual_button_id": str(chosen_params.get("button_id", "")).strip(),
                                    "page_url": str(chosen_payload.get("page_url", "")).strip(),
                                },
                            )
                        else:
                            _debug_match_log(
                                sid,
                                "row_third_tie_rejected",
                                {
                                    "definition_row_id": row_id,
                                    "definition_no": row_no,
                                    "definition_event_name": row_event_name,
                                    "qa_mode": qa_mode,
                                    "top_score": top_score,
                                    "tie_count": len(top_items),
                                },
                            )

            if not matched:
                session.current_definition_row_id = row_id

                _debug_match_log(
                    sid,
                    "row_not_matched_stop_here",
                    {
                        "definition_row_id": row_id,
                        "definition_no": row_no,
                        "definition_event_name": row_event_name,
                        "expected_section_name": row_expected_section,
                    },
                )
                return sorted(session.matched_definition_rows)

        remaining_after = [
            str(row.get("definition_row_id", "")).strip()
            for row in runtime_rows
            if str(row.get("definition_row_id", "")).strip()
            not in _get_processed_definition_row_ids(session)
        ]
        session.current_definition_row_id = (
            remaining_after[0] if remaining_after else ""
        )

        return sorted(session.matched_definition_rows)


def _skip_definition_row(session_id: str, row: Dict[str, object]) -> Dict[str, str]:
    sid = str(session_id or "").strip()
    row_id = (
        str(row.get("definition_row_id", "")).strip() if isinstance(row, dict) else ""
    )
    result = {"skipped_row_id": row_id, "next_row_id": "", "next_row_no": ""}
    if not sid or not row_id:
        return result
    with _LOCK:
        session = _SESSIONS.get(sid)
        if not session:
            return result
        session.skipped_definition_rows.add(row_id)
        runtime_rows = _get_definition_runtime_rows(session.run_settings)
        processed = _get_processed_definition_row_ids(session)
        next_row: Dict[str, object] = {}
        for candidate in runtime_rows:
            candidate_id = str(candidate.get("definition_row_id", "")).strip()
            if candidate_id and candidate_id not in processed:
                next_row = candidate
                break
        session.current_definition_row_id = str(
            next_row.get("definition_row_id", "")
        ).strip()
        result["next_row_id"] = session.current_definition_row_id
        result["next_row_no"] = str(next_row.get("no", "")).strip()
    return result


def _annotate_definition_scope(
    session_id: str, hits: List[Dict[str, object]]
) -> List[Dict[str, object]]:
    sid = str(session_id or "").strip()
    if not hits:
        return hits
    with _LOCK:
        session = _SESSIONS.get(sid)
        target_names = set(
            _get_definition_event_targets(session.run_settings if session else None)
        )
    for payload in hits:
        event_name = _extract_payload_event_name(payload)
        if target_names:
            payload["definition_in_scope"] = bool(
                event_name and event_name in target_names
            )
        else:
            payload["definition_in_scope"] = None
    return hits


def _ingest_ga_hit_payloads(
    session_id: str,
    hits: List[Dict[str, object]],
    output_path: Path,
    db_path: Path,
) -> int:
    if not hits:
        return 0
    with _LOCK:
        session = _SESSIONS.get(session_id)
        source_mode = str(
            (
                session.run_settings.get("analytics_source_mode", "both")
                if session and isinstance(session.run_settings, dict)
                else "both"
            )
            or "both"
        ).strip().lower()
    if source_mode not in {"both", "ga4", "amplitude"}:
        source_mode = "both"
    if source_mode != "both":
        allowed_source = "ga_hit" if source_mode == "ga4" else "amplitude_hit"
        hits = [
            h
            for h in hits
            if str(h.get("source", "")).strip() == allowed_source
        ]
        if not hits:
            return 0
    for payload in hits:
        _append_event_with_db(output_path, payload, db_path)
    _mark_definition_hits(session_id, hits)
    _mark_definition_rows(session_id, hits)
    with _LOCK:
        current = _SESSIONS.get(session_id)
        if current:
            current.captured_events += len(hits)
            for payload in reversed(hits):
                event_name = _extract_payload_event_name(payload)
                if event_name:
                    current.last_ingested_hit_event = event_name
                    break
            should_sync = current.captured_events % 10 == 0
        else:
            should_sync = False
    if should_sync:
        _sync_session_db(session_id)
    return len(hits)


def _get_definition_progress(
    session_id: str, run_settings: Optional[Dict[str, object]] = None
) -> tuple[List[str], List[str], bool]:
    sid = str(session_id or "").strip()
    runtime_rows = _get_definition_runtime_rows(run_settings)
    if not runtime_rows:
        session = _get_session(sid)
        runtime_rows = _get_definition_runtime_rows(
            session.run_settings if session else None
        )
    if not runtime_rows:
        return [], [], False
    target_ids = [
        str(row.get("definition_row_id", "")).strip()
        for row in runtime_rows
        if str(row.get("definition_row_id", "")).strip()
    ]
    matched_ids: List[str] = []
    with _LOCK:
        session = _SESSIONS.get(sid)
        if session:
            matched_ids = sorted(session.matched_definition_rows)
    matched = [row_id for row_id in matched_ids if row_id in set(target_ids)]
    completed = bool(target_ids and set(target_ids).issubset(set(matched)))
    return target_ids, matched, completed


def _launch_browser(playwright, run_settings: Optional[Dict[str, object]] = None):
    last_err: Exception | None = None
    attempt_errors: List[str] = []
    browser_name = "chrome"
    if isinstance(playwright, tuple):
        playwright, browser_name = playwright
    settings = _normalize_run_settings(run_settings) if run_settings else {}
    force_headed = bool(settings.get("force_headed", False))
    fixed_window_args: List[str] = []
    if settings and not settings.get("mobile_mode", False):
        width = int(settings.get("viewport_width", 1440))
        height = int(settings.get("viewport_height", 900))
        fixed_window_args = [
            f"--window-size={width},{height}",
            "--force-device-scale-factor=1",
            "--high-dpi-support=1",
        ]
    common_args = [
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-search-engine-choice-screen",
        "--disable-features=ChromeWhatsNewUI,WelcomePage,SigninIntercept",
    ] + fixed_window_args
    # 기본은 headed -> headless 순으로 폴백, force_headed면 headed만 시도
    launch_attempts = [
        (
            {"headless": False, "channel": "chrome", "args": common_args}
            if browser_name == "chrome"
            else {"headless": False, "args": common_args}
        ),
        {"headless": False, "args": common_args},
        (
            {"headless": True, "channel": "chrome", "args": common_args}
            if browser_name == "chrome"
            else {"headless": True, "args": common_args}
        ),
        {"headless": True, "args": common_args},
        # 일부 EC2/컨테이너 환경에서 sandbox 관련 실패를 우회하기 위한 최후 폴백
        (
            {
                "headless": True,
                "channel": "chrome",
                "args": common_args + ["--no-sandbox", "--disable-dev-shm-usage"],
            }
            if browser_name == "chrome"
            else {
                "headless": True,
                "args": common_args + ["--no-sandbox", "--disable-dev-shm-usage"],
            }
        ),
        {
            "headless": True,
            "args": common_args + ["--no-sandbox", "--disable-dev-shm-usage"],
        },
    ]
    if force_headed:
        launch_attempts = [launch_attempts[0], launch_attempts[1]]
    # EC2(무GUI) 환경에서도 동작하도록 headed -> headless 순으로 폴백한다.
    for launch_kwargs in launch_attempts:
        if platform.system().lower() != "linux" and any(
            arg in launch_kwargs.get("args", [])
            for arg in ["--no-sandbox", "--disable-dev-shm-usage"]
        ):
            pass
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


def _build_context_kwargs(
    playwright, run_settings: Dict[str, object]
) -> Dict[str, object]:
    settings = _normalize_run_settings(run_settings)
    if settings.get("mobile_mode", False):
        device_profile = playwright.devices.get(
            str(settings.get("mobile_device", "iPhone 13"))
        )
        if isinstance(device_profile, dict) and device_profile:
            return dict(device_profile)
    width = int(settings.get("viewport_width", 1440))
    height = int(settings.get("viewport_height", 900))
    return {
        # Disable viewport emulation so the window size stays fixed.
        "viewport": None,
        "screen": {"width": width, "height": height},
        "device_scale_factor": 1,
        "is_mobile": False,
    }


def _build_init_script(
    session_id: str,
    definition_event_names: List[str] | None = None,
    restrict_prefix: str = "",
    manual_profile_options: List[str] | None = None,
    manual_profile_selected: str = "default",
    manual_recording_catalog: List[Dict[str, object]] | None = None,
    manual_recording_selected: str = "",
    manual_recording_name: str = "",
    manual_recording_enabled: bool = False,
    manual_flow_replay_enabled: bool = True,
    auto_crawl_start_mode: str = "manual",
) -> str:
    sid = json.dumps(session_id)
    definition_events = [
        str(v).strip() for v in (definition_event_names or []) if str(v).strip()
    ]
    def_list = json.dumps(definition_events)
    prefix = json.dumps(str(restrict_prefix or "").strip())
    profile_options = [
        str(v).strip() for v in (manual_profile_options or []) if str(v).strip()
    ] or ["default"]
    recording_catalog = _normalize_manual_recording_catalog(manual_recording_catalog or [])
    recording_options_html = "".join(
        (
            f'<option value="{html.escape(str(item.get("id", "")))}" '
            f'{"selected" if str(item.get("id", "")) == str(manual_recording_selected or "").strip() else ""}>'
            f'{html.escape(str(item.get("name", "")))} ({int(item.get("step_count", 0))} steps)</option>'
        )
        for item in recording_catalog
        if str(item.get("id", "")).strip()
    )
    profile_options_js = json.dumps(profile_options)
    recording_catalog_js = json.dumps(recording_catalog, ensure_ascii=False)
    recording_selected_js = json.dumps(str(manual_recording_selected or "").strip())
    recording_name_js = json.dumps(str(manual_recording_name or "").strip(), ensure_ascii=False)
    recording_options_html_js = json.dumps(recording_options_html, ensure_ascii=False)
    profile_selected_js = json.dumps(str(manual_profile_selected or "default").strip() or "default")
    recording_enabled_js = "true" if bool(manual_recording_enabled) else "false"
    replay_enabled_js = "true" if bool(manual_flow_replay_enabled) else "false"
    start_mode_js = json.dumps(str(auto_crawl_start_mode or "manual").strip().lower() or "manual")
    return f"""
(() => {{
  const SID = {sid};
  const DEFINITION_EVENTS = {def_list};
  const HAS_DEFINITION = Array.isArray(DEFINITION_EVENTS) && DEFINITION_EVENTS.length > 0;
  const STATE_SCOPE = String(SID || "default");
  const _stateKey = (name) => `qa_rt_${{STATE_SCOPE}}_${{String(name || "").trim()}}`;
  const readState = (name, fallback = "") => {{
    const key = _stateKey(name);
    try {{
      const v = sessionStorage.getItem(key);
      if (v !== null && v !== undefined) return v;
    }} catch (e) {{}}
    try {{
      const v = localStorage.getItem(key);
      if (v !== null && v !== undefined) return v;
    }} catch (e) {{}}
    return fallback;
  }};
  const writeState = (name, value) => {{
    const key = _stateKey(name);
    const v = String(value ?? "");
    try {{ sessionStorage.setItem(key, v); }} catch (e) {{}}
    try {{ localStorage.setItem(key, v); }} catch (e) {{}}
  }};
  const readBoolState = (name, fallback = false) => {{
    const raw = String(readState(name, fallback ? "1" : "0")).trim().toLowerCase();
    if (["1", "true", "y", "yes", "on"].includes(raw)) return true;
    if (["0", "false", "n", "no", "off"].includes(raw)) return false;
    return Boolean(fallback);
  }};
  const MANUAL_PROFILE_OPTIONS = {profile_options_js};
  const MANUAL_PROFILE_SELECTED = {profile_selected_js};
  const MANUAL_RECORDING_CATALOG = {recording_catalog_js};
  let MANUAL_RECORDING_SELECTED = String(readState("manual_recording_selected", {recording_selected_js}) || "").trim();
  let MANUAL_RECORDING_NAME = String(readState("manual_recording_name", {recording_name_js}) || "").trim();
  const MANUAL_RECORDING_OPTIONS_HTML = {recording_options_html_js};
  let MANUAL_RECORDING_ENABLED = readBoolState("manual_recording_enabled", {recording_enabled_js});
  let MANUAL_REPLAY_ENABLED = readBoolState("manual_replay_enabled", {replay_enabled_js});
  const AUTO_CRAWL_START_MODE = {start_mode_js};
  let CURRENT_PHASE = String(
    readState(
      "phase",
      (AUTO_CRAWL_START_MODE === "manual") ? "auto_crawl_paused" : "running"
    )
  ).trim() || ((AUTO_CRAWL_START_MODE === "manual") ? "auto_crawl_paused" : "running");
  const MODE_KEY = "qa_debug_mode";
  const SID_KEY = "qa_debug_session_id";
  const RESTRICT_PREFIX = {prefix};
  const REMOTE_MIN_KEY = "qa_remote_control_minimized";
  window.__qaReplayNavBlock = false;
  window.__qaReplayNavBypass = false;
  window.__qaReplayNavReleaseAt = 0;
  const buildUiStructureSignature = () => {{
    try {{
      const sectionNames = Array.from(document.querySelectorAll("[data-section-name]"))
        .map((el) => String(el.getAttribute("data-section-name") || "").trim())
        .filter(Boolean)
        .slice(0, 40);
      const uniqSections = Array.from(new Set(sectionNames)).sort();
      const tabCount =
        document.querySelectorAll("[role='tab'], .tab, [class*='tab-'], [class*='tabs']").length;
      const filterCount =
        document.querySelectorAll("[data-filter], [class*='filter'], [aria-label*='필터'], [aria-label*='filter']").length;
      const moreCount = Array.from(document.querySelectorAll("button, a, [role='button']"))
        .filter((el) => {{
          const txt = String((el.innerText || el.textContent || "")).replace(/\\s+/g, " ").trim().toLowerCase();
          return txt.includes("더보기") || txt.includes("more");
        }}).length;
      const repeatMax = Math.max(
        0,
        document.querySelectorAll("ul li").length,
        document.querySelectorAll("[class*='list'] > *").length,
        document.querySelectorAll("[class*='grid'] > *").length,
        document.querySelectorAll("[data-section-name] li").length
      );
      const dataQaCount = document.querySelectorAll("[data-qa]").length;
      const dataButtonCount = document.querySelectorAll("[data-button-id]").length;
      const dataSectionCount = document.querySelectorAll("[data-section-name]").length;
      const path = String(location.pathname || "").trim();
      return [
        `path=${{path}}`,
        `sections=${{uniqSections.join("|")}}`,
        `data_qa=${{dataQaCount}}`,
        `data_button_id=${{dataButtonCount}}`,
        `data_section_name=${{dataSectionCount}}`,
        `tabs=${{tabCount}}`,
        `filters=${{filterCount}}`,
        `more=${{moreCount}}`,
        `repeat_max=${{repeatMax}}`,
      ].join(";");
    }} catch (e) {{
      return "";
    }}
  }};
  const shortHash = (text) => {{
    const raw = String(text || "");
    let hash = 2166136261;
    for (let i = 0; i < raw.length; i += 1) {{
      hash ^= raw.charCodeAt(i);
      hash += (hash << 1) + (hash << 4) + (hash << 7) + (hash << 8) + (hash << 24);
    }}
    return (`${{(hash >>> 0).toString(16)}}00000000`).slice(0, 8);
  }};
  window.__qaUiSignatureText = "";
  window.__qaUiSignatureHash = "";
  const refreshUiSignature = () => {{
    try {{
      const txt = buildUiStructureSignature();
      window.__qaUiSignatureText = txt;
      window.__qaUiSignatureHash = shortHash(txt);
    }} catch (e) {{}}
  }};
  try {{
    refreshUiSignature();
    window.addEventListener("load", () => setTimeout(refreshUiSignature, 250), {{ once: true }});
    document.addEventListener("DOMContentLoaded", () => setTimeout(refreshUiSignature, 100), {{ once: true }});
  }} catch (e) {{}}

  try {{
    sessionStorage.setItem(MODE_KEY, "1");
    if (SID) {{
      sessionStorage.setItem(SID_KEY, SID);
    }}
  }} catch (e) {{}}

  try {{
    document.documentElement.style.scrollbarGutter = "stable";
    document.documentElement.style.overflowY = "scroll";
    if (document.body) {{
      document.body.style.overflowY = "scroll";
    }}
    document.documentElement.style.setProperty("scrollbar-gutter", "stable");
    document.documentElement.style.setProperty("scrollbar-gutter", "stable both-edges");
  }} catch (e) {{}}

  try {{
    if (window.visualViewport && typeof window.visualViewport.addEventListener === "function") {{
      window.visualViewport.addEventListener("resize", () => {{
        try {{
          if (document.activeElement && typeof document.activeElement.blur === "function") {{
            document.activeElement.blur();
          }}
        }} catch (e) {{}}
      }}, true);
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

  const isInScope = (href) => {{
    if (!RESTRICT_PREFIX) return true;
    if (!href) return true;
    const raw = String(href || "").trim();
    if (!raw || raw.startsWith("#") || raw.toLowerCase().startsWith("javascript:")) return true;
    try {{
      const abs = new URL(raw, location.href);
      const path = abs.pathname.replace(/\\/+$/, "") || "/";
      const host = (abs.host || abs.hostname || "").toLowerCase();
      const key = `${{host}}${{path}}`;
      return key.startsWith(RESTRICT_PREFIX);
    }} catch (e) {{
      return true;
    }}
  }};
  const isReplayBlockedNav = (urlLike) => {{
    try {{
      if (window.__qaReplayNavBypass) return false;
      const active = Boolean(window.__qaReplayNavBlock) || (Number(window.__qaReplayNavReleaseAt || 0) > Date.now());
      if (!active) return false;
      const raw = String(urlLike || "").trim();
      if (!raw || raw.startsWith("#") || raw.toLowerCase().startsWith("javascript:")) return false;
      return true;
    }} catch (e) {{
      return false;
    }}
  }};

  try {{
    const originalOpen = window.open;
    window.open = function (url, ...args) {{
      if (isReplayBlockedNav(url)) return null;
      if (!isInScope(url)) return null;
      return originalOpen ? originalOpen.call(this, url, ...args) : null;
    }};
  }} catch (e) {{}}

  try {{
    const originalAssign = window.location.assign.bind(window.location);
    window.location.assign = function (url) {{
      if (isReplayBlockedNav(url)) return;
      if (!isInScope(url)) return;
      return originalAssign(url);
    }};
  }} catch (e) {{}}

  try {{
    const originalReplace = window.location.replace.bind(window.location);
    window.location.replace = function (url) {{
      if (isReplayBlockedNav(url)) return;
      if (!isInScope(url)) return;
      return originalReplace(url);
    }};
  }} catch (e) {{}}

  try {{
    const originalPushState = history.pushState.bind(history);
    history.pushState = function (state, title, url) {{
      if (isReplayBlockedNav(url)) return;
      if (url && !isInScope(url)) return;
      return originalPushState(state, title, url);
    }};
    const originalReplaceState = history.replaceState.bind(history);
    history.replaceState = function (state, title, url) {{
      if (isReplayBlockedNav(url)) return;
      if (url && !isInScope(url)) return;
      return originalReplaceState(state, title, url);
    }};
  }} catch (e) {{}}

  try {{
    const originalAnchorClick = HTMLAnchorElement.prototype.click;
    HTMLAnchorElement.prototype.click = function (...args) {{
      const href = this && this.getAttribute ? this.getAttribute("href") : "";
      if (isReplayBlockedNav(href)) return;
      if (href && !isInScope(href)) return;
      return originalAnchorClick.apply(this, args);
    }};
  }} catch (e) {{}}

  try {{
    const originalFormSubmit = HTMLFormElement.prototype.submit;
    HTMLFormElement.prototype.submit = function (...args) {{
      const action = this && this.getAttribute ? this.getAttribute("action") : "";
      if (isReplayBlockedNav(action)) return;
      if (action && !isInScope(action)) return;
      return originalFormSubmit.apply(this, args);
    }};
  }} catch (e) {{}}

  try {{
    document.addEventListener("click", (ev) => {{
      const el = ev.target && ev.target.closest ? ev.target.closest("a[href]") : null;
      if (!el) return;
      const href = el.getAttribute("href") || "";
      if (isReplayBlockedNav(href)) {{
        ev.preventDefault();
        ev.stopPropagation();
        return;
      }}
      if (!isInScope(href)) {{
        ev.preventDefault();
        ev.stopPropagation();
      }}
    }}, true);
  }} catch (e) {{}}

  const toShortSelector = (el) => {{
    try {{
      if (!el || !el.tagName) return "";
      const tag = String(el.tagName || "").toLowerCase();
      const id = String(el.id || "").trim();
      if (id) return `${{tag}}#${{id}}`;
      const cls = String(el.className || "").trim().split(/\\s+/).filter(Boolean).slice(0, 2);
      const clsText = cls.length ? "." + cls.join(".") : "";
      return `${{tag}}${{clsText}}`;
    }} catch (e) {{
      return "";
    }}
  }};

  const resolveClickTarget = (node) => {{
    try {{
      if (!node || !node.closest) return null;
      const selectors = [
        "button",
        "a[href]",
        "[role='button']",
        "input[type='button']",
        "input[type='submit']",
        "input[type='radio']",
        "input[type='checkbox']",
        "[onclick]",
        "[data-button-id]",
        "[data-qa]",
        "[tabindex]"
      ];
      const raw = node.closest(selectors.join(","));
      if (!raw || !raw.getBoundingClientRect) return raw;
      const rect = raw.getBoundingClientRect();
      const vw = Math.max(1, window.innerWidth || 1);
      const vh = Math.max(1, window.innerHeight || 1);
      const tooBig = rect.width >= vw * 0.94 || rect.height >= vh * 0.82;
      if (!tooBig) return raw;
      // 너무 큰 래퍼를 잡았으면 내부에서 가장 작은 interactive 후보를 선택
      const candidates = Array.from(raw.querySelectorAll(selectors.join(",")));
      let refined = null;
      let bestArea = Number.POSITIVE_INFINITY;
      for (const cand of candidates) {{
        if (!cand || !cand.getBoundingClientRect) continue;
        const cRect = cand.getBoundingClientRect();
        if (cRect.width < 8 || cRect.height < 8) continue;
        const area = Number(cRect.width || 0) * Number(cRect.height || 0);
        if (area <= 0) continue;
        if (area < bestArea) {{
          refined = cand;
          bestArea = area;
        }}
      }}
      return refined || raw;
    }} catch (e) {{
      return null;
    }}
  }};

  try {{
    document.addEventListener("click", (ev) => {{
      try {{
        const el = resolveClickTarget(ev.target);
        if (!el) return;
        const rect = el.getBoundingClientRect();
        const sectionHost = el.closest("[data-section-name]");
        window.__qaLastInteractMeta = {{
          ts: Date.now(),
          selector: toShortSelector(el),
          section_name: String(el.getAttribute("data-section-name") || (sectionHost ? sectionHost.getAttribute("data-section-name") : "") || "").trim(),
          button_id: String(el.getAttribute("data-button-id") || "").trim(),
          bbox: {{
            x: Math.max(0, Math.round(rect.left)),
            y: Math.max(0, Math.round(rect.top)),
            width: Math.max(0, Math.round(rect.width)),
            height: Math.max(0, Math.round(rect.height)),
          }}
        }};
      }} catch (e) {{}}
    }}, true);
  }} catch (e) {{}}

  try {{
    document.addEventListener("click", (ev) => {{
      const runtime = window.__qaRuntimePanelState || {{}};
      const phase = String(runtime.phase || "").trim();
      if (phase !== "auto_crawl_paused") return;
      const el = resolveClickTarget(ev.target);
      if (!el) return;
      const targetId = String(
        el.getAttribute("data-button-id")
        || el.getAttribute("data-qa")
        || el.getAttribute("id")
        || ""
      ).trim();
      const text = String((el.innerText || el.textContent || "")).replace(/\\s+/g, " ").trim().slice(0, 120);
      emit("manual_flow", "manual_flow_step", {{
        current_definition_row_id: String(runtime.row_id || "").trim(),
        current_definition_row_no: String(runtime.row_no || "").trim(),
        current_definition_event_name: String(runtime.event_name || "").trim(),
        current_definition_section_name: String(runtime.section_name || "").trim(),
        current_definition_canonical_key: String(runtime.canonical_key || "").trim(),
        current_definition_event_type: String(runtime.event_type || "").trim(),
        manual_profile: String(window.__qaManualProfileSelected || MANUAL_PROFILE_SELECTED || "default"),
        selector: toShortSelector(el),
        target_id: targetId,
        text,
        wait_ms: 600,
        ui_signature_text: String(window.__qaUiSignatureText || ""),
        ui_signature_hash: String(window.__qaUiSignatureHash || ""),
      }});
    }}, true);
  }} catch (e) {{}}

  const attachSession = (obj) => {{
    if (!obj || typeof obj !== "object" || Array.isArray(obj)) return obj;
    if (SID && !obj[SID_KEY]) {{
      obj[SID_KEY] = SID;
    }}
    return obj;
  }};

  const normalizeTransportBody = (body) => {{
    try {{
      if (typeof body === "string") return body;
      if (!body) return "";
      if (typeof URLSearchParams !== "undefined" && body instanceof URLSearchParams) {{
        return body.toString();
      }}
      if (typeof FormData !== "undefined" && body instanceof FormData) {{
        const pairs = [];
        for (const [k, v] of body.entries()) {{
          if (typeof v === "string") {{
            pairs.push(`${{encodeURIComponent(String(k || ""))}}=${{encodeURIComponent(v)}}`);
          }}
        }}
        return pairs.join("&");
      }}
      if (typeof body === "object") {{
        return JSON.stringify(body);
      }}
      return String(body || "");
    }} catch (e) {{
      return "";
    }}
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

  const ensureStatusPanel = () => null;

  const ensureDimLayer = () => {{
    let layer = document.getElementById("__qaDimLayer");
    if (layer) return layer;
    layer = document.createElement("div");
    layer.id = "__qaDimLayer";
    layer.style.position = "fixed";
    layer.style.inset = "0";
    layer.style.zIndex = "2147483645";
    layer.style.pointerEvents = "none";
    layer.style.opacity = "0";
    layer.style.background = "rgba(2,6,23,0.18)";
    layer.style.transition = "opacity .12s ease";
    document.documentElement.appendChild(layer);
    return layer;
  }};

  window.__qaApplyHitBorder = () => {{ try {{}} catch (e) {{}} }};
  window.__qaClearHitBorder = () => {{ try {{}} catch (e) {{}} }};
  window.__qaShowDim = () => {{
    try {{
      const layer = ensureDimLayer();
      layer.style.opacity = "1";
    }} catch (e) {{}}
  }};
  window.__qaHideDim = () => {{
    try {{
      const layer = ensureDimLayer();
      layer.style.opacity = "0";
    }} catch (e) {{}}
  }};

  const ensureRemoteControl = () => {{
    let remote = document.getElementById("__qaRemoteControl");
    if (remote) return remote;
    remote = document.createElement("div");
    remote.id = "__qaRemoteControl";
    remote.style.position = "fixed";
    remote.style.right = "12px";
    remote.style.bottom = "12px";
    remote.style.zIndex = "2147483647";
    remote.style.width = "min(206px, calc(100vw - 24px))";
    remote.style.padding = "8px 9px";
    remote.style.borderRadius = "10px";
    remote.style.background = "rgba(15,23,42,.92)";
    remote.style.color = "#ffffff";
    remote.style.border = "1px solid rgba(148,163,184,.35)";
    remote.style.boxShadow = "0 8px 20px rgba(0,0,0,.28)";
    remote.style.font = "600 11px/1.3 -apple-system, BlinkMacSystemFont, Segoe UI, sans-serif";
    remote.style.pointerEvents = "auto";
    const profileOptionsHtml = MANUAL_PROFILE_OPTIONS.map((v) => {{
      const selected = String(v) === String(MANUAL_PROFILE_SELECTED) ? "selected" : "";
      return `<option value="${{String(v)}}" ${{selected}}>${{String(v)}}</option>`;
    }}).join("");
    remote.innerHTML = `
      <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:5px;">
        <div style="font-weight:800;font-size:11px;">Mode</div>
        <button id="__qaPanelMinimize" style="border:0;border-radius:8px;padding:2px 8px;background:#1f2937;color:#cbd5e1;font-weight:800;cursor:pointer;line-height:1;">-</button>
      </div>
      <div id="__qaRemoteStatus" style="color:#cbd5f5;margin-bottom:6px;">status: unknown</div>
      <div style="display:flex;gap:5px;margin-bottom:6px;">
        <button id="__qaModeAuto" style="flex:1;border:0;border-radius:8px;padding:6px 6px;background:#2dd4bf;color:#0f172a;font-weight:700;cursor:pointer;">자동</button>
        <button id="__qaModeManual" style="flex:1;border:0;border-radius:8px;padding:6px 6px;background:#334155;color:#e2e8f0;font-weight:700;cursor:pointer;">수동</button>
      </div>
      <div style="font-size:10px;color:#94a3b8;margin-bottom:3px;">수동 프로필</div>
      <select id="__qaManualProfile" style="width:100%;border:1px solid rgba(148,163,184,.35);border-radius:7px;padding:5px 6px;margin-bottom:6px;background:#0f172a;color:#e2e8f0;">
        ${{profileOptionsHtml}}
      </select>
      <div style="font-size:10px;color:#94a3b8;margin-bottom:3px;">녹화본 선택</div>
      <select id="__qaRecordingSelect" style="width:100%;border:1px solid rgba(148,163,184,.35);border-radius:7px;padding:5px 6px;margin-bottom:6px;background:#0f172a;color:#e2e8f0;">
        ${{MANUAL_RECORDING_OPTIONS_HTML || '<option value=\"\">(없음)</option>'}}
      </select>
      <div style="display:flex;gap:5px;margin-bottom:6px;">
        <button id="__qaRecordingDelete" style="flex:1;border:0;border-radius:8px;padding:6px 4px;background:#7f1d1d;color:#fee2e2;font-weight:700;cursor:pointer;">삭제</button>
        <button id="__qaRecordingRun" style="flex:3;border:0;border-radius:8px;padding:6px 6px;background:#2563eb;color:#e2e8f0;font-weight:700;cursor:pointer;">녹화본 실행</button>
      </div>
      <button id="__qaReplayToggle" style="width:100%;border:0;border-radius:8px;padding:6px 6px;margin-top:1px;background:#334155;color:#e2e8f0;font-weight:700;cursor:pointer;">녹화본 자동재생: ON</button>
      <input id="__qaRecordingName" type="text" placeholder="녹화본 이름" value="${{String(MANUAL_RECORDING_NAME || '')}}" style="width:100%;border:1px solid rgba(148,163,184,.35);border-radius:7px;padding:5px 6px;margin:6px 0;background:#0f172a;color:#e2e8f0;" />
      <div id="__qaRecordStatus" style="font-size:10px;color:#94a3b8;margin-bottom:5px;">녹화 상태: OFF</div>
      <div style="display:flex;gap:5px;margin-bottom:5px;">
        <button id="__qaRecordStart" style="flex:1;border:0;border-radius:8px;padding:6px 6px;background:#475569;color:#e2e8f0;font-weight:700;cursor:pointer;">녹화 시작</button>
        <button id="__qaRecordStop" style="flex:1;border:0;border-radius:8px;padding:6px 6px;background:#1f2937;color:#94a3b8;font-weight:700;cursor:pointer;">녹화 종료</button>
      </div>
      <button id="__qaRecordSave" style="width:100%;border:0;border-radius:8px;padding:6px 6px;background:#0f766e;color:#e2e8f0;font-weight:700;cursor:pointer;">저장</button>
      <div id="__qaReplayStatus" style="font-size:10px;color:#94a3b8;margin:6px 0 5px;">녹화본 상태: IDLE</div>
      <div id="__qaAutoRunStatus" style="font-size:10px;color:#94a3b8;margin:0 0 5px;">자동 탐색 상태: IDLE</div>
      <button id="__qaReplayRun" style="width:100%;border:0;border-radius:8px;padding:6px 6px;margin-top:1px;background:#2563eb;color:#e2e8f0;font-weight:700;cursor:pointer;">실행하기</button>
      <button id="__qaStopTest" style="width:100%;border:0;border-radius:8px;padding:6px 6px;margin-top:6px;background:#ef4444;color:#fff;font-weight:800;cursor:pointer;">테스트 종료</button>
    `;
    document.documentElement.appendChild(remote);
    let launcher = document.getElementById("__qaRemoteLauncher");
    if (!launcher) {{
      launcher = document.createElement("button");
      launcher.id = "__qaRemoteLauncher";
      launcher.type = "button";
      launcher.textContent = "QA";
      launcher.style.position = "fixed";
      launcher.style.right = "12px";
      launcher.style.bottom = "12px";
      launcher.style.zIndex = "2147483647";
      launcher.style.width = "42px";
      launcher.style.height = "42px";
      launcher.style.border = "1px solid rgba(148,163,184,.45)";
      launcher.style.borderRadius = "999px";
      launcher.style.background = "rgba(15,23,42,.95)";
      launcher.style.color = "#e2e8f0";
      launcher.style.font = "800 11px/1 -apple-system, BlinkMacSystemFont, Segoe UI, sans-serif";
      launcher.style.cursor = "pointer";
      launcher.style.boxShadow = "0 8px 20px rgba(0,0,0,.28)";
      launcher.style.display = "none";
      document.documentElement.appendChild(launcher);
    }}
    const setPanelMinimized = (minimized) => {{
      try {{
        const isMin = Boolean(minimized);
        remote.style.display = isMin ? "none" : "block";
        if (launcher) launcher.style.display = isMin ? "block" : "none";
        try {{
          localStorage.setItem(REMOTE_MIN_KEY, isMin ? "1" : "0");
        }} catch (e) {{}}
      }} catch (e) {{}}
    }};
    const panelMinBtn = remote.querySelector("#__qaPanelMinimize");
    if (panelMinBtn) {{
      panelMinBtn.addEventListener("click", () => setPanelMinimized(true));
    }}
    if (launcher) {{
      launcher.addEventListener("click", () => setPanelMinimized(false));
    }}
    let startMinimized = true;
    try {{
      startMinimized = String(localStorage.getItem(REMOTE_MIN_KEY) || "1") !== "0";
    }} catch (e) {{}}
    setPanelMinimized(startMinimized);
    const onAction = (action, extra = {{}}) => {{
      try {{
        if (typeof window.__qaRuntimeCommand === "function") {{
          window.__qaRuntimeCommand({{ action, ...extra }});
        }}
      }} catch (e) {{}}
    }};
    const autoBtn = remote.querySelector("#__qaModeAuto");
    const manualBtn = remote.querySelector("#__qaModeManual");
    const profileSel = remote.querySelector("#__qaManualProfile");
    const recordingSel = remote.querySelector("#__qaRecordingSelect");
    const recordingDeleteBtn = remote.querySelector("#__qaRecordingDelete");
    const recordingNameInput = remote.querySelector("#__qaRecordingName");
    const recordStatus = remote.querySelector("#__qaRecordStatus");
    const recordStartBtn = remote.querySelector("#__qaRecordStart");
    const recordStopBtn = remote.querySelector("#__qaRecordStop");
    const recordSaveBtn = remote.querySelector("#__qaRecordSave");
    const replayBtn = remote.querySelector("#__qaReplayToggle");
    const replayStatus = remote.querySelector("#__qaReplayStatus");
    const autoRunStatus = remote.querySelector("#__qaAutoRunStatus");
    const recordingRunBtn = remote.querySelector("#__qaRecordingRun");
    const replayRunBtn = remote.querySelector("#__qaReplayRun");
    const stopBtn = remote.querySelector("#__qaStopTest");
    if (autoBtn) autoBtn.addEventListener("click", () => {{
      try {{ updateRemoteStatus("running"); }} catch (e) {{}}
      onAction("auto_crawl_resume");
    }});
    if (manualBtn) manualBtn.addEventListener("click", () => {{
      try {{ updateRemoteStatus("auto_crawl_paused"); }} catch (e) {{}}
      onAction("auto_crawl_pause");
    }});
    if (profileSel) {{
      window.__qaManualProfileSelected = String(profileSel.value || MANUAL_PROFILE_SELECTED || "default");
      profileSel.addEventListener("change", () => {{
        window.__qaManualProfileSelected = String(profileSel.value || "default");
        onAction("manual_profile_select", {{ profile: window.__qaManualProfileSelected }});
      }});
    }}
    if (recordingSel) {{
      recordingSel.value = String(MANUAL_RECORDING_SELECTED || recordingSel.value || "");
      recordingSel.addEventListener("change", () => {{
        MANUAL_RECORDING_SELECTED = String(recordingSel.value || "").trim();
        writeState("manual_recording_selected", MANUAL_RECORDING_SELECTED);
        onAction("manual_recording_select", {{ recording_id: MANUAL_RECORDING_SELECTED }});
      }});
    }}
    if (recordingDeleteBtn) {{
      recordingDeleteBtn.addEventListener("click", () => {{
        const rid = String((recordingSel && recordingSel.value) || MANUAL_RECORDING_SELECTED || "").trim();
        if (!rid) return;
        onAction("manual_recording_delete", {{ recording_id: rid }});
        recordingDeleteBtn.textContent = "삭제 요청됨";
        setTimeout(() => {{
          recordingDeleteBtn.textContent = "선택 녹화본 삭제";
          if (recordingSel) {{
            const opt = recordingSel.querySelector(`option[value="${{rid}}"]`);
            if (opt) opt.remove();
            recordingSel.value = "";
          }}
          MANUAL_RECORDING_SELECTED = "";
          writeState("manual_recording_selected", "");
        }}, 900);
      }});
    }}
    if (recordingNameInput) {{
      recordingNameInput.value = String(MANUAL_RECORDING_NAME || recordingNameInput.value || "");
      recordingNameInput.addEventListener("change", () => {{
        MANUAL_RECORDING_NAME = String(recordingNameInput.value || "").trim();
        writeState("manual_recording_name", MANUAL_RECORDING_NAME);
        onAction("manual_recording_name_set", {{ name: MANUAL_RECORDING_NAME }});
      }});
      recordingNameInput.addEventListener("blur", () => {{
        MANUAL_RECORDING_NAME = String(recordingNameInput.value || "").trim();
        writeState("manual_recording_name", MANUAL_RECORDING_NAME);
        onAction("manual_recording_name_set", {{ name: MANUAL_RECORDING_NAME }});
      }});
    }}
    const syncRecordControls = () => {{
      const enabled = Boolean(MANUAL_RECORDING_ENABLED);
      if (recordStatus) {{
        recordStatus.textContent = `녹화 상태: ${{enabled ? "ON (기록 중)" : "OFF"}}`;
        recordStatus.style.color = enabled ? "#86efac" : "#94a3b8";
      }}
      if (recordStartBtn) {{
        recordStartBtn.style.background = enabled ? "#1f2937" : "#475569";
        recordStartBtn.style.color = enabled ? "#94a3b8" : "#e2e8f0";
      }}
      if (recordStopBtn) {{
        recordStopBtn.style.background = enabled ? "#fb7185" : "#1f2937";
        recordStopBtn.style.color = enabled ? "#0f172a" : "#94a3b8";
      }}
    }};
    syncRecordControls();
    if (recordStartBtn) {{
      recordStartBtn.addEventListener("click", () => {{
        MANUAL_RECORDING_ENABLED = true;
        writeState("manual_recording_enabled", "1");
        syncRecordControls();
        onAction("manual_recording_start");
      }});
    }}
    if (recordStopBtn) {{
      recordStopBtn.addEventListener("click", () => {{
        MANUAL_RECORDING_ENABLED = false;
        writeState("manual_recording_enabled", "0");
        syncRecordControls();
        onAction("manual_recording_stop");
      }});
    }}
    if (recordSaveBtn) {{
      recordSaveBtn.addEventListener("click", () => {{
        const name = String((recordingNameInput && recordingNameInput.value) || MANUAL_RECORDING_NAME || "").trim();
        if (!name) {{
          recordSaveBtn.textContent = "제목 입력 후 저장";
          setTimeout(() => {{ recordSaveBtn.textContent = "제목으로 저장"; }}, 1100);
          return;
        }}
        onAction("manual_recording_save", {{ name }});
        recordSaveBtn.textContent = "저장 요청됨";
        setTimeout(() => {{ recordSaveBtn.textContent = "제목으로 저장"; }}, 1000);
      }});
    }}
    if (replayBtn) {{
      const syncReplayBtn = () => {{
        const enabled = Boolean(MANUAL_REPLAY_ENABLED);
        replayBtn.textContent = `녹화본 자동재생(사전조건): ${{enabled ? "ON" : "OFF"}}`;
        replayBtn.style.background = enabled ? "#334155" : "#1f2937";
        replayBtn.style.color = enabled ? "#e2e8f0" : "#94a3b8";
      }};
      syncReplayBtn();
      replayBtn.addEventListener("click", () => {{
        MANUAL_REPLAY_ENABLED = !MANUAL_REPLAY_ENABLED;
        writeState("manual_replay_enabled", MANUAL_REPLAY_ENABLED ? "1" : "0");
        syncReplayBtn();
        onAction(
          MANUAL_REPLAY_ENABLED ? "manual_replay_on" : "manual_replay_off"
        );
      }});
    }}
    const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, Math.max(0, Number(ms) || 0)));
    const isVisible = (el) => {{
      try {{
        if (!el || !el.getBoundingClientRect) return false;
        const rect = el.getBoundingClientRect();
        const style = window.getComputedStyle(el);
        if (style.display === "none" || style.visibility === "hidden" || Number(style.opacity || "1") < 0.05) return false;
        return rect.width >= 4 && rect.height >= 4;
      }} catch (e) {{
        return false;
      }}
    }};
    const findReplayTarget = (step) => {{
      try {{
        const selector = String(step && step.selector || "").trim();
        if (selector) {{
          const nodes = Array.from(document.querySelectorAll(selector));
          const hit = nodes.find((n) => isVisible(n));
          if (hit) return hit;
        }}
      }} catch (e) {{}}
      const hint = String(step && step.target_hint || "").trim();
      if (!hint) return null;
      const candidates = Array.from(document.querySelectorAll("button,a,[role='button'],[onclick],[data-button-id],[data-qa],input[type='button'],input[type='submit']"));
      const normHint = hint.replace(/\\s+/g, " ").toLowerCase();
      for (const node of candidates) {{
        if (!isVisible(node)) continue;
        const txt = String((node.innerText || node.textContent || "")).replace(/\\s+/g, " ").toLowerCase();
        if (txt && (txt.includes(normHint) || normHint.includes(txt))) return node;
      }}
      return null;
    }};
    let replayNavGuard = null;
    let replayNavWatchTimer = null;
    let replayNavStartPath = "";
    let replayNavStartUrl = "";
    const clearReplayNavGuardInternal = () => {{
      try {{
        if (replayNavGuard) {{
          document.removeEventListener("click", replayNavGuard.clickGuard, true);
          document.removeEventListener("submit", replayNavGuard.submitGuard, true);
        }}
      }} catch (e) {{}}
      replayNavGuard = null;
      if (replayNavWatchTimer) {{
        try {{ window.clearInterval(replayNavWatchTimer); }} catch (e) {{}}
        replayNavWatchTimer = null;
      }}
      replayNavStartPath = "";
      replayNavStartUrl = "";
      window.__qaReplayNavReleaseAt = 0;
    }};
    const setReplayNavigationGuard = (enabled) => {{
      try {{
        window.__qaReplayNavBlock = Boolean(enabled);
        if (enabled) {{
          if (replayNavGuard) return;
          window.__qaReplayNavReleaseAt = 0;
          replayNavStartPath = String(location.pathname || "").trim();
          replayNavStartUrl = String(location.href || "").trim();
          const clickGuard = (event) => {{
            try {{
              const target = event && event.target && event.target.closest ? event.target.closest("a[href]") : null;
              if (!target) return;
              event.preventDefault();
              event.stopPropagation();
              event.stopImmediatePropagation && event.stopImmediatePropagation();
            }} catch (e) {{}}
          }};
          const submitGuard = (event) => {{
            try {{
              event.preventDefault();
              event.stopPropagation();
              event.stopImmediatePropagation && event.stopImmediatePropagation();
            }} catch (e) {{}}
          }};
          document.addEventListener("click", clickGuard, true);
          document.addEventListener("submit", submitGuard, true);
          replayNavGuard = {{ clickGuard, submitGuard }};
          replayNavWatchTimer = window.setInterval(() => {{
            try {{
              const inCooldown = Number(window.__qaReplayNavReleaseAt || 0) > Date.now();
              if (!window.__qaReplayNavBlock && !inCooldown) {{
                clearReplayNavGuardInternal();
                return;
              }}
              const currentPath = String(location.pathname || "").trim();
              if (!replayNavStartPath || !currentPath || currentPath === replayNavStartPath) return;
              try {{ history.back(); }} catch (e) {{}}
              window.setTimeout(() => {{
                try {{
                  const stillMoved = String(location.pathname || "").trim() !== replayNavStartPath;
                  if (!stillMoved || !replayNavStartUrl) return;
                  window.__qaReplayNavBypass = true;
                  try {{
                    window.location.replace(replayNavStartUrl);
                  }} catch (e) {{
                    try {{ window.location.assign(replayNavStartUrl); }} catch (_e) {{}}
                  }}
                }} finally {{
                  window.setTimeout(() => {{ window.__qaReplayNavBypass = false; }}, 500);
                }}
              }}, 220);
            }} catch (e) {{}}
          }}, 180);
          return;
        }}
        if (!replayNavGuard) return;
        // 지연 이동(클릭 후 비동기 네비게이션)을 잡기 위해 짧은 쿨다운 유지
        window.__qaReplayNavReleaseAt = Date.now() + 4000;
      }} catch (e) {{}}
    }};
    const isLikelyNavigationAction = (el) => {{
      try {{
        if (!el) return false;
        const anchor = el.closest && el.closest("a[href]");
        if (anchor) {{
          const href = String(anchor.getAttribute("href") || "").trim().toLowerCase();
          if (href && !href.startsWith("#") && !href.startsWith("javascript:")) return true;
        }}
        const form = el.closest && el.closest("form[action]");
        if (form) {{
          const action = String(form.getAttribute("action") || "").trim();
          if (action) return true;
        }}
        const onclickRaw = String((el.getAttribute && el.getAttribute("onclick")) || "").trim().toLowerCase();
        if (/(location\\.|window\\.open|\\.href\\s*=|assign\\(|replace\\(|navigate\\()/i.test(onclickRaw)) return true;
      }} catch (e) {{}}
      return false;
    }};
    const safeReplayClick = (el) => {{
      try {{
        if (!el) return;
        if (isLikelyNavigationAction(el)) return false;
        const isLink = Boolean(el.closest && el.closest("a[href]"));
        if (isLink) {{
          const anchor = el.closest("a[href]");
          if (anchor) {{
            const blocker = (event) => event.preventDefault();
            anchor.addEventListener("click", blocker, {{ capture: true, once: true }});
          }}
        }}
        if (typeof el.click === "function") el.click();
        else el.dispatchEvent(new MouseEvent("click", {{ bubbles: true, cancelable: true, view: window }}));
        return true;
      }} catch (e) {{}}
      return false;
    }};
    const runReplay = async (recordingId) => {{
      const rid = String(recordingId || "").trim();
      if (!rid) return false;
      const found = (Array.isArray(MANUAL_RECORDING_CATALOG) ? MANUAL_RECORDING_CATALOG : []).find((v) => String(v && v.id || "") === rid);
      if (!found || !Array.isArray(found.steps) || !found.steps.length) return false;
      for (const step of found.steps) {{
        const el = findReplayTarget(step || {{}});
        if (!el) {{
          await sleep(Number(step && step.wait_ms || 400));
          continue;
        }}
        safeReplayClick(el);
        await sleep(Number(step && step.wait_ms || 600));
      }}
      return true;
    }};
    let recordingReplayRunning = false;
    let recordingReplayStopRequested = false;
    let recordingReplayDone = 0;
    let recordingReplayTotal = 0;
    let recordingReplaySkipped = 0;
    const syncRecordingReplayUx = () => {{
      if (replayStatus) {{
        if (recordingReplayRunning) {{
          replayStatus.textContent = `녹화본 상태: 실행중 (${{recordingReplayDone}}/${{recordingReplayTotal || 0}}, skip:${{recordingReplaySkipped}})`;
        }} else {{
          replayStatus.textContent = "녹화본 상태: IDLE";
        }}
        replayStatus.style.color = recordingReplayRunning ? "#93c5fd" : "#94a3b8";
      }}
      if (recordingRunBtn) {{
        recordingRunBtn.textContent = recordingReplayRunning ? "중지하기" : "녹화본 실행";
        recordingRunBtn.style.background = recordingReplayRunning ? "#f97316" : "#2563eb";
      }}
    }};
    syncRecordingReplayUx();
    if (recordingRunBtn) {{
      recordingRunBtn.addEventListener("click", async () => {{
        if (recordingReplayRunning) {{
          recordingReplayStopRequested = true;
          return;
        }}
        const rid = String((recordingSel && recordingSel.value) || MANUAL_RECORDING_SELECTED || "").trim();
        if (!rid) return;
        recordingReplayRunning = true;
        recordingReplayStopRequested = false;
        recordingReplayDone = 0;
        recordingReplayTotal = 0;
        recordingReplaySkipped = 0;
        syncRecordingReplayUx();
        try {{
          setReplayNavigationGuard(true);
          emit("manual_flow", "manual_replay_start", {{ recording_id: rid }});
          onAction("manual_replay_run", {{ recording_id: rid }});
          const found = (Array.isArray(MANUAL_RECORDING_CATALOG) ? MANUAL_RECORDING_CATALOG : []).find((v) => String(v && v.id || "") === rid);
          if (found && Array.isArray(found.steps)) {{
            recordingReplayTotal = Number(found.steps.length || 0);
            syncRecordingReplayUx();
            for (const step of found.steps) {{
              if (recordingReplayStopRequested) break;
              const el = findReplayTarget(step || {{}});
              if (el) {{
                const clicked = safeReplayClick(el);
                if (!clicked) recordingReplaySkipped += 1;
              }} else {{
                recordingReplaySkipped += 1;
              }}
              recordingReplayDone += 1;
              syncRecordingReplayUx();
              await sleep(Number(step && step.wait_ms || 600));
            }}
          }} else {{
            recordingReplayDone = 0;
            recordingReplayTotal = 0;
            await runReplay(rid);
          }}
        }} finally {{
          setReplayNavigationGuard(false);
        }}
        emit("manual_flow", "manual_replay_end", {{
          recording_id: rid,
          total: Number(recordingReplayTotal || 0),
          done: Number(recordingReplayDone || 0),
          skipped: Number(recordingReplaySkipped || 0),
        }});
        recordingReplayRunning = false;
        recordingReplayStopRequested = false;
        syncRecordingReplayUx();
      }});
    }}
    const syncAutoRunUx = () => {{
      const running = CURRENT_PHASE !== "auto_crawl_paused";
      if (autoRunStatus) {{
        autoRunStatus.textContent = running ? "자동 탐색 상태: 실행중" : "자동 탐색 상태: IDLE";
        autoRunStatus.style.color = running ? "#86efac" : "#94a3b8";
      }}
      if (replayRunBtn) {{
        replayRunBtn.textContent = running ? "중지하기" : "실행하기";
        replayRunBtn.style.background = running ? "#f97316" : "#2563eb";
        replayRunBtn.style.color = "#e2e8f0";
      }}
    }};
    window.__qaSyncReplayRunUx = syncAutoRunUx;
    syncAutoRunUx();
    if (replayRunBtn) {{
      replayRunBtn.addEventListener("click", () => {{
        const running = CURRENT_PHASE !== "auto_crawl_paused";
        if (running) {{
          onAction("auto_crawl_pause");
          try {{ updateRemoteStatus("auto_crawl_paused"); }} catch (e) {{}}
        }} else {{
          onAction("auto_crawl_resume");
          try {{ updateRemoteStatus("running"); }} catch (e) {{}}
        }}
      }});
    }}
    if (stopBtn) {{
      stopBtn.addEventListener("click", () => {{
        onAction("stop_test");
      }});
    }}
    return remote;
  }};

  const updateRemoteStatus = (phase) => {{
    try {{
      CURRENT_PHASE = String(phase || "").trim();
      writeState("phase", CURRENT_PHASE || "running");
      const remote = ensureRemoteControl();
      const status = remote.querySelector("#__qaRemoteStatus");
      const autoBtn = remote.querySelector("#__qaModeAuto");
      const manualBtn = remote.querySelector("#__qaModeManual");
      if (!status) return;
      if (phase === "auto_crawl_paused") {{
        status.textContent = "status: manual";
        status.style.color = "#fca5a5";
        if (autoBtn) {{
          autoBtn.style.opacity = "0.85";
          autoBtn.style.background = "#334155";
          autoBtn.style.color = "#e2e8f0";
          autoBtn.style.border = "1px solid rgba(148,163,184,.35)";
        }}
        if (manualBtn) {{
          manualBtn.style.opacity = "1";
          manualBtn.style.background = "#fb7185";
          manualBtn.style.color = "#0f172a";
          manualBtn.style.border = "1px solid rgba(255,255,255,.35)";
        }}
      }} else {{
        status.textContent = "status: auto";
        status.style.color = "#a7f3d0";
        if (autoBtn) {{
          autoBtn.style.opacity = "1";
          autoBtn.style.background = "#2dd4bf";
          autoBtn.style.color = "#0f172a";
          autoBtn.style.border = "1px solid rgba(255,255,255,.35)";
        }}
        if (manualBtn) {{
          manualBtn.style.opacity = "0.85";
          manualBtn.style.background = "#334155";
          manualBtn.style.color = "#e2e8f0";
          manualBtn.style.border = "1px solid rgba(148,163,184,.35)";
        }}
      }}
      try {{
        if (typeof window.__qaSyncReplayRunUx === "function") {{
          window.__qaSyncReplayRunUx();
        }}
      }} catch (e) {{}}
    }} catch (e) {{}}
  }};

  window.__qaUpdateRuntimePanel = (payload) => {{
    try {{
      updateRemoteStatus(String(payload && payload.phase || ""));
    }} catch (e) {{}}
  }};

  setTimeout(() => {{
    try {{
      ensureRemoteControl();
      updateRemoteStatus(CURRENT_PHASE || (AUTO_CRAWL_START_MODE === "manual" ? "auto_crawl_paused" : "running"));
    }} catch (e) {{}}
  }}, 500);

  const isCollectUrl = (rawUrl) => {{
    try {{
      const u = new URL(String(rawUrl || ""), location.href);
      const path = (u.pathname || "").toLowerCase();
      const q = u.searchParams;
      if (path.includes("/g/collect") || path.includes("/mp/collect")) return true;
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
      const getCollectParams = (rawUrl, bodyText) => {{
        const out = {{}};
        try {{
          const u = new URL(String(rawUrl || ""), location.href);
          u.searchParams.forEach((v, k) => {{
            out[String(k || "").trim()] = String(v || "").trim();
          }});
        }} catch (e) {{}}
        try {{
          const bt = String(bodyText || "").trim();
          if (bt && bt.includes("=")) {{
            const qs = new URLSearchParams(bt);
            qs.forEach((v, k) => {{
              out[String(k || "").trim()] = String(v || "").trim();
            }});
          }}
        }} catch (e) {{}}
        return out;
      }};
      const hitParams = getCollectParams(url, window.__qaLastCollectBody || "");
      const p = (name) => String(hitParams[`ep.${{name}}`] || hitParams[name] || "").trim();
      const sectionName = p("section_name");
      const buttonId = p("button_id");
      const eventType = String(eventName || "").toLowerCase().startsWith("click")
        ? "click"
        : (String(eventName || "").toLowerCase().startsWith("impression") || String(eventName || "").toLowerCase().startsWith("view") ? "impression" : "other");

      const ensureHitAreaLayer = () => {{
        let layer = document.getElementById("__qaHitAreaLayer");
        if (layer) return layer;
        layer = document.createElement("div");
        layer.id = "__qaHitAreaLayer";
        layer.style.position = "fixed";
        layer.style.inset = "0";
        layer.style.pointerEvents = "none";
        layer.style.zIndex = "2147483646";
        document.documentElement.appendChild(layer);
        return layer;
      }};
      const pickTargetBBox = () => {{
        const last = window.__qaLastInteractMeta || null;
        if (eventType === "click" && last && Number(Date.now() - Number(last.ts || 0)) <= 5000) {{
          const b = last.bbox || {{}};
          if (Number(b.width || 0) > 8 && Number(b.height || 0) > 8) return b;
        }}
        const pickSmallClickable = (nodes) => {{
          let best = null;
          let bestArea = Number.POSITIVE_INFINITY;
          for (const node of Array.from(nodes || [])) {{
            if (!node || !node.getBoundingClientRect) continue;
            const cand = node.closest ? (node.closest("button,a[href],[role='button'],[onclick],.gtm-click-button,.gtm-select-item") || node) : node;
            const r = cand.getBoundingClientRect();
            const st = window.getComputedStyle(cand);
            if (r.width < 8 || r.height < 8) continue;
            if (st.visibility === "hidden" || st.display === "none" || Number(st.opacity || "1") <= 0.02) continue;
            const area = Number(r.width || 0) * Number(r.height || 0);
            if (area > 0 && area < bestArea) {{
              best = cand;
              bestArea = area;
            }}
          }}
          return best;
        }};
        if (buttonId) {{
          const node = pickSmallClickable(document.querySelectorAll(`[data-button-id="${{CSS && CSS.escape ? CSS.escape(buttonId) : buttonId}}"]`));
          if (node && node.getBoundingClientRect) {{
            const r = node.getBoundingClientRect();
            return {{ x: Math.round(r.left), y: Math.round(r.top), width: Math.round(r.width), height: Math.round(r.height) }};
          }}
        }}
        const itemId = p("item_id");
        if (itemId) {{
          const node = pickSmallClickable(
            document.querySelectorAll(
              `[data-item-id="${{CSS && CSS.escape ? CSS.escape(itemId) : itemId}}"], [data-product-id="${{CSS && CSS.escape ? CSS.escape(itemId) : itemId}}"], [data-goods-no="${{CSS && CSS.escape ? CSS.escape(itemId) : itemId}}"]`
            )
          );
          if (node && node.getBoundingClientRect) {{
            const r = node.getBoundingClientRect();
            return {{ x: Math.round(r.left), y: Math.round(r.top), width: Math.round(r.width), height: Math.round(r.height) }};
          }}
        }}
        if (eventType !== "click" && sectionName) {{
          const node = document.querySelector(`[data-section-name="${{CSS && CSS.escape ? CSS.escape(sectionName) : sectionName}}"]`);
          if (node) {{
            const r = node.getBoundingClientRect();
            return {{ x: Math.round(r.left), y: Math.round(r.top), width: Math.round(r.width), height: Math.round(r.height) }};
          }}
        }}
        return null;
      }};
      const drawHitArea = () => {{
        const bbox = pickTargetBBox();
        if (!bbox || Number(bbox.width || 0) < 8 || Number(bbox.height || 0) < 8) return;
        const layer = ensureHitAreaLayer();
        layer.innerHTML = "";
        const color = eventType === "click" ? "#ef4444" : (eventType === "impression" ? "#f97316" : "#3b82f6");
        const box = document.createElement("div");
        box.style.position = "fixed";
        box.style.left = `${{Math.max(0, Number(bbox.x || 0))}}px`;
        box.style.top = `${{Math.max(0, Number(bbox.y || 0))}}px`;
        box.style.width = `${{Math.max(0, Number(bbox.width || 0))}}px`;
        box.style.height = `${{Math.max(0, Number(bbox.height || 0))}}px`;
        box.style.boxSizing = "border-box";
        box.style.border = `3px solid ${{color}}`;
        box.style.borderRadius = "8px";
        box.style.boxShadow = `0 0 0 3px rgba(255,255,255,.88), 0 0 0 7px ${{color}}55`;
        box.style.background = `${{color}}14`;
        box.style.transition = "opacity .22s ease";
        box.style.opacity = "1";
        layer.appendChild(box);
        setTimeout(() => {{
          box.style.opacity = "0";
        }}, 360);
        setTimeout(() => {{
          layer.innerHTML = "";
        }}, 620);
      }};
      drawHitArea();

      const inDefinition = HAS_DEFINITION
        ? (eventName ? DEFINITION_EVENTS.includes(eventName) : false)
        : null;
      const borderColor = inDefinition === null
        ? "#22c55e"
        : (inDefinition ? "#f97316" : "#38bdf8");
      const badgeColor = inDefinition === null
        ? "#16a34a"
        : (inDefinition ? "#f97316" : "#0284c7");

      window.__qaLastHitEvent = eventName || "";
      badge.textContent = `QA HIT ${{count}}`;
      badge.title = eventName
        ? `collect event=${{eventName}} / ${{transport || "unknown"}}`
        : `collect / ${{transport || "unknown"}}`;

      badge.style.background = badgeColor;
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
      flashLayer.style.backgroundColor = inDefinition === null
        ? "rgba(34,197,94,.16)"
        : (inDefinition ? "rgba(249,115,22,.16)" : "rgba(56,189,248,.16)");
      clearTimeout(window.__qaHitFlashTimer);
      window.__qaHitFlashTimer = setTimeout(() => {{
        flashLayer.style.opacity = "0";
        flashLayer.style.backgroundColor = "rgba(34,197,94,0)";
      }}, 190);

      window.__qaApplyHitBorder(borderColor);
      clearTimeout(window.__qaHitBorderTimer);
      window.__qaHitBorderTimer = setTimeout(() => {{
        window.__qaClearHitBorder();
      }}, 420);

      emit("qa_hit", eventName || "", {{
        event_name: eventName || "",
        transport: transport || "unknown",
        definition_match: inDefinition,
        border_color: borderColor
      }});

      try {{
        if (typeof window.__qaUpdateRuntimePanel === "function") {{
          window.__qaUpdateRuntimePanel(window.__qaRuntimePanelState || {{}});
        }}
      }} catch (e) {{}}
    }} catch (e) {{}}
  }};

  const wrapDataLayerObject = (arr) => {{
    try {{
      if (!arr || typeof arr.push !== "function") return;
      if (arr.__qaWrappedPush) return;
      const originalPush = arr.push.bind(arr);
      arr.push = function () {{
        const args = Array.prototype.slice.call(arguments);
        for (const entry of args) {{
          if (entry && typeof entry === "object" && !Array.isArray(entry)) {{
            attachSession(entry);
            emit("dataLayer.push", entry.event || "", safeClone(entry));
          }}
        }}
        return originalPush.apply(arr, args);
      }};
      arr.__qaWrappedPush = true;
    }} catch (e) {{}}
  }};

  const wrapDataLayer = () => {{
    try {{
      if (!window.dataLayer || !Array.isArray(window.dataLayer)) {{
        window.dataLayer = [];
      }}
      wrapDataLayerObject(window.dataLayer);
      if (window.__qaDataLayerAccessorInstalled) return;
      let current = window.dataLayer;
      Object.defineProperty(window, "dataLayer", {{
        configurable: true,
        enumerable: true,
        get() {{ return current; }},
        set(v) {{
          current = Array.isArray(v) ? v : [];
          wrapDataLayerObject(current);
        }},
      }});
      window.__qaDataLayerAccessorInstalled = true;
    }} catch (e) {{}}
  }};

  const wrapGtagFunction = (fn) => {{
    if (typeof fn !== "function") return fn;
    if (fn.__qaWrappedGtag) return fn;
    const wrapped = function () {{
      const args = Array.prototype.slice.call(arguments);
      if (args[0] === "event") {{
        const params = (args[2] && typeof args[2] === "object") ? args[2] : {{}};
        attachSession(params);
        args[2] = params;
        emit("gtag", args[1] || "", safeClone(params));
      }}
      return fn.apply(window, args);
    }};
    wrapped.__qaWrappedGtag = true;
    return wrapped;
  }};

  const wrapGtag = () => {{
    try {{
      if (typeof window.gtag === "function") {{
        window.gtag = wrapGtagFunction(window.gtag);
      }}
      if (window.__qaGtagAccessorInstalled) return;
      let current = typeof window.gtag === "function" ? window.gtag : null;
      Object.defineProperty(window, "gtag", {{
        configurable: true,
        enumerable: true,
        get() {{ return current; }},
        set(v) {{
          current = (typeof v === "function") ? wrapGtagFunction(v) : v;
        }},
      }});
      window.__qaGtagAccessorInstalled = true;
    }} catch (e) {{}}
  }};

  const wrapBeacon = () => {{
    if (window.__qaBeaconWrapped) return;
    if (!navigator || typeof navigator.sendBeacon !== "function") {{
      window.__qaBeaconWrapped = true;
      return;
    }}
    const originalBeacon = navigator.sendBeacon.bind(navigator);
    navigator.sendBeacon = function (url, data) {{
      try {{
        window.__qaLastCollectBody = normalizeTransportBody(data);
      }} catch (e) {{}}
      if (isCollectUrl(url)) {{
        flashHit(url, "sendBeacon");
        emit("transport.sendBeacon", "", {{
          transport: "sendBeacon",
          url: String(url || ""),
          body: String(window.__qaLastCollectBody || "").slice(0, 1200),
        }});
      }}
      return originalBeacon(url, data);
    }};
    window.__qaBeaconWrapped = true;
  }};

  const wrapFetch = () => {{
    if (window.__qaFetchWrapped || typeof window.fetch !== "function") return;
    const originalFetch = window.fetch.bind(window);
    window.fetch = function (input, init) {{
      const url = typeof input === "string"
        ? input
        : (input && typeof input.url === "string" ? input.url : "");
      try {{
        const body = init && typeof init === "object" ? init.body : "";
        window.__qaLastCollectBody = normalizeTransportBody(body);
      }} catch (e) {{}}
      if (isCollectUrl(url)) {{
        flashHit(url, "fetch");
        emit("transport.fetch", "", {{
          transport: "fetch",
          url: String(url || ""),
          body: String(window.__qaLastCollectBody || "").slice(0, 1200),
        }});
      }}
      return originalFetch(input, init);
    }};
    window.__qaFetchWrapped = true;
  }};

  const wrapXHR = () => {{
    if (window.__qaXHRWrapped || typeof window.XMLHttpRequest === "undefined") return;
    const originalOpen = window.XMLHttpRequest.prototype.open;
    const originalSend = window.XMLHttpRequest.prototype.send;
    window.XMLHttpRequest.prototype.open = function (method, url) {{
      this.__qaUrl = url;
      return originalOpen.apply(this, arguments);
    }};
    window.XMLHttpRequest.prototype.send = function (body) {{
      try {{
        this.__qaBody = normalizeTransportBody(body);
        window.__qaLastCollectBody = this.__qaBody || "";
      }} catch (e) {{}}
      if (isCollectUrl(this.__qaUrl)) {{
        flashHit(this.__qaUrl, "xhr");
        emit("transport.xhr", "", {{
          transport: "xhr",
          url: String(this.__qaUrl || ""),
          body: String(this.__qaBody || "").slice(0, 1200),
        }});
      }}
      return originalSend.apply(this, arguments);
    }};
    window.__qaXHRWrapped = true;
  }};

  const installWrappers = () => {{
    wrapDataLayer();
    wrapGtag();
    wrapBeacon();
    wrapFetch();
    wrapXHR();
  }};

  ensureHitIndicator();
  ensureHitFlashLayer();
  ensureStatusPanel();
  installWrappers();

  let retryCount = 0;
  const retryTimer = setInterval(() => {{
    retryCount += 1;
    installWrappers();
    if (retryCount >= 5) {{
      clearInterval(retryTimer);
    }}
  }}, 1000);
}})();
"""


def _append_event(output_file: Path, payload: Dict[str, object]) -> None:
    output_file.parent.mkdir(parents=True, exist_ok=True)
    with output_file.open("a", encoding="utf-8") as fp:
        fp.write(json.dumps(payload, ensure_ascii=False) + "\n")


def _append_event_with_db(
    output_file: Path, payload: Dict[str, object], db_path: Path
) -> None:
    _append_event(output_file, payload)
    try:
        append_event_to_db(db_path, payload)
    except Exception:
        return


def _debug_match_log(
    session_id: str,
    stage: str,
    payload: Dict[str, object],
) -> None:
    try:
        _record_runtime_payload(
            session_id,
            {
                "source": "definition_match_debug",
                "event_name": "definition_match_debug",
                "page_url": str(payload.get("page_url", "")).strip(),
                "params": {
                    "stage": stage,
                    **payload,
                },
                "request_method": "DEBUG",
            },
            count_as_event=False,
        )
    except Exception:
        return


def _record_runtime_payload(
    session_id: str,
    payload: Dict[str, object],
    *,
    count_as_event: bool = True,
) -> None:
    session = _get_session(session_id)
    if not session:
        return
    if str(payload.get("source", "")).strip() == "definition_match_debug":
        count_as_event = False
    if str(payload.get("event_name", "")).strip() == "definition_match_debug":
        count_as_event = False

    payload.setdefault("captured_at", datetime.now(timezone.utc).isoformat())
    payload.setdefault("session_id", session_id)
    payload.setdefault("page_url", session.target_url)
    _append_event_with_db(session.output_file, payload, session.db_path)

    if not count_as_event:
        return

    with _LOCK:
        current = _SESSIONS.get(session_id)
        if current:
            current.captured_events += 1
            should_sync = current.captured_events % 10 == 0
        else:
            should_sync = False

    if should_sync:
        _sync_session_db(session_id)


def _next_hit_screenshot_index(session_id: str) -> int:
    with _LOCK:
        session = _SESSIONS.get(session_id)
        if not session:
            return 0
        session.hit_screenshot_count += 1
        return session.hit_screenshot_count


def _next_event_annotation_index(session_id: str, event_key: str) -> int:
    key = str(event_key or "").strip() or "__event__"
    with _LOCK:
        session = _SESSIONS.get(session_id)
        if not session:
            return 1
        current = int(session.annotation_counters.get(key, 0))
        current += 1
        session.annotation_counters[key] = current
        return current


def _apply_hit_border(page, color: str) -> None:
    try:
        page.evaluate(
            "(borderColor) => { if (window.__qaApplyHitBorder) window.__qaApplyHitBorder(borderColor); }",
            color,
        )
    except Exception:
        return


def _clear_hit_border(page) -> None:
    try:
        page.evaluate(
            "() => { if (window.__qaClearHitBorder) window.__qaClearHitBorder(); }"
        )
    except Exception:
        return


def _normalize_event_type_label(value: str) -> str:
    raw = str(value or "").strip().lower()
    if not raw:
        return ""
    if raw in {"impression", "view", "exposure", "노출", "impression_event"}:
        return "impression"
    if raw in {"click", "tap", "select", "클릭"}:
        return "click"
    if raw in {"filter", "필터"}:
        return "filter"
    return raw


def _classify_event_type(event_name: str, event_type_map: Dict[str, str]) -> str:
    if event_type_map:
        mapped = _normalize_event_type_label(event_type_map.get(event_name, ""))
        if mapped:
            return mapped
    name = str(event_name or "").strip().lower()
    if name.startswith(("impression", "view")) or "view_item_list" in name:
        return "impression"
    if "filter" in name:
        return "filter"
    if name.startswith(("click", "select", "tap")) or "click_" in name:
        return "click"
    return "click"


def _action_color_for_event_type(event_type: str) -> str:
    et = _normalize_event_type_label(event_type)
    palette = {
        "click": "#ef4444",
        "impression": "#f97316",
        "filter": "#3b82f6",
    }
    return palette.get(et, "#14b8a6")


def _canonical_key_for_event(event_name: str, section_name: str) -> str:
    en = str(event_name or "").strip()
    sn = str(section_name or "").strip()
    return f"{en}|{sn}" if en else ""


def _resolve_hit_bbox(page, payload: Dict[str, object]) -> Dict[str, int]:
    if not page:
        return {"bbox_x": 0, "bbox_y": 0, "bbox_width": 0, "bbox_height": 0}
    params = payload.get("params", {}) if isinstance(payload.get("params", {}), dict) else {}
    try:
        bbox = page.evaluate(
            """
            ({ eventType, params }) => {
              const out = { bbox_x: 0, bbox_y: 0, bbox_width: 0, bbox_height: 0 };
              const safe = (v) => String(v || "").trim();
              const norm = (v) => safe(v).toLowerCase();
              const et = norm(eventType);
              const read = (k) => safe(params && Object.prototype.hasOwnProperty.call(params, k) ? params[k] : "");
              const q = (sel) => {
                try { return Array.from(document.querySelectorAll(sel)); } catch (e) { return []; }
              };
              const vis = (el) => {
                if (!el || !el.getBoundingClientRect) return false;
                const r = el.getBoundingClientRect();
                const st = window.getComputedStyle(el);
                return r.width > 8 && r.height > 8 && st.visibility !== "hidden" && st.display !== "none" && Number(st.opacity || "1") > 0.02;
              };
              const rectObj = (el) => {
                const r = el.getBoundingClientRect();
                return {
                  bbox_x: Math.max(0, Math.round(r.left)),
                  bbox_y: Math.max(0, Math.round(r.top)),
                  bbox_width: Math.max(0, Math.round(r.width)),
                  bbox_height: Math.max(0, Math.round(r.height)),
                };
              };
              const score = (el) => {
                if (!vis(el)) return Number.POSITIVE_INFINITY;
                const r = el.getBoundingClientRect();
                const area = Number(r.width || 0) * Number(r.height || 0);
                if (area <= 0) return Number.POSITIVE_INFINITY;
                const clickable = el.matches && el.matches("button,a[href],[role='button'],input[type='button'],input[type='submit'],[onclick],.gtm-click-button,.gtm-select-item");
                return area + (clickable ? 0 : 1000000);
              };
              const pickSmallest = (nodes) => {
                let best = null;
                let bestScore = Number.POSITIVE_INFINITY;
                for (const n of (nodes || [])) {
                  if (!n || !vis(n)) continue;
                  const cand = n.closest ? (n.closest("button,a[href],[role='button'],[onclick],.gtm-click-button,.gtm-select-item") || n) : n;
                  const s = score(cand);
                  if (s < bestScore) {
                    best = cand;
                    bestScore = s;
                  }
                }
                return best;
              };

              // click 이벤트는 직전 실제 상호작용 bbox를 최우선 사용하되
              // payload 힌트(button_id/selector/section_name)와 불일치하면 사용하지 않는다.
              if (et === "click") {
                const buttonIdHint = read("button_id");
                const targetIdHint = read("target_id");
                const sectionHint = read("section_name");
                const selectorHint = read("selector");
                const last = window.__qaLastInteractMeta || null;
                if (last && Number(Date.now() - Number(last.ts || 0)) <= 1700) {
                  const sameButton = !!(buttonIdHint && safe(last.button_id) && norm(buttonIdHint) === norm(last.button_id));
                  const sameTarget = !!(
                    targetIdHint
                    && (
                      norm(targetIdHint) === norm(last.button_id || "")
                      || norm(targetIdHint) === norm(last.selector || "")
                    )
                  );
                  const sameSection = !!(sectionHint && safe(last.section_name) && norm(sectionHint) === norm(last.section_name));
                  const sameSelector = !!(
                    selectorHint
                    && safe(last.selector)
                    && (
                      safe(last.selector) === selectorHint
                      || safe(last.selector).includes(selectorHint)
                      || selectorHint.includes(safe(last.selector))
                    )
                  );
                  const hasHints = !!(buttonIdHint || targetIdHint || sectionHint || selectorHint);
                  const isConsistent = !hasHints || sameButton || sameTarget || sameSection || sameSelector;
                  const b = last.bbox || {};
                  if (isConsistent && Number(b.width || 0) > 8 && Number(b.height || 0) > 8) {
                    return {
                      bbox_x: Math.max(0, Math.round(Number(b.x || 0))),
                      bbox_y: Math.max(0, Math.round(Number(b.y || 0))),
                      bbox_width: Math.max(0, Math.round(Number(b.width || 0))),
                      bbox_height: Math.max(0, Math.round(Number(b.height || 0))),
                    };
                  }
                }
              }

              const buttonId = read("button_id");
              const itemId = read("item_id");
              const contentId = read("content_id");
              const bannerId = read("banner_id");
              const targetId = read("target_id");
              const sectionName = read("section_name");
              const selector = read("selector");

              const candidateSets = [];
              if (buttonId) candidateSets.push(q(`[data-button-id="${CSS && CSS.escape ? CSS.escape(buttonId) : buttonId}"]`));
              if (itemId) candidateSets.push(q(`[data-item-id="${CSS && CSS.escape ? CSS.escape(itemId) : itemId}"], [data-product-id="${CSS && CSS.escape ? CSS.escape(itemId) : itemId}"], [data-goods-no="${CSS && CSS.escape ? CSS.escape(itemId) : itemId}"]`));
              if (contentId) candidateSets.push(q(`[data-content-id="${CSS && CSS.escape ? CSS.escape(contentId) : contentId}"]`));
              if (bannerId) candidateSets.push(q(`[data-banner-id="${CSS && CSS.escape ? CSS.escape(bannerId) : bannerId}"]`));
              if (targetId && targetId.startsWith("data-item-id:")) {
                const v = targetId.split(":").slice(1).join(":");
                if (v) candidateSets.push(q(`[data-item-id="${CSS && CSS.escape ? CSS.escape(v) : v}"]`));
              }
              if (selector) candidateSets.push(q(selector));
              for (const set of candidateSets) {
                const picked = pickSmallest(set);
                if (picked) return rectObj(picked);
              }
              // click은 section 전체 fallback 금지 (너무 큰 박스 방지)
              if (et !== "click" && sectionName) {
                const sectionNode = document.querySelector(`[data-section-name="${CSS && CSS.escape ? CSS.escape(sectionName) : sectionName}"]`);
                if (sectionNode && vis(sectionNode)) return rectObj(sectionNode);
              }
              return out;
            }
            """,
            {
                "eventType": str(payload.get("event_type", "")).strip(),
                "params": params,
            },
        )
        if isinstance(bbox, dict):
            return {
                "bbox_x": int(max(0, bbox.get("bbox_x", 0) or 0)),
                "bbox_y": int(max(0, bbox.get("bbox_y", 0) or 0)),
                "bbox_width": int(max(0, bbox.get("bbox_width", 0) or 0)),
                "bbox_height": int(max(0, bbox.get("bbox_height", 0) or 0)),
            }
    except Exception:
        pass
    return {"bbox_x": 0, "bbox_y": 0, "bbox_width": 0, "bbox_height": 0}


def _capture_ga_hit_screenshots(
    page,
    session_id: str,
    payload: Dict[str, object],
    settings: Dict[str, object],
) -> List[str]:
    if not page:
        return []
    event_name = str(payload.get("event_name", "")).strip()
    border_color = str(payload.get("border_color", "")).strip() or "#22c55e"
    event_type = str(payload.get("event_type", "")).strip()
    canonical_key_no = int(_safe_int(payload.get("canonical_key_no", 1), fallback=1))
    screenshots_dir = Path("data/debug_screens") / session_id
    row_no = ""
    with _LOCK:
        session = _SESSIONS.get(session_id)
        if session:
            current_row_id = str(session.current_definition_row_id or "").strip()
            if current_row_id:
                for row in _get_definition_runtime_rows(session.run_settings):
                    if str(row.get("definition_row_id", "")).strip() == current_row_id:
                        row_no = str(row.get("no", "")).strip()
                        break
    image_format = str(settings.get("screenshot_format", "png")).strip().lower()
    screenshot_quality = int(settings.get("screenshot_quality", 85))
    delays = [120]
    if event_type == "impression":
        delays = [160]
    elif event_type == "click":
        delays = [650]
    elif event_type == "filter":
        delays = [200, 900]
    screenshot_paths: List[str] = []
    stable_bbox: Dict[str, int] = {"bbox_x": 0, "bbox_y": 0, "bbox_width": 0, "bbox_height": 0}

    def _is_valid_bbox(bbox: Dict[str, int]) -> bool:
        if not isinstance(bbox, dict):
            return False
        return int(max(0, bbox.get("bbox_width", 0) or 0)) > 8 and int(max(0, bbox.get("bbox_height", 0) or 0)) > 8

    for delay_ms in delays:
        marker = _next_hit_screenshot_index(session_id) or 1
        screenshot_name = _make_screenshot_name(
            "hit", row_no or "00", marker, image_format
        )
        screenshot_path = screenshots_dir / screenshot_name
        try:
            page.wait_for_timeout(int(delay_ms))
            candidate_bbox = _resolve_hit_bbox(page, payload)
            if _is_valid_bbox(candidate_bbox):
                stable_bbox = candidate_bbox
            try:
                page.evaluate("() => { if (window.__qaShowDim) window.__qaShowDim(); }")
                page.wait_for_timeout(60)
            except Exception:
                pass
            screenshot_path.parent.mkdir(parents=True, exist_ok=True)
            page.screenshot(
                path=str(screenshot_path),
                full_page=False,
                type=image_format,
                quality=(
                    screenshot_quality if image_format in {"jpeg", "webp"} else None
                ),
            )
            _postprocess_capture_image(
                screenshot_path,
                bbox=stable_bbox,
                border_color=border_color,
                dim_opacity=0.22,
                border_width=6,
                badge_text=f"{event_name} #{canonical_key_no}",
            )
            screenshot_paths.append(str(screenshot_path).strip())
        except Exception:
            continue
        finally:
            try:
                page.evaluate("() => { if (window.__qaHideDim) window.__qaHideDim(); }")
            except Exception:
                pass
    return screenshot_paths


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
        "manual_recording_enabled": bool(session.manual_recording_enabled),
        "manual_profile_selected": str(session.manual_profile_selected or "default"),
        "manual_flow_replay_enabled": bool(session.manual_flow_replay_enabled),
        "manual_recording_selected": str(session.manual_recording_selected or ""),
        "manual_recording_name": str(session.manual_recording_name or ""),
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


def _is_session_paused(session_id: str) -> bool:
    with _LOCK:
        session = _SESSIONS.get(str(session_id or "").strip())
        return bool(session.auto_crawl_paused) if session else False


def _wait_interruptible(
    page,
    session_id: str,
    total_ms: int,
    step_ms: int = 120,
) -> bool:
    remain = max(0, int(total_ms or 0))
    tick = max(60, int(step_ms or 120))
    while remain > 0:
        if _is_session_paused(session_id):
            return True
        wait_ms = min(tick, remain)
        try:
            page.wait_for_timeout(wait_ms)
        except Exception:
            return _is_session_paused(session_id)
        remain -= wait_ms
    return _is_session_paused(session_id)


def _build_compare_key(url: str) -> str:
    try:
        parsed = urlparse(str(url or "").strip())
        host = (parsed.netloc or "").lower().strip()
        path = parsed.path or "/"
        return f"{host}{path}"
    except Exception:
        return ""


def _build_scope_prefix(url: str) -> str:
    try:
        parsed = urlparse(str(url or "").strip())
        host = (parsed.netloc or "").lower().strip()
        path = parsed.path or "/"
        path = path.rstrip("/") or "/"
        return f"{host}{path}"
    except Exception:
        return ""


def _is_url_in_scope(href: str, base_url: str, scope_prefix: str) -> bool:
    try:
        raw = str(href or "").strip()
        if not raw:
            return True
        if raw.startswith("#") or raw.lower().startswith("javascript:"):
            return True
        absolute = urljoin(str(base_url or "").strip(), raw)
        parsed = urlparse(absolute)
        host = (parsed.netloc or "").lower().strip()
        path = parsed.path or "/"
        path = path.rstrip("/") or "/"
        compare = f"{host}{path}"
        return bool(scope_prefix and compare.startswith(scope_prefix))
    except Exception:
        return True


def _capture_annotated_screenshot(
    page,
    screenshot_path: Path,
    bbox: Dict[str, int],
    marker_index: int,
    selector_label: str = "",
    *,
    save_raw: bool = False,
    image_format: str = "png",
    quality: int = 85,
    border_color: str = "#ef4444",
    badge_text: str = "",
) -> tuple[str, Dict[str, int]]:
    screenshot_path.parent.mkdir(parents=True, exist_ok=True)

    normalized_bbox = {
        "bbox_x": int(max(0, bbox.get("bbox_x", 0))),
        "bbox_y": int(max(0, bbox.get("bbox_y", 0))),
        "bbox_width": int(max(0, bbox.get("bbox_width", 0))),
        "bbox_height": int(max(0, bbox.get("bbox_height", 0))),
    }

    fmt = str(image_format or "png").strip().lower()
    if fmt not in {"png", "jpeg", "webp"}:
        fmt = "png"

    suffix = ".jpg" if fmt == "jpeg" else f".{fmt}"
    if screenshot_path.suffix.lower() != suffix:
        screenshot_path = screenshot_path.with_suffix(suffix)

    raw_path_str = ""
    raw_bytes = b""

    if save_raw:
        raw_bytes = page.screenshot(full_page=False, type="png")
        raw_path = screenshot_path.with_name(f"{screenshot_path.stem}_raw.png")
        raw_path.write_bytes(raw_bytes)
        raw_path_str = str(raw_path)

    try:
        page.evaluate("() => { if (window.__qaShowDim) window.__qaShowDim(); }")
        page.wait_for_timeout(60)
    except Exception:
        pass
    page.screenshot(
        path=str(screenshot_path),
        full_page=False,
        type=fmt,
        quality=quality if fmt in {"jpeg", "webp"} else None,
    )
    try:
        page.evaluate("() => { if (window.__qaHideDim) window.__qaHideDim(); }")
    except Exception:
        pass
    _postprocess_capture_image(
        screenshot_path,
        bbox=normalized_bbox,
        border_color=str(border_color or "#ef4444"),
        dim_opacity=0.16,
        border_width=5,
        badge_text=badge_text or f"#{int(marker_index)}",
    )

    return raw_path_str, normalized_bbox


def _postprocess_capture_image(
    screenshot_path: Path,
    *,
    bbox: Optional[Dict[str, int]] = None,
    border_color: str = "#22c55e",
    dim_opacity: float = 0.22,
    border_width: int = 6,
    badge_text: str = "",
) -> None:
    try:
        from PIL import Image, ImageColor, ImageDraw, ImageFont
    except Exception:
        return
    try:
        image = Image.open(screenshot_path)
    except Exception:
        return
    try:
        opacity = max(0.0, min(0.5, float(dim_opacity)))
    except Exception:
        opacity = 0.22
    width, height = image.size
    original = image.convert("RGBA")
    rgba = original.copy()
    dim_layer = Image.new("RGBA", (width, height), (0, 0, 0, int(255 * opacity)))
    rgba = Image.alpha_composite(rgba, dim_layer)
    draw = ImageDraw.Draw(rgba)
    try:
        color = ImageColor.getrgb(str(border_color).strip() or "#22c55e")
    except Exception:
        color = (34, 197, 94)
    bw = int(max(2, border_width))
    safe_bbox = bbox if isinstance(bbox, dict) else {}
    x = int(max(0, safe_bbox.get("bbox_x", 0) or 0))
    y = int(max(0, safe_bbox.get("bbox_y", 0) or 0))
    w = int(max(0, safe_bbox.get("bbox_width", 0) or 0))
    h = int(max(0, safe_bbox.get("bbox_height", 0) or 0))
    x1 = max(0, min(width - 1, x))
    y1 = max(0, min(height - 1, y))
    x2 = max(x1 + 1, min(width, x + w))
    y2 = max(y1 + 1, min(height, y + h))
    has_target_bbox = w > 0 and h > 0 and x2 > x1 and y2 > y1
    if has_target_bbox:
        # 작은 아이콘/텍스트 클릭도 육안으로 보이도록 bbox를 소폭 확장
        pad = max(3, int(round(min(width, height) * 0.004)))
        x1 = max(0, x1 - pad)
        y1 = max(0, y1 - pad)
        x2 = min(width, x2 + pad)
        y2 = min(height, y2 + pad)
        target_region = original.crop((x1, y1, x2, y2))
        rgba.paste(target_region, (x1, y1))
        # 흰색 외곽 + 컬러 내곽 이중선으로 어떤 영역인지 분명하게 표시
        outer_w = bw + 3
        draw.rectangle(
            [x1, y1, x2 - 1, y2 - 1],
            outline=(255, 255, 255),
            width=outer_w,
        )
        draw.rectangle(
            [x1, y1, x2 - 1, y2 - 1],
            outline=color,
            width=bw,
        )
        # 코너 강조(작은 점)로 박스 식별력 향상
        dot_r = max(3, bw + 1)
        for cx, cy in ((x1, y1), (x2 - 1, y1), (x1, y2 - 1), (x2 - 1, y2 - 1)):
            draw.ellipse(
                [cx - dot_r, cy - dot_r, cx + dot_r, cy + dot_r],
                fill=(255, 255, 255),
                outline=color,
                width=max(1, bw // 2),
            )
    badge = str(badge_text or "").strip()
    if badge:
        pill_h = 30
        pad_x = 12
        try:
            font = ImageFont.load_default()
            text_w = int(draw.textlength(badge, font=font))
        except Exception:
            font = None
            text_w = max(80, len(badge) * 7)
        pill_w = max(80, text_w + (pad_x * 2))
        badge_x1 = max(12, x1) if has_target_bbox else 12
        badge_y1 = max(12, y1 - 34) if has_target_bbox else 12
        badge_x2 = min(width - 12, badge_x1 + pill_w)
        badge_y2 = min(height - 12, badge_y1 + pill_h)
        fill_color = color[:3] if isinstance(color, tuple) and len(color) >= 3 else (34, 197, 94)
        draw.rounded_rectangle([badge_x1, badge_y1, badge_x2, badge_y2], radius=999, fill=fill_color)
        try:
            draw.text((badge_x1 + pad_x, badge_y1 + 9), badge, fill=(255, 255, 255), font=font)
        except Exception:
            pass
    out_image = (
        rgba.convert("RGB")
        if screenshot_path.suffix.lower() in {".jpg", ".jpeg", ".webp"}
        else rgba
    )
    try:
        out_image.save(screenshot_path)
    except Exception:
        pass


def _extract_candidate_metadata(handle) -> Dict[str, object]:
    return handle.evaluate(
        """
        (el) => {
          const readAttr = (node, name) => (node && node.getAttribute ? (node.getAttribute(name) || "") : "");
          const normalizeText = (value) => String(value || "").replace(/\\s+/g, " ").trim();
          const toPattern = (value) => String(value || "").replace(/\\d+/g, "#");
          const semanticSelectors = [
            '[data-qa]',
            '[data-button-id]',
            '[data-section-name]',
            '[data-index]',
            'button',
            'a',
            '[role="button"]',
            '[onclick]',
            '.gtm-click-button',
            '.gtm-click-content'
          ];
          const clickableSelectors = [
            'button',
            'a[href]',
            '[role="button"]',
            'input[type="button"]',
            'input[type="submit"]',
            'input[type="radio"]',
            'input[type="checkbox"]',
            '[onclick]',
            '[data-qa]',
            '[data-button-id]'
          ];
          const isClickable = (node) => {
            try {
              return Boolean(node && node.matches && node.matches(clickableSelectors.join(',')));
            } catch (e) {
              return false;
            }
          };
          const hasSemanticIdentity = (node) => semanticSelectors.some((selector) => {
            try {
              return node && node.matches && node.matches(selector);
            } catch (e) {
              return false;
            }
          });
          const getRect = (node) => {
            if (!node || !node.getBoundingClientRect) {
              return { x: 0, y: 0, width: 0, height: 0 };
            }
            const rect = node.getBoundingClientRect();
            return {
              x: Math.max(0, Math.round(rect.left)),
              y: Math.max(0, Math.round(rect.top)),
              width: Math.max(0, Math.round(rect.width)),
              height: Math.max(0, Math.round(rect.height)),
            };
          };
          const isTooSmall = (rect) => rect.width < 40 || rect.height < 18;
          const isTooLarge = (rect) => rect.width >= (window.innerWidth * 0.94) || rect.height >= (window.innerHeight * 0.82);
          const resolveSemanticTarget = (node) => {
            let current = node;
            let best = null;
            let bestArea = Number.POSITIVE_INFINITY;
            let depth = 0;
            while (current && current.nodeType === 1 && depth < 6) {
              const rect = getRect(current);
              const clickable = isClickable(current) || hasSemanticIdentity(current);
              if (clickable && rect.width > 0 && rect.height > 0 && !isTooSmall(rect) && !isTooLarge(rect)) {
                const area = Number(rect.width || 0) * Number(rect.height || 0);
                if (area > 0 && area < bestArea) {
                  best = current;
                  bestArea = area;
                }
              }
              current = current.parentElement;
              depth += 1;
            }
            if (!best && node && node.closest) {
              best = node.closest(clickableSelectors.join(',')) || node;
            }
            let target = best;
            let rect = getRect(target);
            let fallbackDepth = 0;
            while (target && isTooSmall(rect) && target.parentElement && fallbackDepth < 4) {
              target = target.parentElement;
              rect = getRect(target);
              fallbackDepth += 1;
            }
            return { target, rect };
          };
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

          const resolved = resolveSemanticTarget(el);
          const target = resolved.target || el;
          const targetRect = resolved.rect || getRect(target);
          const sectionHost = target.closest("[data-section-name]");
          const sectionName = readAttr(target, "data-section-name") || readAttr(sectionHost, "data-section-name") || "";
          const text = normalizeText(target.innerText || target.textContent || readAttr(target, "aria-label") || readAttr(target, "title"));
          const href = readAttr(target, "href");
          const tag = (target.tagName || "").toLowerCase();
          const classAttribute = normalizeText(readAttr(target, "class"));
          const role = readAttr(target, "role");
          const dataQa = readAttr(target, "data-qa");
          const dataButtonId = readAttr(target, "data-button-id");
          const dataButtonName = readAttr(target, "data-button-name");
          const dataSectionName = readAttr(target, "data-section-name");
          const dataItemId = readAttr(target, "data-item-id") || readAttr(target, "data-product-id") || readAttr(target, "data-goods-no");
          const dataContentId = readAttr(target, "data-content-id");
          const dataBannerId = readAttr(target, "data-banner-id");
          const dataBrandId = readAttr(target, "data-brand-id");
          const dataCategoryId = readAttr(target, "data-category-id");
          const dataIndex = readAttr(target, "data-index");
          const priorityHost = target.closest("[data-qa-priority]");
          const dataQaPriority = (
            readAttr(target, "data-qa-priority")
            || readAttr(priorityHost, "data-qa-priority")
            || ""
          ).toLowerCase();
          const ignoreHost = target.closest("[data-auto-crawl]");
          const autoCrawlIgnore = (
            readAttr(target, "data-auto-crawl")
            || readAttr(ignoreHost, "data-auto-crawl")
            || ""
          ).toLowerCase() === "ignore";
          const inMainArea = Boolean(
            target.closest('main,[role="main"],#content,.content,.contents,[data-qa-page]')
          );
          const inHeaderArea = Boolean(
            target.closest('header,[role="banner"],#header,.header,[class*="header"]')
          );
          const inFooterArea = Boolean(
            target.closest('footer,#footer,.footer,[class*="footer"]')
          );
          const inNavArea = Boolean(
            target.closest('nav,.gnb,[class*="gnb"],[class*="global-nav"],[class*="global_nav"],[id*="gnb"]')
          );
          const inFloatingArea = Boolean(
            target.closest(
              '.floating,[class*="floating"],.quick,[class*="quick-menu"],[class*="quick_menu"],.fab,[class*="sticky"],[class*="tooltip"],[role="tooltip"]'
            )
          );
          const inGlobalArea = Boolean(inHeaderArea || inFooterArea || inNavArea);
          const targetId = dataItemId
            ? `data-item-id:${dataItemId}`
            : (dataContentId
              ? `data-content-id:${dataContentId}`
              : (dataBannerId
                ? `data-banner-id:${dataBannerId}`
                : (dataBrandId
                  ? `data-brand-id:${dataBrandId}`
                  : (dataButtonId
                    ? `data-button-id:${dataButtonId}`
                    : (dataQa
                      ? `data-qa:${dataQa}`
                      : (dataCategoryId
                        ? `data-category-id:${dataCategoryId}`
                        : (dataSectionName
                          ? `data-section-name:${dataSectionName}`
                          : (target.id ? `id:${target.id}` : ""))))))));
          const uiRole = role || (tag === "a" ? "link" : (tag === "button" || tag === "input" ? "button_like" : "clickable"));
          const selector = buildSelector(target);
          const selectorPattern = toPattern(selector);
          const style = window.getComputedStyle(target);
          const disabled = Boolean(
            target.disabled ||
            readAttr(target, "disabled") !== "" ||
            String(readAttr(target, "aria-disabled")).toLowerCase() === "true"
          );
          const hidden = Boolean(
            targetRect.width <= 0 ||
            targetRect.height <= 0 ||
            style.visibility === "hidden" ||
            style.display === "none" ||
            Number(style.opacity || "1") <= 0.02
          );
          const _layerSelStr = '[role="dialog"],[aria-modal="true"],.modal,[class*="modal"],[class*="popup"],[class*="drawer"],[class*="layer"],[id*="layer"],[class*="sheet"],[class*="flyout"],[class*="gateway"],[data-type="layer"]';
          const screenState = `modal:${document.querySelectorAll(_layerSelStr).length > 0 ? 1 : 0}|expanded:${document.querySelectorAll('[aria-expanded="true"]').length > 0 ? 1 : 0}|tooltip:${document.querySelectorAll('[role="tooltip"], [class*="tooltip"]').length > 0 ? 1 : 0}|self_expanded:${readAttr(target, 'aria-expanded') || 'na'}`;
          const pageId = location.pathname || "/";
          const classPattern = toPattern(classAttribute.split(" ").slice(0, 2).join("."));
          const key = [pageId, tag, targetId || selector, text.slice(0, 80), href].join("|");
          const structureKey = [pageId, sectionName || "section", uiRole, tag, classPattern || selectorPattern, screenState].join("|");
          const stableId = dataButtonId || dataQa || dataItemId || dataContentId || dataBannerId || dataBrandId || dataCategoryId;
          const hasStableControlId = Boolean(stableId) || (Boolean(dataQa) && !/(content|banner|card|item|product|goods)/i.test(dataQa));
          const patternKey = hasStableControlId
            ? [structureKey, targetId || stableId || "na", href || "na"].join("|")
            : [structureKey, href || "na"].join("|");
          return {
            key,
            pattern_key: patternKey,
            structure_key: structureKey,
            text,
            href,
            button_id: dataButtonId,
            button_name: dataButtonName,
            target_id: targetId || selector,
            item_id: dataItemId,
            content_id: dataContentId,
            banner_id: dataBannerId,
            brand_id: dataBrandId,
            category_id: dataCategoryId,
            data_index: dataIndex,
            data_qa: dataQa,
            data_qa_priority: dataQaPriority,
            auto_crawl_ignore: autoCrawlIgnore,
            selector,
            selector_pattern: selectorPattern,
            section_name: sectionName,
            in_main_area: inMainArea,
            in_global_area: inGlobalArea,
            in_floating_area: inFloatingArea,
            screen_state: screenState,
            class_attribute: classAttribute,
            ui_role: uiRole,
            tag,
            disabled,
            hidden,
            page_id: pageId,
            bbox_x: targetRect.x,
            bbox_y: targetRect.y,
            bbox_width: targetRect.width,
            bbox_height: targetRect.height
          };
        }
        """
    )


def _has_open_popup(page) -> bool:
    """팝업/레이어 열림 여부 확인. 명시적 셀렉터만 사용 (z-index 스캔 제외 - 오탐 방지)."""
    try:
        return bool(
            page.evaluate(
                """
                () => {
                  const selectors = [
                    '[role="dialog"]','[aria-modal="true"]','.modal',
                    '[class*="modal"]','[class*="popup"]','[class*="drawer"]',
                    '[class*="gateway"]','[id*="gateway"]','[data-section-name="gateway"]',
                    '[class*="layer"]','[id*="layer"]',
                    '[class*="sheet"]','[class*="flyout"]','[class*="offcanvas"]',
                    '[data-type="layer"]','[data-type="modal"]','[class*="bottom-"]'
                  ];
                  return Array.from(document.querySelectorAll(selectors.join(','))).some(node => {
                    if (node.id === '__qaAutoTargetOverlay') return false;
                    const rect = node.getBoundingClientRect();
                    const style = window.getComputedStyle(node);
                    return rect.width > 24 && rect.height > 24
                      && style.visibility !== 'hidden' && style.display !== 'none'
                      && Number(style.opacity || '1') > 0.02;
                  });
                }
                """
            )
        )
    except Exception:
        return False


def _get_popup_layer_state(page) -> Dict[str, object]:
    try:
        state = page.evaluate(
            """
            () => {
              const isVisible = (node) => {
                if (!node) return false;
                const rect = node.getBoundingClientRect();
                const style = window.getComputedStyle(node);
                return rect.width > 24 && rect.height > 24 && style.visibility !== 'hidden' && style.display !== 'none' && Number(style.opacity || '1') > 0.02;
              };
              const popupSelectors = [
                '[role="dialog"]',
                '[aria-modal="true"]',
                '.modal',
                '[class*="modal"]',
                '[class*="popup"]',
                '[class*="drawer"]',
                '[class*="gateway"]',
                '[id*="gateway"]',
                '[data-section-name="gateway"]',
                '[class*="layer"]',
                '[id*="layer"]',
                '[class*="sheet"]',
                '[class*="flyout"]',
                '[class*="offcanvas"]',
                '[data-type="layer"]',
                '[data-type="modal"]',
                '[class*="bottom-"]'
              ];
              const overlaySelectors = [
                '[class*="backdrop"]',
                '[class*="overlay"]',
                '[class*="dimmed"]',
                '[class*="scrim"]',
                '[class*="dim"]',
                '[data-testid*="backdrop"]',
                '[data-qa*="backdrop"]',
                '[class*="gateway"]'
              ];
              const popupCount = Array.from(document.querySelectorAll(popupSelectors.join(','))).filter(isVisible).length;
              const overlayCount = Array.from(document.querySelectorAll(overlaySelectors.join(','))).filter(isVisible).length;
              return {
                popup_count: popupCount,
                overlay_count: overlayCount,
                popup_open: popupCount > 0 || overlayCount > 0,
                overlay_open: overlayCount > 0,
                closed_confirmed: popupCount === 0 && overlayCount === 0
              };
            }
            """
        )
        if isinstance(state, dict):
            return {
                "popup_count": int(state.get("popup_count", 0) or 0),
                "overlay_count": int(state.get("overlay_count", 0) or 0),
                "popup_open": bool(state.get("popup_open", False)),
                "overlay_open": bool(state.get("overlay_open", False)),
                "closed_confirmed": bool(state.get("closed_confirmed", False)),
            }
    except Exception:
        pass
    return {
        "popup_count": 0,
        "overlay_count": 0,
        "popup_open": False,
        "overlay_open": False,
        "closed_confirmed": True,
    }


def _close_popup_if_possible(page) -> Dict[str, object]:
    close_selectors = [
        # gateway 전용 닫기 (data-section-name 기반 - 가장 정확)
        '[data-section-name="gateway"] button[data-button-id="close"]',
        '[data-section-name="gateway"] [data-button-id="close"]',
        '[data-section-name="gateway"] .gtm-click-button[data-button-id="close"]',
        # data-button-id 기반
        'button[data-button-id="close"][data-button-name="닫기"]',
        "button[data-button-id='close']",
        "[data-button-id='close']",
        "button[data-button-name='닫기']",
        "[data-button-name='닫기']",
        ".gtm-click-button[data-button-id='close']",
        # gateway class 포함 컨테이너 내 닫기
        "[class*='gateway'] button[data-button-id='close']",
        "[class*='gateway'] [data-button-id='close']",
        "[class*='gateway'] button[data-button-name='닫기']",
        "[class*='gateway'] .gtm-click-button",
        # layer/sheet 닫기
        "[class*='layer'] button[data-button-id='close']",
        "[class*='layer'] [data-button-id='close']",
        "[class*='sheet'] button[data-button-id='close']",
        "[class*='sheet'] [data-button-id='close']",
        "[role='dialog'] button[aria-label*='닫']",
        "[role='dialog'] button[aria-label*='close' i]",
        "[role='dialog'] [data-qa*='close']",
        "[role='dialog'] [data-button-id*='close']",
        "[role='dialog'] button[class*='close']",
        "[aria-modal='true'] button[aria-label*='닫']",
        "[aria-modal='true'] button[aria-label*='close' i]",
        "[aria-modal='true'] [data-qa*='close']",
        "[aria-modal='true'] [data-button-id*='close']",
        "[aria-modal='true'] button[class*='close']",
        ".modal button[aria-label*='닫']",
        ".modal button[aria-label*='close' i]",
        ".modal [data-qa*='close']",
        ".modal [data-button-id*='close']",
        ".modal button[class*='close']",
    ]
    result = {
        "closed": False,
        "method": "",
        "overlay_cleared": False,
        "popup_count": 0,
        "overlay_count": 0,
    }
    try:

        def snapshot(method: str = "", closed: bool = False) -> Dict[str, object]:
            state = _get_popup_layer_state(page)
            return {
                "closed": bool(closed and state.get("closed_confirmed", False)),
                "method": method,
                "overlay_cleared": bool(state.get("closed_confirmed", False)),
                "popup_count": int(state.get("popup_count", 0) or 0),
                "overlay_count": int(state.get("overlay_count", 0) or 0),
            }

        for selector in close_selectors:
            locator = page.locator(selector).first
            if locator.count():
                locator.click(timeout=1200, force=True)
                # navigation이 일어날 수 있으므로 domcontentloaded 까지 대기
                try:
                    page.wait_for_load_state("domcontentloaded", timeout=2000)
                except Exception:
                    pass
                # 팝업 닫힘 확인: 최대 1.6초 폴링 (애니메이션 완료 대기)
                for _ in range(5):
                    page.wait_for_timeout(150)
                    if not _has_open_popup(page):
                        return snapshot(method="close_button", closed=True)
        closed_via_text = page.evaluate(
            """
            () => {
              const layerSels = [
                '[role="dialog"]','[aria-modal="true"]','.modal',
                '[class*="modal"]','[class*="popup"]','[class*="drawer"]',
                '[class*="gateway"]','[id*="gateway"]','[data-section-name="gateway"]',
                '[class*="layer"]','[id*="layer"]',
                '[class*="sheet"]','[class*="flyout"]','[class*="offcanvas"]',
                '[data-type="layer"]','[data-type="modal"]','[class*="bottom-"]'
              ];
              const popupRoots = Array.from(document.querySelectorAll(layerSels.join(','))).filter((node) => {
                const rect = node.getBoundingClientRect();
                const style = window.getComputedStyle(node);
                return rect.width > 24 && rect.height > 24 && style.visibility !== 'hidden' && style.display !== 'none';
              });
              const closeTexts = ['닫기', 'close', '취소', 'cancel', '나중에', 'later', 'skip', '건너뛰기'];
              const clickableSelector = 'button, [role="button"], a, [onclick], [data-qa], [data-button-id]';
              for (const root of popupRoots) {
                const candidates = Array.from(root.querySelectorAll(clickableSelector));
                for (const node of candidates) {
                  const text = String(node.innerText || node.textContent || node.getAttribute('aria-label') || '').trim().toLowerCase();
                  if (!text) continue;
                  if (!closeTexts.some((keyword) => text.includes(keyword))) continue;
                  node.click();
                  return true;
                }
              }
              return false;
            }
            """
        )
        if closed_via_text:
            try:
                page.wait_for_load_state("domcontentloaded", timeout=2000)
            except Exception:
                pass
            for _ in range(5):
                page.wait_for_timeout(150)
                if not _has_open_popup(page):
                    return snapshot(method="close_button_text", closed=True)
        page.keyboard.press("Escape")
        page.wait_for_timeout(300)
        if not _has_open_popup(page):
            return snapshot(method="esc", closed=True)
        backdrop_points = page.evaluate(
            """
            () => {
              const isVisible = (node) => {
                if (!node) return false;
                const rect = node.getBoundingClientRect();
                const style = window.getComputedStyle(node);
                return rect.width > 24 && rect.height > 24 && style.visibility !== 'hidden' && style.display !== 'none' && Number(style.opacity || '1') > 0.02;
              };
              const backdropSelectors = ['[class*="backdrop"]', '[class*="overlay"]', '[class*="dimmed"]', '[class*="scrim"]', '[data-testid*="backdrop"]', '[data-qa*="backdrop"]'];
              const backdrops = Array.from(document.querySelectorAll(backdropSelectors.join(','))).filter(isVisible);
              return backdrops.map((node) => {
                const rect = node.getBoundingClientRect();
                return {
                  x: Math.max(12, Math.round(rect.left + rect.width / 2)),
                  y: Math.max(12, Math.round(rect.top + rect.height / 2))
                };
              });
            }
            """
        )
        if isinstance(backdrop_points, list):
            for point in backdrop_points:
                try:
                    x = float(point.get("x", 0) or 0)
                    y = float(point.get("y", 0) or 0)
                    if x <= 0 or y <= 0:
                        continue
                    page.mouse.click(x, y)
                    page.wait_for_timeout(250)
                    if not _has_open_popup(page):
                        return snapshot(method="backdrop_click", closed=True)
                except Exception:
                    continue
        click_points = page.evaluate(
            """
            () => {
              const layerSels = [
                '[role="dialog"]','[aria-modal="true"]','.modal',
                '[class*="modal"]','[class*="popup"]','[class*="drawer"]',
                '[class*="layer"]','[id*="layer"]','[class*="sheet"]',
                '[class*="flyout"]','[class*="offcanvas"]','[class*="gateway"]'
              ];
              const popupRoots = Array.from(document.querySelectorAll(layerSels.join(','))).filter((node) => {
                const rect = node.getBoundingClientRect();
                const style = window.getComputedStyle(node);
                return rect.width > 24 && rect.height > 24 && style.visibility !== 'hidden' && style.display !== 'none';
              });
              const vw = Math.max(window.innerWidth || 0, document.documentElement.clientWidth || 0, 1);
              const vh = Math.max(window.innerHeight || 0, document.documentElement.clientHeight || 0, 1);
              if (!popupRoots.length) {
                return [];
              }
              const rect = popupRoots[0].getBoundingClientRect();
              const points = [
                { x: Math.max(12, Math.round(rect.left) - 18), y: Math.max(12, Math.round(rect.top) + 18) },
                { x: Math.min(vw - 12, Math.round(rect.right) + 18), y: Math.max(12, Math.round(rect.top) + 18) },
                { x: Math.max(12, Math.round(rect.left) + 18), y: Math.max(12, Math.round(rect.top) - 18) },
                { x: Math.max(12, Math.round(rect.left) + 18), y: Math.min(vh - 12, Math.round(rect.bottom) + 18) },
              ];
              return points.filter((point) => point.x >= 0 && point.y >= 0 && point.x <= vw && point.y <= vh);
            }
            """
        )
        if isinstance(click_points, list):
            for point in click_points:
                try:
                    x = float(point.get("x", 0) or 0)
                    y = float(point.get("y", 0) or 0)
                    if x <= 0 or y <= 0:
                        continue
                    page.mouse.click(x, y)
                    page.wait_for_timeout(250)
                    if not _has_open_popup(page):
                        return snapshot(method="modal_root_outside_click", closed=True)
                except Exception:
                    continue
        return snapshot(method="skip", closed=False)
    except Exception:
        return result


def _is_gateway_open(page) -> bool:
    try:
        return bool(
            page.evaluate(
                """
                () => {
                  const node = document.querySelector('[data-section-name="gateway"]');
                  if (!node) return false;
                  const rect = node.getBoundingClientRect();
                  const style = window.getComputedStyle(node);
                  return (
                    rect.width > 24 &&
                    rect.height > 24 &&
                    style.visibility !== 'hidden' &&
                    style.display !== 'none' &&
                    Number(style.opacity || '1') > 0.02
                  );
                }
                """
            )
        )
    except Exception:
        return False


def _definition_row_requires_popup(row: Dict[str, object]) -> bool:
    if not isinstance(row, dict):
        return False
    haystacks = [
        str(row.get("screen_state", "")).strip().lower(),
        str(row.get("description", "")).strip().lower(),
        str(row.get("event_name", "")).strip().lower(),
        (
            " ".join(
                str(v).strip().lower()
                for v in row.get("text_hints", [])
                if str(v).strip()
            )
            if isinstance(row.get("text_hints", []), list)
            else ""
        ),
    ]
    markers = [
        "modal",
        "popup",
        "dialog",
        "tooltip",
        "drawer",
        "open",
        "layer",
        "sheet",
        "flyout",
        "offcanvas",
        "바텀시트",
        "팝업",
        "툴팁",
        "모달",
        "레이어",
        "시트",
    ]
    return any(
        marker in haystack for haystack in haystacks for marker in markers if haystack
    )


def _get_visible_popup_bbox(page) -> Dict[str, int]:
    try:
        bbox = page.evaluate(
            """
            () => {
              const selectors = [
                '[role="dialog"]',
                '[aria-modal="true"]',
                '.modal',
                '[class*="modal"]',
                '[class*="popup"]',
                '[class*="drawer"]',
                '[class*="gateway"]',
                '[id*="gateway"]',
                '[data-section-name="gateway"]',
              ];
              const nodes = Array.from(document.querySelectorAll(selectors.join(',')));
              for (const node of nodes) {
                const rect = node.getBoundingClientRect();
                const style = window.getComputedStyle(node);
                if (
                  rect.width > 24 &&
                  rect.height > 24 &&
                  style.visibility !== 'hidden' &&
                  style.display !== 'none' &&
                  Number(style.opacity || '1') > 0.02
                ) {
                  return {
                    bbox_x: Math.max(0, Math.round(rect.left)),
                    bbox_y: Math.max(0, Math.round(rect.top)),
                    bbox_width: Math.max(0, Math.round(rect.width)),
                    bbox_height: Math.max(0, Math.round(rect.height)),
                  };
                }
              }
              return { bbox_x: 0, bbox_y: 0, bbox_width: 0, bbox_height: 0 };
            }
            """
        )
        if isinstance(bbox, dict):
            return {
                "bbox_x": int(max(0, bbox.get("bbox_x", 0) or 0)),
                "bbox_y": int(max(0, bbox.get("bbox_y", 0) or 0)),
                "bbox_width": int(max(0, bbox.get("bbox_width", 0) or 0)),
                "bbox_height": int(max(0, bbox.get("bbox_height", 0) or 0)),
            }
    except Exception:
        pass
    return {"bbox_x": 0, "bbox_y": 0, "bbox_width": 0, "bbox_height": 0}


def _make_screenshot_name(
    shot_type: str,
    row_no: object,
    marker: int,
    image_format: str = "png",
) -> str:
    """파일명 포맷: {timestamp}_{type}_{rowNo}_{marker}.{ext}
    예) 20260313_110021_123456_click_03_12.webp
    """
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")[:-3]
    ext = ".jpg" if image_format == "jpeg" else f".{image_format}"
    row = str(row_no or "00").strip().zfill(2)
    mk = str(marker).zfill(2)
    return f"{ts}_{shot_type}_{row}_{mk}{ext}"


def _capture_runtime_probe(
    page,
    screenshots_dir: Path,
    shot_type: str,
    marker_index: int,
    label: str,
    bbox: Optional[Dict[str, int]] = None,
    meta: Optional[Dict[str, object]] = None,
    *,
    row_no: object = None,
    enabled: bool = True,
    save_raw: bool = False,
    phase: str = "warning",
    image_format: str = "png",
    quality: int = 85,
) -> Dict[str, object]:
    """실패/이상 상황 캡처. enabled=True이면 yellow overlay + annotated 1장 저장."""
    safe_bbox = bbox or {"bbox_x": 0, "bbox_y": 0, "bbox_width": 0, "bbox_height": 0}
    normalized_bbox = {
        "bbox_x": int(max(0, safe_bbox.get("bbox_x", 0) or 0)),
        "bbox_y": int(max(0, safe_bbox.get("bbox_y", 0) or 0)),
        "bbox_width": int(max(0, safe_bbox.get("bbox_width", 0) or 0)),
        "bbox_height": int(max(0, safe_bbox.get("bbox_height", 0) or 0)),
    }

    if not enabled:
        return {
            "screenshot_path": "",
            "raw_screenshot_file": "",
            "annotation_no": str(marker_index),
            "bounding_box": {
                "x": int(normalized_bbox.get("bbox_x", 0)),
                "y": int(normalized_bbox.get("bbox_y", 0)),
                "width": int(normalized_bbox.get("bbox_width", 0)),
                "height": int(normalized_bbox.get("bbox_height", 0)),
            },
            **normalized_bbox,
        }

    screenshot_name = _make_screenshot_name(
        shot_type, row_no, marker_index, image_format
    )
    screenshot_path = screenshots_dir / screenshot_name
    raw_screenshot_file = ""

    # raw(오버레이 없는 원본)를 먼저 저장
    if save_raw:
        try:
            raw_path = screenshot_path.with_name(
                f"{screenshot_path.stem}_raw{screenshot_path.suffix}"
            )
            raw_path.parent.mkdir(parents=True, exist_ok=True)
            page.screenshot(
                path=str(raw_path),
                full_page=False,
                type=image_format,
                quality=quality if image_format in {"jpeg", "webp"} else None,
            )
            raw_screenshot_file = str(raw_path)
        except Exception:
            raw_screenshot_file = ""

    # overlay 표시 후 annotated 캡처
    _show_target_overlay(
        page,
        normalized_bbox,
        marker_index,
        meta or {"selector": label, "target_id": label},
        phase=phase,
    )
    try:
        page.wait_for_timeout(180)
    except Exception:
        pass

    try:
        _, normalized_bbox = _capture_annotated_screenshot(
            page,
            screenshot_path,
            normalized_bbox,
            marker_index,
            label,
            save_raw=False,
            image_format=image_format,
            quality=quality,
        )
    except Exception:
        pass

    return {
        "screenshot_path": str(screenshot_path).strip(),
        "raw_screenshot_file": str(raw_screenshot_file).strip(),
        "annotation_no": str(marker_index),
        "bounding_box": {
            "x": int(normalized_bbox.get("bbox_x", 0)),
            "y": int(normalized_bbox.get("bbox_y", 0)),
            "width": int(normalized_bbox.get("bbox_width", 0)),
            "height": int(normalized_bbox.get("bbox_height", 0)),
        },
        **normalized_bbox,
    }


def _scroll_page_for_candidate_search(page) -> Dict[str, object]:
    try:
        return page.evaluate(
            """
            () => {
              const viewportHeight = Math.max(window.innerHeight || 0, document.documentElement.clientHeight || 0, 1);
              const scrollTop = window.scrollY || window.pageYOffset || 0;
              const maxScrollTop = Math.max(
                0,
                (document.documentElement.scrollHeight || document.body.scrollHeight || 0) - viewportHeight
              );
              const nextTop = Math.min(maxScrollTop, scrollTop + Math.max(240, Math.floor(viewportHeight * 0.72)));
              window.scrollTo({ top: nextTop, behavior: "instant" });
              return {
                moved: nextTop > scrollTop,
                from_top: Math.round(scrollTop),
                to_top: Math.round(nextTop),
                max_top: Math.round(maxScrollTop),
              };
            }
            """
        )
    except Exception:
        return {"moved": False, "from_top": 0, "to_top": 0, "max_top": 0}


def _collect_view_candidate_signature(page) -> str:
    try:
        payload = page.evaluate(
            """
            () => {
              const isVisible = (el) => {
                if (!(el instanceof Element)) return false;
                const rect = el.getBoundingClientRect();
                if (rect.width < 12 || rect.height < 12) return false;
                if (rect.bottom <= 0 || rect.top >= (window.innerHeight || 0)) return false;
                const style = window.getComputedStyle(el);
                if (!style) return false;
                if (style.display === "none" || style.visibility === "hidden") return false;
                if (Number(style.opacity || "1") === 0) return false;
                return true;
              };
              const nodes = Array.from(
                document.querySelectorAll(
                  "[data-qa], [data-button-id], button, a.gtm-click-button, a.gtm-click-content, a[href], [role='button'], input[type='button'], input[type='submit'], [onclick]"
                )
              ).filter(isVisible);
              const keys = nodes.slice(0, 16).map((el) => {
                const section = String(el.getAttribute("data-section-name") || "").trim();
                const button = String(el.getAttribute("data-button-id") || "").trim();
                const idx = String(el.getAttribute("data-index") || "").trim();
                const role = String(el.getAttribute("role") || "").trim();
                const text = String((el.textContent || "")).replace(/\\s+/g, " ").trim().toLowerCase().slice(0, 40);
                return `${button}|${section}|${idx}|${role}|${text}`;
              });
              return keys.join("||");
            }
            """
        )
        return str(payload or "").strip()
    except Exception:
        return ""


def _auto_expand_ui_containers(page) -> Dict[str, object]:
    try:
        payload = page.evaluate(
            """
            () => {
              const isVisible = (el) => {
                if (!(el instanceof Element)) return false;
                const rect = el.getBoundingClientRect();
                if (rect.width < 8 || rect.height < 8) return false;
                if (rect.bottom <= 0 || rect.top >= (window.innerHeight || 0)) return false;
                const style = window.getComputedStyle(el);
                if (!style) return false;
                if (style.display === "none" || style.visibility === "hidden") return false;
                if (Number(style.opacity || "1") === 0) return false;
                return true;
              };
              const isEnabled = (el) => {
                if (!el) return false;
                if (el.hasAttribute && (el.hasAttribute("disabled") || el.getAttribute("aria-disabled") === "true")) {
                  return false;
                }
                return true;
              };
              const getActiveTabState = () => {
                try {
                  const sel = [
                    ".gtm-click-button[data-section-name*='tab']",
                    "[role='tab']",
                    "[data-button-id*='tab']",
                    "[data-button-id^='theme_']",
                    "[data-button-id^='category_tab_']",
                  ].join(",");
                  const nodes = Array.from(document.querySelectorAll(sel));
                  const sig = [];
                  for (const node of nodes) {
                    const target = node.closest
                      ? (node.closest("button,a[href],[role='button'],[onclick],.gtm-click-button,[data-button-id]") || node)
                      : node;
                    if (!target || !isVisible(target)) continue;
                    const ariaSelected = String(target.getAttribute("aria-selected") || "").trim().toLowerCase();
                    const ariaCurrent = String(target.getAttribute("aria-current") || "").trim().toLowerCase();
                    const cls = String(target.className || "").toLowerCase();
                    const hasUnderline = Boolean(target.querySelector && target.querySelector(".underline, [class*='underline']"));
                    const activeByClass = /(active|selected|current|\\bon\\b)/.test(cls);
                    const activeByStyleHint = (cls.includes("text-black") && !cls.includes("text-gray"));
                    const active = (
                      ariaSelected === "true" ||
                      (ariaCurrent && ariaCurrent !== "false") ||
                      activeByClass ||
                      hasUnderline ||
                      activeByStyleHint
                    );
                    if (!active) continue;
                    const sectionName = String(target.getAttribute("data-section-name") || "").trim();
                    const buttonId = String(target.getAttribute("data-button-id") || "").trim();
                    const buttonName = String(target.getAttribute("data-button-name") || "").trim();
                    const idx = String(target.getAttribute("data-index") || "").trim();
                    const txt = String((target.textContent || "")).replace(/\\s+/g, " ").trim().slice(0, 24);
                    sig.push(`${sectionName}|${buttonId || buttonName || idx || txt}`);
                  }
                  return Array.from(new Set(sig)).slice(0, 12).join(" > ");
                } catch (e) {
                  return "";
                }
              };
              const markAndClick = (el, kind, keyHint = "") => {
                try {
                  if (!el || !isVisible(el) || !isEnabled(el)) return false;
                  const sectionName = String(el.getAttribute("data-section-name") || "").trim();
                  const buttonId = String(el.getAttribute("data-button-id") || "").trim();
                  const buttonName = String(el.getAttribute("data-button-name") || "").trim();
                  const idx = String(el.getAttribute("data-index") || "").trim();
                  const textKey = String((el.textContent || "")).replace(/\s+/g, " ").trim().slice(0, 40);
                  const stateCtx = kind === "tab" ? getActiveTabState() : "";
                  const key = keyHint || `${kind}|${sectionName}|${idx || buttonId || buttonName || textKey}|ctx:${stateCtx}`;
                  try {
                    if (!window.__qaAutoExpandKeys || !(window.__qaAutoExpandKeys instanceof Set)) {
                      window.__qaAutoExpandKeys = new Set();
                    }
                    if (window.__qaAutoExpandKeys.has(key)) return false;
                    window.__qaAutoExpandKeys.add(key);
                  } catch (e) {}
                  if (String(el.getAttribute("data-qa-auto-expanded") || "") === "1") return false;
                  el.setAttribute("data-qa-auto-expanded", "1");
                  el.scrollIntoView({ block: "center", inline: "nearest", behavior: "instant" });
                  el.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true, view: window }));
                  return true;
                } catch (e) {
                  return false;
                }
              };
              const out = { expanded_count: 0, tab_count: 0, accordion_count: 0, keys: [] };

              // 1) 탭 우선: 비활성 탭만 소수 클릭
              const tabSelectors = [
                "[role='tab'][aria-selected='false']",
                "[data-button-id='tab']",
                "[data-button-id*='tab']",
                "[data-button-id^='theme_']",
                "[data-button-id^='category_tab_']",
                ".gtm-click-button[data-button-id^='theme_']",
                ".gtm-click-button[data-button-id^='category_tab_']",
                ".gtm-click-button[data-section-name*='tab']",
                "[data-mds='TabTextItem']",
                ".gtm-click-button[data-button-id='tab']",
              ];
              const tabNodesRaw = Array.from(document.querySelectorAll(tabSelectors.join(",")));
              const tabNodes = [];
              const tabSeenNodes = new Set();
              for (const rawNode of tabNodesRaw) {
                const target = rawNode && rawNode.closest
                  ? (rawNode.closest("button,a[href],[role='button'],[onclick],.gtm-click-button,[data-button-id]") || rawNode)
                  : rawNode;
                if (!target || tabSeenNodes.has(target)) continue;
                tabSeenNodes.add(target);
                tabNodes.push(target);
              }
              const tabCandidates = [];
              for (const node of tabNodes) {
                if (out.tab_count >= 1) break;
                const ariaSelected = String(node.getAttribute("aria-selected") || "").trim().toLowerCase();
                if (ariaSelected === "true") continue;
                const ariaCurrent = String(node.getAttribute("aria-current") || "").trim().toLowerCase();
                if (ariaCurrent && ariaCurrent !== "false") continue;
                const cls = String(node.className || "").toLowerCase();
                if (cls.includes("carousel-arrow-button")) continue;
                if (/(active|selected|current|on)/.test(cls) && ariaSelected !== "false") continue;
                const sectionName = String(node.getAttribute("data-section-name") || "").trim();
                const buttonId = String(node.getAttribute("data-button-id") || "").trim();
                const buttonName = String(node.getAttribute("data-button-name") || "").trim();
                const idx = String(node.getAttribute("data-index") || "").trim();
                const txt = String((node.textContent || "")).replace(/\\s+/g, " ").trim().slice(0, 30);
                const depthHint = `${sectionName}|${buttonId}`.toLowerCase();
                const depth = depthHint.includes("3depth") ? 3 : (depthHint.includes("2depth") ? 2 : (depthHint.includes("1depth") ? 1 : 0));
                const stateCtx = getActiveTabState();
                const candidateKey = `tab|${sectionName}|${idx || buttonName || buttonId || txt}|ctx:${stateCtx}`;
                tabCandidates.push({ node, sectionName, idx, buttonName, buttonId, txt, depth, candidateKey });
              }
              tabCandidates.sort((a, b) => {
                if (b.depth !== a.depth) return b.depth - a.depth;
                const aIdx = Number.parseInt(a.idx || "0", 10);
                const bIdx = Number.parseInt(b.idx || "0", 10);
                if (Number.isFinite(aIdx) && Number.isFinite(bIdx) && aIdx !== bIdx) return aIdx - bIdx;
                return String(a.txt || "").localeCompare(String(b.txt || ""));
              });
              for (const cand of tabCandidates) {
                if (out.tab_count >= 1) break;
                if (markAndClick(cand.node, "tab", cand.candidateKey)) {
                  out.tab_count += 1;
                  out.expanded_count += 1;
                  out.keys.push(`tab:${cand.sectionName}|${cand.idx || cand.buttonName || cand.txt}|d${cand.depth}`);
                }
              }

              // 2) 아코디언/접힘 영역: aria-expanded=false 기반
              const accSelectors = [
                "button[aria-expanded='false']",
                "[role='button'][aria-expanded='false']",
                "[data-expanded='false']",
                "summary",
              ];
              const accNodes = Array.from(document.querySelectorAll(accSelectors.join(",")));
              for (const node of accNodes) {
                if (out.accordion_count >= 2) break;
                const txt = String((node.textContent || "")).replace(/\\s+/g, " ").trim().toLowerCase();
                if (txt && /(logout|delete|remove|탈퇴|삭제)/.test(txt)) continue;
                const ariaExpanded = String(node.getAttribute("aria-expanded") || "").trim().toLowerCase();
                if (ariaExpanded === "true") continue;
                if (markAndClick(node, "accordion")) {
                  out.accordion_count += 1;
                  out.expanded_count += 1;
                  out.keys.push(`accordion:${txt.slice(0, 30)}`);
                }
              }
              return out;
            }
            """
        )
        if isinstance(payload, dict):
            return payload
    except Exception:
        pass
    return {"expanded_count": 0, "tab_count": 0, "accordion_count": 0, "keys": []}


def _find_best_candidate(
    page,
    candidate_selectors: List[str],
    definition_targets: List[str],
    current_definition_hints: Dict[str, List[str]],
    seen_pattern_keys: set[str],
    interaction_excluded_keys: Optional[set[str]] = None,
    pattern_hit_stats: Optional[Dict[str, Dict[str, int]]] = None,
    scope_selectors: Optional[List[str]] = None,
    prefer_scoped_candidates: bool = False,
    excluded_content_sections: Optional[set[str]] = None,
    content_section_click_counts: Optional[Dict[str, int]] = None,
    content_section_click_budget: int = 2,
    seen_section_names: Optional[set[str]] = None,
    blocked_section_names: Optional[set[str]] = None,
    blocked_selectors: Optional[set[str]] = None,
    blocked_href_paths: Optional[set[str]] = None,
    global_fallback_mode: bool = False,
    return_debug: bool = False,
    row_expected_kind: str = "",
    enforce_main_area: bool = True,
):
    selected_handle = None
    selected_meta: Dict[str, object] = {}
    selected_bbox: Dict[str, int] = {}
    selected_hint_score = -999.0
    excluded_keys = interaction_excluded_keys or set()
    excluded_sections = {str(v).strip() for v in (excluded_content_sections or set()) if str(v).strip()}
    section_click_counts = dict(content_section_click_counts or {})
    seen_sections = {str(v).strip() for v in (seen_section_names or set()) if str(v).strip()}
    blocked_sections = {
        str(v).strip().lower()
        for v in (blocked_section_names or set())
        if str(v).strip()
    }
    blocked_selector_set = {
        str(v).strip() for v in (blocked_selectors or set()) if str(v).strip()
    }
    blocked_href_set = {
        str(v).strip().lower()
        for v in (blocked_href_paths or set())
        if str(v).strip()
    }
    stats = pattern_hit_stats or {}
    search_scopes = list(scope_selectors or [""])
    candidate_rows: List[tuple] = []
    seen_local_patterns: set[str] = set()
    for scope_selector in search_scopes:
        for selector in candidate_selectors:
            scoped_selector = (
                f"{scope_selector} {selector}".strip() if scope_selector else selector
            )
            handles = page.locator(scoped_selector).element_handles()
            for handle in handles:
                try:
                    meta = _extract_candidate_metadata(handle)
                    if not meta:
                        continue
                    if bool(meta.get("auto_crawl_ignore", False)):
                        continue
                    if bool(meta.get("hidden", False)) or bool(meta.get("disabled", False)):
                        continue
                    if _is_risky_candidate(meta):
                        continue
                    if enforce_main_area and not bool(meta.get("in_main_area", False)):
                        continue
                    if bool(meta.get("in_global_area", False)):
                        continue
                    if bool(meta.get("in_floating_area", False)) and str(row_expected_kind or "").strip().lower() != "tooltip":
                        continue
                    if str(meta.get("data_qa_priority", "")).strip().lower() == "global":
                        continue
                    bbox = {
                        "x": float(meta.get("bbox_x", 0) or 0),
                        "y": float(meta.get("bbox_y", 0) or 0),
                        "width": float(meta.get("bbox_width", 0) or 0),
                        "height": float(meta.get("bbox_height", 0) or 0),
                    }
                    if (
                        float(bbox.get("width", 0)) < 12
                        or float(bbox.get("height", 0)) < 12
                    ):
                        continue
                    interaction_keys = _candidate_interaction_keys(meta)
                    if excluded_keys and interaction_keys and bool(interaction_keys & excluded_keys):
                        continue
                    section_name = str(meta.get("section_name", "")).strip()
                    section_name_norm = section_name.lower()
                    if section_name_norm and section_name_norm in blocked_sections:
                        continue
                    selector_raw = str(meta.get("selector", "")).strip()
                    if selector_raw and selector_raw in blocked_selector_set:
                        continue
                    href_key = _href_path_key(str(meta.get("href", "")).strip())
                    if href_key and href_key.lower() in blocked_href_set:
                        continue
                    expected_sections_norm = {
                        str(v).strip().lower()
                        for v in current_definition_hints.get("section_names", [])
                        if str(v).strip()
                    }
                    if (
                        section_name_norm
                        and _is_global_noise_section(section_name_norm)
                        and section_name_norm not in expected_sections_norm
                    ):
                        continue
                    if (
                        definition_targets
                        and expected_sections_norm
                        and section_name_norm
                        and section_name_norm not in expected_sections_norm
                    ):
                        continue
                    if row_expected_kind and row_expected_kind != "generic":
                        cand_kind = _candidate_kind(meta)
                        if cand_kind != row_expected_kind:
                            continue
                    if (
                        section_name
                        and excluded_sections
                        and section_name in excluded_sections
                        and _is_content_like_candidate(meta)
                    ):
                        continue
                    if (
                        section_name
                        and _is_content_like_candidate(meta)
                        and int(section_click_counts.get(section_name, 0)) >= int(max(1, content_section_click_budget))
                    ):
                        continue
                    hint_score = 0.0
                    if definition_targets:
                        scored = _score_candidate_definition_match(
                            meta, current_definition_hints
                        )
                        if not bool(scored.get("accepted", False)):
                            continue
                        hint_score = float(scored.get("score", 0.0))
                        if prefer_scoped_candidates and scope_selector:
                            hint_score += 1.0
                    text = str(meta.get("text", "")).strip()
                    tag = str(meta.get("tag", "")).strip().lower()
                    target_id = str(meta.get("target_id", "")).strip()
                    href = str(meta.get("href", "")).strip()
                    # Allow text-less anchors/semantic controls if they still have stable identity.
                    if (
                        not text
                        and tag not in {"button", "input", "a"}
                        and not target_id
                        and not href
                    ):
                        continue
                    dedup_key = _candidate_dedup_key(meta)
                    if dedup_key and dedup_key in seen_pattern_keys:
                        continue
                    pattern_key = (
                        str(meta.get("pattern_key", "")).strip()
                        or str(meta.get("structure_key", "")).strip()
                        or str(meta.get("selector_pattern", "")).strip()
                    )
                    if pattern_key and pattern_key in seen_local_patterns:
                        continue
                    if pattern_key:
                        seen_local_patterns.add(pattern_key)
                    attempts = int((stats.get(pattern_key, {}) or {}).get("attempts", 0))
                    hits = int((stats.get(pattern_key, {}) or {}).get("hits", 0))
                    history_bias = 0.0
                    if attempts > 0 and hits == 0:
                        history_bias -= 2.5
                    elif hits > 0:
                        history_bias += 0.6
                    if _looks_like_tab_candidate(meta):
                        history_bias += 1.0
                    elif _is_content_like_candidate(meta):
                        history_bias -= 0.2
                    # section_name 단위로 폭넓게 커버하도록 미탐색 section 우선
                    if section_name:
                        if section_name not in seen_sections:
                            history_bias += 1.4
                        else:
                            history_bias -= 0.4
                    area = float(bbox.get("width", 0)) * float(bbox.get("height", 0))
                    area_bias = max(-0.6, min(0.6, (24000.0 - area) / 24000.0))
                    total_score = float(hint_score) + history_bias + area_bias
                    candidate_rows.append(
                        (
                            total_score,
                            float(hint_score),
                            handle,
                            meta,
                            {
                                "bbox_x": int(max(0, bbox.get("x", 0))),
                                "bbox_y": int(max(0, bbox.get("y", 0))),
                                "bbox_width": int(max(0, bbox.get("width", 0))),
                                "bbox_height": int(max(0, bbox.get("height", 0))),
                            },
                        )
                    )
                except Exception:
                    continue
    if candidate_rows:
        candidate_rows.sort(key=lambda row: (row[0], row[1]), reverse=True)
        _, selected_hint_score, selected_handle, selected_meta, selected_bbox = candidate_rows[0]
    if not return_debug:
        return selected_handle, selected_meta, selected_bbox, selected_hint_score
    top_candidates: List[Dict[str, object]] = []
    sorted_rows = sorted(candidate_rows, key=lambda row: (row[0], row[1]), reverse=True)
    for item in sorted_rows[:5]:
        total_score, hint_score, _h, meta, _bbox = item
        section = str(meta.get("section_name", "")).strip()
        top_candidates.append(
            {
                "score": float(total_score),
                "hint_score": float(hint_score),
                "section_name": section,
                "selector": str(meta.get("selector", "")).strip(),
                "target_id": str(meta.get("target_id", "")).strip(),
                "text": str(meta.get("text", "")).strip()[:80],
                "is_global_section": bool(_is_global_noise_section(section)),
            }
        )
    best_score = float(sorted_rows[0][0]) if sorted_rows else -999.0
    second_score = float(sorted_rows[1][0]) if len(sorted_rows) > 1 else -999.0
    debug_payload = {
        "candidate_count": int(len(sorted_rows)),
        "best_score": best_score,
        "second_score": second_score,
        "score_gap": float(best_score - second_score) if len(sorted_rows) > 1 else 999.0,
        "top_candidates": top_candidates,
        "global_fallback_mode": bool(global_fallback_mode),
    }
    return selected_handle, selected_meta, selected_bbox, selected_hint_score, debug_payload


def _emit_auto_crawl_event(
    session_id: str, event_name: str, page_url: str, params: Dict[str, object]
) -> None:
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


def _detect_access_challenge(page) -> str:
    try:
        title = str(page.title() or "").strip().lower()
    except Exception:
        title = ""
    try:
        body_text = (
            str(page.locator("body").inner_text(timeout=1500) or "").strip().lower()
        )
    except Exception:
        body_text = ""
    haystack = " ".join([title, body_text])
    try:
        dom_challenge_count = int(
            page.evaluate(
                """
                () => {
                  const sels = [
                    'iframe[src*="recaptcha"]',
                    'iframe[src*="hcaptcha"]',
                    'iframe[src*="turnstile"]',
                    '.g-recaptcha',
                    '.h-captcha',
                    '[data-sitekey]',
                    '[name="cf-turnstile-response"]',
                    '[id*="challenge"]',
                    '[class*="challenge"]',
                    '[class*="captcha"]',
                    '[id*="captcha"]'
                  ];
                  let count = 0;
                  for (const sel of sels) {
                    count += document.querySelectorAll(sel).length;
                  }
                  return count;
                }
                """
            )
            or 0
        )
    except Exception:
        dom_challenge_count = 0

    strong_text_markers = [
        "verify you are human",
        "are you human",
        "captcha verification",
        "cloudflare",
        "unusual traffic",
        "access denied",
        "비정상적인 접근",
        "봇인지 확인",
    ]
    weak_text_markers = [
        "captcha",
        "robot",
        "bot",
        "접속할 수 없습니다",
        "자동 접속",
    ]

    for marker in strong_text_markers:
        if marker in haystack:
            return marker
    if dom_challenge_count > 0:
        for marker in weak_text_markers:
            if marker in haystack:
                return marker
        if dom_challenge_count >= 2:
            return "challenge_dom_detected"
    return ""


def _open_session_page_with_retry(
    page,
    session_id: str,
    target_url: str,
    debug_url: str,
) -> str:
    attempts = [
        {"url": debug_url, "wait_until": "domcontentloaded", "timeout_ms": 30000},
        {"url": debug_url, "wait_until": "commit", "timeout_ms": 20000},
        {"url": target_url, "wait_until": "domcontentloaded", "timeout_ms": 25000},
        {"url": target_url, "wait_until": "commit", "timeout_ms": 18000},
    ]
    last_error = ""
    for idx, attempt in enumerate(attempts, start=1):
        url = str(attempt.get("url", "")).strip() or target_url
        wait_until = str(attempt.get("wait_until", "domcontentloaded")).strip()
        timeout_ms = int(attempt.get("timeout_ms", 30000) or 30000)
        try:
            page.goto(url, wait_until=wait_until, timeout=timeout_ms)
            try:
                page.wait_for_timeout(450)
            except Exception:
                pass
            challenge = _detect_access_challenge(page)
            if challenge and idx < len(attempts):
                _emit_auto_crawl_event(
                    session_id,
                    "auto_open_retry_challenge",
                    page.url or url,
                    {
                        "attempt": idx,
                        "attempt_total": len(attempts),
                        "open_url": url,
                        "wait_until": wait_until,
                        "timeout_ms": timeout_ms,
                        "challenge_marker": challenge,
                    },
                )
                continue
            return url
        except Exception as exc:
            last_error = str(exc)
            _emit_auto_crawl_event(
                session_id,
                "auto_open_retry_failed",
                url,
                {
                    "attempt": idx,
                    "attempt_total": len(attempts),
                    "open_url": url,
                    "wait_until": wait_until,
                    "timeout_ms": timeout_ms,
                    "error": last_error,
                },
            )
            continue
    raise RuntimeError(last_error or "page_open_failed")


def _show_target_overlay(
    page,
    bbox: Dict[str, int],
    marker_index: int,
    meta: Dict[str, object],
    phase: str = "target",
    event_name: str = "",
    event_type: str = "",
) -> None:
    try:
        label = (
            str(meta.get("selector", "")).strip()
            or str(meta.get("target_id", "")).strip()
            or str(meta.get("text", "")).strip()
        )
        label = label[:96]
        page.evaluate(
            """
            ({ bbox, markerIndex, label, phase, eventName, eventType }) => {
              const overlayId = "__qaAutoTargetOverlay";
              let root = document.getElementById(overlayId);
              if (!root) {
                root = document.createElement("div");
                root.id = overlayId;
                root.style.position = "fixed";
                root.style.inset = "0";
                root.style.pointerEvents = "none";
                root.style.zIndex = "2147483645";
                document.documentElement.appendChild(root);
              }
              root.innerHTML = "";
              const x = Math.max(0, Number(bbox.x || 0));
              const y = Math.max(0, Number(bbox.y || 0));
              const width = Math.max(0, Number(bbox.width || 0));
              const height = Math.max(0, Number(bbox.height || 0));
              const et = String(eventType || "").toLowerCase().trim();
              const isImpression = et === "impression" || et === "view";
              const base = isImpression
                ? { border: "#f97316", shadow: "rgba(249,115,22,.30)", bg: "rgba(249,115,22,.08)" }
                : { border: "#ef4444", shadow: "rgba(239,68,68,.28)", bg: "rgba(239,68,68,.08)" };
              const C = (phase === "warning" || phase === "popup" || phase === "skip")
                  ? { border: "#d97706", shadow: "rgba(217,119,6,.35)", bg: "rgba(217,119,6,.08)" }
                  : base;

              const box = document.createElement("div");
              box.style.position = "fixed";
              box.style.left = `${x}px`;
              box.style.top = `${y}px`;
              box.style.width = `${width}px`;
              box.style.height = `${height}px`;
              box.style.boxSizing = "border-box";
              box.style.border = `4px solid ${C.border}`;
              box.style.boxShadow = `0 0 0 3px rgba(255,255,255,.92), 0 0 0 8px ${C.shadow}`;
              box.style.background = C.bg;
              box.style.borderRadius = "8px";
              root.appendChild(box);

              const badge = document.createElement("div");
              const eventPrefix = String(eventName || "").trim();
              badge.textContent = `${eventPrefix ? eventPrefix + " " : ""}#${markerIndex} ${label}`;
              badge.style.position = "fixed";
              badge.style.left = `${Math.max(8, x)}px`;
              badge.style.top = `${Math.max(8, y - 34)}px`;
              badge.style.maxWidth = "420px";
              badge.style.padding = "6px 10px";
              badge.style.borderRadius = "999px";
              badge.style.background = C.border;
              badge.style.color = "#fff";
              badge.style.font = "700 12px/1.2 -apple-system, BlinkMacSystemFont, Segoe UI, sans-serif";
              badge.style.boxShadow = "0 8px 24px rgba(15,23,42,.28)";
              badge.style.whiteSpace = "nowrap";
              badge.style.overflow = "hidden";
              badge.style.textOverflow = "ellipsis";
              root.appendChild(badge);

              clearTimeout(window.__qaAutoTargetOverlayTimer);
              window.__qaAutoTargetOverlayTimer = setTimeout(() => {
                const current = document.getElementById(overlayId);
                if (current) current.innerHTML = "";
              }, phase === "clicked" ? 1800 : 1600);
            }
            """,
            {
                "bbox": {
                    "x": int(bbox.get("bbox_x", 0)),
                    "y": int(bbox.get("bbox_y", 0)),
                    "width": int(bbox.get("bbox_width", 0)),
                    "height": int(bbox.get("bbox_height", 0)),
                },
                "markerIndex": int(marker_index),
                "label": label,
                "phase": str(phase or "target"),
                "eventName": str(event_name or "").strip(),
                "eventType": str(event_type or "").strip(),
            },
        )
    except Exception:
        return


def _parse_precondition_steps(row: Dict[str, object]) -> List[Dict[str, object]]:
    if not isinstance(row, dict):
        return []
    prebuilt = row.get("precondition_steps", None)
    if isinstance(prebuilt, list) and prebuilt:
        return [dict(step) for step in prebuilt if isinstance(step, dict)]
    raw = row.get("preconditions", "")
    steps: List[Dict[str, object]] = []
    payload = raw
    if isinstance(payload, str):
        text = payload.strip()
        if text:
            try:
                payload = json.loads(text)
            except Exception:
                payload = text
    if isinstance(payload, dict):
        items = payload.get("steps", payload.get("actions", []))
        if isinstance(items, list):
            payload = items
    if isinstance(payload, list):
        for item in payload:
            if isinstance(item, dict):
                steps.append(dict(item))
            elif isinstance(item, str) and item.strip():
                steps.append({"action": "click", "target_hint": item.strip()})
    elif isinstance(payload, str) and payload.strip():
        for token in [t.strip() for t in payload.split(";") if t.strip()]:
            steps.append({"action": "click", "target_hint": token})
    if not steps:
        hint = str(row.get("action_target_hint", "")).strip()
        if hint:
            steps.append({"action": "click", "target_hint": hint})
    normalized: List[Dict[str, object]] = []
    for step in steps:
        if not isinstance(step, dict):
            continue
        action = str(step.get("action", "click") or "click").strip().lower()
        target_hint = str(step.get("target_hint", step.get("hint", "") or "")).strip()
        selector = str(step.get("selector", "")).strip()
        wait_ms = int(step.get("wait_ms", step.get("post_wait_ms", 600)) or 600)
        normalized.append(
            {
                "action": action or "click",
                "target_hint": target_hint,
                "selector": selector,
                "wait_ms": wait_ms,
            }
        )
    return normalized


def _apply_precondition_step(
    page,
    step: Dict[str, object],
    candidate_selectors: List[str],
    scope_prefix: str,
    base_url: str,
    block_link_navigation: bool,
) -> bool:
    action = str(step.get("action", "click") or "click").strip().lower()
    target_hint = str(step.get("target_hint", "")).strip()
    selector = str(step.get("selector", "")).strip()
    handle = None
    meta: Dict[str, object] = {}
    if selector:
        try:
            handle = page.locator(selector).element_handle()
        except Exception:
            handle = None
    if handle is None and target_hint:
        hints = {
            "target_ids": [],
            "section_names": [],
            "page_ids": [],
            "text_hints": [target_hint],
            "screen_states": [],
        }
        handle, meta, _, _ = _find_best_candidate(
            page,
            candidate_selectors,
            definition_targets=[],
            current_definition_hints=hints,
            seen_pattern_keys=set(),
            scope_selectors=[""],
            prefer_scoped_candidates=False,
        )
    if handle is None:
        return False
    if action not in {"click", "select_tab", "toggle_on", "toggle_off"}:
        return False
    if action == "select_tab":
        # 탭이 이미 활성화된 경우 클릭하지 않음
        try:
            active = handle.evaluate(
                """
                (el) => {
                  const aria = (el.getAttribute && (el.getAttribute('aria-selected'))) || '';
                  if (aria === 'true') return true;
                  const cls = (el.className || '').toString().toLowerCase();
                  if (/(active|on|selected|current)/.test(cls)) return true;
                  return false;
                }
                """
            )
            if active:
                return True
        except Exception:
            pass
    try:
        handle.scroll_into_view_if_needed(timeout=800)
    except Exception:
        pass
    try:
        meta = meta or _extract_candidate_metadata(handle)
    except Exception:
        meta = meta or {}
    if action in {"toggle_on", "toggle_off"}:
        desired = action == "toggle_on"
        try:
            state = handle.evaluate(
                """
                (el) => {
                  const aria = (el.getAttribute && (el.getAttribute('aria-pressed') || el.getAttribute('aria-checked'))) || '';
                  if (aria === 'true') return true;
                  if (aria === 'false') return false;
                  const cls = (el.className || '').toString().toLowerCase();
                  if (/(active|on|selected|checked)/.test(cls)) return true;
                  return null;
                }
                """
            )
            if state is not None and state == desired:
                return True
        except Exception:
            pass
    href = str(meta.get("href", "")).strip()
    if href and (
        block_link_navigation
        or (scope_prefix and not _is_url_in_scope(href, base_url, scope_prefix))
    ):
        try:
            handle.evaluate(
                """
                (el) => {
                  const blocker = (event) => event.preventDefault();
                  el.addEventListener("click", blocker, { capture: true, once: true });
                  el.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true, view: window }));
                }
                """
            )
            return True
        except Exception:
            return False
    try:
        handle.click(timeout=1800)
        return True
    except Exception:
        try:
            handle.click(timeout=2500, force=True)
            return True
        except Exception:
            return False


def _run_preconditions(
    page,
    session_id: str,
    row: Dict[str, object],
    candidate_selectors: List[str],
    scope_prefix: str,
    base_url: str,
    block_link_navigation: bool,
) -> bool:
    row_id = str(row.get("definition_row_id", "")).strip()
    with _LOCK:
        session = _SESSIONS.get(str(session_id or "").strip())
        selected_profile = (
            str(getattr(session, "manual_profile_selected", "default") or "default")
            .strip()
            or "default"
        )
        session_run_settings = (
            dict(getattr(session, "run_settings", {}) or {})
            if session and isinstance(getattr(session, "run_settings", {}), dict)
            else {}
        )
    replay_enabled = (
        bool(getattr(session, "manual_flow_replay_enabled", True))
        if session
        else bool(session_run_settings.get("manual_flow_replay_enabled", True))
    )
    steps = _parse_precondition_steps(row)
    manual_steps = _load_manual_steps_for_row(session_run_settings, selected_profile, row_id) if replay_enabled else []
    if manual_steps:
        merged_steps: List[Dict[str, object]] = []
        seen_keys: set[tuple[str, str, str]] = set()
        for step in list(manual_steps) + list(steps):
            if not isinstance(step, dict):
                continue
            step_key = (
                str(step.get("action", "click")).strip().lower(),
                str(step.get("selector", "")).strip(),
                str(step.get("target_hint", "")).strip(),
            )
            if step_key in seen_keys:
                continue
            seen_keys.add(step_key)
            merged_steps.append(step)
        steps = merged_steps
    if not steps:
        return True
    _emit_auto_crawl_event(
        session_id,
        "auto_crawl_preconditions_start",
        page.url,
        {
            "current_definition_row_id": str(row.get("definition_row_id", "")).strip(),
            "current_definition_row_no": str(row.get("no", "")).strip(),
            "current_definition_event_name": str(row.get("event_name", "")).strip(),
            "steps": steps,
            "run_mode": "Auto Crawl",
        },
    )
    for idx, step in enumerate(steps, start=1):
        _emit_auto_crawl_event(
            session_id,
            "auto_crawl_precondition_step",
            page.url,
            {
                "step_index": idx,
                "step": step,
                "current_definition_row_id": str(
                    row.get("definition_row_id", "")
                ).strip(),
                "current_definition_row_no": str(row.get("no", "")).strip(),
                "current_definition_event_name": str(row.get("event_name", "")).strip(),
                "run_mode": "Auto Crawl",
            },
        )
        ok = _apply_precondition_step(
            page,
            step,
            candidate_selectors,
            scope_prefix,
            base_url,
            block_link_navigation,
        )
        if not ok and str(step.get("action", "")).strip().lower() == "select_tab":
            # 탭 전환 실패 시 현재 화면 상태를 다시 로드해 재시도 여지 확보
            try:
                page.wait_for_timeout(300)
            except Exception:
                pass
        if not ok:
            _emit_auto_crawl_event(
                session_id,
                "auto_crawl_precondition_failed",
                page.url,
                {
                    "step_index": idx,
                    "step": step,
                    "current_definition_row_id": str(
                        row.get("definition_row_id", "")
                    ).strip(),
                    "current_definition_row_no": str(row.get("no", "")).strip(),
                    "current_definition_event_name": str(
                        row.get("event_name", "")
                    ).strip(),
                    "run_mode": "Auto Crawl",
                },
            )
            return False
        try:
            page.wait_for_timeout(int(step.get("wait_ms", 600) or 600))
        except Exception:
            pass
    _emit_auto_crawl_event(
        session_id,
        "auto_crawl_preconditions_done",
        page.url,
        {
            "current_definition_row_id": str(row.get("definition_row_id", "")).strip(),
            "current_definition_row_no": str(row.get("no", "")).strip(),
            "current_definition_event_name": str(row.get("event_name", "")).strip(),
            "run_mode": "Auto Crawl",
        },
    )
    return True


def _update_runtime_panel(
    page,
    current_row: Dict[str, object],
    popup_open: bool,
    popup_status: str = "",
    phase: str = "",
    phase_detail: str = "",
) -> None:
    try:
        page.evaluate(
            """
            (payload) => {
              try {
                window.__qaRuntimePanelState = payload || {};
                if (typeof window.__qaUpdateRuntimePanel === "function") {
                  window.__qaUpdateRuntimePanel(window.__qaRuntimePanelState);
                }
              } catch (e) {}
            }
            """,
            {
                "row_id": (
                    str(current_row.get("definition_row_id", "")).strip()
                    if isinstance(current_row, dict)
                    else ""
                ),
                "row_no": (
                    str(current_row.get("no", "")).strip()
                    if isinstance(current_row, dict)
                    else ""
                ),
                "event_name": (
                    str(current_row.get("event_name", "")).strip()
                    if isinstance(current_row, dict)
                    else ""
                ),
                "section_name": (
                    str(current_row.get("section_name", "")).strip()
                    if isinstance(current_row, dict)
                    else ""
                ),
                "canonical_key": (
                    str(current_row.get("canonical_key", "")).strip()
                    if isinstance(current_row, dict)
                    else ""
                ),
                "event_type": (
                    str(current_row.get("event_type", "")).strip()
                    if isinstance(current_row, dict)
                    else ""
                ),
                "popup_open": bool(popup_open),
                "popup_status": str(popup_status or "").strip(),
                "phase": str(phase or "").strip(),
                "phase_detail": str(phase_detail or "").strip(),
            },
        )
    except Exception:
        return


def _run_auto_crawl(
    page, session_id: str, run_settings: Dict[str, object], stop_event: threading.Event
) -> str:
    settings = _normalize_run_settings(run_settings)
    definition_targets = _get_definition_event_targets(settings)
    definition_hints = _get_definition_runtime_hints(settings)
    definition_rows = _get_definition_runtime_rows(settings)
    definition_validation_mode = (
        str(settings.get("qa_mode", "")).strip() == "정의서 검증"
    )
    collection_goal_events = (
        _get_collection_goal_event_names(settings) if not definition_validation_mode else []
    )
    goal_event_set = {str(v).strip() for v in collection_goal_events if str(v).strip()}
    max_auto_clicks = int(settings.get("max_auto_clicks", 80))
    max_run_minutes = int(settings.get("max_run_minutes", 25))
    content_section_click_budget = int(settings.get("content_section_click_budget", 1))
    no_novelty_limit = int(settings.get("no_novelty_limit", 10))
    wait_after_click_ms = int(settings.get("wait_after_click_ms", 1200))
    click_interval_ms = int(settings.get("click_interval_ms", 1200))
    block_link_navigation = bool(settings.get("block_link_navigation", True))
    single_page_only = bool(settings.get("single_page_only", True))
    restrict_to_start_url = bool(settings.get("restrict_to_start_url", True))
    scenario_seed_urls_raw = settings.get("scenario_seed_urls", [])
    scenario_seed_urls: List[str] = []
    if isinstance(scenario_seed_urls_raw, list):
        seen_seed: set[str] = set()
        for raw in scenario_seed_urls_raw:
            text = str(raw or "").strip()
            if not text or text in seen_seed:
                continue
            seen_seed.add(text)
            scenario_seed_urls.append(text)
    scenario_seed_mode = bool(
        (not definition_validation_mode)
        and scenario_seed_urls
        and str(settings.get("scenario_template", "")).strip()
    )
    scenario_step_count = max(1, len(scenario_seed_urls)) if scenario_seed_mode else 1
    scenario_click_budget_per_step = (
        max(1, int(max_auto_clicks // scenario_step_count))
        if scenario_seed_mode
        else max_auto_clicks
    )
    scenario_time_budget_per_step = (
        max(45, int((max_run_minutes * 60) // scenario_step_count))
        if scenario_seed_mode
        else max(60, max_run_minutes * 60)
    )
    scenario_step_index = 0
    scenario_step_started_mono = time.monotonic()
    scenario_step_click_start = 0

    save_raw_screenshot = bool(settings.get("save_raw_screenshot", False))
    save_probe_screenshot = bool(settings.get("save_probe_screenshot", False))
    screenshot_format = str(settings.get("screenshot_format", "png")).strip().lower()
    screenshot_quality = int(settings.get("screenshot_quality", 85))

    start_url = page.url
    start_compare_key = _build_compare_key(start_url)
    scope_prefix = _build_scope_prefix(start_url) if restrict_to_start_url else ""
    if scenario_seed_mode:
        try:
            first_seed = str(scenario_seed_urls[0] or "").strip()
            if first_seed and _build_compare_key(page.url) != _build_compare_key(first_seed):
                page.goto(first_seed, wait_until="domcontentloaded")
                page.wait_for_timeout(300)
            start_url = str(scenario_seed_urls[0] or "").strip() or page.url
            start_compare_key = _build_compare_key(start_url)
            scope_prefix = _build_scope_prefix(start_url) if restrict_to_start_url else ""
        except Exception:
            start_url = page.url
            start_compare_key = _build_compare_key(start_url)
            scope_prefix = _build_scope_prefix(start_url) if restrict_to_start_url else ""
        _emit_auto_crawl_event(
            session_id,
            "auto_crawl_scenario_step_start",
            page.url,
            {
                "step_index": 1,
                "step_total": int(scenario_step_count),
                "seed_url": str(start_url or "").strip(),
                "click_budget": int(scenario_click_budget_per_step),
                "time_budget_sec": int(scenario_time_budget_per_step),
                "run_mode": "Auto Crawl",
            },
        )

    def _advance_scenario_seed(reason: str) -> bool:
        nonlocal scenario_step_index
        nonlocal start_url
        nonlocal start_compare_key
        nonlocal scope_prefix
        nonlocal scenario_step_started_mono
        nonlocal scenario_step_click_start
        if not scenario_seed_mode:
            return False
        current_seed = str(
            scenario_seed_urls[scenario_step_index]
            if scenario_step_index < len(scenario_seed_urls)
            else start_url
        ).strip()
        _emit_auto_crawl_event(
            session_id,
            "auto_crawl_scenario_step_end",
            page.url,
            {
                "step_index": int(scenario_step_index + 1),
                "step_total": int(scenario_step_count),
                "seed_url": current_seed,
                "reason": str(reason or "").strip() or "step_done",
                "clicked_in_step": int(max(0, clicked_count - scenario_step_click_start)),
                "elapsed_sec_in_step": int(max(0, round(time.monotonic() - scenario_step_started_mono))),
                "run_mode": "Auto Crawl",
            },
        )
        if scenario_step_index + 1 >= len(scenario_seed_urls):
            return False
        scenario_step_index += 1
        next_seed = str(scenario_seed_urls[scenario_step_index] or "").strip()
        try:
            page.goto(next_seed, wait_until="domcontentloaded")
            page.wait_for_timeout(350)
        except Exception:
            pass
        start_url = next_seed or page.url
        start_compare_key = _build_compare_key(start_url)
        scope_prefix = _build_scope_prefix(start_url) if restrict_to_start_url else ""
        scenario_step_started_mono = time.monotonic()
        scenario_step_click_start = int(clicked_count)
        _emit_auto_crawl_event(
            session_id,
            "auto_crawl_scenario_step_start",
            page.url,
            {
                "step_index": int(scenario_step_index + 1),
                "step_total": int(scenario_step_count),
                "seed_url": str(start_url or "").strip(),
                "click_budget": int(scenario_click_budget_per_step),
                "time_budget_sec": int(scenario_time_budget_per_step),
                "run_mode": "Auto Crawl",
            },
        )
        return True
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
    popup_scope_selectors = [
        "[role='dialog']",
        "[aria-modal='true']",
        ".modal",
        "[class*='modal']",
        "[class*='popup']",
        "[class*='drawer']",
        "[class*='gateway']",
        "[id*='gateway']",
        "[class*='layer']",
        "[id*='layer']",
        "[class*='sheet']",
        "[class*='flyout']",
        "[class*='offcanvas']",
        "[data-type='layer']",
        "[data-type='modal']",
        "[class*='bottom-']",
    ]
    content_scope_selectors = [
        "main",
        "[role='main']",
        "main section",
        "main article",
        ".content",
        ".contents",
        "main .container",
        "#content",
        "#content .container",
    ]
    global_section_cooldown_turns = int(settings.get("global_section_cooldown_turns", 4) or 4)
    section_fail_cooldown_turns = int(settings.get("section_fail_cooldown_turns", 5) or 5)
    weak_hint_min_non_empty = int(settings.get("weak_hint_min_non_empty", 2) or 2)
    confidence_min_best_score = float(settings.get("confidence_min_best_score", 6.8) or 6.8)
    confidence_min_score_gap = float(settings.get("confidence_min_score_gap", 1.2) or 1.2)
    challenge_pause_cooldown_seconds = float(
        settings.get("challenge_pause_cooldown_seconds", 8.0) or 8.0
    )
    challenge_resume_clear_checks = int(settings.get("challenge_resume_clear_checks", 3) or 3)
    challenge_max_pause_retries = int(settings.get("challenge_max_pause_retries", 8) or 8)
    seen_pattern_keys: set[str] = set()
    seen_pattern_keys_by_row: Dict[str, set[str]] = {}
    pattern_hit_stats: Dict[str, Dict[str, int]] = {}
    auto_clicked_keys: set[str] = set()
    seen_section_names: set[str] = set()
    section_fail_streak: Dict[str, int] = {}
    section_cooldown_until_click: Dict[str, int] = {}
    seen_selectors: set[str] = set()
    seen_href_paths: set[str] = set()
    content_section_click_counts: Dict[str, int] = {}
    seen_content_sections: set[str] = set()
    row_scroll_attempts: Dict[str, int] = {}
    row_scroll_stagnant_attempts: Dict[str, int] = {}
    no_candidate_streak = 0
    clicked_count = 0
    no_novelty_streak = 0
    hypothesis_fail_streak = 0
    challenge_detect_streak = 0
    challenge_pause_until_mono = 0.0
    challenge_clear_streak = 0
    challenge_pause_retries = 0
    last_challenge_emit_at = 0.0
    run_started_mono = time.monotonic()
    last_pause_notice_at = 0.0
    last_pause_reason = ""

    while not stop_event.is_set() and clicked_count < max_auto_clicks:
        if scenario_seed_mode:
            step_clicked = int(max(0, clicked_count - scenario_step_click_start))
            step_elapsed = float(max(0.0, time.monotonic() - scenario_step_started_mono))
            if step_clicked >= int(max(1, scenario_click_budget_per_step)):
                if _advance_scenario_seed("step_click_budget_reached"):
                    continue
                return "scenario_steps_completed"
            if step_elapsed >= float(max(15, scenario_time_budget_per_step)):
                if _advance_scenario_seed("step_time_budget_reached"):
                    continue
                return "scenario_steps_completed"
        if goal_event_set:
            with _LOCK:
                session = _SESSIONS.get(session_id)
                matched_events = (
                    set(getattr(session, "matched_definition_events", set()) or set())
                    if session
                    else set()
                )
            if goal_event_set.issubset(matched_events):
                _emit_auto_crawl_event(
                    session_id,
                    "collection_goals_reached",
                    page.url,
                    {
                        "reason": "all_goal_events_observed",
                        "goal_event_names": sorted(list(goal_event_set)),
                        "matched_event_names": sorted(list(matched_events)),
                        "auto_click_index": clicked_count,
                        "run_mode": "Auto Crawl",
                    },
                )
                return "collection_goals_reached"
        if (time.monotonic() - run_started_mono) >= max(60, max_run_minutes * 60):
            _emit_auto_crawl_event(
                session_id,
                "max_run_minutes_reached",
                page.url,
                {
                    "reason": "auto_crawl_runtime_guard",
                    "elapsed_seconds": int(time.monotonic() - run_started_mono),
                    "max_run_minutes": int(max_run_minutes),
                    "auto_click_index": clicked_count,
                    "run_mode": "Auto Crawl",
                },
            )
            return "max_run_minutes_reached"
        with _LOCK:
            session = _SESSIONS.get(session_id)
            paused = bool(session.auto_crawl_paused) if session else False
            pause_reason = (
                str(session.auto_crawl_pause_reason or "").strip() if session else ""
            )
            current_group = (
                str(getattr(session, "current_scenario_group", "") or "").strip()
                if session
                else ""
            )
            current_definition_row = (
                next(
                    (
                        row
                        for row in definition_rows
                        if str(row.get("definition_row_id", "")).strip()
                        == str(session.current_definition_row_id).strip()
                    ),
                    {},
                )
                if session and definition_rows
                else {}
            )
        if paused:
            now_mono = time.monotonic()
            if pause_reason == "access_challenge_detected":
                if now_mono < challenge_pause_until_mono:
                    try:
                        page.wait_for_timeout(300)
                    except Exception:
                        pass
                    continue
                challenge_reason = _detect_access_challenge(page)
                if challenge_reason:
                    challenge_clear_streak = 0
                    challenge_pause_retries += 1
                    backoff_scale = min(max(challenge_pause_retries, 1), 4)
                    challenge_pause_until_mono = (
                        now_mono + (challenge_pause_cooldown_seconds * backoff_scale)
                    )
                    if (now_mono - last_challenge_emit_at) >= 6.0:
                        _emit_auto_crawl_event(
                            session_id,
                            "auto_crawl_challenge_backoff",
                            page.url,
                            {
                                "reason": "access_challenge_detected",
                                "challenge_marker": challenge_reason,
                                "retry_count": int(challenge_pause_retries),
                                "next_retry_in_sec": int(
                                    max(1, round(challenge_pause_until_mono - now_mono))
                                ),
                                "auto_click_index": clicked_count,
                                "run_mode": "Auto Crawl",
                            },
                        )
                        last_challenge_emit_at = now_mono
                    if challenge_pause_retries >= max(1, challenge_max_pause_retries):
                        _emit_auto_crawl_event(
                            session_id,
                            "auto_crawl_blocked_persistent",
                            page.url,
                            {
                                "reason": "access_challenge_detected_persistent",
                                "retry_count": int(challenge_pause_retries),
                                "max_retries": int(challenge_max_pause_retries),
                                "auto_click_index": clicked_count,
                                "run_mode": "Auto Crawl",
                            },
                        )
                        return "challenge_detected"
                    try:
                        page.wait_for_timeout(250)
                    except Exception:
                        pass
                    continue
                challenge_clear_streak += 1
                if challenge_clear_streak >= max(1, challenge_resume_clear_checks):
                    with _LOCK:
                        session = _SESSIONS.get(session_id)
                        if session:
                            session.auto_crawl_paused = False
                            session.auto_crawl_pause_reason = ""
                    _emit_auto_crawl_event(
                        session_id,
                        "auto_crawl_resumed_auto",
                        page.url,
                        {
                            "reason": "challenge_cleared",
                            "cleared_checks": int(challenge_clear_streak),
                            "retry_count": int(challenge_pause_retries),
                            "auto_click_index": clicked_count,
                            "run_mode": "Auto Crawl",
                        },
                    )
                    challenge_detect_streak = 0
                    challenge_pause_retries = 0
                    challenge_clear_streak = 0
                    challenge_pause_until_mono = 0.0
                    continue
                try:
                    page.wait_for_timeout(220)
                except Exception:
                    pass
                continue
            now = time.time()
            reason_text = pause_reason or "paused"
            pause_notice_interval = 15.0 if reason_text == "access_challenge_detected" else 4.0
            if (now - last_pause_notice_at) >= pause_notice_interval or reason_text != last_pause_reason:
                _emit_auto_crawl_event(
                    session_id,
                    "auto_crawl_paused",
                    page.url,
                    {
                        "reason": reason_text,
                        "auto_click_index": clicked_count,
                        "run_mode": "Auto Crawl",
                    },
                )
                _update_runtime_panel(
                    page,
                    current_definition_row,
                    False,
                    f"paused:{reason_text}",
                    phase="auto_crawl_paused",
                    phase_detail="수동 클릭 대기",
                )
                last_pause_notice_at = now
                last_pause_reason = reason_text
            try:
                page.wait_for_timeout(120)
            except Exception:
                pass
            continue
        challenge_reason = _detect_access_challenge(page)
        if challenge_reason:
            challenge_detect_streak += 1
            if challenge_detect_streak < 3:
                _emit_auto_crawl_event(
                    session_id,
                    "auto_crawl_challenge_soft_detected",
                    page.url,
                    {
                        "reason": "challenge_signal_not_stable_yet",
                        "challenge_marker": challenge_reason,
                        "streak": int(challenge_detect_streak),
                        "auto_click_index": clicked_count,
                        "run_mode": "Auto Crawl",
                    },
                )
                try:
                    page.wait_for_timeout(500)
                except Exception:
                    pass
                continue
            with _LOCK:
                session = _SESSIONS.get(session_id)
                if session:
                    session.auto_crawl_paused = True
                    session.auto_crawl_pause_reason = "access_challenge_detected"
                    session.last_error = (
                        "접근 제한/봇 확인 페이지가 감지되어 자동수집을 일시 중지했습니다. "
                        "브라우저에서 확인 후 재개하거나 수동 클릭으로 진행하세요."
                    )
            challenge_pause_retries = 0
            challenge_clear_streak = 0
            challenge_pause_until_mono = time.monotonic() + max(
                1.0, challenge_pause_cooldown_seconds
            )
            _emit_auto_crawl_event(
                session_id,
                "auto_crawl_blocked",
                page.url,
                {
                    "reason": "access_challenge_detected",
                    "challenge_marker": challenge_reason,
                    "challenge_streak": int(challenge_detect_streak),
                    "auto_click_index": clicked_count,
                    "run_mode": "Auto Crawl",
                },
            )
            continue
        challenge_detect_streak = 0
        target_ids, matched_ids, definition_done = _get_definition_progress(
            session_id, settings
        )
        if definition_done:
            if current_group:
                with _LOCK:
                    session = _SESSIONS.get(session_id)
                    skipped = (
                        set(getattr(session, "skipped_definition_rows", set()) or set())
                        if session
                        else set()
                    )
                counts = _group_counts(
                    definition_rows, current_group, set(matched_ids), skipped
                )
                _emit_auto_crawl_event(
                    session_id,
                    "auto_crawl_scenario_group_end",
                    page.url,
                    {
                        "scenario_group": current_group,
                        "done_rows": counts.get("done", 0),
                        "total_rows": counts.get("total", 0),
                        "reason": "definition_targets_reached",
                        "run_mode": "Auto Crawl",
                    },
                )
            should_emit = False
            with _LOCK:
                session = _SESSIONS.get(session_id)
                if session and not session.definition_targets_reached:
                    session.definition_targets_reached = True
                    should_emit = True
            if should_emit:
                _emit_auto_crawl_event(
                    session_id,
                    "definition_targets_reached",
                    page.url,
                    {
                        "reason": "all_definition_rows_matched",
                        "matched_definition_rows": matched_ids,
                        "definition_row_ids": target_ids,
                        "run_mode": "Auto Crawl",
                    },
                )
            if not definition_validation_mode:
                return "definition_targets_reached"
        current_definition_row: Dict[str, object] = {}
        current_definition_hints = definition_hints
        if definition_rows:
            matched_row_ids = set(matched_ids)
            with _LOCK:
                session = _SESSIONS.get(session_id)
                skipped_row_ids = (
                    set(getattr(session, "skipped_definition_rows", set()) or set())
                    if session
                    else set()
                )
            current_definition_row = _select_next_definition_row(
                definition_rows, matched_row_ids, skipped_row_ids
            )
            if current_definition_row:
                _emit_auto_crawl_event(
                    session_id,
                    "current_definition_row_selected",
                    page.url,
                    {
                        "current_definition_row_id": str(
                            current_definition_row.get("definition_row_id", "")
                        ).strip(),
                        "current_definition_row_no": str(
                            current_definition_row.get("no", "")
                        ).strip(),
                        "current_definition_event_name": str(
                            current_definition_row.get("event_name", "")
                        ).strip(),
                        "expected_section_name": _get_expected_param(
                            current_definition_row, "section_name"
                        ),
                        "expected_button_id": _get_expected_param(
                            current_definition_row, "button_id", fallback_row_key=False
                        ),
                        "expected_target_id": _get_expected_param(
                            current_definition_row, "target_id", fallback_row_key=False
                        ),
                        "expected_page_id": _get_expected_param(
                            current_definition_row, "page_id"
                        ),
                        "run_mode": "Auto Crawl",
                    },
                )
            if current_definition_row:
                row_id = str(
                    current_definition_row.get("definition_row_id", "")
                ).strip()
                next_group = str(
                    current_definition_row.get("scenario_group", "")
                ).strip()
                row_hints = _row_hints_from_definition_row(current_definition_row)
                if (
                    not row_hints.get("section_names")
                    and isinstance(definition_hints.get("section_names", []), list)
                ):
                    row_hints["section_names"] = [
                        str(v).strip()
                        for v in definition_hints.get("section_names", [])
                        if str(v).strip()
                    ]
                current_definition_hints = row_hints
                with _LOCK:
                    session = _SESSIONS.get(session_id)
                    if session:
                        prev_group = str(
                            getattr(session, "current_scenario_group", "") or ""
                        ).strip()
                        if prev_group != next_group:
                            if prev_group:
                                counts = _group_counts(
                                    definition_rows,
                                    prev_group,
                                    matched_row_ids,
                                    skipped_row_ids,
                                )
                                _emit_auto_crawl_event(
                                    session_id,
                                    "auto_crawl_scenario_group_end",
                                    page.url,
                                    {
                                        "scenario_group": prev_group,
                                        "done_rows": counts.get("done", 0),
                                        "total_rows": counts.get("total", 0),
                                        "reason": "scenario_group_completed",
                                        "run_mode": "Auto Crawl",
                                    },
                                )
                            if next_group:
                                counts = _group_counts(
                                    definition_rows,
                                    next_group,
                                    matched_row_ids,
                                    skipped_row_ids,
                                )
                                _emit_auto_crawl_event(
                                    session_id,
                                    "auto_crawl_scenario_group_start",
                                    page.url,
                                    {
                                        "scenario_group": next_group,
                                        "done_rows": counts.get("done", 0),
                                        "total_rows": counts.get("total", 0),
                                        "run_mode": "Auto Crawl",
                                    },
                                )
                        session.current_scenario_group = next_group
                        session.current_definition_row_id = row_id
            if not current_definition_row:
                with _LOCK:
                    session = _SESSIONS.get(session_id)
                    if session:
                        prev_group = str(
                            getattr(session, "current_scenario_group", "") or ""
                        ).strip()
                        if prev_group:
                            counts = _group_counts(
                                definition_rows,
                                prev_group,
                                matched_row_ids,
                                skipped_row_ids,
                            )
                            _emit_auto_crawl_event(
                                session_id,
                                "auto_crawl_scenario_group_end",
                                page.url,
                                {
                                    "scenario_group": prev_group,
                                    "done_rows": counts.get("done", 0),
                                    "total_rows": counts.get("total", 0),
                                    "reason": "definition_rows_checked",
                                    "run_mode": "Auto Crawl",
                                },
                            )
                        session.current_scenario_group = ""
                        session.current_definition_row_id = ""
                _emit_auto_crawl_event(
                    session_id,
                    "definition_rows_checked",
                    page.url,
                    {
                        "reason": "all_definition_rows_checked",
                        "matched_definition_rows": sorted(matched_row_ids),
                        "run_mode": "Auto Crawl",
                    },
                )
                return "definition_rows_checked"
        try:
            page.wait_for_load_state("domcontentloaded", timeout=1500)
        except Exception:
            pass

        if restrict_to_start_url and _build_compare_key(page.url) != start_compare_key:
            _emit_auto_crawl_event(
                session_id,
                "auto_crawl_nav_blocked",
                page.url,
                {
                    "reason": "navigation_out_of_scope",
                    "start_url": start_url,
                    "current_url": page.url,
                    "auto_click_index": clicked_count,
                    "run_mode": "Auto Crawl",
                },
            )
            try:
                page.goto(start_url, wait_until="domcontentloaded")
                page.wait_for_timeout(500)
            except Exception:
                pass
            continue

        if current_definition_row:
            current_row_id = str(
                current_definition_row.get("definition_row_id", "")
            ).strip()
            with _LOCK:
                session = _SESSIONS.get(session_id)
                done = bool(
                    session and current_row_id in (session.preconditions_done or set())
                )
                attempts = (
                    int(session.preconditions_attempts.get(current_row_id, 0))
                    if session
                    else 0
                )
            if current_row_id and not done and attempts >= 2:
                skipped = _skip_definition_row(session_id, current_definition_row)
                _emit_auto_crawl_event(
                    session_id,
                    "auto_crawl_skip_row",
                    page.url,
                    {
                        "reason": "preconditions_failed",
                        "current_definition_row_id": str(
                            current_definition_row.get("definition_row_id", "")
                        ).strip(),
                        "current_definition_row_no": str(
                            current_definition_row.get("no", "")
                        ).strip(),
                        "current_definition_event_name": str(
                            current_definition_row.get("event_name", "")
                        ).strip(),
                        "next_definition_row_id": str(
                            skipped.get("next_row_id", "")
                        ).strip(),
                        "next_definition_row_no": str(
                            skipped.get("next_row_no", "")
                        ).strip(),
                        "auto_click_index": clicked_count,
                        "run_mode": "Auto Crawl",
                    },
                )
                try:
                    page.wait_for_timeout(200)
                except Exception:
                    pass
                continue
            if current_row_id and not done and attempts < 2:
                ok = _run_preconditions(
                    page,
                    session_id,
                    current_definition_row,
                    candidate_selectors,
                    scope_prefix,
                    start_url,
                    block_link_navigation,
                )
                with _LOCK:
                    session = _SESSIONS.get(session_id)
                    if session:
                        session.preconditions_attempts[current_row_id] = attempts + 1
                        if ok:
                            session.preconditions_done.add(current_row_id)
                if not ok:
                    try:
                        page.wait_for_timeout(400)
                    except Exception:
                        pass
                    continue

        popup_state = _get_popup_layer_state(page)
        popup_open = bool(popup_state.get("popup_open", False))
        popup_status = (
            f"popup={int(popup_state.get('popup_count', 0))}/overlay={int(popup_state.get('overlay_count', 0))}"
            if popup_open or int(popup_state.get("overlay_count", 0) or 0) > 0
            else "closed_confirmed"
        )
        popup_required = _definition_row_requires_popup(current_definition_row)
        if popup_open and current_definition_row and not popup_required:
            _p_row_no = str(current_definition_row.get("no", "")).strip()
            _p_marker = clicked_count + 1
            popup_probe = _capture_runtime_probe(
                page,
                screenshots_dir,
                "popup",
                _p_marker,
                str(current_definition_row.get("event_name", "")).strip()
                or str(current_definition_row.get("section_name", "")).strip()
                or "popup_autoclose",
                _get_visible_popup_bbox(page),
                row_no=_p_row_no,
                enabled=True,  # 팝업 차단 상황은 항상 캡처
                save_raw=save_raw_screenshot,
                phase="popup",
                image_format=screenshot_format,
                quality=screenshot_quality,
            )
            _emit_auto_crawl_event(
                session_id,
                "popup_close_attempted",
                page.url,
                {
                    "reason": "popup_blocks_current_definition_row",
                    "current_definition_row_id": str(
                        current_definition_row.get("definition_row_id", "")
                    ).strip(),
                    "current_definition_row_no": str(
                        current_definition_row.get("no", "")
                    ).strip(),
                    "current_definition_event_name": str(
                        current_definition_row.get("event_name", "")
                    ).strip(),
                    "run_mode": "Auto Crawl",
                    "screenshot_path": str(
                        popup_probe.get("screenshot_path", "")
                    ).strip(),
                    "selector_screenshot": str(
                        popup_probe.get("screenshot_path", "")
                    ).strip(),
                    "raw_screenshot_file": str(
                        popup_probe.get("raw_screenshot_file", "")
                    ).strip(),
                    "annotation_no": str(popup_probe.get("annotation_no", "")).strip(),
                    "bounding_box": popup_probe.get("bounding_box", {}),
                    "bbox_x": popup_probe.get("bbox_x", 0),
                    "bbox_y": popup_probe.get("bbox_y", 0),
                    "bbox_width": popup_probe.get("bbox_width", 0),
                    "bbox_height": popup_probe.get("bbox_height", 0),
                    "popup_count": int(popup_state.get("popup_count", 0) or 0),
                    "overlay_count": int(popup_state.get("overlay_count", 0) or 0),
                },
            )
            popup_close_result = _close_popup_if_possible(page)
            recovered_handle, recovered_meta, recovered_bbox, recovered_hint_score = (
                _find_best_candidate(
                    page,
                    _candidate_selectors_for_row_kind(
                        _infer_definition_row_kind(current_definition_row)
                    ),
                    definition_targets,
                    current_definition_hints,
                    seen_pattern_keys,
                    scope_selectors=[""],
                    prefer_scoped_candidates=False,
                    row_expected_kind=_infer_definition_row_kind(current_definition_row),
                    enforce_main_area=False,
                )
            )
            popup_closed = bool(recovered_handle is not None)
            popup_status = (
                f"{str(popup_close_result.get('method', '')).strip() or 'close_attempt'} / "
                f"overlay_cleared={bool(popup_close_result.get('overlay_cleared', False))} / "
                f"popup={int(popup_close_result.get('popup_count', 0) or 0)} / "
                f"overlay={int(popup_close_result.get('overlay_count', 0) or 0)} / "
                f"target_recovered={popup_closed}"
            )
            popup_event_name = (
                "popup_close_confirmed" if popup_closed else "popup_close_failed"
            )
            popup_reason = (
                "interactable_target_recovered"
                if popup_closed
                else "target_not_recovered_after_close_attempt"
            )
            popup_event_payload = {
                "reason": popup_reason,
                "popup_closed": popup_closed,
                "popup_close_method": str(popup_close_result.get("method", "")).strip(),
                "overlay_cleared": bool(
                    popup_close_result.get("overlay_cleared", False)
                ),
                "popup_count": int(popup_close_result.get("popup_count", 0) or 0),
                "overlay_count": int(popup_close_result.get("overlay_count", 0) or 0),
                "current_definition_row_id": str(
                    current_definition_row.get("definition_row_id", "")
                ).strip(),
                "current_definition_row_no": str(
                    current_definition_row.get("no", "")
                ).strip(),
                "current_definition_event_name": str(
                    current_definition_row.get("event_name", "")
                ).strip(),
                "run_mode": "Auto Crawl",
                "screenshot_path": str(popup_probe.get("screenshot_path", "")).strip(),
                "selector_screenshot": str(
                    popup_probe.get("screenshot_path", "")
                ).strip(),
                "raw_screenshot_file": str(
                    popup_probe.get("raw_screenshot_file", "")
                ).strip(),
                "annotation_no": str(popup_probe.get("annotation_no", "")).strip(),
                "bounding_box": popup_probe.get("bounding_box", {}),
                "bbox_x": popup_probe.get("bbox_x", 0),
                "bbox_y": popup_probe.get("bbox_y", 0),
                "bbox_width": popup_probe.get("bbox_width", 0),
                "bbox_height": popup_probe.get("bbox_height", 0),
            }
            if popup_closed:
                popup_event_payload.update(
                    {
                        "recovered_selector": str(
                            recovered_meta.get("selector", "")
                        ).strip(),
                        "recovered_target_id": str(
                            recovered_meta.get("target_id", "")
                        ).strip(),
                        "recovered_hint_score": float(recovered_hint_score or 0.0),
                    }
                )
            _emit_auto_crawl_event(
                session_id, popup_event_name, page.url, popup_event_payload
            )
            if popup_closed:
                _update_runtime_panel(
                    page,
                    current_definition_row,
                    False,
                    popup_status,
                    phase="popup_close_confirmed",
                    phase_detail="원래 row target 복구됨",
                )
                try:
                    page.wait_for_timeout(200)
                except Exception:
                    pass
                popup_open = False
                popup_state = _get_popup_layer_state(page)
                popup_status = f"{str(popup_close_result.get('method', '')).strip() or 'close_attempt'} / closed_confirmed"
            else:
                popup_count = int(popup_close_result.get("popup_count", 0) or 0)
                overlay_count = int(popup_close_result.get("overlay_count", 0) or 0)
                # Popup close failed but no popup/overlay remains: treat as false-positive lock.
                if popup_count <= 0 and overlay_count <= 0:
                    popup_open = False
                    popup_status = "close_attempt / no_popup_detected_after_attempt"
                _update_runtime_panel(
                    page,
                    current_definition_row,
                    popup_open,
                    popup_status,
                    phase="popup_close_failed",
                    phase_detail="target 복구 실패",
                )
        _update_runtime_panel(
            page,
            current_definition_row,
            popup_open,
            popup_status,
            phase="candidate_search",
            phase_detail="클릭 가능한 target 찾는 중",
        )
        current_row_id = str(
            current_definition_row.get("definition_row_id", "")
        ).strip()
        manual_excluded_keys = _collect_manual_excluded_keys(session_id, settings)
        excluded_interaction_keys = set(manual_excluded_keys)
        excluded_interaction_keys.update(auto_clicked_keys)
        if definition_validation_mode and current_row_id:
            active_seen_pattern_keys = seen_pattern_keys_by_row.setdefault(
                current_row_id, set()
            )
        else:
            active_seen_pattern_keys = seen_pattern_keys
        search_in_popup = bool(popup_open and popup_required)
        row_expected_kind = _infer_definition_row_kind(current_definition_row)
        row_candidate_selectors = _candidate_selectors_for_row_kind(row_expected_kind)
        # 클릭 가설 실패가 누적되면 상태 분류를 보수적으로 바꾼다.
        # 1) popup이 보이면 row 요구 여부와 무관하게 popup 우선 복구
        # 2) filter/generic row는 filter selector 우선 탐색
        if popup_open and hypothesis_fail_streak >= 2:
            search_in_popup = True
        if hypothesis_fail_streak >= 2 and row_expected_kind in {"filter", "generic"}:
            filter_first = _candidate_selectors_for_row_kind("filter")
            row_candidate_selectors = filter_first + [
                s for s in row_candidate_selectors if s not in set(filter_first)
            ]
        blocked_sections_now = {
            sec
            for sec, until_click in section_cooldown_until_click.items()
            if int(until_click) >= int(clicked_count)
        }
        candidate_debug: Dict[str, object] = {}
        global_fallback_triggered = False
        stage1_candidate_count = 0
        if search_in_popup:
            selected_handle, selected_meta, selected_bbox, selected_hint_score, candidate_debug = (
                _find_best_candidate(
                    page,
                    row_candidate_selectors,
                    definition_targets,
                    current_definition_hints,
                    active_seen_pattern_keys,
                    interaction_excluded_keys=excluded_interaction_keys,
                    pattern_hit_stats=pattern_hit_stats,
                    scope_selectors=(popup_scope_selectors + [""]),
                    prefer_scoped_candidates=True,
                    excluded_content_sections=(
                        seen_content_sections
                        if (not definition_validation_mode and not goal_event_set)
                        else set()
                    ),
                    content_section_click_counts=(
                        content_section_click_counts if not definition_validation_mode else {}
                    ),
                    content_section_click_budget=content_section_click_budget,
                    seen_section_names=(
                        seen_section_names if not definition_validation_mode else set()
                    ),
                    blocked_section_names=blocked_sections_now,
                    blocked_selectors=seen_selectors,
                    blocked_href_paths=seen_href_paths,
                    global_fallback_mode=False,
                    return_debug=True,
                    row_expected_kind=row_expected_kind,
                    enforce_main_area=False,
                )
            )
            stage1_candidate_count = int(candidate_debug.get("candidate_count", 0) or 0)
        else:
            selected_handle, selected_meta, selected_bbox, selected_hint_score, candidate_debug = (
                _find_best_candidate(
                    page,
                    row_candidate_selectors,
                    definition_targets,
                    current_definition_hints,
                    active_seen_pattern_keys,
                    interaction_excluded_keys=excluded_interaction_keys,
                    pattern_hit_stats=pattern_hit_stats,
                    scope_selectors=content_scope_selectors,
                    prefer_scoped_candidates=True,
                    excluded_content_sections=(
                        seen_content_sections
                        if (not definition_validation_mode and not goal_event_set)
                        else set()
                    ),
                    content_section_click_counts=(
                        content_section_click_counts if not definition_validation_mode else {}
                    ),
                    content_section_click_budget=content_section_click_budget,
                    seen_section_names=(
                        seen_section_names if not definition_validation_mode else set()
                    ),
                    blocked_section_names=blocked_sections_now,
                    blocked_selectors=seen_selectors,
                    blocked_href_paths=seen_href_paths,
                    global_fallback_mode=False,
                    return_debug=True,
                    row_expected_kind=row_expected_kind,
                    enforce_main_area=True,
                )
            )
            stage1_candidate_count = int(candidate_debug.get("candidate_count", 0) or 0)

        non_empty_hint_count = 0
        for key in ["target_ids", "section_names", "text_hints"]:
            values = current_definition_hints.get(key, [])
            if isinstance(values, list) and any(str(v).strip() for v in values):
                non_empty_hint_count += 1
        row_hint_strength = "strong" if non_empty_hint_count >= weak_hint_min_non_empty else "weak"
        top_candidates = candidate_debug.get("top_candidates", []) if isinstance(candidate_debug.get("top_candidates", []), list) else []
        best_score = float(candidate_debug.get("best_score", -999.0) or -999.0)
        score_gap = float(candidate_debug.get("score_gap", -999.0) or -999.0)
        top3 = top_candidates[:3]
        top3_global_count = sum(
            1 for item in top3 if bool((item or {}).get("is_global_section", False))
        )
        rejected_top_candidates = [
            {
                "section_name": str((item or {}).get("section_name", "")).strip(),
                "selector": str((item or {}).get("selector", "")).strip(),
                "score": float((item or {}).get("score", 0.0) or 0.0),
            }
            for item in top_candidates[1:4]
        ]
        skip_reasons: List[str] = []
        if selected_handle is not None:
            if row_hint_strength == "weak":
                skip_reasons.append("weak_hint")
            if best_score < confidence_min_best_score:
                skip_reasons.append("low_best_score")
            if score_gap < confidence_min_score_gap:
                skip_reasons.append("low_confidence_gap")
            if top3_global_count >= 2:
                skip_reasons.append("global_top_candidates")
        selected_reason = "selected"
        if global_fallback_triggered:
            selected_reason = "selected_with_global_fallback"
        if skip_reasons:
            selected_reason = "hold_unchecked"
        _emit_auto_crawl_event(
            session_id,
            "auto_candidate_decision",
            page.url,
            {
                "selected_reason": selected_reason,
                "global_fallback_triggered": bool(global_fallback_triggered),
                "global_fallback_trigger_reason": (
                    "no_content_candidates" if global_fallback_triggered and stage1_candidate_count <= 0 else
                    "no_content_candidate_selected" if global_fallback_triggered else ""
                ),
                "content_stage_candidate_count": int(stage1_candidate_count),
                "row_hint_strength": row_hint_strength,
                "non_empty_hint_count": int(non_empty_hint_count),
                "best_score": float(best_score),
                "confidence_gap": float(score_gap),
                "candidate_count": int(candidate_debug.get("candidate_count", 0) or 0),
                "rejected_top_candidates": rejected_top_candidates,
                "top3_global_count": int(top3_global_count),
                "skip_reasons": skip_reasons,
                "current_definition_row_id": str(current_definition_row.get("definition_row_id", "")).strip(),
                "current_definition_row_no": str(current_definition_row.get("no", "")).strip(),
                "run_mode": "Auto Crawl",
            },
        )
        if selected_handle is not None and skip_reasons and current_definition_row:
            skipped = _skip_definition_row(session_id, current_definition_row)
            _emit_auto_crawl_event(
                session_id,
                "auto_crawl_skip_row",
                page.url,
                {
                    "reason": "low_confidence_skip",
                    "skip_reasons": skip_reasons,
                    "global_fallback_triggered": bool(global_fallback_triggered),
                    "row_hint_strength": row_hint_strength,
                    "best_score": float(best_score),
                    "confidence_gap": float(score_gap),
                    "next_definition_row_id": str(skipped.get("next_row_id", "")).strip(),
                    "next_definition_row_no": str(skipped.get("next_row_no", "")).strip(),
                    "auto_click_index": clicked_count,
                    "run_mode": "Auto Crawl",
                },
            )
            try:
                page.wait_for_timeout(220)
            except Exception:
                pass
            continue

        if selected_handle is None:
            if not current_definition_row:
                no_candidate_streak += 1
                signature_before_scroll = _collect_view_candidate_signature(page)
                scroll_result = _scroll_page_for_candidate_search(page)
                signature_after_scroll = _collect_view_candidate_signature(page)
                signature_changed = bool(
                    signature_before_scroll
                    and signature_after_scroll
                    and signature_before_scroll != signature_after_scroll
                )
                _emit_auto_crawl_event(
                    session_id,
                    "auto_crawl_scroll_search",
                    page.url,
                    {
                        "reason": "scroll_before_tab_switch",
                        "scroll_moved": bool(scroll_result.get("moved", False)),
                        "scroll_from_top": int(scroll_result.get("from_top", 0) or 0),
                        "scroll_to_top": int(scroll_result.get("to_top", 0) or 0),
                        "scroll_max_top": int(scroll_result.get("max_top", 0) or 0),
                        "view_signature_changed": bool(signature_changed),
                        "no_candidate_streak": int(no_candidate_streak),
                        "auto_click_index": clicked_count,
                        "run_mode": "Auto Crawl",
                    },
                )
                # 일반 자동수집에서는 탭을 바로 넘기지 않고,
                # 같은 탭에서 스크롤 탐색을 최소 2번 시도한 뒤 탭 전환을 허용한다.
                if int(no_candidate_streak) < 2:
                    try:
                        page.wait_for_timeout(280)
                    except Exception:
                        pass
                    continue
            expand_result = _auto_expand_ui_containers(page)
            if int(expand_result.get("expanded_count", 0) or 0) > 0:
                no_candidate_streak = 0
                _emit_auto_crawl_event(
                    session_id,
                    "auto_expand_ui",
                    page.url,
                    {
                        "reason": "no_candidates_try_expand_tabs_accordions",
                        "expanded_count": int(
                            expand_result.get("expanded_count", 0) or 0
                        ),
                        "tab_count": int(expand_result.get("tab_count", 0) or 0),
                        "accordion_count": int(
                            expand_result.get("accordion_count", 0) or 0
                        ),
                        "expanded_keys": list(expand_result.get("keys", []) or []),
                        "auto_click_index": clicked_count,
                        "run_mode": "Auto Crawl",
                    },
                )
                try:
                    page.wait_for_timeout(320)
                except Exception:
                    pass
                continue
            challenge_reason = _detect_access_challenge(page)
            if challenge_reason:
                _emit_auto_crawl_event(
                    session_id,
                    "auto_crawl_blocked",
                    page.url,
                    {
                        "reason": "access_challenge_detected",
                        "challenge_marker": challenge_reason,
                        "auto_click_index": clicked_count,
                        "run_mode": "Auto Crawl",
                    },
                )
                return "challenge_detected"
            if current_definition_row:
                current_row_id = str(
                    current_definition_row.get("definition_row_id", "")
                ).strip()
                scroll_attempt_count = int(row_scroll_attempts.get(current_row_id, 0))
                stagnant_attempt_count = int(
                    row_scroll_stagnant_attempts.get(current_row_id, 0)
                )
                if scroll_attempt_count < 6 and stagnant_attempt_count < 2:
                    _s_row_no = str(current_definition_row.get("no", "")).strip()
                    signature_before_scroll = _collect_view_candidate_signature(page)
                    scroll_probe = _capture_runtime_probe(
                        page,
                        screenshots_dir,
                        "scroll",
                        clicked_count + 1,
                        str(current_definition_row.get("event_name", "")).strip()
                        or str(current_definition_row.get("section_name", "")).strip()
                        or "scroll_search",
                        row_no=_s_row_no,
                        enabled=save_probe_screenshot,  # 스크롤 탐색은 옵션
                        save_raw=False,
                        phase="warning",
                        image_format=screenshot_format,
                        quality=screenshot_quality,
                    )
                    scroll_result = _scroll_page_for_candidate_search(page)
                    signature_after_scroll = _collect_view_candidate_signature(page)
                    signature_changed = bool(
                        signature_before_scroll
                        and signature_after_scroll
                        and signature_before_scroll != signature_after_scroll
                    )
                    row_scroll_attempts[current_row_id] = scroll_attempt_count + 1
                    if bool(scroll_result.get("moved", False)) and not signature_changed:
                        row_scroll_stagnant_attempts[current_row_id] = (
                            stagnant_attempt_count + 1
                        )
                    elif signature_changed:
                        row_scroll_stagnant_attempts[current_row_id] = 0
                    _emit_auto_crawl_event(
                        session_id,
                        "auto_crawl_scroll_search",
                        page.url,
                        {
                            "reason": "scrolling_to_find_current_definition_row",
                            "current_definition_row_id": current_row_id,
                            "current_definition_row_no": str(
                                current_definition_row.get("no", "")
                            ).strip(),
                            "current_definition_event_name": str(
                                current_definition_row.get("event_name", "")
                            ).strip(),
                            "scroll_attempt": row_scroll_attempts[current_row_id],
                            "scroll_moved": bool(scroll_result.get("moved", False)),
                            "scroll_from_top": int(
                                scroll_result.get("from_top", 0) or 0
                            ),
                            "scroll_to_top": int(scroll_result.get("to_top", 0) or 0),
                            "scroll_max_top": int(scroll_result.get("max_top", 0) or 0),
                            "view_signature_changed": bool(signature_changed),
                            "stagnant_scroll_attempt": int(
                                row_scroll_stagnant_attempts.get(current_row_id, 0)
                            ),
                            "run_mode": "Auto Crawl",
                            "screenshot_path": str(
                                scroll_probe.get("screenshot_path", "")
                            ).strip(),
                            "selector_screenshot": str(
                                scroll_probe.get("screenshot_path", "")
                            ).strip(),
                            "raw_screenshot_file": str(
                                scroll_probe.get("raw_screenshot_file", "")
                            ).strip(),
                            "annotation_no": str(
                                scroll_probe.get("annotation_no", "")
                            ).strip(),
                            "bounding_box": scroll_probe.get("bounding_box", {}),
                            "bbox_x": scroll_probe.get("bbox_x", 0),
                            "bbox_y": scroll_probe.get("bbox_y", 0),
                            "bbox_width": scroll_probe.get("bbox_width", 0),
                            "bbox_height": scroll_probe.get("bbox_height", 0),
                        },
                    )
                    _update_runtime_panel(
                        page,
                        current_definition_row,
                        popup_open,
                        popup_status,
                        phase="scroll_search",
                        phase_detail=(
                            f"{row_scroll_attempts[current_row_id]}회 스크롤 탐색 "
                            f"(정체 {int(row_scroll_stagnant_attempts.get(current_row_id, 0))}회)"
                        ),
                    )
                    if bool(scroll_result.get("moved", False)) and (
                        signature_changed
                        or int(row_scroll_stagnant_attempts.get(current_row_id, 0)) < 2
                    ):
                        try:
                            page.wait_for_timeout(500)
                        except Exception:
                            pass
                        continue

                _sk_row_no = str(current_definition_row.get("no", "")).strip()
                skip_probe = _capture_runtime_probe(
                    page,
                    screenshots_dir,
                    "skip",
                    clicked_count + 1,
                    str(current_definition_row.get("selector", "")).strip()
                    or str(current_definition_row.get("event_name", "")).strip()
                    or str(current_definition_row.get("section_name", "")).strip()
                    or "skip_row",
                    _get_visible_popup_bbox(page) if popup_open else None,
                    row_no=_sk_row_no,
                    enabled=True,  # skip은 항상 캡처 (실패 상황)
                    save_raw=save_raw_screenshot,
                    phase="skip",
                    image_format=screenshot_format,
                    quality=screenshot_quality,
                )

                skipped = _skip_definition_row(session_id, current_definition_row)
                if current_row_id:
                    row_scroll_attempts.pop(current_row_id, None)
                    row_scroll_stagnant_attempts.pop(current_row_id, None)
                _emit_auto_crawl_event(
                    session_id,
                    "auto_crawl_skip_row",
                    page.url,
                    {
                        "reason": "no_new_candidates_for_current_definition_row",
                        "current_definition_row_id": str(
                            current_definition_row.get("definition_row_id", "")
                        ).strip(),
                        "current_definition_row_no": str(
                            current_definition_row.get("no", "")
                        ).strip(),
                        "current_definition_event_name": str(
                            current_definition_row.get("event_name", "")
                        ).strip(),
                        "next_definition_row_id": str(
                            skipped.get("next_row_id", "")
                        ).strip(),
                        "next_definition_row_no": str(
                            skipped.get("next_row_no", "")
                        ).strip(),
                        "auto_click_index": clicked_count,
                        "run_mode": "Auto Crawl",
                        "screenshot_path": str(
                            skip_probe.get("screenshot_path", "")
                        ).strip(),
                        "selector_screenshot": str(
                            skip_probe.get("screenshot_path", "")
                        ).strip(),
                        "raw_screenshot_file": str(
                            skip_probe.get("raw_screenshot_file", "")
                        ).strip(),
                        "annotation_no": str(
                            skip_probe.get("annotation_no", "")
                        ).strip(),
                        "bounding_box": skip_probe.get("bounding_box", {}),
                        "bbox_x": skip_probe.get("bbox_x", 0),
                        "bbox_y": skip_probe.get("bbox_y", 0),
                        "bbox_width": skip_probe.get("bbox_width", 0),
                        "bbox_height": skip_probe.get("bbox_height", 0),
                    },
                )
                try:
                    page.wait_for_timeout(350)
                except Exception:
                    pass
                continue
            _emit_auto_crawl_event(
                session_id,
                "auto_crawl_idle",
                page.url,
                {
                    "reason": "no_new_candidates",
                    "current_definition_row_id": str(
                        current_definition_row.get("definition_row_id", "")
                    ).strip(),
                    "current_definition_row_no": str(
                        current_definition_row.get("no", "")
                    ).strip(),
                    "current_definition_event_name": str(
                        current_definition_row.get("event_name", "")
                    ).strip(),
                    "auto_click_index": clicked_count,
                    "max_auto_clicks": max_auto_clicks,
                    "run_mode": "Auto Crawl",
                },
            )
            return "idle"

        clicked_count += 1
        no_candidate_streak = 0
        if current_row_id:
            row_scroll_attempts.pop(current_row_id, None)
            row_scroll_stagnant_attempts.pop(current_row_id, None)
        selected_dedup_key = _candidate_dedup_key(selected_meta)
        if selected_dedup_key:
            active_seen_pattern_keys.add(selected_dedup_key)
            if not (definition_validation_mode and current_row_id):
                seen_pattern_keys.add(selected_dedup_key)
        selected_section_name = str(selected_meta.get("section_name", "")).strip()
        if selected_section_name:
            seen_section_names.add(selected_section_name)
            if len(seen_section_names) > 500:
                seen_section_names = set(list(seen_section_names)[-350:])
            if _is_global_noise_section(selected_section_name):
                section_cooldown_until_click[selected_section_name.lower()] = int(
                    clicked_count + max(1, global_section_cooldown_turns)
                )
        selected_selector = str(selected_meta.get("selector", "")).strip()
        if selected_selector:
            seen_selectors.add(selected_selector)
            if len(seen_selectors) > 2000:
                seen_selectors = set(list(seen_selectors)[-1200:])
        selected_href_key = _href_path_key(str(selected_meta.get("href", "")).strip())
        if selected_href_key:
            seen_href_paths.add(selected_href_key.lower())
            if len(seen_href_paths) > 1200:
                seen_href_paths = set(list(seen_href_paths)[-800:])
        auto_clicked_keys.update(_candidate_interaction_keys(selected_meta))
        if len(auto_clicked_keys) > 1000:
            auto_clicked_keys = set(list(auto_clicked_keys)[-700:])
        _row_no = (
            str(current_definition_row.get("no", "")).strip()
            if current_definition_row
            else ""
        )
        screenshot_name = _make_screenshot_name(
            "click", _row_no, clicked_count, screenshot_format
        )
        screenshot_path = screenshots_dir / screenshot_name
        screenshot_file = str(screenshot_path).strip()
        raw_screenshot_file = ""
        navigation_blocked = False
        current_url = page.url

        try:
            selected_handle.scroll_into_view_if_needed(timeout=800)
        except Exception:
            pass

        # 스크롤 후 요소의 뷰포트 위치가 변하므로 bbox를 다시 측정한다
        try:
            fresh_meta = _extract_candidate_metadata(selected_handle)
            if fresh_meta:
                fw = int(max(0, float(fresh_meta.get("bbox_width", 0) or 0)))
                fh = int(max(0, float(fresh_meta.get("bbox_height", 0) or 0)))
                if fw >= 12 and fh >= 12:
                    selected_bbox = {
                        "bbox_x": int(max(0, float(fresh_meta.get("bbox_x", 0) or 0))),
                        "bbox_y": int(max(0, float(fresh_meta.get("bbox_y", 0) or 0))),
                        "bbox_width": fw,
                        "bbox_height": fh,
                    }
        except Exception:
            pass

        _update_runtime_panel(
            page,
            current_definition_row,
            popup_open,
            popup_status,
            phase="target_found",
            phase_detail="대상 위치로 이동 후 캡처",
        )

        current_event_name = (
            str(current_definition_row.get("event_name", "")).strip()
            if current_definition_row
            else ""
        )
        current_section_name = (
            str(_get_expected_param(current_definition_row, "section_name")).strip()
            if current_definition_row
            else ""
        )
        current_event_type = _normalize_event_type_label(
            str(current_definition_row.get("event_type", "")).strip()
            if current_definition_row
            else ""
        )
        if not current_event_type:
            current_event_type = _classify_event_type(current_event_name, {})
        annotation_key = (
            _canonical_key_for_event(current_event_name, current_section_name)
            or current_event_name
            or current_event_type
            or "event"
        )
        annotation_no = _next_event_annotation_index(session_id, annotation_key)
        target_border_color = "#f97316" if current_event_type == "impression" else "#ef4444"
        hit_border_color = "#f97316" if current_event_type == "impression" else "#22c55e"

        _apply_hit_border(page, hit_border_color)
        # raw 스크린샷(오버레이 없는 순수 화면)을 먼저 저장
        raw_screenshot_file = ""
        if save_raw_screenshot:
            try:
                raw_path = screenshot_path.with_name(
                    f"{screenshot_path.stem}_raw{screenshot_path.suffix}"
                )
                raw_path.parent.mkdir(parents=True, exist_ok=True)
                page.screenshot(
                    path=str(raw_path),
                    full_page=False,
                    type=screenshot_format,
                    quality=(
                        screenshot_quality
                        if screenshot_format in {"jpeg", "webp"}
                        else None
                    ),
                )
                raw_screenshot_file = str(raw_path)
            except Exception:
                raw_screenshot_file = ""

        # 오버레이(빨간 박스 + 넘버링) 표시 후 스크린샷
        _show_target_overlay(
            page,
            selected_bbox,
            annotation_no,
            selected_meta,
            phase="target",
            event_name=current_event_name,
            event_type=current_event_type,
        )
        try:
            page.wait_for_timeout(180)
        except Exception:
            pass

        try:
            _, selected_bbox = _capture_annotated_screenshot(
                page,
                screenshot_path,
                selected_bbox,
                annotation_no,
                str(selected_meta.get("selector", "")).strip(),
                save_raw=False,  # raw은 이미 위에서 저장
                image_format=screenshot_format,
                quality=screenshot_quality,
                border_color=target_border_color,
                badge_text=f"{current_event_name} #{annotation_no}",
            )
        except Exception:
            screenshot_file = ""

        click_reason = "clicked"
        hit_count_before_click = _read_session_hit_count(session_id)
        if hit_count_before_click < 0:
            hit_count_before_click = _read_page_hit_count(page)
        view_signature_before_click = _collect_view_candidate_signature(page)
        expected_event_name = (
            str(current_definition_row.get("event_name", "")).strip()
            if current_definition_row
            else ""
        )
        expected_section_name = (
            str(_get_expected_param(current_definition_row, "section_name")).strip()
            if current_definition_row
            else ""
        )
        expected_target_id = (
            str(
                _get_expected_param(
                    current_definition_row, "target_id", fallback_row_key=True
                )
            ).strip()
            if current_definition_row
            else ""
        )
        expected_button_id = (
            str(
                _get_expected_param(
                    current_definition_row, "button_id", fallback_row_key=False
                )
            ).strip()
            if current_definition_row
            else ""
        )
        expected_page_id = (
            str(_get_expected_param(current_definition_row, "page_id")).strip()
            if current_definition_row
            else ""
        )
        expected_event_family = _event_family_simple(expected_event_name)
        expected_state_changes: List[str] = []
        if row_expected_kind == "tab":
            expected_state_changes.append("tab_selection_changed")
        elif row_expected_kind == "filter":
            expected_state_changes.append("filter_state_changed")
        elif row_expected_kind in {"content", "cta", "menu"}:
            expected_state_changes.append("content_or_section_changed")
        click_hypothesis = {
            "expected_event_name": expected_event_name,
            "expected_event_family": expected_event_family,
            "expected_section_name": expected_section_name,
            "expected_target_id": expected_target_id,
            "expected_button_id": expected_button_id,
            "expected_page_id": expected_page_id,
            "expected_dom_change": bool(row_expected_kind in {"tab", "filter", "content", "cta", "menu"}),
            "expected_state_changes": expected_state_changes,
            "row_expected_kind": row_expected_kind,
            "selected_target_id": str(selected_meta.get("target_id", "")).strip(),
            "selected_button_id": str(selected_meta.get("button_id", "")).strip(),
            "selected_section_name": str(selected_meta.get("section_name", "")).strip(),
            "selected_selector": str(selected_meta.get("selector", "")).strip(),
        }
        _emit_auto_crawl_event(
            session_id,
            "auto_click_hypothesis",
            current_url,
            {
                **click_hypothesis,
                "auto_click_index": clicked_count,
                "run_mode": "Auto Crawl",
                "hit_count_before": int(hit_count_before_click if hit_count_before_click >= 0 else 0),
            },
        )
        try:
            href = str(selected_meta.get("href", "")).strip()
            if href and (
                block_link_navigation
                or (
                    scope_prefix and not _is_url_in_scope(href, start_url, scope_prefix)
                )
            ):
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
                try:
                    selected_handle.click(timeout=1800)
                except Exception:
                    selected_handle.click(timeout=2500, force=True)
        except Exception as exc:
            click_reason = f"error:{exc}"

        _show_target_overlay(
            page,
            selected_bbox,
            annotation_no,
            selected_meta,
            phase="clicked" if click_reason == "clicked" else "target",
            event_name=current_event_name,
            event_type=current_event_type,
        )
        _clear_hit_border(page)
        _update_runtime_panel(
            page,
            current_definition_row,
            popup_open,
            popup_status,
            phase="hit_wait",
            phase_detail="이벤트 발생 및 GA hit 대기",
        )

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
                "class_attribute": str(
                    selected_meta.get("class_attribute", "")
                ).strip(),
                "ui_role": str(selected_meta.get("ui_role", "")).strip(),
                "selector_pattern": str(
                    selected_meta.get("selector_pattern", "")
                ).strip(),
                "tag": str(selected_meta.get("tag", "")).strip(),
                "navigation_blocked": navigation_blocked,
                "hit_count_before": int(hit_count_before_click if hit_count_before_click >= 0 else 0),
                "url": current_url,
                "auto_click_index": clicked_count,
                "annotation_no": annotation_no,
                "max_auto_clicks": max_auto_clicks,
                "run_mode": "Auto Crawl",
                "current_definition_row_id": str(
                    current_definition_row.get("definition_row_id", "")
                ).strip(),
                "current_definition_row_no": str(
                    current_definition_row.get("no", "")
                ).strip(),
                "current_definition_event_name": str(
                    current_definition_row.get("event_name", "")
                ).strip(),
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
            # GA4 hit이 보통 200-400ms 내 발생 → domcontentloaded로 충분
            # networkidle은 analytics XHR 때문에 항상 느림
            page.wait_for_load_state(
                "domcontentloaded",
                timeout=min(350, max(120, int(wait_after_click_ms))),
            )
        except Exception:
            pass
        if _wait_interruptible(
            page,
            session_id,
            max(0, int(wait_after_click_ms) - 350),
            step_ms=100,
        ):
            continue
        hit_count_after_click = _read_session_hit_count(session_id)
        if hit_count_after_click < 0:
            hit_count_after_click = _read_page_hit_count(page)
        view_signature_after_click = _collect_view_candidate_signature(page)
        view_changed = bool(
            view_signature_before_click
            and view_signature_after_click
            and view_signature_before_click != view_signature_after_click
        )
        url_changed = _build_compare_key(page.url) != _build_compare_key(current_url)
        hit_delta = (
            max(0, int(hit_count_after_click - hit_count_before_click))
            if hit_count_before_click >= 0 and hit_count_after_click >= 0
            else 0
        )
        network_hit_confirmed = hit_delta > 0
        observed_last_hit_event = _read_session_last_hit_event(session_id)
        if not observed_last_hit_event:
            observed_last_hit_event = _read_page_last_hit_event(page)
        observed_last_hit_family = _event_family_simple(observed_last_hit_event)
        expected_event_name = str(click_hypothesis.get("expected_event_name", "")).strip()
        expected_event_family = str(click_hypothesis.get("expected_event_family", "")).strip()
        event_expectation_met = True
        if expected_event_name:
            event_expectation_met = bool(
                observed_last_hit_event == expected_event_name
                or (
                    expected_event_family
                    and observed_last_hit_family == expected_event_family
                    and network_hit_confirmed
                )
            )
        dom_expectation_required = bool(click_hypothesis.get("expected_dom_change", False))
        dom_expectation_met = (
            (not dom_expectation_required)
            or bool(view_changed)
            or bool(url_changed)
        )
        state_expectation_required = bool(
            isinstance(click_hypothesis.get("expected_state_changes", []), list)
            and click_hypothesis.get("expected_state_changes", [])
        )
        state_expectation_met = (
            (not state_expectation_required)
            or bool(url_changed)
            or bool(view_changed)
            or bool(network_hit_confirmed)
        )
        overall_hypothesis_met = bool(
            event_expectation_met and dom_expectation_met and state_expectation_met
        )
        selected_pattern_key = (
            str(selected_meta.get("pattern_key", "")).strip()
            or str(selected_meta.get("structure_key", "")).strip()
            or str(selected_meta.get("selector_pattern", "")).strip()
        )
        if selected_pattern_key:
            stat = pattern_hit_stats.setdefault(selected_pattern_key, {"attempts": 0, "hits": 0})
            stat["attempts"] = int(stat.get("attempts", 0)) + 1
            if network_hit_confirmed:
                stat["hits"] = int(stat.get("hits", 0)) + 1
        _emit_auto_crawl_event(
            session_id,
            "auto_click_hit_check",
            page.url,
            {
                "pattern_key": selected_pattern_key,
                "network_hit_confirmed": bool(network_hit_confirmed),
                "network_hit_delta": int(hit_delta),
                "view_changed": bool(view_changed),
                "url_changed": bool(url_changed),
                "hit_count_before": int(hit_count_before_click if hit_count_before_click >= 0 else 0),
                "hit_count_after": int(hit_count_after_click if hit_count_after_click >= 0 else 0),
                "auto_click_index": clicked_count,
                "run_mode": "Auto Crawl",
            },
        )
        _emit_auto_crawl_event(
            session_id,
            "auto_click_hypothesis_check",
            page.url,
            {
                **click_hypothesis,
                "observed_last_hit_event": observed_last_hit_event,
                "observed_last_hit_family": observed_last_hit_family,
                "network_hit_confirmed": bool(network_hit_confirmed),
                "network_hit_delta": int(hit_delta),
                "view_changed": bool(view_changed),
                "url_changed": bool(url_changed),
                "event_expectation_met": bool(event_expectation_met),
                "dom_expectation_met": bool(dom_expectation_met),
                "state_expectation_met": bool(state_expectation_met),
                "hypothesis_met": bool(overall_hypothesis_met),
                "auto_click_index": clicked_count,
                "run_mode": "Auto Crawl",
            },
        )
        recovery_action = "none"
        if overall_hypothesis_met:
            hypothesis_fail_streak = 0
        else:
            hypothesis_fail_streak += 1
            post_popup_state = _get_popup_layer_state(page)
            if bool(post_popup_state.get("popup_open", False)):
                recovery_action = "popup_priority_next"
            elif not network_hit_confirmed and not view_changed and not url_changed:
                extra_wait_ms = min(
                    2400,
                    max(
                        700,
                        int(wait_after_click_ms) + (400 * min(3, int(hypothesis_fail_streak))),
                    ),
                )
                if _wait_interruptible(
                    page,
                    session_id,
                    max(0, int(extra_wait_ms)),
                    step_ms=120,
                ):
                    continue
                re_hit_after = _read_session_hit_count(session_id)
                if re_hit_after < 0:
                    re_hit_after = _read_page_hit_count(page)
                re_view_signature = _collect_view_candidate_signature(page)
                re_view_changed = bool(
                    view_signature_after_click
                    and re_view_signature
                    and view_signature_after_click != re_view_signature
                )
                re_hit_delta = (
                    max(0, int(re_hit_after - hit_count_after_click))
                    if hit_count_after_click >= 0 and re_hit_after >= 0
                    else 0
                )
                if re_hit_delta > 0 or re_view_changed:
                    network_hit_confirmed = bool(network_hit_confirmed or (re_hit_delta > 0))
                    view_changed = bool(view_changed or re_view_changed)
                    overall_hypothesis_met = bool(
                        event_expectation_met
                        and (
                            (not dom_expectation_required)
                            or bool(view_changed)
                            or bool(url_changed)
                        )
                        and (
                            (not state_expectation_required)
                            or bool(url_changed)
                            or bool(view_changed)
                            or bool(network_hit_confirmed)
                        )
                    )
                    if overall_hypothesis_met:
                        hypothesis_fail_streak = 0
                        recovery_action = "loading_recheck_recovered"
                    else:
                        recovery_action = "loading_recheck_partial"
                else:
                    recovery_action = "loading_recheck_no_change"
            if selected_pattern_key and hypothesis_fail_streak >= 2:
                seen_pattern_keys.add(selected_pattern_key)
                recovery_action = (
                    f"{recovery_action}|deprioritize_pattern"
                    if recovery_action != "none"
                    else "deprioritize_pattern"
                )
        if recovery_action != "none":
            _emit_auto_crawl_event(
                session_id,
                "auto_hypothesis_recovery",
                page.url,
                {
                    "recovery_action": recovery_action,
                    "hypothesis_fail_streak": int(hypothesis_fail_streak),
                    "hypothesis_met": bool(overall_hypothesis_met),
                    "row_expected_kind": row_expected_kind,
                    "pattern_key": selected_pattern_key,
                    "network_hit_confirmed": bool(network_hit_confirmed),
                    "view_changed": bool(view_changed),
                    "url_changed": bool(url_changed),
                    "auto_click_index": clicked_count,
                    "run_mode": "Auto Crawl",
                },
            )
        if selected_section_name:
            sec_key = selected_section_name.lower()
            if network_hit_confirmed or view_changed:
                section_fail_streak[sec_key] = 0
            else:
                section_fail_streak[sec_key] = int(section_fail_streak.get(sec_key, 0)) + 1
                if int(section_fail_streak.get(sec_key, 0)) >= 2:
                    section_cooldown_until_click[sec_key] = int(
                        clicked_count + max(1, section_fail_cooldown_turns)
                    )
                    _emit_auto_crawl_event(
                        session_id,
                        "auto_section_blacklist",
                        page.url,
                        {
                            "reason": "consecutive_section_failures",
                            "section_name": selected_section_name,
                            "fail_streak": int(section_fail_streak.get(sec_key, 0)),
                            "cooldown_until_click": int(section_cooldown_until_click.get(sec_key, clicked_count)),
                            "auto_click_index": clicked_count,
                            "run_mode": "Auto Crawl",
                        },
                    )
        if (not definition_validation_mode) and _is_content_like_candidate(selected_meta):
            section_name_clicked = str(selected_meta.get("section_name", "")).strip()
            if section_name_clicked:
                content_section_click_counts[section_name_clicked] = int(
                    content_section_click_counts.get(section_name_clicked, 0)
                ) + 1
                if not goal_event_set:
                    seen_content_sections.add(section_name_clicked)
                    if len(seen_content_sections) > 200:
                        seen_content_sections = set(list(seen_content_sections)[-140:])

        if not definition_validation_mode:
            # 백그라운드 hit(주기성 요청)로 novelty가 계속 리셋되는 것을 방지하기 위해
            # 화면 구조/후보 시그니처 변화가 있을 때만 "의미 있는 진행"으로 본다.
            if view_changed:
                no_novelty_streak = 0
            else:
                no_novelty_streak += 1
                if no_novelty_streak >= max(2, int(no_novelty_limit)):
                    _emit_auto_crawl_event(
                        session_id,
                        "auto_crawl_no_novelty_stop",
                        page.url,
                        {
                            "reason": "consecutive_no_view_change",
                            "no_novelty_streak": int(no_novelty_streak),
                            "no_novelty_limit": int(no_novelty_limit),
                            "network_hit_confirmed_last": bool(network_hit_confirmed),
                            "auto_click_index": clicked_count,
                            "run_mode": "Auto Crawl",
                        },
                    )
                    if scenario_seed_mode:
                        no_novelty_streak = 0
                        if _advance_scenario_seed("step_no_novelty"):
                            continue
                        return "scenario_steps_completed"
                    return "no_novelty_stop"

        if definition_targets:
            target_ids, matched_ids, definition_done = _get_definition_progress(
                session_id, settings
            )
            if definition_done:
                should_emit = False
                with _LOCK:
                    session = _SESSIONS.get(session_id)
                    if session and not session.definition_targets_reached:
                        session.definition_targets_reached = True
                        should_emit = True
                if should_emit:
                    _emit_auto_crawl_event(
                        session_id,
                        "definition_targets_reached",
                        page.url,
                        {
                            "reason": "all_definition_rows_matched",
                            "matched_definition_rows": matched_ids,
                            "definition_row_ids": target_ids,
                            "current_definition_row_id": str(
                                current_definition_row.get("definition_row_id", "")
                            ).strip(),
                            "current_definition_row_no": str(
                                current_definition_row.get("no", "")
                            ).strip(),
                            "run_mode": "Auto Crawl",
                            "auto_click_index": clicked_count,
                        },
                    )
                if not definition_validation_mode:
                    return "definition_targets_reached"

        if (single_page_only or restrict_to_start_url) and _build_compare_key(
            page.url
        ) != start_compare_key:
            try:
                page.goto(start_url, wait_until="domcontentloaded")
                if _wait_interruptible(page, session_id, click_interval_ms, step_ms=100):
                    continue
            except Exception:
                pass
        else:
            if _wait_interruptible(page, session_id, click_interval_ms, step_ms=100):
                continue

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
        return "max_auto_clicks_reached"
    return "stopped"


def _run_debug_session(session_id: str) -> None:
    session = _get_session(session_id)
    if not session:
        return
    browser = None
    context = None
    page = None

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
            browser = _launch_browser(
                (p, str(run_settings.get("browser_name", "chrome"))),
                run_settings,
            )
            context = browser.new_context(**_build_context_kwargs(p, run_settings))

            def on_emit(source, payload):  # noqa: ANN001
                try:
                    if not isinstance(payload, dict):
                        return
                    payload.setdefault(
                        "captured_at", datetime.now(timezone.utc).isoformat()
                    )
                    payload.setdefault("session_id", session_id)
                    payload.setdefault("source", "browser")
                    source_name = str(payload.get("source", "")).strip()
                    event_name = str(payload.get("event_name", "")).strip()
                    is_manual_flow = (
                        source_name == "manual_flow" or event_name == "manual_flow_step"
                    )
                    _record_runtime_payload(
                        session_id,
                        payload,
                        count_as_event=not is_manual_flow,
                    )
                    if is_manual_flow:
                        _remember_manual_click(session_id, payload)
                        _buffer_manual_flow_step(session_id, payload)
                except Exception:
                    return

            context.expose_binding("__qaDebugEmit", on_emit)

            def on_command(source, payload):  # noqa: ANN001
                try:
                    if not isinstance(payload, dict):
                        return
                    action = str(payload.get("action", "")).strip()
                    if action == "auto_crawl_pause":
                        pause_auto_crawl(session_id, True, reason="manual")
                    elif action == "auto_crawl_resume":
                        pause_auto_crawl(session_id, False, reason="")
                    elif action == "manual_recording_on":
                        with _LOCK:
                            s = _SESSIONS.get(session_id)
                            if s:
                                s.manual_recording_enabled = True
                                if isinstance(s.run_settings, dict):
                                    s.run_settings["manual_recording_enabled"] = True
                    elif action == "manual_recording_off":
                        with _LOCK:
                            s = _SESSIONS.get(session_id)
                            if s:
                                s.manual_recording_enabled = False
                                if isinstance(s.run_settings, dict):
                                    s.run_settings["manual_recording_enabled"] = False
                    elif action == "manual_recording_start":
                        with _LOCK:
                            s = _SESSIONS.get(session_id)
                            if s:
                                s.manual_recording_enabled = True
                                s.manual_record_buffer = []
                                if isinstance(s.run_settings, dict):
                                    s.run_settings["manual_recording_enabled"] = True
                    elif action == "manual_recording_stop":
                        with _LOCK:
                            s = _SESSIONS.get(session_id)
                            if s:
                                s.manual_recording_enabled = False
                                if isinstance(s.run_settings, dict):
                                    s.run_settings["manual_recording_enabled"] = False
                    elif action == "manual_recording_save":
                        recording_name = str(payload.get("name", "")).strip()
                        with _LOCK:
                            s = _SESSIONS.get(session_id)
                            if s:
                                s.manual_recording_name = recording_name
                                if isinstance(s.run_settings, dict):
                                    s.run_settings["manual_recording_name"] = recording_name
                        _flush_manual_recording_buffer(session_id, reason="manual_save")
                    elif action == "manual_profile_select":
                        profile = str(payload.get("profile", "")).strip() or "default"
                        with _LOCK:
                            s = _SESSIONS.get(session_id)
                            if s:
                                s.manual_profile_selected = profile
                                if isinstance(s.run_settings, dict):
                                    s.run_settings["manual_profile_selected"] = profile
                    elif action == "manual_recording_select":
                        recording_id = str(payload.get("recording_id", "")).strip()
                        with _LOCK:
                            s = _SESSIONS.get(session_id)
                            if s:
                                s.manual_recording_selected = recording_id
                                if isinstance(s.run_settings, dict):
                                    s.run_settings["manual_recording_selected"] = recording_id
                    elif action == "manual_recording_delete":
                        recording_id = str(payload.get("recording_id", "")).strip()
                        with _LOCK:
                            s = _SESSIONS.get(session_id)
                            profile = (
                                str(getattr(s, "manual_profile_selected", "default")).strip()
                                if s
                                else "default"
                            ) or "default"
                            rs = (
                                dict(getattr(s, "run_settings", {}) or {})
                                if s and isinstance(getattr(s, "run_settings", {}), dict)
                                else {}
                            )
                        deleted = _delete_manual_recording(rs, profile, recording_id)
                        if deleted:
                            with _LOCK:
                                s = _SESSIONS.get(session_id)
                                if s and str(s.manual_recording_selected or "").strip() == recording_id:
                                    s.manual_recording_selected = ""
                                if s and isinstance(s.run_settings, dict):
                                    s.run_settings["manual_recording_catalog"] = _load_manual_recording_catalog(
                                        s.run_settings, profile
                                    )
                                    if str(s.run_settings.get("manual_recording_selected", "")).strip() == recording_id:
                                        s.run_settings["manual_recording_selected"] = ""
                    elif action == "manual_recording_name_set":
                        recording_name = str(payload.get("name", "")).strip()
                        with _LOCK:
                            s = _SESSIONS.get(session_id)
                            if s:
                                s.manual_recording_name = recording_name
                                if isinstance(s.run_settings, dict):
                                    s.run_settings["manual_recording_name"] = recording_name
                    elif action == "manual_replay_on":
                        with _LOCK:
                            s = _SESSIONS.get(session_id)
                            if s:
                                s.manual_flow_replay_enabled = True
                                if isinstance(s.run_settings, dict):
                                    s.run_settings["manual_flow_replay_enabled"] = True
                    elif action == "manual_replay_off":
                        with _LOCK:
                            s = _SESSIONS.get(session_id)
                            if s:
                                s.manual_flow_replay_enabled = False
                                if isinstance(s.run_settings, dict):
                                    s.run_settings["manual_flow_replay_enabled"] = False
                    elif action == "manual_replay_run":
                        recording_id = str(payload.get("recording_id", "")).strip()
                        if recording_id:
                            with _LOCK:
                                s = _SESSIONS.get(session_id)
                                if s:
                                    s.manual_recording_selected = recording_id
                                    if isinstance(s.run_settings, dict):
                                        s.run_settings["manual_recording_selected"] = recording_id
                    elif action == "stop_test":
                        stop_debug_session(session_id)
                except Exception:
                    return

            context.expose_binding("__qaRuntimeCommand", on_command)
            definition_events = _get_definition_event_targets(run_settings)
            definition_event_type_map = _get_definition_event_type_map(run_settings)
            selected_profile = str(
                run_settings.get("manual_profile_selected", "default")
            ).strip() or "default"
            recording_catalog_for_ui = _normalize_manual_recording_catalog(
                run_settings.get("manual_recording_catalog", [])
            )
            if not recording_catalog_for_ui:
                recording_catalog_for_ui = _load_manual_recording_catalog(
                    run_settings, selected_profile
                )
            restrict_prefix = ""
            if bool(run_settings.get("restrict_to_start_url", True)):
                restrict_prefix = _build_scope_prefix(session.target_url)
            context.add_init_script(
                _build_init_script(
                    session_id,
                    definition_events,
                    restrict_prefix=restrict_prefix,
                    manual_profile_options=(
                        run_settings.get("manual_profile_options", [])
                        if isinstance(run_settings.get("manual_profile_options", []), list)
                        else []
                    ),
                    manual_profile_selected=str(
                        run_settings.get("manual_profile_selected", "default")
                    ).strip()
                    or "default",
                    manual_recording_catalog=recording_catalog_for_ui,
                    manual_recording_selected=str(
                        run_settings.get("manual_recording_selected", "")
                    ).strip(),
                    manual_recording_name=str(
                        run_settings.get("manual_recording_name", "")
                    ).strip(),
                    manual_recording_enabled=bool(
                        run_settings.get("manual_recording_enabled", False)
                    ),
                    manual_flow_replay_enabled=bool(
                        run_settings.get("manual_flow_replay_enabled", True)
                    ),
                    auto_crawl_start_mode=str(
                        run_settings.get("auto_crawl_start_mode", "manual")
                    ).strip()
                    or "manual",
                )
            )

            def on_request(request):  # noqa: ANN001
                try:
                    request_url = str(request.url)
                    request_method = str(request.method)
                    request_body = str(request.post_data or "")
                    hits = _extract_analytics_hit_payloads(
                        url=request_url,
                        method=request_method,
                        post_data=request_body,
                        session_id=session_id,
                    )
                    _record_analytics_probe_if_needed(
                        session_id=session_id,
                        request_url=request_url,
                        request_method=request_method,
                        request_body=request_body,
                        extracted_hits=hits,
                    )
                    if not hits:
                        return
                    annotated_hits = _annotate_definition_scope(session_id, hits)
                    for hit in annotated_hits:
                        event_name = str(hit.get("event_name", "")).strip()
                        event_type = _classify_event_type(
                            event_name, definition_event_type_map
                        )
                        params = (
                            hit.get("params", {})
                            if isinstance(hit.get("params", {}), dict)
                            else {}
                        )
                        section_name = str(params.get("section_name", "")).strip()
                        canonical_key = _canonical_key_for_event(event_name, section_name)
                        canonical_key_no = (
                            _next_event_annotation_index(session_id, canonical_key)
                            if canonical_key
                            else _next_event_annotation_index(session_id, event_name or event_type)
                        )
                        border_color = _action_color_for_event_type(event_type)
                        hit["border_color"] = border_color
                        hit["event_type"] = event_type
                        hit["canonical_key"] = canonical_key
                        hit["canonical_key_no"] = canonical_key_no
                        screenshot_paths = _capture_ga_hit_screenshots(
                            page,
                            session_id,
                            {
                                "event_name": event_name,
                                "border_color": border_color,
                                "event_type": event_type,
                                "canonical_key_no": canonical_key_no,
                                "params": params,
                            },
                            run_settings,
                        )
                        if screenshot_paths:
                            hit["screenshot_path"] = screenshot_paths[0]
                            hit["selector_screenshot"] = screenshot_paths[0]
                            if len(screenshot_paths) > 1:
                                hit["screenshot_path_2"] = screenshot_paths[1]
                            params["screenshot_path"] = screenshot_paths[0]
                            params["selector_screenshot"] = screenshot_paths[0]
                            if len(screenshot_paths) > 1:
                                params["screenshot_path_2"] = screenshot_paths[1]
                            params["event_type"] = event_type
                            params["canonical_key"] = canonical_key
                            params["canonical_key_no"] = canonical_key_no
                            hit["params"] = params
                    _ingest_ga_hit_payloads(
                        session_id=session_id,
                        hits=annotated_hits,
                        output_path=output_path,
                        db_path=session.db_path,
                    )
                except Exception:
                    return

            context.on("request", on_request)

            page = context.new_page()
            test_url = _append_query(
                session.target_url,
                {"qa_debug_mode": "1", "qa_debug_session_id": session_id},
            )
            _open_session_page_with_retry(
                page=page,
                session_id=session_id,
                target_url=session.target_url,
                debug_url=test_url,
            )
            page.bring_to_front()
            _ensure_manual_replay_compatibility(page, session_id, run_settings)

            if run_settings.get("auto_crawl_enabled", True):
                try:
                    page.wait_for_timeout(1200)
                    crawl_outcome = _run_auto_crawl(
                        page, session_id, run_settings, session.stop_event
                    )
                except Exception as exc:
                    crawl_outcome = "error"
                    _emit_auto_crawl_event(
                        session_id,
                        "auto_crawl_error",
                        page.url,
                        {"reason": str(exc), "run_mode": "Auto Crawl"},
                    )
                if crawl_outcome == "challenge_detected":
                    _set_session_fields(
                        session_id,
                        last_error="접근 제한/봇 확인 페이지가 감지되어 브라우저를 열어둔 채 대기합니다. 확인 후 Stop Test로 종료하세요.",
                    )
                elif run_settings.get("auto_stop_after_crawl", True):
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
    finally:
        # 예외 경로에서도 브라우저 핸들을 정리해 FD 누수를 방지한다.
        try:
            if page is not None:
                page.close()
        except Exception:
            pass
        try:
            if context is not None:
                context.close()
        except Exception:
            pass
        try:
            if browser is not None:
                browser.close()
        except Exception:
            pass


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

    hits = _extract_analytics_hit_payloads(
        url=request_url.strip(),
        method=(request_method or "GET").strip().upper(),
        post_data=request_body or "",
        session_id=sid,
    )
    _record_analytics_probe_if_needed(
        session_id=sid,
        request_url=request_url.strip(),
        request_method=(request_method or "GET").strip().upper(),
        request_body=request_body or "",
        extracted_hits=hits,
    )
    if not hits:
        return 0

    annotated_hits = _annotate_definition_scope(sid, hits)
    return _ingest_ga_hit_payloads(
        session_id=sid,
        hits=annotated_hits,
        output_path=output_path,
        db_path=db_path,
    )


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
    effective_db_path = (
        Path(db_path) if db_path is not None else Path("data/test_logs/qa_runs.db")
    )

    with _LOCK:
        running_count = sum(
            1
            for s in _SESSIONS.values()
            if str(getattr(s, "status", "")).strip() == "running"
        )
        if running_count >= _max_running_debug_sessions():
            raise RuntimeError(
                f"동시 디버깅 세션 제한을 초과했습니다. "
                f"(running={running_count}, limit={_max_running_debug_sessions()})"
            )
        existing = _SESSIONS.get(sid)
        if existing and existing.status == "running":
            return get_debug_session_snapshot(sid)

        stop_event = threading.Event()
        normalized_settings = _normalize_run_settings(run_settings)
        start_mode = str(
            normalized_settings.get("auto_crawl_start_mode", "manual")
        ).strip().lower()
        start_paused = start_mode == "manual"
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
            run_settings=normalized_settings,
            matched_definition_events=set(),
            matched_definition_rows=set(),
            skipped_definition_rows=set(),
            current_definition_row_id="",
            auto_crawl_paused=start_paused,
            auto_crawl_pause_reason="manual_default" if start_paused else "",
            manual_recording_enabled=bool(
                normalized_settings.get("manual_recording_enabled", False)
            ),
            manual_profile_selected=str(
                normalized_settings.get(
                    "manual_profile_selected", "default"
                )
                or "default"
            ).strip()
            or "default",
            manual_flow_replay_enabled=bool(
                normalized_settings.get("manual_flow_replay_enabled", True)
            ),
            manual_recording_selected=str(
                normalized_settings.get("manual_recording_selected", "")
            ).strip(),
            manual_recording_name=str(
                normalized_settings.get("manual_recording_name", "")
            ).strip(),
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
    thread_ref = None
    with _LOCK:
        session = _SESSIONS.get(sid)
        if not session:
            return {}
        session.stop_event.set()
        if session.status == "running":
            session.status = "stopping" if session.launch_browser else "stopped"
            if not session.launch_browser:
                session.ended_at = datetime.now(timezone.utc).isoformat()
        thread_ref = session.thread
    # stop 이후 짧게 join해서 종료된 thread가 정리되도록 한다.
    try:
        if thread_ref and thread_ref.is_alive():
            thread_ref.join(timeout=1.5)
    except Exception:
        pass
    with _LOCK:
        session = _SESSIONS.get(sid)
        if session and session.status == "stopping":
            alive = bool(session.thread and session.thread.is_alive())
            if not alive:
                session.status = "stopped"
                if not str(session.ended_at or "").strip():
                    session.ended_at = datetime.now(timezone.utc).isoformat()
    _sync_session_db(sid)
    return get_debug_session_snapshot(sid)


def pause_auto_crawl(
    session_id: str, paused: bool, reason: str = "manual"
) -> Dict[str, object]:
    sid = session_id.strip()
    if not sid:
        return {}
    with _LOCK:
        session = _SESSIONS.get(sid)
        if not session:
            return {}
        session.auto_crawl_paused = bool(paused)
        session.auto_crawl_pause_reason = str(reason or "").strip() if paused else ""
    return get_debug_session_snapshot(sid)


def get_debug_session_snapshot(session_id: str) -> Dict[str, object]:

    sid = session_id.strip()
    if not sid:
        return {}
    with _LOCK:
        session = _SESSIONS.get(sid)
        if not session:
            return {}
        current_row_no = ""
        for row in _get_definition_runtime_rows(session.run_settings):
            if (
                str(row.get("definition_row_id", "")).strip()
                == str(session.current_definition_row_id).strip()
            ):
                current_row_no = str(row.get("no", "")).strip()
                break
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
            "current_definition_row_id": session.current_definition_row_id,
            "current_definition_row_no": current_row_no,
            "matched_definition_row_count": len(session.matched_definition_rows),
            "skipped_definition_row_count": len(session.skipped_definition_rows),
            "matched_definition_event_names": sorted(session.matched_definition_events),
            "matched_definition_row_ids": sorted(session.matched_definition_rows),
            "skipped_definition_row_ids": sorted(session.skipped_definition_rows),
            "auto_crawl_paused": bool(session.auto_crawl_paused),
            "auto_crawl_pause_reason": str(
                session.auto_crawl_pause_reason or ""
            ).strip(),
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
