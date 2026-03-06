from __future__ import annotations

from datetime import date, datetime, timedelta
import hashlib
import json
import os
import io
import re
import shutil
import sqlite3
import zipfile
from pathlib import Path
from typing import Dict, List, Tuple
from urllib import error as urllib_error
from urllib import parse as urllib_parse
from urllib import request as urllib_request
from urllib.parse import parse_qs, urlparse
from uuid import uuid4
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components

from src.debug_runtime import (
    get_debug_session_snapshot,
    load_debug_events,
    start_debug_session,
    stop_debug_session,
)
from src.flatten import flatten_events, normalize_param_name
from src.funnel import analyze_funnel_sequence
from src.issue_store import (
    DEFAULT_ISSUE_PATH,
    issues_to_dataframe,
    resolve_issue,
    upsert_auto_issues,
)
from src.qa_ingest_server import ensure_ingest_server
from src.reporting import (
    build_report_filename,
    events_to_csv_bytes,
    results_to_csv_bytes,
    results_to_pdf_bytes,
)
from src.scenario_validation import validate_single_scenario
from src.rules import run_qa_rules
from src.scoring import compute_integrity_score
from src.test_log_db import append_ui_action, get_session, init_test_log_db, list_recent_sessions


st.set_page_config(page_title="GA4 QA Reporter", layout="wide")
st.title("GA4 QA 리포터 (룰 기반)")
st.caption("실시간 DebugView 대체가 아닌, 로그 수집 + 자동 정리 + 룰 기반 판정을 위한 내부 QA 도구")
BASE_DIR = Path(__file__).resolve().parent
DEFAULT_OAUTH_REDIRECT_URI = "https://asknuggetdata.com/oauth2callback"
TEST_LOG_DB_PATH = Path("data/test_logs/qa_runs.db")
SCHEMA_STORE_PATH = Path("data/schemas/event_schemas.json")
WORKSPACE_ROOT = Path("data/workspace")
DEFAULT_PROJECT_SLUG = "default-project"
OAUTH_CONTEXT_DIR = Path("data/oauth_context")
CIRCLED_NUMBERS = {
    1: "①", 2: "②", 3: "③", 4: "④", 5: "⑤", 6: "⑥", 7: "⑦", 8: "⑧", 9: "⑨", 10: "⑩",
    11: "⑪", 12: "⑫", 13: "⑬", 14: "⑭", 15: "⑮", 16: "⑯", 17: "⑰", 18: "⑱", 19: "⑲", 20: "⑳",
}


def _slugify_project_name(text: str) -> str:
    raw = str(text or "").strip().lower()
    if not raw:
        return DEFAULT_PROJECT_SLUG
    # Keep unicode alnum chars (e.g. Korean), normalize separators to '-'
    normalized = "".join(ch if (ch.isalnum() or ch in {"-", "_"}) else "-" for ch in raw)
    normalized = re.sub(r"[-_]{2,}", "-", normalized)
    slug = normalized.strip("-_")
    return slug or DEFAULT_PROJECT_SLUG


def to_circled_number(n: int) -> str:
    return CIRCLED_NUMBERS.get(int(n), str(n))


def get_current_app_base_url() -> str:
    override = get_config_value("GA4_APP_BASE_URL", "").strip()
    if override:
        return override.rstrip("/")
    headers = _get_request_headers()
    host = str(headers.get("x-forwarded-host", "")).strip() or str(headers.get("host", "")).strip()
    proto = str(headers.get("x-forwarded-proto", "")).split(",")[0].strip().lower()
    if not host:
        return ""
    if proto not in {"http", "https"}:
        proto = "https"
    return f"{proto}://{host}".rstrip("/")


def save_oauth_context(state: str, context: Dict[str, object]) -> None:
    sid = str(state or "").strip()
    if not sid:
        return
    OAUTH_CONTEXT_DIR.mkdir(parents=True, exist_ok=True)
    payload = dict(context)
    payload["saved_at"] = datetime.now(tz=ZoneInfo("UTC")).isoformat()
    (OAUTH_CONTEXT_DIR / f"{sid}.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def pop_oauth_context(state: str) -> Dict[str, object]:
    sid = str(state or "").strip()
    if not sid:
        return {}
    path = OAUTH_CONTEXT_DIR / f"{sid}.json"
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            data = {}
    except Exception:
        data = {}
    try:
        path.unlink(missing_ok=True)
    except Exception:
        pass
    return data


def _workspace_projects_root() -> Path:
    return WORKSPACE_ROOT / "projects"


def list_workspace_projects() -> List[str]:
    root = _workspace_projects_root()
    if not root.exists():
        return [DEFAULT_PROJECT_SLUG]
    names = sorted([p.name for p in root.iterdir() if p.is_dir()])
    return names or [DEFAULT_PROJECT_SLUG]


def ensure_project_structure(project_slug: str) -> Dict[str, Path]:
    slug = _slugify_project_name(project_slug)
    project_root = _workspace_projects_root() / slug
    paths = {
        "project_root": project_root,
        "schemas_dir": project_root / "schemas",
        "qa_sessions_dir": project_root / "qa_sessions",
        "event_logs_dir": project_root / "event_logs",
        "tracking_dir": project_root / "tracking_plan",
        "exports_dir": project_root / "exports",
        "artifacts_dir": project_root / "artifacts",
        "schema_file": project_root / "schemas" / "event_schemas.json",
        "db_file": project_root / "qa_sessions" / "qa_runs.db",
        "action_map_file": project_root / "tracking_plan" / "action_object_map.csv",
    }
    for key in ["project_root", "schemas_dir", "qa_sessions_dir", "event_logs_dir", "tracking_dir", "exports_dir", "artifacts_dir"]:
        paths[key].mkdir(parents=True, exist_ok=True)
    return paths


def get_active_project_slug() -> str:
    try:
        slug = _slugify_project_name(st.session_state.get("qa_project_slug", DEFAULT_PROJECT_SLUG))
    except Exception:
        slug = DEFAULT_PROJECT_SLUG
    return slug


def get_active_project_paths() -> Dict[str, Path]:
    return ensure_project_structure(get_active_project_slug())


def get_active_schema_path() -> Path:
    return get_active_project_paths()["schema_file"]


def get_active_test_log_db_path() -> Path:
    return get_active_project_paths()["db_file"]


def get_active_action_map_path() -> Path:
    return get_active_project_paths()["action_map_file"]


def _normalize_action_map_df(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame(
            columns=[
                "event_name",
                "action",
                "object",
                "page_id",
                "section_name",
                "section_index",
                "description",
                "object_index",
            ]
        )
    rename = {str(c): normalize_param_name(str(c)) for c in df.columns}
    work = df.rename(columns=rename).copy()
    aliases = {
        "section": "section_name",
        "sectionid": "section_index",
        "index": "object_index",
        "event": "event_name",
    }
    for src, dst in aliases.items():
        if src in work.columns and dst not in work.columns:
            work[dst] = work[src]

    required_cols = [
        "event_name",
        "action",
        "object",
        "page_id",
        "section_name",
        "section_index",
        "description",
        "object_index",
    ]
    for col in required_cols:
        if col not in work.columns:
            work[col] = ""
    work = work[required_cols].copy()
    for col in required_cols:
        work[col] = work[col].astype(str).str.strip()
    work = work[work["event_name"] != ""].drop_duplicates(subset=["event_name"], keep="last").reset_index(drop=True)
    return work


def load_action_object_map(path: Path | None = None) -> pd.DataFrame:
    if path is None:
        path = get_active_action_map_path()
    if not path.exists():
        return _normalize_action_map_df(pd.DataFrame())
    try:
        raw = pd.read_csv(path)
    except Exception:
        return _normalize_action_map_df(pd.DataFrame())
    return _normalize_action_map_df(raw)


def save_action_object_map(df: pd.DataFrame, path: Path | None = None) -> None:
    if path is None:
        path = get_active_action_map_path()
    normalized = _normalize_action_map_df(df)
    path.parent.mkdir(parents=True, exist_ok=True)
    normalized.to_csv(path, index=False, encoding="utf-8-sig")


def action_map_template_csv_bytes() -> bytes:
    template = pd.DataFrame(
        [
            {
                "event_name": "click_button",
                "action": "click",
                "object": "button",
                "page_id": "/home",
                "section_name": "hero_banner",
                "section_index": "1",
                "description": "메인 배너 버튼 클릭",
                "object_index": "1",
            },
            {
                "event_name": "view_item_list",
                "action": "view",
                "object": "product",
                "page_id": "/home",
                "section_name": "product_list",
                "section_index": "2",
                "description": "상품 리스트 노출",
                "object_index": "{index}",
            },
        ]
    )
    return template.to_csv(index=False).encode("utf-8-sig")


def import_legacy_data_to_project(project_slug: str) -> Dict[str, int]:
    paths = ensure_project_structure(project_slug)
    target_db = paths["db_file"]
    init_test_log_db(target_db)

    counts = {
        "sessions": 0,
        "events": 0,
        "actions": 0,
        "streams": 0,
        "schemas": 0,
    }

    legacy_db = Path("data/test_logs/qa_runs.db")
    if legacy_db.exists():
        with sqlite3.connect(str(legacy_db)) as src, sqlite3.connect(str(target_db)) as dst:
            src.row_factory = sqlite3.Row
            dst.execute("PRAGMA journal_mode=WAL;")
            dst.execute("PRAGMA synchronous=NORMAL;")

            session_cols = [
                "session_id",
                "target_url",
                "status",
                "started_at",
                "ended_at",
                "captured_events",
                "last_error",
                "tester_name",
                "tester_note",
                "created_at",
            ]
            src_rows = src.execute(f"SELECT {', '.join(session_cols)} FROM qa_sessions").fetchall()
            for row in src_rows:
                vals = [row[c] for c in session_cols]
                dst.execute(
                    f"INSERT OR IGNORE INTO qa_sessions ({', '.join(session_cols)}) VALUES ({', '.join(['?'] * len(session_cols))})",
                    vals,
                )
            counts["sessions"] = int(len(src_rows))

            event_cols = [
                "session_id",
                "captured_at",
                "source",
                "event_name",
                "params_json",
                "page_url",
                "measurement_id",
                "client_id",
                "request_method",
            ]
            existing_event_count = int(dst.execute("SELECT COUNT(1) FROM qa_events").fetchone()[0] or 0)
            if existing_event_count == 0:
                event_rows = src.execute(f"SELECT {', '.join(event_cols)} FROM qa_events").fetchall()
                for row in event_rows:
                    vals = [row[c] for c in event_cols]
                    dst.execute(
                        f"INSERT INTO qa_events ({', '.join(event_cols)}) VALUES ({', '.join(['?'] * len(event_cols))})",
                        vals,
                    )
                counts["events"] = int(len(event_rows))

            action_cols = [
                "action_at",
                "session_id",
                "action_type",
                "actor_role",
                "actor_name",
                "detail_json",
                "remote_addr_hash",
                "user_agent",
            ]
            existing_action_count = int(dst.execute("SELECT COUNT(1) FROM qa_ui_actions").fetchone()[0] or 0)
            if existing_action_count == 0:
                action_rows = src.execute(f"SELECT {', '.join(action_cols)} FROM qa_ui_actions").fetchall()
                for row in action_rows:
                    vals = [row[c] for c in action_cols]
                    dst.execute(
                        f"INSERT INTO qa_ui_actions ({', '.join(action_cols)}) VALUES ({', '.join(['?'] * len(action_cols))})",
                        vals,
                    )
                counts["actions"] = int(len(action_rows))

            dst.commit()

    legacy_stream_dir = Path("data/debug_stream")
    if legacy_stream_dir.exists():
        for src_file in legacy_stream_dir.glob("dbg_*.jsonl"):
            sid = src_file.stem
            dst_file = paths["qa_sessions_dir"] / sid / "debug_stream.jsonl"
            dst_file.parent.mkdir(parents=True, exist_ok=True)
            if not dst_file.exists():
                shutil.copy2(src_file, dst_file)
                counts["streams"] += 1

    legacy_schema_file = Path("data/schemas/event_schemas.json")
    target_schema_file = paths["schema_file"]
    try:
        src_schema = {}
        dst_schema = {}
        if legacy_schema_file.exists():
            src_raw = json.loads(legacy_schema_file.read_text(encoding="utf-8"))
            src_schema = src_raw.get("events", src_raw) if isinstance(src_raw, dict) else {}
        if target_schema_file.exists():
            dst_raw = json.loads(target_schema_file.read_text(encoding="utf-8"))
            dst_schema = dst_raw.get("events", dst_raw) if isinstance(dst_raw, dict) else {}
        if isinstance(src_schema, dict) and src_schema:
            merged = dict(dst_schema) if isinstance(dst_schema, dict) else {}
            for k, v in src_schema.items():
                if k not in merged:
                    merged[k] = v
                    counts["schemas"] += 1
            target_schema_file.parent.mkdir(parents=True, exist_ok=True)
            target_schema_file.write_text(
                json.dumps({"events": merged, "updated_at": datetime.now(tz=ZoneInfo("UTC")).isoformat()}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
    except Exception:
        pass

    return counts


def _bootstrap_dotenv_value(key: str) -> str:
    dotenv_path = BASE_DIR / ".env"
    if not dotenv_path.exists():
        return ""
    try:
        for raw in dotenv_path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            if k.strip() == key:
                return v.strip().strip("'").strip('"')
    except Exception:
        return ""
    return ""


DISPLAY_TZ_NAME = str(os.getenv("QA_DISPLAY_TZ", "")).strip() or _bootstrap_dotenv_value("QA_DISPLAY_TZ")
if DISPLAY_TZ_NAME:
    try:
        LOCAL_TZ = ZoneInfo(DISPLAY_TZ_NAME)
    except Exception:
        LOCAL_TZ = datetime.now().astimezone().tzinfo
        DISPLAY_TZ_NAME = str(LOCAL_TZ)
else:
    LOCAL_TZ = datetime.now().astimezone().tzinfo
    DISPLAY_TZ_NAME = str(LOCAL_TZ)


def parse_csv_list(text: str) -> List[str]:
    return [item.strip() for item in text.split(",") if item.strip()]


def parse_required_params(text: str) -> Dict[str, List[str]]:
    mapping: Dict[str, List[str]] = {}
    for line in text.strip().splitlines():
        if not line.strip() or ":" not in line:
            continue
        event, params = line.split(":", 1)
        mapping[event.strip()] = parse_csv_list(params)
    return mapping


def load_event_schemas(path: Path | None = None) -> Dict[str, Dict[str, object]]:
    if path is None:
        path = get_active_schema_path()
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}

    src = raw.get("events", raw) if isinstance(raw, dict) else {}
    if not isinstance(src, dict):
        return {}

    out: Dict[str, Dict[str, object]] = {}
    for event_name, schema in src.items():
        if not isinstance(schema, dict):
            continue
        name = str(event_name).strip()
        if not name:
            continue
        required = [str(v).strip() for v in schema.get("required", []) if str(v).strip()]
        optional = [str(v).strip() for v in schema.get("optional", []) if str(v).strip()]
        out[name] = {
            "required": list(dict.fromkeys(required)),
            "optional": list(dict.fromkeys(optional)),
            "updated_at": str(schema.get("updated_at", "")).strip(),
        }
    return out


def save_event_schemas(schemas: Dict[str, Dict[str, object]], path: Path | None = None) -> None:
    if path is None:
        path = get_active_schema_path()
    payload = {
        "events": schemas,
        "updated_at": datetime.now(tz=ZoneInfo("UTC")).isoformat(),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def infer_event_schema_from_rows(
    rt_events: pd.DataFrame,
    event_name: str,
    include_system_params: bool = False,
) -> Dict[str, object]:
    if rt_events.empty:
        return {"required": [], "optional": [], "sample_size": 0, "excluded_system_params": 0}
    subset = rt_events[rt_events["이벤트"].astype(str) == str(event_name)].copy()
    if subset.empty:
        return {"required": [], "optional": [], "sample_size": 0, "excluded_system_params": 0}

    nonmissing_count: Dict[str, int] = {}
    seen_count: Dict[str, int] = {}
    total = int(len(subset))
    excluded_system_params = 0

    for _, row in subset.iterrows():
        params = row.get("전체 파라미터")
        if not isinstance(params, dict):
            continue
        row_seen: set[str] = set()
        row_nonmissing: set[str] = set()
        for key, value in params.items():
            key_text = str(key).strip()
            if not key_text:
                continue
            if _is_system_param_key(key_text) and (not include_system_params):
                excluded_system_params += 1
                continue
            row_seen.add(key_text)
            if not _is_missing_like_value(_to_text_value(value)):
                row_nonmissing.add(key_text)
        for key in row_seen:
            seen_count[key] = seen_count.get(key, 0) + 1
        for key in row_nonmissing:
            nonmissing_count[key] = nonmissing_count.get(key, 0) + 1

    all_keys = sorted(seen_count.keys())
    required = [k for k in all_keys if nonmissing_count.get(k, 0) >= total]
    optional = [k for k in all_keys if k not in required]
    return {
        "required": required,
        "optional": optional,
        "sample_size": total,
        "excluded_system_params": int(excluded_system_params),
    }


def validate_schema_for_event(
    event_name: str,
    params: Dict[str, object],
    schemas: Dict[str, Dict[str, object]],
) -> Dict[str, object]:
    schema = schemas.get(str(event_name).strip(), {})
    required = [str(v).strip() for v in schema.get("required", []) if str(v).strip()] if isinstance(schema, dict) else []
    optional = [str(v).strip() for v in schema.get("optional", []) if str(v).strip()] if isinstance(schema, dict) else []
    checks: List[Dict[str, object]] = []

    if not required and not optional:
        return {
            "status": "WARN",
            "message": "기준표 없음(스키마 없음)",
            "checks": checks,
        }

    missing_required: List[str] = []
    for key in required:
        value = _to_text_value(params.get(key, ""))
        ok = (key in params) and (not _is_missing_like_value(value))
        if not ok:
            missing_required.append(key)
        checks.append({"param": key, "ok": ok, "value": value or "-", "group": "required"})

    for key in optional:
        value = _to_text_value(params.get(key, ""))
        checks.append(
            {
                "param": key,
                "ok": (key in params) and (not _is_missing_like_value(value)),
                "value": value or "-",
                "group": "optional",
            }
        )

    if missing_required:
        return {
            "status": "FAIL",
            "message": f"필수 누락 {len(missing_required)}개",
            "checks": checks,
        }
    return {
        "status": "PASS",
        "message": "필수 파라미터 정상",
        "checks": checks,
    }


def summarize_schema_validation(rt_events: pd.DataFrame, schemas: Dict[str, Dict[str, object]]) -> Dict[str, int]:
    out = {"PASS": 0, "WARN": 0, "FAIL": 0}
    if rt_events.empty:
        return out
    for row in rt_events.to_dict("records"):
        params = row.get("전체 파라미터", {})
        if not isinstance(params, dict):
            params = {}
        status = str(
            validate_schema_for_event(
                event_name=str(row.get("이벤트", "")),
                params=params,
                schemas=schemas,
            ).get("status", "WARN")
        ).upper()
        if status not in out:
            status = "WARN"
        out[status] += 1
    return out


def build_schema_validation_by_event(rt_events: pd.DataFrame, schemas: Dict[str, Dict[str, object]]) -> pd.DataFrame:
    if rt_events.empty:
        return pd.DataFrame(columns=["event_name", "PASS", "WARN", "FAIL", "total"])
    rows: List[Dict[str, object]] = []
    for row in rt_events.to_dict("records"):
        event_name = str(row.get("이벤트", "")).strip()
        if not event_name:
            continue
        params = row.get("전체 파라미터", {})
        if not isinstance(params, dict):
            params = {}
        status = str(
            validate_schema_for_event(
                event_name=event_name,
                params=params,
                schemas=schemas,
            ).get("status", "WARN")
        ).upper()
        if status not in {"PASS", "WARN", "FAIL"}:
            status = "WARN"
        rows.append({"event_name": event_name, "status": status, "count": 1})
    if not rows:
        return pd.DataFrame(columns=["event_name", "PASS", "WARN", "FAIL", "total"])
    work = pd.DataFrame(rows)
    pivot = (
        work.pivot_table(index="event_name", columns="status", values="count", aggfunc="sum", fill_value=0)
        .reset_index()
    )
    for col in ["PASS", "WARN", "FAIL"]:
        if col not in pivot.columns:
            pivot[col] = 0
    pivot["total"] = pivot["PASS"] + pivot["WARN"] + pivot["FAIL"]
    return pivot[["event_name", "PASS", "WARN", "FAIL", "total"]].sort_values(
        by=["FAIL", "WARN", "total", "event_name"], ascending=[False, False, False, True]
    )


def build_issue_summary(rt_events: pd.DataFrame, schemas: Dict[str, Dict[str, object]]) -> pd.DataFrame:
    if rt_events.empty:
        return pd.DataFrame(columns=["event_name", "status", "reason", "count"])

    issue_rows: List[Dict[str, object]] = []
    for row in rt_events.to_dict("records"):
        event_name = str(row.get("이벤트", "")).strip()
        if not event_name:
            continue

        reasons: List[str] = []
        event_status = str(row.get("상태", "OK")).upper()
        if event_status == "ERROR":
            reasons.append("이벤트 판정 오류(ERROR)")
        elif event_status == "WARN":
            reasons.append("이벤트 판정 경고(WARN)")

        missing_group = row.get("값없음 그룹", {})
        if isinstance(missing_group, dict) and missing_group:
            reasons.append(f"값 비어있음 {len(missing_group)}개")
        suspicious_group = row.get("의심 그룹", {})
        if isinstance(suspicious_group, dict) and suspicious_group:
            reasons.append(f"항상 동일한 값 {len(suspicious_group)}개")

        params = row.get("전체 파라미터", {})
        if not isinstance(params, dict):
            params = {}
        schema_check = validate_schema_for_event(event_name=event_name, params=params, schemas=schemas)
        schema_status = str(schema_check.get("status", "WARN")).upper()
        if schema_status in {"WARN", "FAIL"}:
            reasons.append(f"스키마: {schema_check.get('message', '-')}")

        if not reasons:
            continue

        merged_status = "FAIL" if ("FAIL" in {schema_status} or event_status == "ERROR") else "WARN"
        issue_rows.append(
            {
                "event_name": event_name,
                "status": merged_status,
                "reason": " | ".join(dict.fromkeys(reasons)),
                "count": 1,
            }
        )

    if not issue_rows:
        return pd.DataFrame(columns=["event_name", "status", "reason", "count"])

    out = pd.DataFrame(issue_rows)
    out = (
        out.groupby(["event_name", "status", "reason"], as_index=False)["count"]
        .sum()
        .sort_values(by=["status", "count", "event_name"], ascending=[True, False, True])
    )
    return out


def build_tracking_plan_df(rt_events: pd.DataFrame, schemas: Dict[str, Dict[str, object]]) -> pd.DataFrame:
    event_counts: Dict[str, int] = {}
    if not rt_events.empty:
        for name in rt_events["이벤트"].astype(str).tolist():
            key = str(name).strip()
            if not key:
                continue
            event_counts[key] = event_counts.get(key, 0) + 1

    all_event_names = sorted(list(dict.fromkeys(list(schemas.keys()) + list(event_counts.keys()))))
    rows: List[Dict[str, object]] = []
    for event_name in all_event_names:
        schema = schemas.get(event_name, {})
        required = [str(v).strip() for v in schema.get("required", []) if str(v).strip()] if isinstance(schema, dict) else []
        optional = [str(v).strip() for v in schema.get("optional", []) if str(v).strip()] if isinstance(schema, dict) else []
        rows.append(
            {
                "event_name": event_name,
                "triggered_count": int(event_counts.get(event_name, 0)),
                "schema_status": "ready" if (required or optional) else "missing",
                "required": ", ".join(required),
                "optional": ", ".join(optional),
            }
        )
    return pd.DataFrame(rows)


def dataframe_to_csv_bytes(df: pd.DataFrame) -> bytes:
    if df.empty:
        return b""
    return df.to_csv(index=False).encode("utf-8-sig")


def dataframes_to_excel_bytes(sheets: Dict[str, pd.DataFrame]) -> bytes:
    if not sheets:
        return b""
    out = io.BytesIO()
    try:
        with pd.ExcelWriter(out, engine="openpyxl") as writer:
            for sheet_name, df in sheets.items():
                safe_sheet = str(sheet_name or "Sheet1")[:31]
                df_to_write = df if isinstance(df, pd.DataFrame) else pd.DataFrame()
                df_to_write.to_excel(writer, index=False, sheet_name=safe_sheet)
        return out.getvalue()
    except Exception:
        return b""


def build_event_logs_export_df(rt_events: pd.DataFrame) -> pd.DataFrame:
    rows: List[Dict[str, object]] = []
    if rt_events.empty:
        return pd.DataFrame(columns=["timestamp", "event_name", "page_url", "parameter", "value"])
    for row in rt_events.to_dict("records"):
        event_name = str(row.get("이벤트", "")).strip()
        timestamp = str(row.get("시간", "-"))
        params = row.get("전체 파라미터", {})
        if not isinstance(params, dict):
            params = {}
        page_url = _to_text_value(params.get("page_location", params.get("page_url", "")))
        if not params:
            rows.append(
                {
                    "timestamp": timestamp,
                    "event_name": event_name,
                    "page_url": page_url,
                    "parameter": "-",
                    "value": "-",
                }
            )
            continue
        for key, value in params.items():
            rows.append(
                {
                    "timestamp": timestamp,
                    "event_name": event_name,
                    "page_url": page_url,
                    "parameter": str(key),
                    "value": _to_text_value(value),
                }
            )
    return pd.DataFrame(rows)


def build_parameter_validation_export_df(rt_events: pd.DataFrame, schemas: Dict[str, Dict[str, object]]) -> pd.DataFrame:
    rows: List[Dict[str, object]] = []
    for row in rt_events.to_dict("records"):
        event_name = str(row.get("이벤트", "")).strip()
        params = row.get("전체 파라미터", {})
        if not isinstance(params, dict):
            params = {}
        check = validate_schema_for_event(event_name=event_name, params=params, schemas=schemas)
        schema_required = set()
        schema_optional = set()
        schema_obj = schemas.get(event_name, {})
        if isinstance(schema_obj, dict):
            schema_required = {str(v).strip() for v in schema_obj.get("required", []) if str(v).strip()}
            schema_optional = {str(v).strip() for v in schema_obj.get("optional", []) if str(v).strip()}
        known_schema_params = schema_required | schema_optional

        for item in check.get("checks", []) if isinstance(check.get("checks", []), list) else []:
            if not isinstance(item, dict):
                continue
            param_name = str(item.get("param", "-"))
            value_text = str(item.get("value", "-"))
            result = "PASS" if bool(item.get("ok", False)) else "FAIL"
            issue_type = "required_missing" if (str(item.get("group", "")) == "required" and result == "FAIL") else "schema_check"
            rows.append(
                {
                    "event_name": event_name,
                    "schema_status": str(check.get("status", "WARN")),
                    "parameter": param_name,
                    "group": str(item.get("group", "-")),
                    "value": value_text,
                    "result": result,
                    "severity": "FAIL" if result == "FAIL" else "PASS",
                    "issue_type": issue_type,
                    "reason": "필수 누락" if issue_type == "required_missing" else "-",
                }
            )

        for key, value in params.items():
            key_text = str(key).strip()
            if not key_text or _is_system_param_key(key_text):
                continue
            value_text = _to_text_value(value)
            if known_schema_params and key_text not in known_schema_params:
                rows.append(
                    {
                        "event_name": event_name,
                        "schema_status": str(check.get("status", "WARN")),
                        "parameter": key_text,
                        "group": "extra",
                        "value": value_text,
                        "result": "WARN",
                        "severity": "WARN",
                        "issue_type": "unexpected_parameter",
                        "reason": "스키마에 정의되지 않은 파라미터",
                    }
                )
            valid_ok, valid_msg = validate_param_format(key_text, value_text)
            if not valid_ok:
                rows.append(
                    {
                        "event_name": event_name,
                        "schema_status": str(check.get("status", "WARN")),
                        "parameter": key_text,
                        "group": "format",
                        "value": value_text,
                        "result": "FAIL",
                        "severity": "FAIL",
                        "issue_type": "type_validation_fail",
                        "reason": valid_msg,
                    }
                )
    if not rows:
        return pd.DataFrame(
            columns=["event_name", "schema_status", "parameter", "group", "value", "result", "severity", "issue_type", "reason"]
        )
    return pd.DataFrame(rows)


def build_event_order_validation_df(rt_events: pd.DataFrame, expected_steps: List[str]) -> pd.DataFrame:
    cols = ["timestamp", "event_name", "status", "reason", "expected_prev_step", "actual_step_pointer"]
    steps = [str(v).strip() for v in expected_steps if str(v).strip()]
    if rt_events.empty or not steps:
        return pd.DataFrame(columns=cols)

    work = rt_events.copy()
    work["captured_at"] = pd.to_datetime(work["captured_at"], errors="coerce")
    work = work.sort_values("captured_at")
    step_index = {name: idx for idx, name in enumerate(steps)}
    pointer = 0
    rows: List[Dict[str, object]] = []

    for _, row in work.iterrows():
        event_name = str(row.get("이벤트", "")).strip()
        if event_name not in step_index:
            continue
        idx = step_index[event_name]
        ts = format_local_time(row.get("captured_at"))
        if idx < pointer:
            rows.append(
                {
                    "timestamp": ts,
                    "event_name": event_name,
                    "status": "WARN",
                    "reason": "이미 지난 step의 이벤트가 재발생",
                    "expected_prev_step": steps[pointer - 1] if pointer > 0 else "-",
                    "actual_step_pointer": pointer,
                }
            )
        elif idx == pointer:
            rows.append(
                {
                    "timestamp": ts,
                    "event_name": event_name,
                    "status": "PASS",
                    "reason": "순서 정상",
                    "expected_prev_step": steps[pointer - 1] if pointer > 0 else "-",
                    "actual_step_pointer": pointer,
                }
            )
            pointer += 1
        else:
            rows.append(
                {
                    "timestamp": ts,
                    "event_name": event_name,
                    "status": "FAIL",
                    "reason": "선행 step 누락 상태에서 발생",
                    "expected_prev_step": steps[pointer] if pointer < len(steps) else "-",
                    "actual_step_pointer": pointer,
                }
            )

    if pointer < len(steps):
        rows.append(
            {
                "timestamp": "-",
                "event_name": steps[pointer],
                "status": "FAIL",
                "reason": "필수 순서 step 미도달",
                "expected_prev_step": steps[pointer - 1] if pointer > 0 else "-",
                "actual_step_pointer": pointer,
            }
        )
    return pd.DataFrame(rows, columns=cols)


def build_event_qa_report_export_df(
    rt_events: pd.DataFrame,
    issue_df: pd.DataFrame,
    schemas: Dict[str, Dict[str, object]],
    parameter_validation_df: pd.DataFrame,
    order_validation_df: pd.DataFrame,
) -> pd.DataFrame:
    qa_by_event = build_schema_validation_by_event(rt_events, schemas)
    issue_reason_map: Dict[str, str] = {}
    if not issue_df.empty:
        for event_name, group in issue_df.groupby("event_name"):
            reasons = [str(v).strip() for v in group["reason"].tolist() if str(v).strip()]
            issue_reason_map[str(event_name).strip()] = " | ".join(dict.fromkeys(reasons))

    param_fail_map: Dict[str, int] = {}
    param_warn_map: Dict[str, int] = {}
    if not parameter_validation_df.empty:
        work = parameter_validation_df.copy()
        work["event_name"] = work["event_name"].astype(str).str.strip()
        for event_name, group in work.groupby("event_name"):
            param_fail_map[event_name] = int((group["severity"].astype(str) == "FAIL").sum())
            param_warn_map[event_name] = int((group["severity"].astype(str) == "WARN").sum())

    order_fail_map: Dict[str, int] = {}
    order_warn_map: Dict[str, int] = {}
    if not order_validation_df.empty:
        owork = order_validation_df.copy()
        owork["event_name"] = owork["event_name"].astype(str).str.strip()
        for event_name, group in owork.groupby("event_name"):
            order_fail_map[event_name] = int((group["status"].astype(str) == "FAIL").sum())
            order_warn_map[event_name] = int((group["status"].astype(str) == "WARN").sum())

    event_error_map: Dict[str, int] = {}
    if not rt_events.empty:
        ework = rt_events.copy()
        ework["이벤트"] = ework["이벤트"].astype(str).str.strip()
        for event_name, group in ework.groupby("이벤트"):
            event_error_map[event_name] = int((group["상태"].astype(str).str.upper() == "ERROR").sum())

    rows: List[Dict[str, object]] = []
    if qa_by_event.empty:
        return pd.DataFrame(columns=["event_name", "schema_status", "parameter_issue", "test_count", "result"])
    for _, row in qa_by_event.iterrows():
        event_name = str(row.get("event_name", "")).strip()
        pass_n = int(row.get("PASS", 0))
        warn_n = int(row.get("WARN", 0))
        fail_n = int(row.get("FAIL", 0))
        total = int(row.get("total", 0))
        param_fail = int(param_fail_map.get(event_name, 0))
        param_warn = int(param_warn_map.get(event_name, 0))
        order_fail = int(order_fail_map.get(event_name, 0))
        order_warn = int(order_warn_map.get(event_name, 0))
        event_error = int(event_error_map.get(event_name, 0))

        # QA 상태 기준
        # FAIL: 스키마 FAIL 또는 필수/타입 FAIL 또는 순서 FAIL 또는 이벤트 ERROR
        # WARN: FAIL이 아니고 스키마 WARN 또는 optional/extra 경고 또는 순서 WARN
        # PASS: 위 조건이 없을 때
        if fail_n > 0 or param_fail > 0 or order_fail > 0 or event_error > 0:
            schema_status = "FAIL"
        elif warn_n > 0 or param_warn > 0 or order_warn > 0:
            schema_status = "WARN"
        else:
            schema_status = "PASS"
        parameter_issue_text = issue_reason_map.get(event_name, "-")
        if parameter_issue_text == "-" and param_fail > 0:
            parameter_issue_text = f"파라미터 FAIL {param_fail}개"
        if parameter_issue_text == "-" and param_warn > 0:
            parameter_issue_text = f"파라미터 WARN {param_warn}개"

        rows.append(
            {
                "event_name": event_name,
                "schema_status": schema_status,
                "parameter_issue": parameter_issue_text,
                "test_count": total,
                "result": schema_status,
                "pass_count": pass_n,
                "warn_count": warn_n,
                "fail_count": fail_n,
                "parameter_fail_count": param_fail,
                "parameter_warn_count": param_warn,
                "order_fail_count": order_fail,
                "order_warn_count": order_warn,
                "event_error_count": event_error,
                "qa_status_criteria": "FAIL=스키마FAIL/필수·타입FAIL/순서FAIL/이벤트ERROR, WARN=스키마WARN/추가파라미터/순서WARN, PASS=그외",
            }
        )
    return pd.DataFrame(rows).sort_values(by=["result", "test_count", "event_name"], ascending=[True, False, True])


def _infer_action_object(event_name: str) -> Tuple[str, str]:
    name = str(event_name).strip().lower()
    action = name.split("_", 1)[0] if "_" in name else name
    if action in {"", "gtm.click", "gtm.linkclick"}:
        action = "click"
    if "impression" in name:
        action = "impression"
    obj = "content"
    if "button" in name:
        obj = "button"
    elif "product" in name:
        obj = "product"
    elif "popup" in name:
        obj = "popup"
    elif "link" in name:
        obj = "link"
    return action or "event", obj


def build_tracking_plan_export_df(
    rt_events: pd.DataFrame,
    schemas: Dict[str, Dict[str, object]],
    action_map_df: pd.DataFrame | None = None,
) -> pd.DataFrame:
    rows: List[Dict[str, object]] = []
    event_names = sorted(list(dict.fromkeys(list(schemas.keys()) + rt_events.get("이벤트", pd.Series(dtype=str)).astype(str).tolist())))
    action_map: Dict[str, Dict[str, str]] = {}
    if isinstance(action_map_df, pd.DataFrame) and not action_map_df.empty:
        norm_map_df = _normalize_action_map_df(action_map_df)
        for _, map_row in norm_map_df.iterrows():
            ev = str(map_row.get("event_name", "")).strip()
            if not ev:
                continue
            action_map[ev] = {
                "action": str(map_row.get("action", "")).strip(),
                "object": str(map_row.get("object", "")).strip(),
                "page_id": str(map_row.get("page_id", "")).strip(),
                "section_name": str(map_row.get("section_name", "")).strip(),
                "section_index": str(map_row.get("section_index", "")).strip(),
                "description": str(map_row.get("description", "")).strip(),
                "object_index": str(map_row.get("object_index", "")).strip(),
            }
    event_meta: Dict[str, Dict[str, str]] = {}
    if not rt_events.empty:
        for _, row in rt_events.iterrows():
            ev = str(row.get("이벤트", "")).strip()
            if not ev:
                continue
            params = row.get("전체 파라미터", {})
            if not isinstance(params, dict):
                params = {}
            meta = event_meta.setdefault(ev, {})
            for key in ["page_id", "section_name", "section_index", "action", "object", "description", "index"]:
                val = _to_text_value(params.get(key, ""))
                if val and (key not in meta or not meta[key]):
                    meta[key] = val

    for event_name in event_names:
        if not str(event_name).strip():
            continue
        schema = schemas.get(str(event_name).strip(), {})
        required = [str(v).strip() for v in schema.get("required", []) if str(v).strip()] if isinstance(schema, dict) else []
        optional = [str(v).strip() for v in schema.get("optional", []) if str(v).strip()] if isinstance(schema, dict) else []
        params = required + [v for v in optional if v not in required]
        action, obj = _infer_action_object(str(event_name))
        meta = event_meta.get(str(event_name).strip(), {})
        map_override = action_map.get(str(event_name).strip(), {})
        page_id = map_override.get("page_id") or meta.get("page_id", "unknown")
        section_name = map_override.get("section_name") or meta.get("section_name", "general")
        section_index = map_override.get("section_index") or meta.get("section_index", "-")
        action_value = map_override.get("action") or meta.get("action", action)
        object_value = map_override.get("object") or meta.get("object", obj)
        description = map_override.get("description") or meta.get("description", "-")
        object_index = map_override.get("object_index") or meta.get("index", "-")
        if not params:
            params = ["-"]
        for p in params:
            rows.append(
                {
                    "page_id": page_id,
                    "section_name": section_name,
                    "section_index": section_index,
                    "action": action_value,
                    "object": object_value,
                    "event_name": str(event_name).strip(),
                    "description": description,
                    "object_index": object_index,
                    "parameter": p,
                }
            )
    if not rows:
        return pd.DataFrame(
            columns=[
                "page_id",
                "section_name",
                "section_index",
                "action",
                "object",
                "event_name",
                "description",
                "object_index",
                "parameter",
            ]
        )
    return pd.DataFrame(rows)


def build_screen_definition_export_df(tracking_plan_df: pd.DataFrame) -> pd.DataFrame:
    if tracking_plan_df.empty:
        return pd.DataFrame(columns=["section_index", "section_name", "UI_type", "event"])
    unique_sections = (
        tracking_plan_df[["section_index", "section_name", "object", "event_name"]]
        .drop_duplicates()
        .fillna("-")
    )
    rows: List[Dict[str, object]] = []
    for idx, row in unique_sections.reset_index(drop=True).iterrows():
        event_name = str(row.get("event_name", "")).strip()
        action, obj = _infer_action_object(event_name)
        sec_idx = str(row.get("section_index", "")).strip() or str(idx + 1)
        sec_name = str(row.get("section_name", "")).strip() or f"section_{idx+1}"
        ui_type = str(row.get("object", "")).strip() or obj
        rows.append(
            {
                "section_index": sec_idx,
                "section_name": sec_name,
                "UI_type": ui_type,
                "event": event_name,
                "action": action,
            }
        )
    return pd.DataFrame(rows)


def build_screen_marked_png_bytes(screen_definition_df: pd.DataFrame) -> bytes:
    try:
        from PIL import Image, ImageDraw
    except Exception:
        return b""

    width, height = 1280, 720
    image = Image.new("RGB", (width, height), color=(248, 250, 252))
    draw = ImageDraw.Draw(image)
    draw.rectangle([20, 20, width - 20, 90], outline=(30, 64, 175), width=3)
    draw.text((40, 45), "Screen Marked Preview (QA)", fill=(30, 64, 175))

    rows = screen_definition_df.to_dict("records")[:8] if not screen_definition_df.empty else []
    top = 130
    box_h = 64
    circle_r = 18
    for idx, row in enumerate(rows, start=1):
        y1 = top + (idx - 1) * (box_h + 14)
        y2 = y1 + box_h
        draw.rectangle([80, y1, width - 40, y2], outline=(220, 38, 38), width=3)
        cx, cy = 54, y1 + int(box_h / 2)
        draw.ellipse([cx - circle_r, cy - circle_r, cx + circle_r, cy + circle_r], fill=(30, 64, 175), outline=(30, 64, 175), width=2)
        draw.text((cx - 6, cy - 10), str(idx), fill=(255, 255, 255))
        marker = to_circled_number(idx)
        label = f"{marker} {row.get('section_name', '-')} | event: {row.get('event', '-')}"
        draw.text((96, y1 + 20), label, fill=(17, 24, 39))

    out = io.BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()


def build_tracking_package_zip_bytes(
    tracking_plan_xlsx: bytes,
    screen_definition_xlsx: bytes,
    screen_marked_png: bytes,
    qa_report_xlsx: bytes,
) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        if tracking_plan_xlsx:
            zf.writestr("tracking_plan.xlsx", tracking_plan_xlsx)
        if screen_definition_xlsx:
            zf.writestr("screen_definition.xlsx", screen_definition_xlsx)
        if screen_marked_png:
            zf.writestr("screen_marked.png", screen_marked_png)
        if qa_report_xlsx:
            zf.writestr("qa_report.xlsx", qa_report_xlsx)
    return out.getvalue()


def load_dotenv_values(dotenv_path: Path | None = None) -> Dict[str, str]:
    values: Dict[str, str] = {}
    if dotenv_path is None:
        dotenv_path = BASE_DIR / ".env"
    if not dotenv_path.exists():
        return values

    for raw in dotenv_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key:
            continue
        values[key] = value.strip().strip("'").strip('"')
    return values


def get_config_value(key: str, default: str = "") -> str:
    env_val = os.getenv(key, "").strip()
    if env_val:
        return env_val
    try:
        secret_val = str(st.secrets.get(key, "")).strip()
        if secret_val:
            return secret_val
    except Exception:
        pass
    dotenv_val = load_dotenv_values().get(key, "").strip()
    if dotenv_val:
        return dotenv_val
    return default


def is_truthy(value: object) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def get_novnc_popup_url() -> str:
    configured = get_config_value("QA_NOVNC_PUBLIC_URL", "").strip()
    if configured:
        return configured
    # Nginx /vnc/ reverse-proxy uses the /vnc/websockify backend path.
    return "/vnc/vnc.html?autoconnect=1&resize=remote&path=vnc/websockify"


def auto_open_popup_window(url: str, popup_name: str = "qa_debug_popup") -> None:
    target_url = str(url or "").strip()
    if not target_url:
        return
    safe_url = json.dumps(target_url)
    safe_name = json.dumps(popup_name)
    components.html(
        f"""
        <script>
          (() => {{
            const w = window.parent || window;
            const features = "width=1320,height=900,resizable=yes,scrollbars=yes";
            w.open({safe_url}, {safe_name}, features);
          }})();
        </script>
        """,
        height=0,
        width=0,
    )


def normalize_debug_target_url(raw_url: str) -> str:
    text = str(raw_url or "").strip()
    if not text:
        return ""
    parsed = urlparse(text)
    if parsed.scheme and parsed.netloc:
        return text
    if (not parsed.scheme) and parsed.path and ("." in parsed.path):
        guess = f"https://{parsed.path}"
        g = urlparse(guess)
        if g.scheme and g.netloc:
            return guess
    return ""


def _get_request_headers() -> Dict[str, str]:
    try:
        raw_headers = st.context.headers
    except Exception:
        return {}
    out: Dict[str, str] = {}
    try:
        for k, v in raw_headers.items():
            out[str(k).strip().lower()] = str(v)
    except Exception:
        return {}
    return out


def _extract_remote_addr() -> str:
    headers = _get_request_headers()
    xff = str(headers.get("x-forwarded-for", "")).strip()
    if xff:
        return xff.split(",")[0].strip()
    xr = str(headers.get("x-real-ip", "")).strip()
    if xr:
        return xr
    return ""


def _hash_remote_addr(remote_addr: str) -> str:
    raw = str(remote_addr or "").strip()
    if not raw:
        return ""
    salt = get_config_value("QA_ACTION_LOG_SALT", "qa_action_salt")
    return hashlib.sha256(f"{salt}:{raw}".encode("utf-8")).hexdigest()[:20]


def log_ui_action(action_type: str, detail: Dict[str, object] | None = None) -> None:
    action = str(action_type or "").strip()
    if not action:
        return
    try:
        active_db_path = get_active_test_log_db_path()
        init_test_log_db(active_db_path)
        headers = _get_request_headers()
        actor_name = str(
            st.session_state.get("qa_access_user", "")
            or st.session_state.get("qa_tester_name", "")
            or "-"
        ).strip()
        append_ui_action(
            active_db_path,
            {
                "action_at": datetime.now(tz=ZoneInfo("UTC")).isoformat(),
                "session_id": str(st.session_state.get("qa_debug_session_id", "")).strip(),
                "action_type": action,
                "actor_role": str(st.session_state.get("qa_access_role", "guest")).strip(),
                "actor_name": actor_name,
                "detail": detail or {},
                "remote_addr_hash": _hash_remote_addr(_extract_remote_addr()),
                "user_agent": str(headers.get("user-agent", "")).strip(),
            },
        )
    except Exception:
        return


def enforce_access_gate() -> None:
    bypass = is_truthy(get_config_value("QA_APP_ACCESS_BYPASS", "1"))
    if bypass:
        if not st.session_state.get("qa_access_authenticated", False):
            st.session_state["qa_access_authenticated"] = True
            st.session_state["qa_access_role"] = "admin"
            st.session_state["qa_access_user"] = "bypass_access"
        return

    enabled = is_truthy(get_config_value("QA_APP_ACCESS_ENABLED", "0"))
    if not enabled:
        if not st.session_state.get("qa_access_authenticated", False):
            st.session_state["qa_access_authenticated"] = True
            st.session_state["qa_access_role"] = "admin"
            st.session_state["qa_access_user"] = "open_access"
        return

    if st.session_state.get("qa_access_authenticated", False):
        return

    tester_code = get_config_value("QA_TESTER_ACCESS_CODE", "").strip()
    admin_code = get_config_value("QA_ADMIN_ACCESS_CODE", "").strip()

    st.subheader("접근 제한")
    st.caption("테스트 전용 접속 코드가 필요합니다.")
    with st.form("qa_access_form", clear_on_submit=False):
        input_user = st.text_input("테스터 식별자", placeholder="예: tester_01")
        input_code = st.text_input("접속 코드", type="password")
        submitted = st.form_submit_button("입장")

    if submitted:
        code = str(input_code or "").strip()
        role = ""
        if admin_code and code == admin_code:
            role = "admin"
        elif tester_code and code == tester_code:
            role = "tester"
        elif (not tester_code) and (not admin_code):
            log_ui_action("access_login_error", {"reason": "codes_not_configured"})
            st.error("서버 접근 코드가 설정되지 않았습니다. 관리자에게 문의하세요.")
            st.stop()

        if role:
            st.session_state["qa_access_authenticated"] = True
            st.session_state["qa_access_role"] = role
            st.session_state["qa_access_user"] = str(input_user or "").strip() or f"{role}_user"
            log_ui_action("access_login_success", {"role": role})
            st.rerun()
        else:
            log_ui_action("access_login_failed", {"user": str(input_user or "").strip()})
            st.error("접속 코드가 올바르지 않습니다.")
    st.stop()


def probe_ingest_health(url: str, timeout_sec: float = 1.0) -> bool:
    target = str(url or "").strip()
    if not target:
        return False
    try:
        req = urllib_request.Request(target, method="GET")
        with urllib_request.urlopen(req, timeout=float(timeout_sec)) as resp:
            body = resp.read().decode("utf-8", errors="ignore").strip()
            if resp.status != 200:
                return False
            try:
                parsed = json.loads(body) if body else {}
                if isinstance(parsed, dict):
                    return bool(parsed.get("ok", False))
            except Exception:
                pass
            return '"ok":true' in body.lower().replace(" ", "")
    except Exception:
        return False


def get_google_access_token(token_path: Path) -> str:
    if not token_path.is_absolute():
        token_path = (BASE_DIR / token_path).resolve()
    if not token_path.exists():
        raise RuntimeError(f"OAuth 토큰 파일이 없습니다: {token_path}")

    token_payload = json.loads(token_path.read_text(encoding="utf-8"))
    access_token = str(token_payload.get("token", "")).strip()
    expiry = pd.to_datetime(token_payload.get("expiry"), errors="coerce", utc=True)
    now_utc = pd.Timestamp.now(tz="UTC")
    if access_token and pd.notna(expiry) and expiry > now_utc + pd.Timedelta(minutes=1):
        return access_token

    refresh_token = str(token_payload.get("refresh_token", "")).strip()
    client_id = str(token_payload.get("client_id", "")).strip()
    client_secret = str(token_payload.get("client_secret", "")).strip()
    token_uri = str(token_payload.get("token_uri", "https://oauth2.googleapis.com/token")).strip()
    if not refresh_token or not client_id or not client_secret:
        raise RuntimeError("OAuth refresh 토큰 정보가 부족합니다. token.json을 다시 발급하세요.")

    payload = urllib_parse.urlencode(
        {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": client_id,
            "client_secret": client_secret,
        }
    ).encode("utf-8")
    req = urllib_request.Request(
        token_uri,
        data=payload,
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib_request.urlopen(req, timeout=20) as resp:
            refreshed = json.loads(resp.read().decode("utf-8"))
    except urllib_error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="ignore")
        raise RuntimeError(f"OAuth 토큰 갱신 실패: {exc.code} {detail}") from exc
    except Exception as exc:
        raise RuntimeError(f"OAuth 토큰 갱신 실패: {exc}") from exc

    new_token = str(refreshed.get("access_token", "")).strip()
    if not new_token:
        raise RuntimeError("OAuth 토큰 갱신 결과에 access_token이 없습니다.")

    token_payload["token"] = new_token
    expires_in = refreshed.get("expires_in")
    if expires_in is not None:
        try:
            token_payload["expiry"] = (
                pd.Timestamp.now(tz="UTC") + pd.Timedelta(seconds=int(expires_in))
            ).isoformat()
        except Exception:
            pass
    try:
        token_path.write_text(json.dumps(token_payload, ensure_ascii=False), encoding="utf-8")
    except Exception:
        # 토큰 파일 저장 실패는 치명적이지 않으므로 무시한다.
        pass

    return new_token


def _ga4_api_request(
    *,
    method: str,
    url: str,
    access_token: str,
    body: Dict[str, object] | None = None,
) -> Dict[str, object]:
    payload = None
    headers = {"Authorization": f"Bearer {access_token}"}
    if body is not None:
        payload = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"

    req = urllib_request.Request(url, method=method.upper(), headers=headers, data=payload)
    try:
        with urllib_request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except urllib_error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="ignore")
        raise RuntimeError(f"GA API 호출 실패: {exc.code} {detail}") from exc
    except Exception as exc:
        raise RuntimeError(f"GA API 호출 실패: {exc}") from exc


def _resolve_project_path(path_text: str) -> Path:
    path_obj = Path(path_text).expanduser()
    if not path_obj.is_absolute():
        path_obj = (BASE_DIR / path_obj).resolve()
    return path_obj


def _load_google_client_config(client_secrets_file: str) -> Dict[str, str]:
    # 1) Streamlit secrets.toml ([auth], [auth.google]) 우선 지원
    #    예:
    #    [auth]
    #    redirect_uri = "https://asknuggetdata.com/oauth2callback"
    #    [auth.google]
    #    client_id = "..."
    #    client_secret = "..."
    try:
        auth_conf = st.secrets.get("auth", {})
        google_conf = auth_conf.get("google", {}) if hasattr(auth_conf, "get") else {}
        sec_client_id = str(google_conf.get("client_id", "")).strip() if hasattr(google_conf, "get") else ""
        sec_client_secret = str(google_conf.get("client_secret", "")).strip() if hasattr(google_conf, "get") else ""
        sec_metadata_url = str(google_conf.get("server_metadata_url", "")).strip() if hasattr(google_conf, "get") else ""

        if sec_client_id and sec_client_secret:
            redirect_uri_override = get_config_value("GA4_OAUTH_REDIRECT_URI", "").strip()
            sec_redirect_uri = str(auth_conf.get("redirect_uri", "")).strip() if hasattr(auth_conf, "get") else ""
            dynamic_redirect_uri = ""
            if is_truthy(get_config_value("GA4_OAUTH_DYNAMIC_REDIRECT", "0")):
                current_base = get_current_app_base_url()
                if current_base:
                    dynamic_redirect_uri = f"{current_base}/oauth2callback"
            redirect_uri = (
                redirect_uri_override
                or sec_redirect_uri
                or dynamic_redirect_uri
                or DEFAULT_OAUTH_REDIRECT_URI
            )
            auth_uri = "https://accounts.google.com/o/oauth2/v2/auth"
            token_uri = "https://oauth2.googleapis.com/token"

            if sec_metadata_url:
                try:
                    req = urllib_request.Request(sec_metadata_url, method="GET")
                    with urllib_request.urlopen(req, timeout=10) as resp:
                        meta = json.loads(resp.read().decode("utf-8"))
                    auth_uri = str(meta.get("authorization_endpoint", auth_uri)).strip() or auth_uri
                    token_uri = str(meta.get("token_endpoint", token_uri)).strip() or token_uri
                except Exception:
                    # metadata 조회 실패 시 기본 엔드포인트 사용
                    pass

            return {
                "client_id": sec_client_id,
                "client_secret": sec_client_secret,
                "auth_uri": auth_uri,
                "token_uri": token_uri,
                "redirect_uri": redirect_uri,
            }
    except Exception:
        pass

    # 2) 기존 client_secret.json 방식 fallback
    cfg_path = _resolve_project_path(client_secrets_file)
    if not cfg_path.exists():
        raise RuntimeError(
            f"OAuth client secret 파일이 없습니다: {cfg_path} "
            "(또는 .streamlit/secrets.toml의 [auth.google] client_id/client_secret를 설정하세요.)"
        )
    raw = json.loads(cfg_path.read_text(encoding="utf-8"))
    base = raw.get("web") or raw.get("installed")
    if not isinstance(base, dict):
        raise RuntimeError("client_secret.json 형식이 올바르지 않습니다. web/installed 설정이 필요합니다.")

    redirect_uris = [str(u).strip() for u in (base.get("redirect_uris") or []) if str(u).strip()]
    dynamic_redirect_uri = ""
    if is_truthy(get_config_value("GA4_OAUTH_DYNAMIC_REDIRECT", "0")):
        current_base = get_current_app_base_url()
        if current_base:
            dynamic_redirect_uri = f"{current_base}/oauth2callback"
    redirect_uri_override = get_config_value("GA4_OAUTH_REDIRECT_URI", "").strip()

    def _is_local_redirect(uri_text: str) -> bool:
        try:
            host = (urlparse(uri_text).hostname or "").strip().lower()
        except Exception:
            host = ""
        return host in {"localhost", "127.0.0.1", "::1"} or host.endswith(".local")

    if redirect_uri_override:
        redirect_uri = redirect_uri_override
    elif dynamic_redirect_uri:
        redirect_uri = dynamic_redirect_uri
    else:
        https_non_local = [
            u for u in redirect_uris if u.lower().startswith("https://") and not _is_local_redirect(u)
        ]
        redirect_uri = https_non_local[0] if https_non_local else (redirect_uris[0] if redirect_uris else DEFAULT_OAUTH_REDIRECT_URI)
    if not redirect_uri:
        raise RuntimeError("client_secret.json에 redirect_uris가 없습니다.")

    return {
        "client_id": str(base.get("client_id", "")).strip(),
        "client_secret": str(base.get("client_secret", "")).strip(),
        "auth_uri": str(base.get("auth_uri", "https://accounts.google.com/o/oauth2/v2/auth")).strip(),
        "token_uri": str(base.get("token_uri", "https://oauth2.googleapis.com/token")).strip(),
        "redirect_uri": redirect_uri,
    }


def build_google_oauth_url(client_secrets_file: str, state: str) -> str:
    conf = _load_google_client_config(client_secrets_file)
    if not conf["client_id"] or not conf["client_secret"]:
        raise RuntimeError("client_id/client_secret가 비어 있습니다.")
    scopes = ["https://www.googleapis.com/auth/analytics.readonly"]
    query = urllib_parse.urlencode(
        {
            "client_id": conf["client_id"],
            "redirect_uri": conf["redirect_uri"],
            "response_type": "code",
            "scope": " ".join(scopes),
            "access_type": "offline",
            "include_granted_scopes": "true",
            "prompt": "consent",
            "state": state,
        }
    )
    return f"{conf['auth_uri']}?{query}"


def get_google_oauth_redirect_uri(client_secrets_file: str) -> str:
    conf = _load_google_client_config(client_secrets_file)
    return str(conf.get("redirect_uri", "")).strip()


def exchange_google_oauth_code(
    *,
    client_secrets_file: str,
    code: str,
    token_file: str,
) -> None:
    conf = _load_google_client_config(client_secrets_file)
    payload = urllib_parse.urlencode(
        {
            "code": code,
            "client_id": conf["client_id"],
            "client_secret": conf["client_secret"],
            "redirect_uri": conf["redirect_uri"],
            "grant_type": "authorization_code",
        }
    ).encode("utf-8")
    req = urllib_request.Request(
        conf["token_uri"],
        data=payload,
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib_request.urlopen(req, timeout=30) as resp:
            token_resp = json.loads(resp.read().decode("utf-8"))
    except urllib_error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="ignore")
        if "redirect_uri_mismatch" in detail:
            raise RuntimeError(
                "Google 로그인 토큰 교환 실패: redirect_uri_mismatch. "
                "Google Cloud OAuth 설정에 현재 redirect URI를 추가하세요 "
                "(예: https://asknuggetdata.com/oauth2callback)."
            ) from exc
        raise RuntimeError(f"Google 로그인 토큰 교환 실패: {exc.code} {detail}") from exc
    except Exception as exc:
        raise RuntimeError(f"Google 로그인 토큰 교환 실패: {exc}") from exc

    access_token = str(token_resp.get("access_token", "")).strip()
    if not access_token:
        raise RuntimeError("로그인 토큰 교환 응답에 access_token이 없습니다.")

    token_path = _resolve_project_path(token_file)
    existing: Dict[str, object] = {}
    if token_path.exists():
        try:
            existing = json.loads(token_path.read_text(encoding="utf-8"))
        except Exception:
            existing = {}

    merged = dict(existing)
    merged["token"] = access_token
    if str(token_resp.get("refresh_token", "")).strip():
        merged["refresh_token"] = str(token_resp.get("refresh_token", "")).strip()
    merged["token_uri"] = conf["token_uri"]
    merged["client_id"] = conf["client_id"]
    merged["client_secret"] = conf["client_secret"]
    merged["scopes"] = token_resp.get("scope", "").split() or merged.get("scopes", [])
    expires_in = token_resp.get("expires_in")
    if expires_in is not None:
        try:
            merged["expiry"] = (
                pd.Timestamp.now(tz="UTC") + pd.Timedelta(seconds=int(expires_in))
            ).isoformat()
        except Exception:
            pass
    merged["universe_domain"] = "googleapis.com"

    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(json.dumps(merged, ensure_ascii=False), encoding="utf-8")


def _query_param_text(name: str) -> str:
    value = st.query_params.get(name)
    if isinstance(value, list):
        return str(value[0]).strip() if value else ""
    return str(value).strip() if value is not None else ""


def process_google_oauth_callback_if_present() -> None:
    error_text = _query_param_text("error")
    code = _query_param_text("code")
    if not error_text and not code:
        return

    state = _query_param_text("state")
    expected_state = str(st.session_state.get("qa_oauth_state", "")).strip()
    token_file = str(
        st.session_state.get("qa_report_token_file", get_config_value("GA4_TOKEN_FILE", "token.json"))
    ).strip() or "token.json"
    client_file = str(
        st.session_state.get("qa_report_client_secret_file", get_config_value("GA4_CLIENT_SECRETS_FILE", "client_secret.json"))
    ).strip() or "client_secret.json"

    try:
        if error_text:
            raise RuntimeError(f"Google 로그인 실패: {error_text}")
        if expected_state and state and state != expected_state:
            raise RuntimeError("Google 로그인 state 검증에 실패했습니다. 다시 시도하세요.")
        if not code:
            raise RuntimeError("Google 로그인 인가 코드가 없습니다.")
        exchange_google_oauth_code(
            client_secrets_file=client_file,
            code=code,
            token_file=token_file,
        )
        resume_state = state or expected_state
        oauth_ctx = pop_oauth_context(resume_state)
        restore_keys = [
            "qa_project_slug",
            "qa_project_domain",
            "qa_debug_session_id",
            "qa_debug_output_file",
            "qa_debug_started_at",
            "qa_debug_target_url",
            "qa_tester_name",
            "qa_tester_note",
            "required_event_text_input",
            "unknown_event_policy",
            "qa_report_property_id",
        ]
        for key in restore_keys:
            if key in oauth_ctx:
                st.session_state[key] = oauth_ctx.get(key)
        if str(st.session_state.get("qa_debug_session_id", "")).strip():
            st.session_state["qa_oauth_resume_notice"] = (
                "Google 로그인 후 기존 실시간 QA 세션을 복원했습니다. "
                "실시간 데이터 새로고침으로 이어서 확인하세요."
            )
        log_ui_action("oauth_callback_success")
        st.session_state["qa_oauth_notice"] = f"Google 로그인 완료. 토큰 저장: {token_file}"
        st.session_state["qa_oauth_error"] = ""
        st.session_state["qa_oauth_auth_url"] = ""
        st.session_state["qa_oauth_state"] = ""
        st.session_state["qa_ui_focus_after_oauth"] = "report"
    except Exception as exc:
        log_ui_action("oauth_callback_error", {"error": str(exc)})
        st.session_state["qa_oauth_error"] = to_user_error_message(exc)
        st.session_state["qa_oauth_notice"] = ""
    finally:
        st.query_params.clear()
        st.rerun()


@st.cache_data(ttl=600, show_spinner=False)
def fetch_ga4_property_list(token_file: str) -> pd.DataFrame:
    access_token = get_google_access_token(_resolve_project_path(token_file))
    rows: List[Dict[str, str]] = []
    page_token = ""

    while True:
        url = "https://analyticsadmin.googleapis.com/v1alpha/accountSummaries?pageSize=200"
        if page_token:
            url = f"{url}&pageToken={urllib_parse.quote(page_token)}"
        resp = _ga4_api_request(method="GET", url=url, access_token=access_token)
        for acc in resp.get("accountSummaries", []) or []:
            account_name = str(acc.get("displayName", "")).strip() or "-"
            account_ref = str(acc.get("account", "")).strip()
            for prop in acc.get("propertySummaries", []) or []:
                prop_ref = str(prop.get("property", "")).strip()
                prop_id = prop_ref.split("/")[-1] if "/" in prop_ref else prop_ref
                prop_name = str(prop.get("displayName", "")).strip() or "-"
                if not prop_id:
                    continue
                rows.append(
                    {
                        "property_id": prop_id,
                        "property_name": prop_name,
                        "account_name": account_name,
                        "account_ref": account_ref,
                        "label": f"{prop_name} ({prop_id}) · {account_name}",
                    }
                )
        page_token = str(resp.get("nextPageToken", "")).strip()
        if not page_token:
            break

    out = pd.DataFrame(rows)
    if out.empty:
        return pd.DataFrame(columns=["property_id", "property_name", "account_name", "account_ref", "label"])
    return out.drop_duplicates(subset=["property_id"]).sort_values(["account_name", "property_name"]).reset_index(drop=True)


@st.cache_data(ttl=600, show_spinner=False)
def fetch_ga4_reference_data(
    property_id: str,
    token_file: str,
    lookback_days: int = 30,
    top_events_limit: int = 200,
) -> Tuple[pd.DataFrame, pd.DataFrame]:
    pid = str(property_id).strip()
    if not pid:
        raise RuntimeError("GA4 Property ID가 비어 있습니다.")

    token_path = _resolve_project_path(token_file)
    access_token = get_google_access_token(token_path)
    base = f"https://analyticsdata.googleapis.com/v1beta/properties/{pid}"

    report = _ga4_api_request(
        method="POST",
        url=f"{base}:runReport",
        access_token=access_token,
        body={
            "dateRanges": [{"startDate": f"{int(lookback_days)}daysAgo", "endDate": "today"}],
            "dimensions": [{"name": "eventName"}],
            "metrics": [{"name": "eventCount"}],
            "orderBys": [{"metric": {"metricName": "eventCount"}, "desc": True}],
            "limit": int(top_events_limit),
        },
    )

    event_rows: List[Dict[str, object]] = []
    for row in report.get("rows", []) or []:
        dims = row.get("dimensionValues", []) or []
        mets = row.get("metricValues", []) or []
        event_name = str(dims[0].get("value", "")).strip() if dims else ""
        event_count_raw = str(mets[0].get("value", "0")).strip() if mets else "0"
        if not event_name:
            continue
        try:
            event_count = int(float(event_count_raw))
        except Exception:
            event_count = 0
        event_rows.append({"event_name": event_name, "event_count_30d": event_count})
    events_df = pd.DataFrame(event_rows)
    if not events_df.empty:
        events_df = events_df.sort_values("event_count_30d", ascending=False).reset_index(drop=True)

    metadata = _ga4_api_request(
        method="GET",
        url=f"{base}/metadata",
        access_token=access_token,
    )
    param_rows: List[Dict[str, str]] = []
    for dim in metadata.get("dimensions", []) or []:
        api_name = str(dim.get("apiName", "")).strip()
        if not api_name.startswith("customEvent:"):
            continue
        param_name = api_name.split("customEvent:", 1)[1].strip()
        if not param_name:
            continue
        param_rows.append(
            {
                "parameter_name": param_name,
                "api_name": api_name,
                "description": str(dim.get("description", "")).strip() or "-",
            }
        )
    params_df = pd.DataFrame(param_rows)
    if not params_df.empty:
        params_df = params_df.drop_duplicates(subset=["parameter_name"]).sort_values("parameter_name").reset_index(drop=True)

    return events_df, params_df


def build_api_aggregate_results(
    events_df: pd.DataFrame,
    required_events: List[str],
    required_params_by_event: Dict[str, List[str]],
    selected_params: List[str],
) -> pd.DataFrame:
    rows: List[Dict[str, object]] = []
    work = events_df.copy()
    if "event_name" not in work.columns:
        work["event_name"] = ""
    if "event_count_30d" not in work.columns:
        work["event_count_30d"] = 0

    event_counts = (
        work.groupby("event_name")["event_count_30d"].sum().to_dict()
        if not work.empty
        else {}
    )

    for event_name in [e.strip() for e in required_events if e.strip()]:
        count = int(event_counts.get(event_name, 0))
        rows.append(
            {
                "rule_id": f"required_event:{event_name}",
                "rule_name": f"필수 이벤트 존재(API): {event_name}",
                "status": "PASS" if count > 0 else "FAIL",
                "detail": f"30일 이벤트 수 {count}건",
                "fail_count": 0 if count > 0 else 1,
                "total_count": 1,
            }
        )

    for event_name, params in required_params_by_event.items():
        for param_name in [p.strip() for p in params if p.strip()]:
            rows.append(
                {
                    "rule_id": f"required_param:{event_name}:{param_name}",
                    "rule_name": f"필수 param 체크(API): {event_name}.{param_name}",
                    "status": "WARN",
                    "detail": "API 집계 모드에서는 이벤트별 param null/누락을 직접 계산하지 않습니다.",
                    "fail_count": 0,
                    "total_count": 1,
                }
            )

    null_targets = list(dict.fromkeys([p for p in selected_params if p.strip()] or ["transaction_id", "value", "currency"]))
    for param_name in null_targets:
        rows.append(
            {
                "rule_id": f"null_ratio:{param_name}",
                "rule_name": f"null 비율 체크(API): {param_name}",
                "status": "WARN",
                "detail": "API 집계 모드에서는 null 비율을 직접 계산하지 않습니다.",
                "fail_count": 0,
                "total_count": 1,
            }
        )

    rows.append(
        {
            "rule_id": "duplicate_transaction_id",
            "rule_name": "중복 transaction_id 체크(API)",
            "status": "WARN",
            "detail": "API 집계 모드에서는 transaction_id 중복을 직접 계산하지 않습니다.",
            "fail_count": 0,
            "total_count": 1,
        }
    )

    return pd.DataFrame(rows)


def build_api_event_preview_df(events_df: pd.DataFrame) -> pd.DataFrame:
    if events_df.empty:
        return pd.DataFrame(columns=["event_name", "event_count_30d", "event_date", "event_timestamp", "user_pseudo_id"])
    out = events_df.copy()
    out["event_date"] = pd.Timestamp.now(tz=LOCAL_TZ).date()
    out["event_timestamp"] = pd.NaT
    out["user_pseudo_id"] = pd.NA
    return out


def format_local_time(value: object) -> str:
    ts = pd.to_datetime(value, errors="coerce", utc=True)
    if pd.isna(ts):
        return "-"
    return ts.tz_convert(LOCAL_TZ).strftime("%H:%M:%S")


def filter_events_for_active_session(
    debug_df: pd.DataFrame,
    session_id: str,
    session_started_at: str = "",
) -> pd.DataFrame:
    if debug_df.empty:
        return debug_df
    sid = str(session_id or "").strip()
    out = debug_df.copy()
    if sid:
        row_sid = out.get("session_id", pd.Series(index=out.index, dtype="object")).astype(str).str.strip()
        param_sid = out.get("params", pd.Series(index=out.index, dtype="object")).apply(
            lambda p: str(p.get("qa_debug_session_id", "")).strip() if isinstance(p, dict) else ""
        )
        # session_id 컬럼이 있으면 우선 신뢰하고, 없을 때만 param sid를 보조로 사용한다.
        sid_mask = row_sid.eq(sid) | (row_sid.eq("") & param_sid.eq(sid))
        out = out[sid_mask].copy()

    started_text = str(session_started_at or "").strip()
    if started_text and not out.empty:
        started_ts = pd.to_datetime(started_text, errors="coerce", utc=True)
        if pd.notna(started_ts):
            cap_ts = pd.to_datetime(out.get("captured_at"), errors="coerce", utc=True)
            out = out[cap_ts >= started_ts].copy()
    return out


def normalize_uploaded_df(df: pd.DataFrame, requested_params: List[str]) -> pd.DataFrame:
    if any(col in df.columns for col in ["eventName", "dateHourMinute", "userPseudoId"]):
        return flatten_events(df, requested_params)

    rename_map = {col: normalize_param_name(str(col)) for col in df.columns}
    out = df.rename(columns=rename_map).copy()

    if "event_timestamp" in out.columns:
        out["event_timestamp"] = pd.to_datetime(out["event_timestamp"], errors="coerce")
    if "event_date" in out.columns:
        out["event_date"] = pd.to_datetime(out["event_date"], errors="coerce").dt.date

    expected = [
        "event_name",
        "event_date",
        "event_timestamp",
        "user_pseudo_id",
        "transaction_id",
        "value",
        "currency",
    ]
    expected.extend(normalize_param_name(p) for p in requested_params)
    for col in expected:
        if col not in out.columns:
            out[col] = pd.NA

    return out


def debug_events_to_qa_df(debug_df: pd.DataFrame, requested_params: List[str]) -> pd.DataFrame:
    if debug_df.empty:
        return normalize_uploaded_df(pd.DataFrame(), requested_params)

    rows: List[Dict[str, object]] = []
    req_norm = [normalize_param_name(p) for p in requested_params if str(p).strip()]
    for _, row in debug_df.iterrows():
        event_name = str(row.get("event_name", "")).strip()
        if not event_name:
            continue
        params = row.get("params")
        if not isinstance(params, dict):
            params = {}
        ts = pd.to_datetime(row.get("captured_at"), errors="coerce")
        out_row: Dict[str, object] = {
            "event_name": event_name,
            "event_timestamp": ts,
            "event_date": ts.date() if pd.notna(ts) else pd.NaT,
            "user_pseudo_id": str(params.get("user_pseudo_id", "")).strip(),
            "event_count": 1,
            "page_location": row.get("page_url", ""),
            "qa_debug_session_id": str(
                row.get("session_id", params.get("qa_debug_session_id", ""))
            ).strip(),
        }
        for key, value in params.items():
            out_row[normalize_param_name(str(key))] = value
        for key in req_norm:
            if key not in out_row:
                out_row[key] = pd.NA
        rows.append(out_row)

    out_df = pd.DataFrame(rows)
    if out_df.empty:
        return normalize_uploaded_df(pd.DataFrame(), requested_params)
    return normalize_uploaded_df(out_df, requested_params)


def describe_param(param_name: str) -> str:
    desc_map = {
        "page_location": "현재 페이지 URL",
        "page_title": "페이지 제목",
        "transaction_id": "구매/주문 식별자",
        "value": "매출/가치 값",
        "currency": "통화 코드",
        "qa_debug_session_id": "테스트 세션 식별자",
        "engagement_time_msec": "페이지 체류 시간(ms)",
        "session_id": "세션 ID",
        "session_engaged": "참여 세션 여부",
        "menu": "클릭 메뉴명",
        "click_text": "클릭 텍스트",
    }
    return desc_map.get(param_name, "-")


def extract_gtm_tag_name(params: Dict[str, object]) -> str:
    for key in ("gtm.tagName", "gtm_tag_name", "tag_name", "tag"):
        val = params.get(key)
        if str(val).strip():
            return str(val).strip()
    triggers = str(params.get("gtm.triggers", "")).strip()
    if triggers:
        return f"trigger:{triggers}"
    return "-"


def evaluate_event_status(
    event_name: str,
    params: Dict[str, object],
    allowed_events: List[str] | None = None,
    unknown_event_policy: str = "정보",
) -> str:
    status = "OK"
    allowed_set = {e.strip() for e in (allowed_events or []) if str(e).strip()}
    internal_prefixes = ("gtm.",)
    internal_events = {"set_user_property"}

    if not event_name:
        return "ERROR"

    if (
        allowed_set
        and event_name not in allowed_set
        and not event_name.startswith(internal_prefixes)
        and event_name not in internal_events
    ):
        if unknown_event_policy == "경고":
            status = "WARN"
        elif unknown_event_policy == "정보":
            status = "INFO"

    if event_name == "purchase":
        if not str(params.get("value", "")).strip() and status == "OK":
            status = "WARN"
        if not str(params.get("transaction_id", "")).strip():
            status = "ERROR"

    return status


def build_event_param_kv_view(
    debug_df: pd.DataFrame,
    max_rows: int = 2000,
    allowed_events: List[str] | None = None,
    unknown_event_policy: str = "정보",
) -> pd.DataFrame:
    cols = [
        "시간",
        "이벤트이름",
        "이벤트에 딸린 파라미터",
        "디스크립션",
        "값",
        "상태",
        "원래 수집될 수 있는 모든 값",
        "gtm태그명",
    ]
    if debug_df.empty:
        return pd.DataFrame(columns=cols)

    work = debug_df.copy()
    if "captured_at" in work.columns:
        work["captured_at"] = pd.to_datetime(work["captured_at"], errors="coerce")
        work = work.sort_values("captured_at", ascending=False)

    rows: List[Dict[str, object]] = []
    for _, row in work.iterrows():
        event_name = str(row.get("event_name", "")).strip()
        if not event_name:
            continue
        params = row.get("params")
        if not isinstance(params, dict):
            params = {}

        all_values = dict(params)
        if str(row.get("measurement_id", "")).strip() and "measurement_id" not in all_values:
            all_values["measurement_id"] = str(row.get("measurement_id", "")).strip()
        if str(row.get("client_id", "")).strip() and "client_id" not in all_values:
            all_values["client_id"] = str(row.get("client_id", "")).strip()

        all_values_text = json.dumps(all_values, ensure_ascii=False, sort_keys=True)
        gtm_tag_name = extract_gtm_tag_name(all_values)
        ts_text = format_local_time(row.get("captured_at"))
        status = evaluate_event_status(
            event_name=event_name,
            params=all_values,
            allowed_events=allowed_events,
            unknown_event_policy=unknown_event_policy,
        )

        if not all_values:
            rows.append(
                {
                    "시간": ts_text,
                    "이벤트이름": event_name,
                    "이벤트에 딸린 파라미터": "-",
                    "디스크립션": "-",
                    "값": "-",
                    "상태": status,
                    "원래 수집될 수 있는 모든 값": "{}",
                    "gtm태그명": gtm_tag_name,
                }
            )
        else:
            for key, value in all_values.items():
                val_text = str(value).strip()
                if not val_text:
                    continue
                rows.append(
                    {
                        "시간": ts_text,
                        "이벤트이름": event_name,
                        "이벤트에 딸린 파라미터": str(key),
                        "디스크립션": describe_param(str(key)),
                        "값": val_text,
                        "상태": status,
                        "원래 수집될 수 있는 모든 값": all_values_text,
                        "gtm태그명": gtm_tag_name,
                    }
                )
                if len(rows) >= max_rows:
                    break
        if len(rows) >= max_rows:
            break

    out = pd.DataFrame(rows)
    if out.empty:
        return pd.DataFrame(columns=cols)
    return out


SYSTEM_PARAM_KEYS = {
    "measurement_id",
    "client_id",
    "protocol_version",
    "user_language",
    "architecture",
    "bitness",
    "browser",
    "platform_version",
    "screen_resolution",
    "random_page_hash",
    "google_consent_default",
    "dma",
    "pscdl",
    "are",
    "frm",
    "npa",
    "tag_exp",
    "tfd",
    "event_usage",
    "hit_counter",
    "session_count",
    "session_engaged",
    "session_id",
}

MISSING_VALUE_TOKENS = {
    "",
    "(not set)",
    "not set",
    "null",
    "none",
    "undefined",
    "nan",
    "(none)",
    "n/a",
    "na",
}

PRIMARY_PARAM_PRIORITY = [
    "transaction_id",
    "value",
    "currency",
    "menu",
    "click_text",
    "page_location",
    "page_title",
    "qa_debug_session_id",
]


def _is_system_param_key(key: str) -> bool:
    k = str(key or "").strip().lower()
    if not k:
        return True
    if k in SYSTEM_PARAM_KEYS:
        return True
    if k.startswith("gtm."):
        return True
    return False


def _to_text_value(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and pd.isna(value):
        return ""
    text = str(value).strip()
    return text


def _is_missing_like_value(value_text: str) -> bool:
    norm = str(value_text).strip().lower()
    return norm in MISSING_VALUE_TOKENS


def _is_false_like_value(value_text: str) -> bool:
    return str(value_text).strip().lower() in {"false"}


def _is_zero_like_value(value_text: str) -> bool:
    try:
        return float(str(value_text).strip()) == 0.0
    except Exception:
        return False


def validate_param_format(param_name: str, value_text: str) -> Tuple[bool, str]:
    key = normalize_param_name(str(param_name))
    value = str(value_text).strip()
    if not value:
        return False, "빈 값"

    if key == "currency":
        return (bool(re.fullmatch(r"[A-Z]{3}", value)), "3자리 대문자 통화코드 필요")
    if key == "value":
        try:
            float(value)
            return True, "숫자형"
        except Exception:
            return False, "숫자형 아님"
    if key in {"page_location", "page_referrer"}:
        return (value.startswith("http://") or value.startswith("https://"), "URL 형식 필요")
    if key == "qa_debug_session_id":
        ok = value.startswith("dbg_") or value.startswith("qa_debug_session_")
        return (ok, "qa 디버그 세션 형식 아님")
    if key.endswith("_id"):
        return (len(value) >= 2, "id 값 길이 부족")
    return True, "기본 검증 통과"


def build_suspicious_param_profile(debug_df: pd.DataFrame) -> Dict[str, str]:
    if debug_df.empty:
        return {}

    key_values: Dict[str, List[str]] = {}
    for _, row in debug_df.iterrows():
        params = row.get("params")
        if not isinstance(params, dict):
            continue
        for key, value in params.items():
            key_text = str(key).strip()
            if not key_text or _is_system_param_key(key_text):
                continue
            value_text = _to_text_value(value)
            if not value_text:
                continue
            key_values.setdefault(key_text, []).append(value_text)

    profile: Dict[str, str] = {}
    for key, values in key_values.items():
        if not values:
            continue
        reasons: List[str] = []
        norms = [v.strip().lower() for v in values if v.strip()]
        uniq_norm = sorted(set(norms))
        if values and all(_is_false_like_value(v) for v in values):
            reasons.append("모든 이벤트에서 false")
        if values and all(_is_zero_like_value(v) for v in values):
            reasons.append("모든 이벤트에서 0")
        if len(values) >= 2 and len(uniq_norm) == 1:
            reasons.append(f"모든 이벤트에서 동일값({values[0]})")
        if reasons:
            profile[key] = " / ".join(dict.fromkeys(reasons))
    return profile


def debug_events_to_full_csv_bytes(debug_df: pd.DataFrame) -> bytes:
    if debug_df.empty:
        return b""
    work = debug_df.copy()
    if "params" in work.columns:
        work["params_json"] = work["params"].apply(
            lambda x: json.dumps(x, ensure_ascii=False, sort_keys=True) if isinstance(x, dict) else "{}"
        )
        work = work.drop(columns=["params"])
    return work.to_csv(index=False).encode("utf-8-sig")


def debug_file_to_full_csv_bytes(output_file: Path) -> bytes:
    path = Path(output_file)
    if not path.exists():
        return b""
    try:
        full_df = pd.read_json(path, lines=True)
    except Exception:
        return b""
    return debug_events_to_full_csv_bytes(full_df)


def _serialize_realtime_events_for_export(rt_events: pd.DataFrame) -> pd.DataFrame:
    if rt_events.empty:
        return pd.DataFrame()
    work = rt_events.copy()
    for col in ["정상 그룹", "값없음 그룹", "의심 그룹", "시스템 그룹"]:
        if col in work.columns:
            work[col] = work[col].apply(
                lambda x: json.dumps(x, ensure_ascii=False, sort_keys=True) if isinstance(x, dict) else "{}"
            )
    if "captured_at" in work.columns:
        work["captured_at"] = pd.to_datetime(work["captured_at"], errors="coerce").astype(str)
    ordered_cols = [
        "group_session_id",
        "시간",
        "이벤트",
        "대표 파라미터",
        "대표 값",
        "상태",
        "정상 그룹",
        "값없음 그룹",
        "의심 그룹",
        "시스템 그룹",
        "captured_at",
    ]
    keep_cols = [c for c in ordered_cols if c in work.columns]
    return work[keep_cols]


def realtime_events_to_csv_bytes(rt_events: pd.DataFrame) -> bytes:
    if rt_events.empty:
        return b""
    export_df = _serialize_realtime_events_for_export(rt_events)
    if export_df.empty:
        return b""
    return export_df.to_csv(index=False).encode("utf-8-sig")


def realtime_events_to_excel_bytes(rt_events: pd.DataFrame) -> bytes:
    if rt_events.empty:
        return b""
    export_df = _serialize_realtime_events_for_export(rt_events)
    if export_df.empty:
        return b""
    out = io.BytesIO()
    try:
        with pd.ExcelWriter(out) as writer:
            export_df.to_excel(writer, index=False, sheet_name="realtime_events")
        return out.getvalue()
    except Exception:
        return b""


def summarize_event_conclusion(row_ev: Dict[str, object]) -> Dict[str, object]:
    normal_group = row_ev.get("정상 그룹", {})
    missing_group = row_ev.get("값없음 그룹", {})
    suspicious_group = row_ev.get("의심 그룹", {})
    system_group = row_ev.get("시스템 그룹", {})

    normal_count = len(normal_group) if isinstance(normal_group, dict) else 0
    missing_count = len(missing_group) if isinstance(missing_group, dict) else 0
    suspicious_count = len(suspicious_group) if isinstance(suspicious_group, dict) else 0
    system_count = len(system_group) if isinstance(system_group, dict) else 0

    primary_key = str(row_ev.get("대표 파라미터", "-")).strip()
    primary_value = str(row_ev.get("대표 값", "-")).strip()
    if (
        primary_key
        and primary_key != "-"
        and primary_value
        and primary_value != "-"
        and not _is_system_param_key(primary_key)
        and not _is_missing_like_value(primary_value)
    ):
        normal_count += 1

    critical_count = 1 if str(row_ev.get("상태", "")).upper() == "ERROR" else 0
    caution_count = missing_count + suspicious_count
    info_count = system_count

    if critical_count > 0:
        message = "이 이벤트는 오류 가능성이 있어 우선 수정이 필요합니다."
    elif caution_count > 0 and suspicious_count > 0:
        message = "이 이벤트는 기능적으로는 정상, 다만 불필요 파라미터 다수 포함 가능성이 있습니다."
    elif caution_count > 0:
        message = "이 이벤트는 기능적으로는 정상, 다만 비어 있는 값 확인이 필요합니다."
    else:
        message = "이 이벤트는 기능적으로 정상입니다."

    return {
        "critical": int(critical_count),
        "caution": int(caution_count),
        "info": int(info_count),
        "ok": int(normal_count),
        "message": message,
    }


def summarize_realtime_quality(rt_events: pd.DataFrame) -> Dict[str, object]:
    if rt_events.empty:
        return {
            "score": 0,
            "critical": 0,
            "caution": 0,
            "info": 0,
            "ok": 0,
            "event_count": 0,
            "message": "실시간 데이터가 없어 점수를 계산할 수 없습니다.",
        }

    total_critical = 0
    total_caution = 0
    total_info = 0
    total_ok = 0
    for row_ev in rt_events.to_dict("records"):
        event_summary = summarize_event_conclusion(row_ev)
        total_critical += int(event_summary["critical"])
        total_caution += int(event_summary["caution"])
        total_info += int(event_summary["info"])
        total_ok += int(event_summary["ok"])

    signals = total_critical + total_caution + total_info + total_ok
    if signals <= 0:
        score = 100
    else:
        risk_ratio = (
            (1.0 * total_critical) + (0.45 * total_caution) + (0.1 * total_info)
        ) / float(signals)
        score = int(round(max(0.0, min(1.0, 1.0 - risk_ratio)) * 100))

    if score >= 85:
        message = "기능적으로 양호한 상태입니다."
    elif score >= 70:
        message = "전반적으로 정상이나 일부 점검이 필요합니다."
    elif score >= 50:
        message = "주의 항목이 많아 우선 점검이 필요합니다."
    else:
        message = "치명/주의 항목 비중이 높아 즉시 점검이 필요합니다."

    return {
        "score": score,
        "critical": int(total_critical),
        "caution": int(total_caution),
        "info": int(total_info),
        "ok": int(total_ok),
        "event_count": int(len(rt_events)),
        "message": message,
    }


def build_realtime_event_rows(
    debug_df: pd.DataFrame,
    allowed_events: List[str] | None = None,
    unknown_event_policy: str = "정보",
) -> pd.DataFrame:
    cols = [
        "group_session_id",
        "시간",
        "이벤트",
        "대표 파라미터",
        "대표 값",
        "상태",
        "정상 그룹",
        "값없음 그룹",
        "의심 그룹",
        "시스템 그룹",
        "전체 파라미터",
        "captured_at",
    ]
    if debug_df.empty:
        return pd.DataFrame(columns=cols)

    work = debug_df.copy()
    if "captured_at" in work.columns:
        work["captured_at"] = pd.to_datetime(work["captured_at"], errors="coerce")
        work = work.sort_values("captured_at", ascending=False)

    suspicious_profile = build_suspicious_param_profile(work)

    rows: List[Dict[str, object]] = []
    for _, row in work.iterrows():
        event_name = str(row.get("event_name", "")).strip()
        if not event_name:
            continue
        raw_params = row.get("params")
        params = raw_params if isinstance(raw_params, dict) else {}
        all_params = dict(params)

        measurement_id = _to_text_value(row.get("measurement_id", ""))
        client_id = _to_text_value(row.get("client_id", ""))
        if measurement_id and "measurement_id" not in all_params:
            all_params["measurement_id"] = measurement_id
        if client_id and "client_id" not in all_params:
            all_params["client_id"] = client_id

        normal_group: Dict[str, Dict[str, str]] = {}
        missing_group: Dict[str, Dict[str, str]] = {}
        suspicious_group: Dict[str, Dict[str, str]] = {}
        system_group: Dict[str, Dict[str, str]] = {}
        for key, value in all_params.items():
            key_text = str(key).strip()
            value_text = _to_text_value(value)
            if not key_text:
                continue
            if _is_system_param_key(key_text):
                system_group[key_text] = {"값": value_text or "-", "사유": "시스템/기술 파라미터"}
                continue
            if _is_missing_like_value(value_text):
                missing_group[key_text] = {"값": value_text or "-", "사유": "미입력/기본값"}
                continue
            if key_text in suspicious_profile:
                suspicious_group[key_text] = {"값": value_text, "사유": suspicious_profile[key_text]}
                continue
            valid_ok, valid_msg = validate_param_format(key_text, value_text)
            if not valid_ok:
                missing_group[key_text] = {"값": value_text, "사유": f"형식 오류: {valid_msg}"}
                continue
            normal_group[key_text] = {"값": value_text, "사유": "형식 검증 통과"}

        primary_key = "-"
        primary_value = "-"
        for cand in PRIMARY_PARAM_PRIORITY:
            if cand in normal_group:
                primary_key = cand
                primary_value = normal_group[cand]["값"]
                break
        if primary_key == "-" and normal_group:
            first_key = next(iter(normal_group.keys()))
            primary_key = first_key
            primary_value = normal_group[first_key]["값"]
        if primary_key == "-" and suspicious_group:
            first_key = next(iter(suspicious_group.keys()))
            primary_key = first_key
            primary_value = suspicious_group[first_key]["값"]
        if primary_key == "-" and missing_group:
            first_key = next(iter(missing_group.keys()))
            primary_key = first_key
            primary_value = missing_group[first_key]["값"]

        if primary_key in normal_group:
            normal_group = {k: v for k, v in normal_group.items() if k != primary_key}
        if primary_key in suspicious_group:
            suspicious_group = {k: v for k, v in suspicious_group.items() if k != primary_key}
        if primary_key in missing_group:
            missing_group = {k: v for k, v in missing_group.items() if k != primary_key}

        group_session_id = _to_text_value(row.get("session_id", "")) or _to_text_value(params.get("qa_debug_session_id", "")) or "-"
        status = evaluate_event_status(
            event_name=event_name,
            params=all_params,
            allowed_events=allowed_events,
            unknown_event_policy=unknown_event_policy,
        )

        rows.append(
            {
                "group_session_id": group_session_id,
                "시간": format_local_time(row.get("captured_at")),
                "이벤트": event_name,
                "대표 파라미터": primary_key,
                "대표 값": primary_value,
                "상태": status,
                "정상 그룹": normal_group,
                "값없음 그룹": missing_group,
                "의심 그룹": suspicious_group,
                "시스템 그룹": system_group,
                "전체 파라미터": all_params,
                "captured_at": row.get("captured_at"),
            }
        )

    out = pd.DataFrame(rows)
    if out.empty:
        return pd.DataFrame(columns=cols)
    return out


def build_realtime_funnel_progress(debug_df: pd.DataFrame, steps: List[str]) -> pd.DataFrame:
    if debug_df.empty or not steps:
        return pd.DataFrame(columns=["step", "상태", "최초 감지 시각"])

    work = debug_df.copy()
    if "captured_at" in work.columns:
        work["captured_at"] = pd.to_datetime(work["captured_at"], errors="coerce")
        work = work.sort_values("captured_at")

    first_seen: Dict[str, str] = {}
    ordered_events: List[str] = []
    for _, row in work.iterrows():
        event_name = str(row.get("event_name", "")).strip()
        if not event_name:
            continue
        ordered_events.append(event_name)
        if event_name not in first_seen:
            first_seen[event_name] = format_local_time(row.get("captured_at"))

    pointer = 0
    for ev in ordered_events:
        if pointer < len(steps) and ev == steps[pointer]:
            pointer += 1

    rows: List[Dict[str, str]] = []
    observed_set = set(ordered_events)
    for idx, step in enumerate(steps):
        if idx < pointer:
            status = "완료"
        elif step in observed_set:
            status = "감지됨(순서대기)"
        else:
            status = "대기"
        rows.append(
            {
                "step": step,
                "상태": status,
                "최초 감지 시각": first_seen.get(step, "-"),
            }
        )
    return pd.DataFrame(rows)


def build_qa_results_view(results_df: pd.DataFrame) -> pd.DataFrame:
    if results_df.empty:
        return results_df

    view = results_df.copy()
    view["구분"] = "기타"
    view["이벤트"] = ""
    view["파라미터"] = ""

    for idx, rule_id in view["rule_id"].astype(str).items():
        if rule_id.startswith("required_event:"):
            event_name = rule_id.split(":", 1)[1]
            view.at[idx, "구분"] = "필수 이벤트"
            view.at[idx, "이벤트"] = event_name
        elif rule_id.startswith("required_param:"):
            parts = rule_id.split(":", 2)
            if len(parts) == 3:
                view.at[idx, "구분"] = "필수 파라미터"
                view.at[idx, "이벤트"] = parts[1]
                view.at[idx, "파라미터"] = parts[2]
        elif rule_id.startswith("null_ratio:"):
            param_name = rule_id.split(":", 1)[1]
            view.at[idx, "구분"] = "Null 비율"
            view.at[idx, "파라미터"] = param_name
        elif rule_id == "value_numeric":
            view.at[idx, "구분"] = "타입 체크"
            view.at[idx, "이벤트"] = "purchase"
            view.at[idx, "파라미터"] = "value"
        elif rule_id == "duplicate_transaction_id":
            view.at[idx, "구분"] = "중복 체크"
            view.at[idx, "파라미터"] = "transaction_id"
        elif rule_id in {"purchase_without_value", "purchase_without_add_to_cart", "purchase_without_sign_up"}:
            view.at[idx, "구분"] = "비즈니스 룰"
        elif rule_id == "funnel_order":
            view.at[idx, "구분"] = "퍼널 순서"
        elif rule_id == "missing_event_timestamp":
            view.at[idx, "구분"] = "로그 품질"
            view.at[idx, "파라미터"] = "event_timestamp"

    ordered_cols = [
        "구분",
        "이벤트",
        "파라미터",
        "status",
        "detail",
        "rule_name",
        "rule_id",
        "fail_count",
        "total_count",
    ]
    keep_cols = [col for col in ordered_cols if col in view.columns]
    return view[keep_cols]


def build_event_preview(df: pd.DataFrame, requested_params: List[str]) -> pd.DataFrame:
    if df.empty:
        return df

    requested_norm = [normalize_param_name(p) for p in requested_params if p.strip()]
    base_cols = ["event_name", "event_date", "event_timestamp", "user_pseudo_id"]
    priority_params = ["transaction_id", "qa_debug_session_id", "value", "currency", "page_location"]
    param_cols = list(dict.fromkeys(priority_params + requested_norm))

    ordered_cols: List[str] = []
    for col in base_cols + param_cols:
        if col in df.columns:
            ordered_cols.append(col)
    for col in df.columns:
        if col not in ordered_cols:
            ordered_cols.append(col)

    return df[ordered_cols]


def resolve_debug_output_file(
    session_id: str,
    snapshot: Dict[str, object],
    state_output_file: str,
) -> Path | None:
    candidates: List[Path] = []
    snap_path_text = str(snapshot.get("output_file", "")).strip() if snapshot else ""
    if snap_path_text:
        candidates.append(Path(snap_path_text))

    state_path_text = str(state_output_file or "").strip()
    if state_path_text:
        candidates.append(Path(state_path_text))

    sid = str(session_id or "").strip()
    if sid:
        candidates.append(get_active_project_paths()["qa_sessions_dir"] / sid / "debug_stream.jsonl")
        candidates.append(Path(f"data/debug_stream/{sid}.jsonl"))

    for cand in candidates:
        if cand.exists():
            return cand
    return candidates[0] if candidates else None


def inject_case_from_page_location(df: pd.DataFrame, case_param: str) -> pd.DataFrame:
    param = case_param.strip()
    if df.empty or not param or "page_location" not in df.columns:
        return df

    out = df.copy()
    if param not in out.columns:
        out[param] = pd.NA

    missing_mask = out[param].isna() | (out[param].astype(str).str.strip() == "")
    if not missing_mask.any():
        return out

    def extract_value(page_url: object) -> str | None:
        if page_url is None or pd.isna(page_url):
            return None
        try:
            parsed = urlparse(str(page_url))
            values = parse_qs(parsed.query).get(param, [])
            if values:
                return values[0]
            return None
        except Exception:
            return None

    out.loc[missing_mask, param] = out.loc[missing_mask, "page_location"].apply(extract_value)
    return out


def _safe_params_dict(value: object) -> Dict[str, object]:
    if isinstance(value, dict):
        return value
    return {}


def source_to_view_label(source: str) -> str:
    src = (source or "").strip()
    if src == "ga_hit":
        return "히트(collect)"
    if src == "gtag":
        return "발화(gtag)"
    if src == "dataLayer.push":
        return "발화(dataLayer)"
    return src or "-"


def filter_debug_events_by_view(debug_df: pd.DataFrame, capture_view: str) -> pd.DataFrame:
    if debug_df.empty or "source" not in debug_df.columns:
        return debug_df
    src_series = debug_df["source"].astype(str).str.strip()
    if capture_view == "히트(collect)":
        return debug_df[src_series == "ga_hit"].copy()
    if capture_view == "발화(dataLayer/gtag)":
        return debug_df[src_series != "ga_hit"].copy()
    return debug_df.copy()


def summarize_major_params(params: Dict[str, object]) -> str:
    if not params:
        return ""
    priority = ["measurement_id", "transaction_id", "value", "currency", "qa_debug_session_id"]
    parts: List[str] = []
    for key in priority:
        if key in params and str(params.get(key, "")).strip():
            parts.append(f"{key}={params[key]}")
    if not parts:
        for key, value in params.items():
            if str(value).strip():
                parts.append(f"{key}={value}")
            if len(parts) >= 3:
                break
    return ", ".join(parts)


def build_realtime_timeline_view(
    debug_df: pd.DataFrame,
    allowed_events: List[str] | None = None,
    unknown_event_policy: str = "정보",
) -> pd.DataFrame:
    if debug_df.empty:
        return pd.DataFrame(columns=["시간", "수집원", "event_name", "주요 파라미터", "상태", "상세"])

    work = debug_df.copy()
    if "captured_at" in work.columns:
        work["captured_at"] = pd.to_datetime(work["captured_at"], errors="coerce")
        work = work.sort_values("captured_at")

    seen_steps: set[str] = set()
    allowed_set = {e.strip() for e in (allowed_events or []) if str(e).strip()}
    internal_prefixes = ("gtm.",)
    internal_events = {"set_user_property"}
    out_rows: List[Dict[str, str]] = []

    for _, row in work.iterrows():
        event_name = str(row.get("event_name", "")).strip()
        params = _safe_params_dict(row.get("params"))
        measurement_id = str(row.get("measurement_id", "")).strip()
        if measurement_id and "measurement_id" not in params:
            params = dict(params)
            params["measurement_id"] = measurement_id
        issues: List[str] = []
        status = "OK"

        if not event_name:
            status = "ERROR"
            issues.append("event_name 없음")
        elif (
            allowed_set
            and event_name not in allowed_set
            and not event_name.startswith(internal_prefixes)
            and event_name not in internal_events
        ):
            if unknown_event_policy == "경고":
                status = "WARN" if status == "OK" else status
                issues.append("정의되지 않은 이벤트명")
            elif unknown_event_policy == "정보":
                if status == "OK":
                    status = "INFO"
                issues.append("정의되지 않은 이벤트명")

        if event_name == "purchase":
            if not str(params.get("value", "")).strip():
                status = "WARN" if status == "OK" else status
                issues.append("purchase에 value 없음")
            if not str(params.get("transaction_id", "")).strip():
                status = "ERROR"
                issues.append("purchase에 transaction_id 없음")
            if "add_to_cart" not in seen_steps:
                status = "WARN" if status == "OK" else status
                issues.append("add_to_cart 없이 purchase")

        if event_name:
            seen_steps.add(event_name)

        ts_text = format_local_time(row.get("captured_at"))

        out_rows.append(
            {
                "시간": ts_text,
                "수집원": source_to_view_label(str(row.get("source", ""))),
                "event_name": event_name or "(empty)",
                "주요 파라미터": summarize_major_params(params),
                "상태": status,
                "상세": " | ".join(issues) if issues else "-",
            }
        )

    out = pd.DataFrame(out_rows)
    return out.iloc[::-1].reset_index(drop=True)


def style_realtime_timeline(df: pd.DataFrame):
    def _row_style(row):
        status = str(row.get("상태", ""))
        if status == "ERROR":
            return ["background-color: #ffebee"] * len(row)
        if status == "WARN":
            return ["background-color: #fff8e1"] * len(row)
        if status == "INFO":
            return ["background-color: #e3f2fd"] * len(row)
        return [""] * len(row)

    return df.style.apply(_row_style, axis=1)


def to_user_error_message(exc: Exception) -> str:
    msg = str(exc).strip()
    low = msg.lower()

    if "playwright" in low and ("not found" in low or "install" in low):
        return "브라우저 디버깅 의존성 오류: `pip install playwright && playwright install chromium` 실행 후 다시 시도하세요."
    if "디버깅 브라우저 실행에 실패했습니다" in msg:
        return (
            "디버깅 브라우저 실행 실패: EC2에서 아래 순서로 복구하세요.\n"
            "1) `cd /opt/ga4-qa-mvp && source .venv/bin/activate`\n"
            "2) `playwright install --with-deps chromium`\n"
            "3) `sudo systemctl restart ga4-qa-mvp`"
        )
    return msg if msg else "알 수 없는 오류가 발생했습니다."


process_google_oauth_callback_if_present()


source = "실시간 디버깅 스트림"
default_required_events = ""
default_param_text = ""
default_funnel_steps = ""
default_scenario_steps = "view_item,add_to_cart,begin_checkout,purchase"
default_params = "transaction_id,value,currency,qa_debug_session_id"
scenario_key_modes = {
    "transaction_id 기준": "transaction_id",
    "user_pseudo_id 기준": "user_pseudo_id",
    "qa_debug_session_id 기준": "qa_debug_session_id",
    "키 없이 집계형": "none",
}
risk_level_ko = {"Low": "낮음", "Medium": "중간", "High": "높음"}
scenario_mode_ko = {"key": "키 기반", "user": "사용자 기반", "aggregate": "집계형", "empty": "데이터 없음"}
debug_case_param_name = "qa_debug_session_id"

if "qa_debug_session_id" not in st.session_state:
    st.session_state["qa_debug_session_id"] = ""
if "qa_debug_output_file" not in st.session_state:
    st.session_state["qa_debug_output_file"] = ""
if "qa_debug_started_at" not in st.session_state:
    st.session_state["qa_debug_started_at"] = ""
if "qa_debug_target_url" not in st.session_state:
    st.session_state["qa_debug_target_url"] = ""
if "qa_tester_name" not in st.session_state:
    st.session_state["qa_tester_name"] = ""
if "qa_tester_note" not in st.session_state:
    st.session_state["qa_tester_note"] = ""
if "unknown_event_policy" not in st.session_state:
    st.session_state["unknown_event_policy"] = "정보"
if "realtime_panel_auto_refresh" not in st.session_state:
    st.session_state["realtime_panel_auto_refresh"] = False
if "realtime_panel_refresh_interval" not in st.session_state:
    st.session_state["realtime_panel_refresh_interval"] = 2
if "realtime_last_refresh_at" not in st.session_state:
    st.session_state["realtime_last_refresh_at"] = ""
if "qa_report_client_secret_file" not in st.session_state:
    st.session_state["qa_report_client_secret_file"] = get_config_value("GA4_CLIENT_SECRETS_FILE", "client_secret.json")
if "qa_report_token_file" not in st.session_state:
    st.session_state["qa_report_token_file"] = get_config_value("GA4_TOKEN_FILE", "token.json")
if "qa_report_property_options" not in st.session_state:
    st.session_state["qa_report_property_options"] = []
if "qa_oauth_auth_url" not in st.session_state:
    st.session_state["qa_oauth_auth_url"] = ""
if "qa_oauth_state" not in st.session_state:
    st.session_state["qa_oauth_state"] = ""
if "qa_oauth_notice" not in st.session_state:
    st.session_state["qa_oauth_notice"] = ""
if "qa_oauth_error" not in st.session_state:
    st.session_state["qa_oauth_error"] = ""
if "qa_ingest_server_error" not in st.session_state:
    st.session_state["qa_ingest_server_error"] = ""
if "qa_ingest_local_ok" not in st.session_state:
    st.session_state["qa_ingest_local_ok"] = False
if "qa_access_authenticated" not in st.session_state:
    st.session_state["qa_access_authenticated"] = False
if "qa_access_role" not in st.session_state:
    st.session_state["qa_access_role"] = "guest"
if "qa_access_user" not in st.session_state:
    st.session_state["qa_access_user"] = ""
if "qa_oauth_go_now_url" not in st.session_state:
    st.session_state["qa_oauth_go_now_url"] = ""
if "qa_oauth_resume_notice" not in st.session_state:
    st.session_state["qa_oauth_resume_notice"] = ""
if "qa_ui_focus_after_oauth" not in st.session_state:
    st.session_state["qa_ui_focus_after_oauth"] = ""
if "qa_project_slug" not in st.session_state:
    st.session_state["qa_project_slug"] = DEFAULT_PROJECT_SLUG
if "qa_project_domain" not in st.session_state:
    st.session_state["qa_project_domain"] = ""
if "qa_new_project_name" not in st.session_state:
    st.session_state["qa_new_project_name"] = ""
if "qa_new_project_domain" not in st.session_state:
    st.session_state["qa_new_project_domain"] = ""
if "qa_project_notice" not in st.session_state:
    st.session_state["qa_project_notice"] = ""

ensure_project_structure(st.session_state.get("qa_project_slug", DEFAULT_PROJECT_SLUG))

enforce_access_gate()

ensure_err = ""
# 기본은 독립 systemd 수집기(ga4-qa-ingest.service)를 사용한다.
ingest_embedded_mode = is_truthy(get_config_value("QA_INGEST_EMBEDDED_MODE", "0"))
if ingest_embedded_mode:
    try:
        ensure_ingest_server(host="127.0.0.1", port=8600)
    except Exception as exc:
        ensure_err = str(exc)

local_health_ok = probe_ingest_health("http://127.0.0.1:8600/qa/health")

st.session_state["qa_ingest_local_ok"] = bool(local_health_ok)
st.session_state["qa_ingest_server_ok"] = bool(local_health_ok)
if st.session_state["qa_ingest_server_ok"]:
    st.session_state["qa_ingest_server_error"] = ""
else:
    detail_bits: List[str] = []
    if ensure_err:
        detail_bits.append(f"ensure: {ensure_err}")
    detail_bits.append(f"local_health({'ok' if local_health_ok else 'fail'}): http://127.0.0.1:8600/qa/health")
    st.session_state["qa_ingest_server_error"] = " | ".join(detail_bits)

scenario_enabled = True
scenario_steps_text = default_scenario_steps
scenario_key_mode_label = "transaction_id 기준"
scenario_key_field = "transaction_id"
scenario_key_value = ""
realtime_debug_start_clicked = False
realtime_debug_stop_clicked = False

with st.sidebar:
    st.header("설정")
    st.caption(
        f"접속: {st.session_state.get('qa_access_user', '-') or '-'} "
        f"({st.session_state.get('qa_access_role', 'guest')})"
    )
    if st.button("로그아웃", key="qa_access_logout_btn"):
        log_ui_action("access_logout")
        st.session_state["qa_access_authenticated"] = False
        st.session_state["qa_access_role"] = "guest"
        st.session_state["qa_access_user"] = ""
        st.rerun()
    with st.expander("0) Workspace / Project", expanded=True):
        project_notice = str(st.session_state.get("qa_project_notice", "")).strip()
        if project_notice:
            st.info(project_notice)
            st.session_state["qa_project_notice"] = ""
        project_options = list_workspace_projects()
        current_project = _slugify_project_name(st.session_state.get("qa_project_slug", DEFAULT_PROJECT_SLUG))
        if current_project not in project_options:
            project_options = sorted(list(dict.fromkeys(project_options + [current_project])))
        selected_project = st.selectbox(
            "Project",
            options=project_options,
            index=project_options.index(current_project) if current_project in project_options else 0,
            key="qa_project_slug_selectbox",
        )
        st.session_state["qa_project_slug"] = _slugify_project_name(selected_project)
        st.text_input(
            "Project Domain",
            value=st.session_state.get("qa_project_domain", ""),
            key="qa_project_domain",
            placeholder="예: datanugget.io",
        )
        p1, p2 = st.columns(2)
        with p1:
            st.text_input("새 Project", key="qa_new_project_name", placeholder="예: musinsa")
        with p2:
            st.text_input("도메인", key="qa_new_project_domain", placeholder="예: musinsa.com")
        if st.button("Project 생성", key="qa_create_project_btn"):
            new_name = str(st.session_state.get("qa_new_project_name", "")).strip()
            if not new_name:
                st.session_state["qa_project_notice"] = "새 Project 이름을 먼저 입력하세요."
                st.rerun()
            new_slug = _slugify_project_name(new_name)
            ensure_project_structure(new_slug)
            existed = new_slug in project_options
            st.session_state["qa_project_slug"] = new_slug
            if existed:
                st.session_state["qa_project_notice"] = f"이미 존재하는 Project를 선택했습니다: {new_slug}"
            else:
                st.session_state["qa_project_notice"] = f"Project 생성 완료: {new_slug}"
            st.rerun()
        if st.button("이전 테스트 데이터 가져오기", key="qa_import_legacy_btn"):
            import_counts = import_legacy_data_to_project(st.session_state.get("qa_project_slug", DEFAULT_PROJECT_SLUG))
            st.success(
                "가져오기 완료 | "
                f"session {import_counts['sessions']}개, "
                f"event {import_counts['events']}개, "
                f"ui_action {import_counts['actions']}개, "
                f"stream {import_counts['streams']}개, "
                f"schema {import_counts['schemas']}개"
            )
            st.rerun()
        project_paths = get_active_project_paths()
        st.caption(f"Workspace 경로: {project_paths['project_root']}")
        sess_df = pd.DataFrame()
        try:
            sess_df = list_recent_sessions(get_active_test_log_db_path(), limit=10)
            if sess_df.empty:
                st.caption("QA Sessions: 없음")
            else:
                st.caption(f"QA Sessions: 최근 {len(sess_df)}개")
                st.dataframe(sess_df[["session_id", "status", "captured_events"]], use_container_width=True, height=180)
        except Exception:
            st.caption("QA Sessions 목록을 불러오지 못했습니다.")
        if not sess_df.empty:
            load_options = sess_df["session_id"].astype(str).tolist()
            load_sid = st.selectbox("불러올 QA Session", options=load_options, key="qa_load_session_id")
            if st.button("선택 세션 불러오기", key="qa_load_session_btn"):
                sid = str(load_sid).strip()
                snap = get_session(get_active_test_log_db_path(), sid)
                output_file = project_paths["qa_sessions_dir"] / sid / "debug_stream.jsonl"
                if not output_file.exists():
                    fallback = Path(f"data/debug_stream/{sid}.jsonl")
                    if fallback.exists():
                        output_file = fallback
                st.session_state["qa_debug_session_id"] = sid
                st.session_state["qa_debug_output_file"] = str(output_file)
                st.session_state["qa_debug_started_at"] = str(snap.get("started_at", "")).strip()
                st.success(f"세션 불러오기 완료: {sid}")
                st.rerun()

    with st.expander("1) 데이터 소스/연결", expanded=True):
        st.markdown("**Playwright 네트워크 인터셉트 + QA 리포트 API 참조 모드**")
        st.caption("확장/스니펫 없이 Playwright request hook으로 collect 히트를 수집합니다.")
        st.caption("QA 리포트 화면에서는 최근 30일 API 이벤트/매개변수 목록을 참고용으로 불러올 수 있습니다.")
        if st.session_state.get("qa_ingest_server_ok", False):
            st.caption("서버 수집기 상태: OK (local:127.0.0.1:8600)")
        else:
            st.error("서버 수집기 상태: 실패 (서비스 로그 확인)")
            last_ingest_err = str(st.session_state.get("qa_ingest_server_error", "")).strip()
            if last_ingest_err:
                st.caption(f"원인: {last_ingest_err}")
        analytics_proxy_on = str(os.getenv("QA_ANALYTICS_PROXY_ENABLED", "0")).strip()
        st.caption(f"Analytics Proxy: {'ON' if analytics_proxy_on in {'1', 'true', 'True'} else 'OFF'}")

    with st.expander("2) 실시간 디버깅 스트림", expanded=True):
        default_debug_url = st.session_state.get("qa_debug_target_url", "").strip()
        if default_debug_url and not st.session_state.get("qa_debug_target_url", "").strip():
            st.session_state["qa_debug_target_url"] = default_debug_url

        debug_target_url = st.text_input(
            "디버깅 대상 URL",
            value=st.session_state.get("qa_debug_target_url", ""),
            key="qa_debug_target_url",
            placeholder="예: https://datanugget.io/",
        )
        st.caption("디버깅 모드 시작 시 Playwright 테스트 브라우저가 열리고 collect 히트를 감시합니다.")
        st.caption(f"현재 Project: {get_active_project_slug()} (Session 단위 저장)")
        novnc_popup_url = get_novnc_popup_url()
        if st.session_state.get("qa_access_role", "guest") == "admin":
            st.caption(f"원격 디버그 팝업 URL: {novnc_popup_url}")
            st.markdown(f"[원격 디버그 화면 열기 (noVNC)]({novnc_popup_url})")
        else:
            st.caption("원격 디버그 팝업(noVNC)은 관리자 계정에서만 열 수 있습니다.")
        st.text_input(
            "테스터 이름",
            value=st.session_state.get("qa_tester_name", ""),
            key="qa_tester_name",
            placeholder="예: kim.qa",
        )
        st.text_input(
            "테스트 메모",
            value=st.session_state.get("qa_tester_note", ""),
            key="qa_tester_note",
            placeholder="예: 랜딩 배너 클릭 시나리오",
        )
        dc1, dc2 = st.columns(2)
        with dc1:
            realtime_debug_start_clicked = st.button("Start QA Session", type="primary")
        with dc2:
            realtime_debug_stop_clicked = st.button("QA Session 종료")

        active_debug_session_id = st.session_state.get("qa_debug_session_id", "").strip()
        debug_snapshot = get_debug_session_snapshot(active_debug_session_id) if active_debug_session_id else {}
        resolved_output_path = resolve_debug_output_file(
            session_id=active_debug_session_id,
            snapshot=debug_snapshot,
            state_output_file=st.session_state.get("qa_debug_output_file", ""),
        )
        recovered_from_file = bool((not debug_snapshot) and resolved_output_path and resolved_output_path.exists())
        if debug_snapshot or recovered_from_file:
            sid_for_view = (
                str(debug_snapshot.get("session_id", "")).strip() if debug_snapshot else active_debug_session_id
            )
            status_for_view = str(debug_snapshot.get("status", "")).strip() if debug_snapshot else "recovered(file)"
            captured_count_for_view = int(debug_snapshot.get("captured_events", 0)) if debug_snapshot else "-"
            st.caption(
                f"세션: {sid_for_view} | "
                f"상태: {status_for_view or '-'} | "
                f"캡처: {captured_count_for_view}건"
            )
            st.caption(f"표시 시간대: {DISPLAY_TZ_NAME}")
            tester_name_view = str(debug_snapshot.get("tester_name", "")).strip() if debug_snapshot else ""
            if tester_name_view:
                st.caption(f"테스터: {tester_name_view}")
            if debug_snapshot and debug_snapshot.get("last_error", "").strip():
                st.error(f"디버깅 런타임 오류: {debug_snapshot.get('last_error')}")
            st.caption("실시간 QA 리스트는 메인 영역의 `실시간 테스트 QA 리스트` 탭에서 확인하세요.")
        else:
            st.caption("디버깅 시작 후 타임라인이 표시됩니다.")

    with st.expander("3) QA 설정", expanded=True):
        today = date.today()
        start_date = st.date_input("시작일", value=today - timedelta(days=1))
        end_date = st.date_input("종료일", value=today)
        param_text = st.text_area("조회할 param (쉼표 구분)", value=default_params)
        st.caption("예: transaction_id,value,currency,qa_debug_session_id")
        required_event_text = st.text_area(
            "필수 이벤트",
            value=default_required_events,
            key="required_event_text_input",
        )
        st.caption("비워두면 필수 이벤트 체크를 생략합니다. 필요할 때만 입력하세요.")
        unknown_event_policy = st.selectbox(
            "실시간 미정의 이벤트 처리",
            options=["정보", "경고", "무시"],
            index=["정보", "경고", "무시"].index(st.session_state.get("unknown_event_policy", "정보")),
            help="필수 이벤트 목록에 없는 이벤트를 실시간 타임라인에서 어떻게 표시할지 선택합니다.",
            key="unknown_event_policy",
        )
        st.caption("사이트 상황에 맞는 이벤트명을 쉼표로 자유 입력하세요.")
        required_param_text = st.text_area(
            "필수 param 규칙 (event: p1,p2)",
            value=default_param_text,
            height=120,
        )
        st.caption("비워두면 필수 파라미터 체크를 생략합니다.")
        funnel_text = st.text_input("퍼널 시퀀스", value=default_funnel_steps)
        null_threshold = st.slider("null 경고 임계치", min_value=0.05, max_value=0.9, value=0.2, step=0.05)
        max_rows = st.number_input("최대 조회 행", min_value=1000, max_value=500000, value=50000, step=1000)

    with st.expander("4) 시나리오 1회 검증", expanded=True):
        scenario_enabled = st.checkbox("시나리오 검증 활성화", value=True)
        scenario_key_mode_label = st.selectbox(
            "식별 방식",
            options=list(scenario_key_modes.keys()),
            index=0,
        )
        scenario_key_field = scenario_key_modes[scenario_key_mode_label]
        if scenario_key_field == "none":
            st.caption("키 없이 전체 로그 기준 집계형 검증으로 동작합니다.")
            scenario_key_value = ""
        else:
            scenario_key_value = st.text_input(f"{scenario_key_field} 값", placeholder="예: dbg_20260302_081843_c90d")
        scenario_steps_text = st.text_input("기대 시나리오 step", value=default_scenario_steps)

st.caption("실시간 디버깅과 테스트 제어는 왼쪽 사이드바에서 실행합니다.")

resume_notice = str(st.session_state.get("qa_oauth_resume_notice", "")).strip()
if resume_notice:
    st.info(resume_notice)
    st.session_state["qa_oauth_resume_notice"] = ""

if realtime_debug_start_clicked:
    try:
        normalized_target_url = normalize_debug_target_url(st.session_state.get("qa_debug_target_url", ""))
        if not normalized_target_url:
            st.error("디버깅 대상 URL 형식이 올바르지 않습니다. 예: https://datanugget.io/")
            log_ui_action("debug_start_error", {"error": "invalid_target_url"})
            st.stop()
        log_ui_action(
            "debug_start_click",
            {"target_url": normalized_target_url},
        )
        previous_sid = st.session_state.get("qa_debug_session_id", "").strip()
        if previous_sid:
            stop_debug_session(previous_sid)
        debug_session_id = f"dbg_{pd.Timestamp.now().strftime('%Y%m%d_%H%M%S_%f')}_{uuid4().hex[:8]}"
        active_paths = get_active_project_paths()
        debug_file = active_paths["qa_sessions_dir"] / debug_session_id / "debug_stream.jsonl"
        debug_file.parent.mkdir(parents=True, exist_ok=True)
        snapshot = start_debug_session(
            session_id=debug_session_id,
            target_url=normalized_target_url,
            output_file=debug_file,
            tester_name=st.session_state.get("qa_tester_name", "").strip(),
            tester_note=st.session_state.get("qa_tester_note", "").strip(),
            db_path=get_active_test_log_db_path(),
            launch_browser=True,
        )
        st.session_state["qa_debug_session_id"] = debug_session_id
        st.session_state["qa_debug_output_file"] = str(debug_file)
        st.session_state["qa_debug_started_at"] = str(snapshot.get("started_at", "")).strip()
        log_ui_action("debug_start_success", {"session_id": debug_session_id})
        st.success(
            f"디버깅 세션 시작: qa_debug_session_id={debug_session_id} "
            f"(상태: {snapshot.get('status', '-')}, 모드: playwright intercept capture)"
        )
        if (
            st.session_state.get("qa_access_role", "guest") == "admin"
            and is_truthy(get_config_value("QA_OPEN_NOVNC_ON_START", "1"))
        ):
            auto_open_popup_window(get_novnc_popup_url(), popup_name=f"qa_debug_popup_{debug_session_id}")
            st.caption("원격 디버그 팝업(noVNC) 자동 열기를 시도했습니다. 차단되면 링크를 직접 열어주세요.")
    except Exception as exc:
        log_ui_action("debug_start_error", {"error": str(exc)})
        st.error(f"디버깅 모드 시작 실패: {to_user_error_message(exc)}")

if realtime_debug_stop_clicked:
    log_ui_action("debug_stop_click")
    sid = st.session_state.get("qa_debug_session_id", "").strip()
    if sid:
        stop_debug_session(sid)
        log_ui_action("debug_stop_success", {"session_id": sid})
        st.success("디버깅 모드 종료 요청을 보냈습니다.")
    else:
        st.info("종료할 디버깅 세션이 없습니다.")

realtime_tab, report_tab, schema_tab = st.tabs(
    ["1단: 실시간 테스트 화면", "2단: QA 리포트 화면", "3단: 이벤트 기준표 탭"]
)
with realtime_tab:
    st.markdown("### QA Workflow")
    wf1, wf2, wf3, wf4 = st.columns(4)
    wf1.info("1. Test Events\n사이트 클릭으로 이벤트 발생")
    wf2.info("2. Define Rules\n이벤트별 필수/선택 규칙 정의")
    wf3.info("3. Validate Events\npayload와 기준표 비교")
    wf4.info("4. Confirm Rules\n수정 후 다시 클릭해 재검증")

    active_debug_session_id = st.session_state.get("qa_debug_session_id", "").strip()
    debug_snapshot = get_debug_session_snapshot(active_debug_session_id) if active_debug_session_id else {}
    resolved_output_path = resolve_debug_output_file(
        session_id=active_debug_session_id,
        snapshot=debug_snapshot,
        state_output_file=st.session_state.get("qa_debug_output_file", ""),
    )
    recovered_from_file = bool((not debug_snapshot) and resolved_output_path and resolved_output_path.exists())
    st.session_state["realtime_last_refresh_at"] = pd.Timestamp.now(tz=LOCAL_TZ).strftime("%H:%M:%S")

    ctrl1, ctrl2, ctrl3, ctrl4 = st.columns([1.3, 1, 1.2, 2.2])
    with ctrl1:
        realtime_manual_refresh_clicked = st.button("실시간 데이터 새로고침", key="realtime_manual_refresh")
    with ctrl2:
        st.checkbox("자동 갱신", key="realtime_panel_auto_refresh")
    with ctrl3:
        st.select_slider(
            "간격(초)",
            options=[1, 2, 3, 5],
            key="realtime_panel_refresh_interval",
        )
    with ctrl4:
        st.caption(f"마지막 갱신: {st.session_state.get('realtime_last_refresh_at', '-')}")

    if realtime_manual_refresh_clicked:
        log_ui_action("realtime_manual_refresh")
        st.rerun()

    sid_for_view = "-"
    status_for_view = "-"
    captured_for_view = 0
    timeline_df = pd.DataFrame()
    rt_events = pd.DataFrame()
    st.session_state["schema_rt_events_cache"] = []

    if debug_snapshot or recovered_from_file:
        if recovered_from_file:
            st.info("세션 객체는 없지만 파일 기준으로 이어서 표시 중입니다. (상태: recovered(file))")
        debug_output_file = str(resolved_output_path) if resolved_output_path else ""
        timeline_df_raw = load_debug_events(Path(debug_output_file), limit=3000) if debug_output_file else pd.DataFrame()
        session_started_at = (
            str(debug_snapshot.get("started_at", "")).strip()
            if debug_snapshot
            else str(st.session_state.get("qa_debug_started_at", "")).strip()
        )
        timeline_df_active = filter_events_for_active_session(
            timeline_df_raw,
            session_id=active_debug_session_id,
            session_started_at=session_started_at,
        )
        timeline_df = filter_debug_events_by_view(timeline_df_active, "히트(collect)")
        sid_for_view = (
            str(debug_snapshot.get("session_id", "")).strip() if debug_snapshot else active_debug_session_id
        ) or "-"
        status_for_view = (
            str(debug_snapshot.get("status", "")).strip() if debug_snapshot else "recovered(file)"
        ) or "-"
        captured_from_snapshot = int(debug_snapshot.get("captured_events", 0)) if debug_snapshot else 0
        captured_from_file = int(len(timeline_df))
        captured_for_view = max(captured_from_snapshot, captured_from_file)
    else:
        st.info("활성 디버깅 세션이 없습니다. 사이드바에서 `디버깅 모드 시작`을 실행하세요.")

    allowed_events_rt = parse_csv_list(st.session_state.get("required_event_text_input", default_required_events))
    if not timeline_df.empty:
        rt_events = build_realtime_event_rows(
            timeline_df,
            allowed_events=allowed_events_rt,
            unknown_event_policy=st.session_state.get("unknown_event_policy", "정보"),
        )
    if not rt_events.empty:
        st.session_state["schema_rt_events_cache"] = rt_events.to_dict("records")

    schema_store = load_event_schemas()
    issue_df = build_issue_summary(rt_events, schema_store)
    tracking_plan_df = build_tracking_plan_df(rt_events, schema_store)
    schema_summary = summarize_schema_validation(rt_events, schema_store)
    rt_summary = summarize_realtime_quality(rt_events)
    parameter_validation_df_all = build_parameter_validation_export_df(rt_events, schema_store)
    order_validation_df_all = build_event_order_validation_df(rt_events, parse_csv_list(funnel_text))

    event_names_seen = (
        sorted(rt_events["이벤트"].astype(str).str.strip().unique().tolist()) if not rt_events.empty else []
    )
    missing_schema_count = 0
    for ev in event_names_seen:
        schema_obj = schema_store.get(ev, {})
        has_schema = isinstance(schema_obj, dict) and bool(schema_obj.get("required", []) or schema_obj.get("optional", []))
        if not has_schema:
            missing_schema_count += 1
    missing_parameter_count = 0
    if not parameter_validation_df_all.empty:
        pwork = parameter_validation_df_all.copy()
        pwork["issue_type"] = pwork["issue_type"].astype(str)
        pwork["severity"] = pwork["severity"].astype(str)
        missing_parameter_count = int(
            (
                (pwork["severity"] == "FAIL")
                & (pwork["issue_type"].isin(["required_missing", "type_validation_fail"]))
            ).sum()
        )
    flow_issue_count = 0
    if not order_validation_df_all.empty:
        owork = order_validation_df_all.copy()
        owork["status"] = owork["status"].astype(str)
        flow_issue_count = int((owork["status"].isin(["FAIL", "WARN"])).sum())

    section_overview, section_issues, section_detail, section_export = st.tabs(
        ["1. QA Overview", "2. Issues (문제 이벤트)", "3. Event Detail", "4. Export / Tracking Plan"]
    )

    with section_overview:
        st.caption(
            f"세션: {sid_for_view} | 상태: {status_for_view} | 캡처: {captured_for_view}건 | 시간대: {DISPLAY_TZ_NAME}"
        )
        if rt_events.empty:
            st.info("아직 캡처된 이벤트가 없습니다.")
        else:
            st.subheader("QA Summary")
            m0, m1, m2, m3, m4, m5 = st.columns(6)
            m0.metric("Events captured", int(captured_for_view))
            m1.metric("실시간 품질 점수", f"{rt_summary['score']} / 100")
            m2.metric("🔴 치명 오류", int(rt_summary["critical"]))
            m3.metric("🟡 주의 필요", int(rt_summary["caution"]))
            m4.metric("⚪ 참고 정보", int(rt_summary["info"]))
            m5.metric("🟢 정상", int(rt_summary["ok"]))
            st.caption(
                f"기준표 QA 상태 | PASS {schema_summary['PASS']} · WARN {schema_summary['WARN']} · FAIL {schema_summary['FAIL']}"
            )
            q1, q2, q3 = st.columns(3)
            q1.metric("Missing schema", int(missing_schema_count))
            q2.metric("Missing parameter", int(missing_parameter_count))
            q3.metric("Flow issues", int(flow_issue_count))
            st.caption(
                f"대상 이벤트 {rt_summary['event_count']}건 기준 | {rt_summary['message']}"
            )

            quick_cols = ["시간", "이벤트", "대표 파라미터", "대표 값", "상태"]
            st.dataframe(rt_events[quick_cols].head(30), use_container_width=True, height=320)

    with section_issues:
        st.caption("문제가 있는 이벤트를 확인하고 여기서 바로 이벤트 기준표를 만들 수 있습니다.")
        if issue_df.empty:
            st.success("현재 문제 이벤트가 없습니다.")
        else:
            issue_event_names = sorted(issue_df["event_name"].astype(str).unique().tolist())
            st.markdown("**이벤트 기준표 만들기 (문제 이벤트 기준)**")
            include_system_params_for_schema = st.checkbox(
                "기술 파라미터까지 포함해서 기준표 만들기",
                value=False,
                key="issue_include_system_params",
                help="기본은 measurement_id/client_id 같은 시스템 파라미터를 제외합니다.",
            )
            selected_issue_events = st.multiselect(
                "기준표 생성 대상 이벤트",
                options=issue_event_names,
                default=issue_event_names,
                key="issue_schema_targets",
            )
            ia1, ia2 = st.columns([1.2, 1.2])
            with ia1:
                if st.button("선택 이벤트 기준표 생성", key="issue_generate_selected"):
                    generated = 0
                    total_excluded = 0
                    for event_name in selected_issue_events:
                        inferred = infer_event_schema_from_rows(
                            rt_events,
                            event_name,
                            include_system_params=include_system_params_for_schema,
                        )
                        if int(inferred.get("sample_size", 0)) <= 0:
                            continue
                        schema_store[event_name] = {
                            "required": list(inferred.get("required", [])),
                            "optional": list(inferred.get("optional", [])),
                            "updated_at": datetime.now(tz=ZoneInfo("UTC")).isoformat(),
                        }
                        generated += 1
                        total_excluded += int(inferred.get("excluded_system_params", 0))
                    save_event_schemas(schema_store)
                    st.success(f"선택 이벤트 기준표 {generated}개 생성 완료")
                    if not include_system_params_for_schema:
                        st.info(f"시스템/기술 파라미터 제외: {total_excluded}건")
                    st.rerun()
            with ia2:
                if st.button("문제 이벤트 전체 기준표 생성", key="issue_generate_all"):
                    generated = 0
                    total_excluded = 0
                    for event_name in issue_event_names:
                        inferred = infer_event_schema_from_rows(
                            rt_events,
                            event_name,
                            include_system_params=include_system_params_for_schema,
                        )
                        if int(inferred.get("sample_size", 0)) <= 0:
                            continue
                        schema_store[event_name] = {
                            "required": list(inferred.get("required", [])),
                            "optional": list(inferred.get("optional", [])),
                            "updated_at": datetime.now(tz=ZoneInfo("UTC")).isoformat(),
                        }
                        generated += 1
                        total_excluded += int(inferred.get("excluded_system_params", 0))
                    save_event_schemas(schema_store)
                    st.success(f"문제 이벤트 전체 기준표 {generated}개 생성 완료")
                    if not include_system_params_for_schema:
                        st.info(f"시스템/기술 파라미터 제외: {total_excluded}건")
                    st.rerun()

            st.dataframe(issue_df, use_container_width=True, height=320)
            st.markdown("**기준표 비교 QA 결과 (이벤트별)**")
            issue_param_validation = build_parameter_validation_export_df(rt_events, schema_store)
            issue_order_validation = build_event_order_validation_df(rt_events, parse_csv_list(funnel_text))
            event_schema_qa = build_event_qa_report_export_df(
                rt_events=rt_events,
                issue_df=issue_df,
                schemas=schema_store,
                parameter_validation_df=issue_param_validation,
                order_validation_df=issue_order_validation,
            )
            if event_schema_qa.empty:
                st.info("비교할 데이터가 없습니다.")
            else:
                show_cols = [
                    "event_name",
                    "schema_status",
                    "parameter_issue",
                    "test_count",
                    "parameter_fail_count",
                    "parameter_warn_count",
                    "order_fail_count",
                    "order_warn_count",
                ]
                keep_cols = [c for c in show_cols if c in event_schema_qa.columns]
                st.dataframe(event_schema_qa[keep_cols], use_container_width=True, height=240)

    with section_detail:
        st.caption("이벤트를 선택해 payload 확인 → schema 검증 순서로 점검하세요.")
        if rt_events.empty:
            st.info("확인할 이벤트가 없습니다.")
        else:
            event_names = sorted(rt_events["이벤트"].astype(str).unique().tolist())
            selected_event_name = st.selectbox("이벤트 선택", options=event_names, key="detail_event_name")
            event_rows = rt_events[rt_events["이벤트"].astype(str) == selected_event_name].copy()
            event_rows["captured_at"] = pd.to_datetime(event_rows["captured_at"], errors="coerce")
            event_rows = event_rows.sort_values("captured_at", ascending=False).reset_index(drop=True)
            option_labels = [
                f"{to_circled_number(idx + 1)} {selected_event_name} | {row.get('시간', '-')} | {row.get('상태', '-')}"
                for idx, row in event_rows.iterrows()
            ]
            selected_option = st.selectbox("발생 건 선택", options=option_labels, key="detail_event_occurrence")
            selected_idx = option_labels.index(selected_option)
            row_ev = event_rows.iloc[selected_idx].to_dict()

            c1, c2, c3 = st.columns([2, 2, 2])
            c1.metric("이벤트", str(row_ev.get("이벤트", "-")))
            c2.metric("수집 시각", str(row_ev.get("시간", "-")))
            c3.metric("상태", str(row_ev.get("상태", "-")))

            all_params = row_ev.get("전체 파라미터", {})
            st.markdown("**Payload**")
            if isinstance(all_params, dict) and all_params:
                payload_rows = [{"param": str(k), "value": _to_text_value(v)} for k, v in all_params.items()]
                st.table(pd.DataFrame(payload_rows))
            else:
                st.caption("payload가 없습니다.")

            schema_check = validate_schema_for_event(
                event_name=str(row_ev.get("이벤트", "")),
                params=all_params if isinstance(all_params, dict) else {},
                schemas=schema_store,
            )
            schema_status = str(schema_check.get("status", "WARN")).upper()
            st.markdown(f"**기준표 검증 (Schema Validation)**: `{schema_status}` · {schema_check.get('message', '-')}")
            schema_checks = schema_check.get("checks", [])
            if isinstance(schema_checks, list) and schema_checks:
                check_rows = []
                for item in schema_checks:
                    if not isinstance(item, dict):
                        continue
                    check_rows.append(
                        {
                            "group": str(item.get("group", "-")),
                            "param": str(item.get("param", "-")),
                            "result": "PASS" if bool(item.get("ok", False)) else "FAIL",
                            "value": str(item.get("value", "-")),
                        }
                    )
                if check_rows:
                    st.table(pd.DataFrame(check_rows))

    with section_export:
        st.caption("Export는 QA 문서/이벤트 원본/트래킹 패키지 3개로 제공합니다.")
        if rt_events.empty:
            st.info("내보낼 실시간 데이터가 없습니다.")
        else:
            browser_timeline = build_realtime_timeline_view(
                timeline_df,
                allowed_events=allowed_events_rt,
                unknown_event_policy=st.session_state.get("unknown_event_policy", "정보"),
            )
            browser_errors_df = (
                browser_timeline[browser_timeline["상태"].isin(["WARN", "ERROR"])].copy()
                if not browser_timeline.empty
                else pd.DataFrame(columns=["시간", "수집원", "event_name", "주요 파라미터", "상태", "상세"])
            )
            event_logs_df = build_event_logs_export_df(rt_events)
            parameter_validation_df = build_parameter_validation_export_df(rt_events, schema_store)
            order_validation_df = build_event_order_validation_df(rt_events, parse_csv_list(funnel_text))
            event_qa_result_df = build_event_qa_report_export_df(
                rt_events=rt_events,
                issue_df=issue_df,
                schemas=schema_store,
                parameter_validation_df=parameter_validation_df,
                order_validation_df=order_validation_df,
            )
            qa_report_xlsx = dataframes_to_excel_bytes(
                {
                    "Event QA Result": event_qa_result_df,
                    "Parameter Validation": parameter_validation_df,
                    "Browser Errors": browser_errors_df,
                    "Event Order Validation": order_validation_df,
                }
            )
            event_logs_csv = dataframe_to_csv_bytes(event_logs_df)

            current_action_map_df = load_action_object_map()
            tracking_plan_export_df = build_tracking_plan_export_df(
                rt_events,
                schema_store,
                action_map_df=current_action_map_df,
            )
            screen_definition_df = build_screen_definition_export_df(tracking_plan_export_df)
            tracking_plan_xlsx = dataframes_to_excel_bytes({"tracking_plan": tracking_plan_export_df})
            screen_definition_xlsx = dataframes_to_excel_bytes({"screen_definition": screen_definition_df})
            screen_marked_png = build_screen_marked_png_bytes(screen_definition_df)
            tracking_package_zip = build_tracking_package_zip_bytes(
                tracking_plan_xlsx=tracking_plan_xlsx,
                screen_definition_xlsx=screen_definition_xlsx,
                screen_marked_png=screen_marked_png,
                qa_report_xlsx=qa_report_xlsx,
            )

            st.markdown("### Export")
            st.caption("QA 상태 기준: FAIL(필수/타입/순서 오류), WARN(기준표 없음/추가 파라미터/순서 경고), PASS(그 외)")
            b1, b2, b3 = st.columns(3)
            with b1:
                st.download_button(
                    "[1] QA Report",
                    data=qa_report_xlsx,
                    file_name="qa_report.xlsx",
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    disabled=not bool(qa_report_xlsx),
                    help="이벤트 QA 결과 문서",
                )
                st.caption("이벤트 QA 결과 문서")
            with b2:
                st.download_button(
                    "[2] Event Logs",
                    data=event_logs_csv,
                    file_name="event_logs.csv",
                    mime="text/csv",
                    disabled=not bool(event_logs_csv),
                    help="테스트 중 수집된 이벤트 로그",
                )
                st.caption("테스트 중 수집된 이벤트 로그")
            with b3:
                st.download_button(
                    "[3] Tracking Plan",
                    data=tracking_package_zip,
                    file_name="tracking_package.zip",
                    mime="application/zip",
                    disabled=not bool(tracking_package_zip),
                    help="tracking_plan + qa_report + screen_marked 패키지",
                )
                st.caption("tracking_plan.xlsx + qa_report.xlsx + screen_marked.png")

            st.markdown("**Tracking Plan 미리보기**")
            st.caption(f"Action/Object 매핑 적용: {int(len(current_action_map_df))}개 이벤트")
            if tracking_plan_export_df.empty:
                st.info("Tracking Plan을 만들 데이터가 없습니다.")
            else:
                st.dataframe(tracking_plan_export_df, use_container_width=True, height=320)

    if (
        st.session_state.get("realtime_panel_auto_refresh", False)
        and status_for_view in {"running", "stopping", "recovered(file)"}
    ):
        interval_sec = int(st.session_state.get("realtime_panel_refresh_interval", 2))
        components.html(
            f"<script>setTimeout(() => window.parent.location.reload(), {interval_sec * 1000});</script>",
            height=0,
            width=0,
        )
        st.caption(f"실시간 자동 갱신 동작 중 ({interval_sec}초 간격)")

with report_tab:
    if str(st.session_state.get("qa_ui_focus_after_oauth", "")).strip() == "report":
        st.success("로그인 완료. QA 리포트 화면에서 이어서 작업하세요.")
        st.session_state["qa_ui_focus_after_oauth"] = ""
    st.subheader("Google 로그인 및 속성 선택")
    c_auth1, c_auth2 = st.columns([2, 2])
    with c_auth1:
        st.text_input(
            "OAuth Client Secret 파일",
            value=st.session_state.get("qa_report_client_secret_file", get_config_value("GA4_CLIENT_SECRETS_FILE", "client_secret.json")),
            key="qa_report_client_secret_file",
            placeholder="예: client_secret.json",
        )
    with c_auth2:
        st.text_input(
            "OAuth 토큰 파일",
            value=st.session_state.get("qa_report_token_file", get_config_value("GA4_TOKEN_FILE", "token.json")),
            key="qa_report_token_file",
            placeholder="예: token.json",
        )
    redirect_preview = ""
    try:
        redirect_preview = get_google_oauth_redirect_uri(
            st.session_state.get("qa_report_client_secret_file", "").strip() or "client_secret.json"
        )
    except Exception:
        redirect_preview = ""
    if redirect_preview:
        st.caption(f"현재 OAuth Redirect URI: {redirect_preview}")
        st.caption("Google Cloud Console의 Authorized redirect URI에 위 주소가 정확히 등록되어 있어야 합니다.")

    b_auth1, b_auth2 = st.columns([1.3, 1.3])
    with b_auth1:
        oauth_start_clicked = st.button("Google 로그인 시작", key="qa_oauth_start")
    with b_auth2:
        property_refresh_clicked = st.button("속성 리스트 불러오기", key="qa_property_refresh")

    oauth_notice = str(st.session_state.get("qa_oauth_notice", "")).strip()
    oauth_error = str(st.session_state.get("qa_oauth_error", "")).strip()
    if oauth_notice:
        st.success(oauth_notice)
    if oauth_error:
        st.error(oauth_error)

    if oauth_start_clicked:
        log_ui_action("oauth_start_click")
        try:
            oauth_state = uuid4().hex
            auth_url = build_google_oauth_url(
                client_secrets_file=st.session_state.get("qa_report_client_secret_file", "").strip() or "client_secret.json",
                state=oauth_state,
            )
            save_oauth_context(
                oauth_state,
                {
                    "qa_project_slug": st.session_state.get("qa_project_slug", DEFAULT_PROJECT_SLUG),
                    "qa_project_domain": st.session_state.get("qa_project_domain", ""),
                    "qa_debug_session_id": st.session_state.get("qa_debug_session_id", ""),
                    "qa_debug_output_file": st.session_state.get("qa_debug_output_file", ""),
                    "qa_debug_started_at": st.session_state.get("qa_debug_started_at", ""),
                    "qa_debug_target_url": st.session_state.get("qa_debug_target_url", ""),
                    "qa_tester_name": st.session_state.get("qa_tester_name", ""),
                    "qa_tester_note": st.session_state.get("qa_tester_note", ""),
                    "required_event_text_input": st.session_state.get("required_event_text_input", ""),
                    "unknown_event_policy": st.session_state.get("unknown_event_policy", "정보"),
                    "qa_report_property_id": st.session_state.get("qa_report_property_id", ""),
                },
            )
            st.session_state["qa_oauth_state"] = oauth_state
            st.session_state["qa_oauth_auth_url"] = auth_url
            st.session_state["qa_oauth_go_now_url"] = auth_url
            st.session_state["qa_oauth_notice"] = ""
            st.session_state["qa_oauth_error"] = ""
            st.rerun()
        except Exception as exc:
            log_ui_action("oauth_start_error", {"error": str(exc)})
            st.session_state["qa_oauth_error"] = to_user_error_message(exc)
            st.session_state["qa_oauth_auth_url"] = ""
            st.session_state["qa_oauth_go_now_url"] = ""

    go_now_url = str(st.session_state.pop("qa_oauth_go_now_url", "")).strip()
    if go_now_url:
        safe_url = json.dumps(go_now_url)
        st.info("현재 창에서 Google 로그인 페이지로 이동합니다...")
        components.html(
            f"""
            <script>
              (() => {{
                const url = {safe_url};
                try {{
                  if (window.top && window.top.location) {{
                    window.top.location.replace(url);
                    return;
                  }}
                }} catch (e) {{}}
                try {{
                  if (window.parent && window.parent.location) {{
                    window.parent.location.replace(url);
                    return;
                  }}
                }} catch (e) {{}}
                window.location.replace(url);
              }})();
            </script>
            """,
            height=0,
            width=0,
        )
        st.markdown(
            f'<a href="{go_now_url}" target="_self">자동 이동이 안 되면 같은 창에서 Google 로그인 열기</a>',
            unsafe_allow_html=True,
        )
        st.stop()

    pending_auth_url = str(st.session_state.get("qa_oauth_auth_url", "")).strip()
    if pending_auth_url:
        st.markdown(
            f'<a href="{pending_auth_url}" target="_self">같은 창에서 Google 로그인 페이지 열기</a>',
            unsafe_allow_html=True,
        )
        st.caption("로그인 완료 후 앱으로 돌아오면 토큰이 저장되고 속성 리스트를 불러올 수 있습니다.")

    if property_refresh_clicked:
        log_ui_action("ga_property_refresh_click")
        try:
            with st.spinner("GA4 속성 리스트를 조회 중입니다..."):
                prop_df = fetch_ga4_property_list(
                    token_file=st.session_state.get("qa_report_token_file", "").strip() or "token.json"
                )
            st.session_state["qa_report_property_options"] = prop_df.to_dict("records")
            log_ui_action("ga_property_refresh_success", {"count": int(len(prop_df))})
            st.success(f"속성 {len(prop_df)}개를 불러왔습니다.")
        except Exception as exc:
            log_ui_action("ga_property_refresh_error", {"error": str(exc)})
            st.error(to_user_error_message(exc))

    property_options_df = pd.DataFrame(st.session_state.get("qa_report_property_options", []))
    if property_options_df.empty:
        st.text_input(
            "GA4 Property ID (수동 입력)",
            value=st.session_state.get("qa_report_property_id", get_config_value("GA4_PROPERTY_ID", "")),
            key="qa_report_property_id",
            placeholder="예: 123456789",
        )
    else:
        option_labels = property_options_df["label"].astype(str).tolist()
        option_ids = property_options_df["property_id"].astype(str).tolist()
        current_pid = str(st.session_state.get("qa_report_property_id", "")).strip()
        default_idx = option_ids.index(current_pid) if current_pid in option_ids else 0
        selected_label = st.selectbox("GA4 속성 선택", options=option_labels, index=default_idx)
        selected_row = property_options_df[property_options_df["label"] == selected_label].iloc[0]
        st.session_state["qa_report_property_id"] = str(selected_row["property_id"])
        st.caption(
            f"선택된 속성: {selected_row['property_name']} ({selected_row['property_id']}) / 계정: {selected_row['account_name']}"
        )

    st.divider()
    st.subheader("최근 30일 API 이벤트/매개변수")

    api_fetch_clicked = st.button("API 목록 조회(최근 30일)", key="qa_report_fetch_api")
    if api_fetch_clicked:
        log_ui_action("ga_api_ref_fetch_click")
        try:
            with st.spinner("GA4 API에서 이벤트/매개변수 목록을 조회 중입니다..."):
                api_events_df, api_params_df = fetch_ga4_reference_data(
                    property_id=st.session_state.get("qa_report_property_id", "").strip(),
                    token_file=st.session_state.get("qa_report_token_file", "").strip() or "token.json",
                    lookback_days=30,
                    top_events_limit=300,
                )
            st.session_state["qa_report_api_events"] = api_events_df.to_dict("records")
            st.session_state["qa_report_api_params"] = api_params_df.to_dict("records")
            st.session_state["qa_report_api_fetched_at"] = pd.Timestamp.now(tz=LOCAL_TZ).strftime("%Y-%m-%d %H:%M:%S")
            log_ui_action(
                "ga_api_ref_fetch_success",
                {"event_count": int(len(api_events_df)), "param_count": int(len(api_params_df))},
            )
            st.success(
                f"API 조회 완료: 이벤트 {len(api_events_df)}개, 매개변수 {len(api_params_df)}개"
            )
        except Exception as exc:
            log_ui_action("ga_api_ref_fetch_error", {"error": str(exc)})
            st.error(to_user_error_message(exc))

    api_events_view = pd.DataFrame(st.session_state.get("qa_report_api_events", []))
    api_params_view = pd.DataFrame(st.session_state.get("qa_report_api_params", []))
    fetched_at = st.session_state.get("qa_report_api_fetched_at", "").strip()
    if fetched_at:
        st.caption(f"마지막 API 조회 시각: {fetched_at}")

    events_col, params_col = st.columns(2)
    with events_col:
        st.markdown("**이벤트 이름(최근 30일 eventCount 기준)**")
        if api_events_view.empty:
            st.info("API 이벤트 목록이 없습니다. `API 목록 조회(최근 30일)`를 실행하세요.")
        else:
            st.dataframe(api_events_view, use_container_width=True, height=220)
    with params_col:
        st.markdown("**매개변수 이름(등록된 customEvent 메타데이터)**")
        if api_params_view.empty:
            st.info("API 매개변수 목록이 없습니다. `API 목록 조회(최근 30일)`를 실행하세요.")
        else:
            st.dataframe(api_params_view, use_container_width=True, height=220)
            st.caption("참고: 매개변수 목록은 Property 메타데이터 기준입니다.")

    selected_event_options = (
        api_events_view["event_name"].dropna().astype(str).str.strip().tolist()
        if not api_events_view.empty and "event_name" in api_events_view.columns
        else []
    )
    selected_param_options = (
        api_params_view["parameter_name"].dropna().astype(str).str.strip().tolist()
        if not api_params_view.empty and "parameter_name" in api_params_view.columns
        else []
    )
    st.multiselect(
        "핵심 이벤트 선택 (선택 시 이 이벤트 기준으로만 리포트 집계)",
        options=selected_event_options,
        key="qa_report_selected_events",
    )
    st.multiselect(
        "조회 파라미터 선택 (리포트 미리보기 컬럼에 포함)",
        options=selected_param_options,
        key="qa_report_selected_params",
    )
    report_source_mode = st.radio(
        "리포트 판정 데이터 소스",
        options=["GA4 API 집계(최근 30일)", "브라우저 히트(디버깅 세션)"],
        horizontal=True,
        key="qa_report_source_mode",
    )

    st.divider()
    run_clicked = st.button("QA 리포트 생성", type="primary")
    if run_clicked:
        log_ui_action(
            "qa_report_run_click",
            {"source_mode": st.session_state.get("qa_report_source_mode", "")},
        )
        requested_params = parse_csv_list(param_text)
        selected_events = [
            str(v).strip() for v in st.session_state.get("qa_report_selected_events", []) if str(v).strip()
        ]
        selected_params = [
            str(v).strip() for v in st.session_state.get("qa_report_selected_params", []) if str(v).strip()
        ]
        for param_name in selected_params:
            if param_name not in requested_params:
                requested_params.append(param_name)

        required_events = selected_events if selected_events else parse_csv_list(required_event_text)
        required_param_map = parse_required_params(required_param_text)
        funnel_steps = parse_csv_list(funnel_text)
        live_case_field = debug_case_param_name

        if live_case_field and live_case_field not in requested_params:
            requested_params.append(live_case_field)
        if scenario_enabled and scenario_key_field not in {"none", "user_pseudo_id"}:
            if scenario_key_field not in requested_params:
                requested_params.append(scenario_key_field)

        config_notes: List[str] = []
        if selected_events:
            config_notes.append(
                f"핵심 이벤트 {len(selected_events)}개 선택 기준으로 리포트를 집계합니다."
            )
        if not required_events:
            config_notes.append("필수 이벤트 미설정: 해당 체크를 생략합니다.")
        if not required_param_map:
            config_notes.append("필수 파라미터 규칙 미설정: 해당 체크를 생략합니다.")
        if not funnel_steps:
            config_notes.append("퍼널 시퀀스 미설정: 퍼널 기반 체크는 제한됩니다.")

        try:
            if report_source_mode == "브라우저 히트(디버깅 세션)":
                debug_file = Path(st.session_state.get("qa_debug_output_file", "").strip())
                if not debug_file.exists():
                    st.error("디버깅 스트림 데이터가 없습니다. 사이드바에서 '디버깅 모드 시작' 후 테스트를 진행하세요.")
                    st.stop()

                raw_df_all = load_debug_events(debug_file, limit=int(max_rows))
                raw_df_active = filter_events_for_active_session(
                    raw_df_all,
                    session_id=st.session_state.get("qa_debug_session_id", "").strip(),
                    session_started_at=st.session_state.get("qa_debug_started_at", "").strip(),
                )
                raw_df = filter_debug_events_by_view(raw_df_active, "히트(collect)")
                if raw_df.empty:
                    st.error(
                        "실제 수집 히트(collect)가 없습니다. 디버깅 모드 시작 후 이벤트를 발생시키고, "
                        "기존 세션이라면 새 세션으로 다시 시작하세요."
                    )
                    st.stop()

                qa_df = debug_events_to_qa_df(raw_df, requested_params)
                if "event_timestamp" in qa_df.columns and not qa_df.empty:
                    ts = pd.to_datetime(qa_df["event_timestamp"], errors="coerce", utc=True)
                    start_local = pd.Timestamp(start_date).tz_localize(LOCAL_TZ)
                    end_local = (pd.Timestamp(end_date) + pd.Timedelta(days=1)).tz_localize(LOCAL_TZ)
                    start_ts = start_local.tz_convert("UTC")
                    end_ts = end_local.tz_convert("UTC")
                    qa_df = qa_df[(ts >= start_ts) & (ts < end_ts)].copy()
                    if qa_df.empty:
                        st.error("선택한 기간에 해당하는 수집 히트가 없습니다. 기간을 조정하세요.")
                        st.stop()
                if selected_events:
                    qa_df = qa_df[qa_df["event_name"].astype(str).isin(selected_events)].copy()
                    if qa_df.empty:
                        st.error("선택한 핵심 이벤트 기준으로는 수집 히트가 없습니다. 이벤트 선택을 조정하세요.")
                        st.stop()

                results_df = run_qa_rules(
                    qa_df,
                    required_events=required_events,
                    required_params_by_event=required_param_map,
                    null_threshold=float(null_threshold),
                    funnel_steps=funnel_steps,
                )
            else:
                property_id = st.session_state.get("qa_report_property_id", "").strip()
                token_file = st.session_state.get("qa_report_token_file", "").strip() or "token.json"
                if not property_id:
                    st.error("GA4 속성을 먼저 선택하세요.")
                    st.stop()
                api_events_df, _ = fetch_ga4_reference_data(
                    property_id=property_id,
                    token_file=token_file,
                    lookback_days=30,
                    top_events_limit=300,
                )
                if selected_events:
                    api_events_df = api_events_df[api_events_df["event_name"].astype(str).isin(selected_events)].copy()
                qa_df = build_api_event_preview_df(api_events_df)
                if qa_df.empty:
                    st.error("선택한 핵심 이벤트 기준으로 30일 집계 데이터가 없습니다.")
                    st.stop()
                results_df = build_api_aggregate_results(
                    api_events_df,
                    required_events=required_events,
                    required_params_by_event=required_param_map,
                    selected_params=selected_params,
                )
                config_notes.append("현재 리포트는 GA4 API 30일 집계 데이터 기준으로 생성되었습니다.")

            results_view = build_qa_results_view(results_df)
            issue_property = st.session_state.get("qa_report_property_id", "").strip() or "browser_intercept_stream"
            new_issues = upsert_auto_issues(results_df, property_id=issue_property, issue_path=DEFAULT_ISSUE_PATH)

            if config_notes:
                for note in config_notes:
                    st.info(note)

            st.subheader("저장 후 오류")
            st.caption(f"실제 수집 히트(collect) 기준 판정 결과 | 신규 이슈 저장: {new_issues}건")
            st.dataframe(results_view, use_container_width=True, height=260)

            st.subheader("null 비율")
            null_view = results_view[results_view["구분"] == "Null 비율"] if not results_view.empty else results_view
            if null_view.empty:
                st.success("null 비율 검사 항목이 없습니다.")
            else:
                st.dataframe(null_view, use_container_width=True, height=220)

            st.subheader("중복 검사")
            dup_view = results_view[results_view["rule_id"].astype(str).str.startswith("duplicate_")] if not results_view.empty else results_view
            if dup_view.empty:
                st.success("중복 검사 항목이 없습니다.")
            else:
                st.dataframe(dup_view, use_container_width=True, height=180)

            st.subheader("정규화 이벤트 미리보기")
            st.dataframe(build_event_preview(qa_df, requested_params), use_container_width=True, height=320)
            log_ui_action(
                "qa_report_run_success",
                {
                    "source_mode": report_source_mode,
                    "row_count": int(len(qa_df)),
                    "rule_count": int(len(results_df)),
                },
            )

        except Exception as exc:
            log_ui_action("qa_report_run_error", {"error": str(exc)})
            st.error(to_user_error_message(exc))
            with st.expander("기술 상세"):
                st.code(str(exc))

    st.subheader("이슈 히스토리")
    issues_df = issues_to_dataframe(DEFAULT_ISSUE_PATH)
    st.dataframe(issues_df, use_container_width=True, height=260)

    open_issues = issues_df[issues_df["status"] == "open"] if not issues_df.empty else issues_df
    if not open_issues.empty:
        st.markdown("**이슈 해결 기록 추가**")
        issue_options = open_issues["issue_id"].tolist()
        issue_id = st.selectbox("해결할 issue_id", options=issue_options)
        resolution = st.text_input("해결 내용", placeholder="예: checkout dataLayer 누락 수정")
        if st.button("이슈 해결 처리"):
            if not resolution.strip():
                st.error("해결 내용을 입력하세요.")
            else:
                ok = resolve_issue(issue_id=issue_id, resolution=resolution, issue_path=DEFAULT_ISSUE_PATH)
                if ok:
                    log_ui_action("issue_resolved", {"issue_id": str(issue_id)})
                    st.success("이슈를 resolved 상태로 저장했습니다.")
                    st.rerun()
                else:
                    st.error("issue_id를 찾지 못했습니다.")
    else:
        st.caption("현재 open 상태 이슈가 없습니다.")

with schema_tab:
    st.subheader("Event Rule Manager (이벤트 기준표)")
    st.caption(
        f"Project: {get_active_project_slug()} | 이벤트별 필수/선택 파라미터 기준표를 관리합니다."
    )

    cached_rt_events = pd.DataFrame(st.session_state.get("schema_rt_events_cache", []))
    schema_store = load_event_schemas()

    captured_events = []
    if not cached_rt_events.empty and "이벤트" in cached_rt_events.columns:
        captured_events = sorted(
            list({str(v).strip() for v in cached_rt_events["이벤트"].dropna().tolist() if str(v).strip()})
        )
    saved_events = sorted(list(schema_store.keys()))
    selectable_events = sorted(list(dict.fromkeys(captured_events + saved_events)))

    if not selectable_events:
        st.info("기준표를 만들 이벤트가 없습니다. 먼저 실시간 테스트에서 이벤트를 수집하세요.")
    else:
        selected_event = st.selectbox("이벤트 선택", options=selectable_events, key="schema_manager_selected_event")
        include_system_params_schema_tab = st.checkbox(
            "기술 파라미터까지 포함",
            value=False,
            key="schema_tab_include_system_params",
            help="기본은 시스템/기술 파라미터를 제외합니다.",
        )
        inferred = infer_event_schema_from_rows(
            cached_rt_events,
            selected_event,
            include_system_params=include_system_params_schema_tab,
        )
        existing_schema = schema_store.get(selected_event, {})
        all_params = sorted(
            list(
                dict.fromkeys(
                    list(inferred.get("required", []))
                    + list(inferred.get("optional", []))
                    + [str(v).strip() for v in existing_schema.get("required", []) if str(v).strip()]
                    + [str(v).strip() for v in existing_schema.get("optional", []) if str(v).strip()]
                )
            )
        )

        c1, c2 = st.columns([1.2, 1.2])
        with c1:
            if st.button("선택 이벤트 기준표 자동 생성", key="schema_generate_for_selected"):
                required_auto = list(inferred.get("required", []))
                optional_auto = list(inferred.get("optional", []))
                schema_store[selected_event] = {
                    "required": required_auto,
                    "optional": optional_auto,
                    "updated_at": datetime.now(tz=ZoneInfo("UTC")).isoformat(),
                }
                save_event_schemas(schema_store)
                st.success(
                    f"{selected_event}: 필수 {len(required_auto)}개 / 선택 {len(optional_auto)}개 저장"
                )
                if not include_system_params_schema_tab:
                    st.info(f"시스템/기술 파라미터 제외: {int(inferred.get('excluded_system_params', 0))}건")
                st.rerun()
        with c2:
            st.caption(
                f"자동 생성 기준 샘플 수: {int(inferred.get('sample_size', 0))}건 | "
                f"제외된 기술 파라미터: {int(inferred.get('excluded_system_params', 0))}건"
            )

        current_required = [str(v).strip() for v in existing_schema.get("required", []) if str(v).strip()]
        current_optional = [str(v).strip() for v in existing_schema.get("optional", []) if str(v).strip()]

        required_keys = st.multiselect(
            "required",
            options=all_params,
            default=[v for v in current_required if v in all_params],
            key=f"schema_required_{selected_event}",
        )
        optional_keys = st.multiselect(
            "optional",
            options=[v for v in all_params if v not in required_keys],
            default=[v for v in current_optional if v in all_params and v not in required_keys],
            key=f"schema_optional_{selected_event}",
        )

        b1, b2 = st.columns([1.2, 1.2])
        with b1:
            if st.button("기준표 저장", key=f"schema_save_{selected_event}"):
                schema_store[selected_event] = {
                    "required": required_keys,
                    "optional": optional_keys,
                    "updated_at": datetime.now(tz=ZoneInfo("UTC")).isoformat(),
                }
                save_event_schemas(schema_store)
                st.success(f"{selected_event} 기준표를 저장했습니다.")
                st.rerun()
        with b2:
            if st.button("기준표 삭제", key=f"schema_delete_{selected_event}"):
                if selected_event in schema_store:
                    del schema_store[selected_event]
                    save_event_schemas(schema_store)
                    st.success(f"{selected_event} 기준표를 삭제했습니다.")
                    st.rerun()

        preview = validate_schema_for_event(
            event_name=selected_event,
            params={k: "sample" for k in required_keys},
            schemas={selected_event: {"required": required_keys, "optional": optional_keys}},
        )
        st.caption(f"기준표 미리보기: {preview.get('status', 'WARN')} · {preview.get('message', '-')}")

    st.divider()
    st.markdown("**저장된 기준표 목록**")
    schema_rows = []
    for event_name, schema in sorted(schema_store.items()):
        required = [str(v).strip() for v in schema.get("required", []) if str(v).strip()]
        optional = [str(v).strip() for v in schema.get("optional", []) if str(v).strip()]
        schema_rows.append(
            {
                "event_name": event_name,
                "required_count": len(required),
                "optional_count": len(optional),
                "required": ", ".join(required),
                "optional": ", ".join(optional),
                "updated_at": str(schema.get("updated_at", "")).strip(),
            }
        )
    if schema_rows:
        st.dataframe(pd.DataFrame(schema_rows), use_container_width=True, height=280)
    else:
        st.info("저장된 기준표가 없습니다.")

    st.divider()
    st.markdown("**Action/Object 매핑표**")
    st.caption("tracking_plan의 action/object/page/section을 이벤트별로 고정합니다.")
    map_col1, map_col2 = st.columns([1.2, 2.0])
    with map_col1:
        st.download_button(
            "매핑표 템플릿 다운로드",
            data=action_map_template_csv_bytes(),
            file_name="action_object_map_template.csv",
            mime="text/csv",
        )
    with map_col2:
        st.caption("필수 컬럼: event_name, action, object (나머지는 선택)")
    upload_file = st.file_uploader(
        "매핑표 업로드 (CSV)",
        type=["csv"],
        key="action_object_map_upload",
        accept_multiple_files=False,
    )
    if st.button("업로드 매핑표 저장", key="save_action_object_map_btn"):
        if upload_file is None:
            st.warning("업로드할 CSV 파일을 먼저 선택하세요.")
        else:
            try:
                uploaded_df = pd.read_csv(upload_file)
                normalized_df = _normalize_action_map_df(uploaded_df)
                if normalized_df.empty:
                    st.error("유효한 event_name 행이 없습니다. 템플릿 형식으로 다시 업로드하세요.")
                else:
                    save_action_object_map(normalized_df)
                    st.success(f"매핑표 저장 완료: {len(normalized_df)}개 이벤트")
                    st.rerun()
            except Exception as exc:
                st.error(f"매핑표 업로드 실패: {to_user_error_message(exc)}")

    current_map_df = load_action_object_map()
    if current_map_df.empty:
        st.info("저장된 매핑표가 없습니다. 기본 규칙(이벤트명 추정)으로 tracking_plan을 생성합니다.")
    else:
        st.dataframe(current_map_df, use_container_width=True, height=240)
