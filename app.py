from __future__ import annotations

from datetime import date, datetime, timedelta
import json
import os
import io
import re
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


st.set_page_config(page_title="GA4 QA Reporter", layout="wide")
st.title("GA4 QA 리포터 (룰 기반)")
st.caption("실시간 DebugView 대체가 아닌, 로그 수집 + 자동 정리 + 룰 기반 판정을 위한 내부 QA 도구")
BASE_DIR = Path(__file__).resolve().parent


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
    cfg_path = _resolve_project_path(client_secrets_file)
    if not cfg_path.exists():
        raise RuntimeError(f"OAuth client secret 파일이 없습니다: {cfg_path}")
    raw = json.loads(cfg_path.read_text(encoding="utf-8"))
    base = raw.get("web") or raw.get("installed")
    if not isinstance(base, dict):
        raise RuntimeError("client_secret.json 형식이 올바르지 않습니다. web/installed 설정이 필요합니다.")

    redirect_uris = base.get("redirect_uris") or []
    redirect_uri = str(redirect_uris[0]).strip() if redirect_uris else ""
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
        st.session_state["qa_oauth_notice"] = f"Google 로그인 완료. 토큰 저장: {token_file}"
        st.session_state["qa_oauth_error"] = ""
        st.session_state["qa_oauth_auth_url"] = ""
        st.session_state["qa_oauth_state"] = ""
    except Exception as exc:
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

ensure_err = ""
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
        novnc_popup_url = get_novnc_popup_url()
        st.caption(f"원격 디버그 팝업 URL: {novnc_popup_url}")
        st.markdown(f"[원격 디버그 화면 열기 (noVNC)]({novnc_popup_url})")
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
            realtime_debug_start_clicked = st.button("디버깅 모드 시작", type="primary")
        with dc2:
            realtime_debug_stop_clicked = st.button("디버깅 모드 종료")

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

if realtime_debug_start_clicked:
    try:
        previous_sid = st.session_state.get("qa_debug_session_id", "").strip()
        if previous_sid:
            stop_debug_session(previous_sid)
        debug_session_id = f"dbg_{pd.Timestamp.now().strftime('%Y%m%d_%H%M%S_%f')}_{uuid4().hex[:8]}"
        debug_file = Path(f"data/debug_stream/{debug_session_id}.jsonl")
        snapshot = start_debug_session(
            session_id=debug_session_id,
            target_url=st.session_state.get("qa_debug_target_url", "").strip(),
            output_file=debug_file,
            tester_name=st.session_state.get("qa_tester_name", "").strip(),
            tester_note=st.session_state.get("qa_tester_note", "").strip(),
            db_path=Path("data/test_logs/qa_runs.db"),
            launch_browser=True,
        )
        st.session_state["qa_debug_session_id"] = debug_session_id
        st.session_state["qa_debug_output_file"] = str(debug_file)
        st.session_state["qa_debug_started_at"] = str(snapshot.get("started_at", "")).strip()
        st.success(
            f"디버깅 세션 시작: qa_debug_session_id={debug_session_id} "
            f"(상태: {snapshot.get('status', '-')}, 모드: playwright intercept capture)"
        )
        if is_truthy(get_config_value("QA_OPEN_NOVNC_ON_START", "1")):
            auto_open_popup_window(get_novnc_popup_url(), popup_name=f"qa_debug_popup_{debug_session_id}")
            st.caption("원격 디버그 팝업(noVNC) 자동 열기를 시도했습니다. 차단되면 링크를 직접 열어주세요.")
    except Exception as exc:
        st.error(f"디버깅 모드 시작 실패: {to_user_error_message(exc)}")

if realtime_debug_stop_clicked:
    sid = st.session_state.get("qa_debug_session_id", "").strip()
    if sid:
        stop_debug_session(sid)
        st.success("디버깅 모드 종료 요청을 보냈습니다.")
    else:
        st.info("종료할 디버깅 세션이 없습니다.")

realtime_tab, report_tab = st.tabs(["1단: 실시간 테스트 화면", "2단: QA 리포트 화면"])
with realtime_tab:
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
        st.rerun()

    if not debug_snapshot and not recovered_from_file:
        st.info("활성 디버깅 세션이 없습니다. 사이드바에서 `디버깅 모드 시작`을 실행하세요.")
    else:
        if recovered_from_file:
            st.warning("세션 객체가 없어 파일 기준으로 복구 표시합니다. (상태: recovered(file))")
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
        st.caption(
            f"세션: {sid_for_view} | "
            f"상태: {status_for_view} | "
            f"캡처: {captured_for_view}건"
        )
        allowed_events_rt = parse_csv_list(st.session_state.get("required_event_text_input", default_required_events))

        st.subheader("실시간 이벤트 리스트")
        rt_events = build_realtime_event_rows(
            timeline_df,
            allowed_events=allowed_events_rt,
            unknown_event_policy=st.session_state.get("unknown_event_policy", "정보"),
        )
        if rt_events.empty:
            st.info("아직 캡처된 이벤트가 없습니다.")
        else:
            st.subheader("실시간 결과 요약")
            rt_summary = summarize_realtime_quality(rt_events)
            m1, m2, m3, m4, m5 = st.columns(5)
            m1.metric("실시간 품질 점수", f"{rt_summary['score']} / 100")
            m2.metric("🔴 치명 오류", int(rt_summary["critical"]))
            m3.metric("🟡 주의 필요", int(rt_summary["caution"]))
            m4.metric("⚪ 참고 정보", int(rt_summary["info"]))
            m5.metric("🟢 정상", int(rt_summary["ok"]))
            st.caption(
                f"대상 이벤트 {rt_summary['event_count']}건 기준 | {rt_summary['message']}"
            )

            suspicious_profile = build_suspicious_param_profile(timeline_df)
            if suspicious_profile:
                with st.expander(f"의심 값 그룹 요약 ({len(suspicious_profile)}개 파라미터)", expanded=False):
                    suspicious_rows = [
                        {"파라미터": k, "의심 사유": v}
                        for k, v in suspicious_profile.items()
                    ]
                    st.dataframe(pd.DataFrame(suspicious_rows), use_container_width=True, height=180)

            st.caption(
                "이벤트당 대표 파라미터 1개만 기본 표시됩니다. "
                "나머지는 4개 그룹(정상/값없음/의심/시스템-고급)으로 확인하세요."
            )
            session_groups = rt_events.groupby("group_session_id", sort=False)
            for session_key, session_df in session_groups:
                session_df = session_df.copy()
                session_df["captured_at"] = pd.to_datetime(session_df["captured_at"], errors="coerce")
                session_df = session_df.sort_values("captured_at", ascending=False)
                session_start = format_local_time(session_df["captured_at"].min())
                session_end = format_local_time(session_df["captured_at"].max())
                with st.expander(
                    f"세션 {session_key} · 이벤트 {len(session_df)}건 · {session_start} ~ {session_end}",
                    expanded=True,
                ):
                    for idx, row_ev in enumerate(session_df.to_dict("records")):
                        conclusion = summarize_event_conclusion(row_ev)
                        st.markdown(
                            "\n".join(
                                [
                                    f"**✅ {row_ev.get('이벤트', '-') or '-'} 이벤트 결과**",
                                    "",
                                    f"🔴 치명 오류: {conclusion['critical']}개",
                                    f"🟡 주의 필요: {conclusion['caution']}개",
                                    f"⚪ 참고 정보: {conclusion['info']}개",
                                    f"🟢 정상: {conclusion['ok']}개",
                                    "",
                                    f"👉 {conclusion['message']}",
                                ]
                            )
                        )
                        status_text = str(row_ev.get("상태", "OK"))
                        status_view = {"OK": "정상", "WARN": "주의", "ERROR": "오류", "INFO": "참고"}.get(status_text, status_text)
                        c1, c2, c3, c4 = st.columns([1.2, 1.6, 4.0, 1.0])
                        c1.markdown(f"`{row_ev.get('시간', '-')}`")
                        c2.markdown(f"**{row_ev.get('이벤트', '-') or '-'}**")
                        primary_key = str(row_ev.get("대표 파라미터", "-"))
                        primary_val = str(row_ev.get("대표 값", "-"))
                        c3.markdown(f"`{primary_key}` = `{primary_val}`")
                        c4.markdown(f"`{status_view}`")

                        all_params = row_ev.get("전체 파라미터", {})
                        if isinstance(all_params, dict) and all_params:
                            with st.expander(f"전체 파라미터 ({len(all_params)}개)", expanded=False):
                                all_rows = [{"파라미터": str(k), "값": _to_text_value(v)} for k, v in all_params.items()]
                                st.table(pd.DataFrame(all_rows))

                        normal_group = row_ev.get("정상 그룹", {})
                        if isinstance(normal_group, dict) and normal_group:
                            with st.expander(
                                f"✅ 정상 작동 중인 파라미터 ({len(normal_group)}개)",
                                expanded=False,
                            ):
                                normal_rows = [
                                    {"파라미터": k, "값": v.get("값", "-"), "검증": v.get("사유", "-")}
                                    for k, v in normal_group.items()
                                ]
                                st.table(pd.DataFrame(normal_rows))

                        missing_group = row_ev.get("값없음 그룹", {})
                        if isinstance(missing_group, dict) and missing_group:
                            with st.expander(
                                f"⚠ 값이 비어있음 (확인 필요) ({len(missing_group)}개)",
                                expanded=False,
                            ):
                                missing_rows = [
                                    {"파라미터": k, "값": v.get("값", "-"), "사유": v.get("사유", "-")}
                                    for k, v in missing_group.items()
                                ]
                                st.table(pd.DataFrame(missing_rows))

                        suspicious_group = row_ev.get("의심 그룹", {})
                        if isinstance(suspicious_group, dict) and suspicious_group:
                            with st.expander(
                                f"🤔 항상 동일한 값 (불필요 가능성) ({len(suspicious_group)}개)",
                                expanded=False,
                            ):
                                suspicious_rows = [
                                    {"파라미터": k, "값": v.get("값", "-"), "의심 사유": v.get("사유", "-")}
                                    for k, v in suspicious_group.items()
                                ]
                                st.table(pd.DataFrame(suspicious_rows))

                        system_group = row_ev.get("시스템 그룹", {})
                        if isinstance(system_group, dict) and system_group:
                            with st.expander(
                                f"⚙ 분석과 무관 (숨김 권장, 고급 보기) ({len(system_group)}개)",
                                expanded=False,
                            ):
                                system_rows = [
                                    {"파라미터": k, "값": v.get("값", "-"), "분류": v.get("사유", "-")}
                                    for k, v in system_group.items()
                                ]
                                st.table(pd.DataFrame(system_rows))
                        if idx < len(session_df) - 1:
                            st.divider()

            export_format = st.radio(
                "테스트 데이터 다운로드 형식",
                options=["CSV", "Excel"],
                horizontal=True,
                key="realtime_export_format",
            )
            rt_csv_bytes = realtime_events_to_csv_bytes(rt_events)
            rt_excel_bytes = realtime_events_to_excel_bytes(rt_events) if export_format == "Excel" else b""
            export_data = rt_excel_bytes if export_format == "Excel" else rt_csv_bytes
            export_file = (
                build_report_filename("realtime_event_list_full", "xlsx")
                if export_format == "Excel"
                else build_report_filename("realtime_event_list_full", "csv")
            )
            export_mime = (
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                if export_format == "Excel"
                else "text/csv"
            )
            if export_format == "Excel" and not export_data:
                st.caption("Excel 내보내기 엔진이 없으면 CSV를 사용하세요.")
            st.download_button(
                "테스트 데이터 전체 다운로드",
                data=export_data,
                file_name=export_file,
                mime=export_mime,
                disabled=not bool(export_data),
            )

        st.subheader("퍼널 진행 상태")
        realtime_steps = parse_csv_list(funnel_text)
        if not realtime_steps:
            st.info("퍼널 시퀀스가 비어 있습니다. 사이드바 QA 설정에서 퍼널 step을 입력하세요.")
        else:
            rt_funnel = build_realtime_funnel_progress(timeline_df, realtime_steps)
            if rt_funnel.empty:
                st.info("퍼널 진행 상태를 계산할 수 있는 이벤트가 아직 없습니다.")
            else:
                st.dataframe(rt_funnel, use_container_width=True, height=220)

        st.subheader("브라우저 전송 직전 오류")
        rt_timeline = build_realtime_timeline_view(
            timeline_df,
            allowed_events=allowed_events_rt,
            unknown_event_policy=st.session_state.get("unknown_event_policy", "정보"),
        )
        rt_issues = rt_timeline[rt_timeline["상태"].isin(["WARN", "ERROR"])] if not rt_timeline.empty else rt_timeline
        if rt_issues.empty:
            st.success("전송 직전 오류(WARN/ERROR)가 없습니다.")
        else:
            st.dataframe(style_realtime_timeline(rt_issues.head(200)), use_container_width=True, height=260)

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
        try:
            oauth_state = uuid4().hex
            auth_url = build_google_oauth_url(
                client_secrets_file=st.session_state.get("qa_report_client_secret_file", "").strip() or "client_secret.json",
                state=oauth_state,
            )
            st.session_state["qa_oauth_state"] = oauth_state
            st.session_state["qa_oauth_auth_url"] = auth_url
            st.session_state["qa_oauth_notice"] = ""
            st.session_state["qa_oauth_error"] = ""
        except Exception as exc:
            st.session_state["qa_oauth_error"] = to_user_error_message(exc)
            st.session_state["qa_oauth_auth_url"] = ""

    pending_auth_url = str(st.session_state.get("qa_oauth_auth_url", "")).strip()
    if pending_auth_url:
        st.link_button("Google 로그인 페이지 열기", pending_auth_url, type="primary")
        st.caption("로그인 완료 후 앱으로 돌아오면 토큰이 저장되고 속성 리스트를 불러올 수 있습니다.")

    if property_refresh_clicked:
        try:
            with st.spinner("GA4 속성 리스트를 조회 중입니다..."):
                prop_df = fetch_ga4_property_list(
                    token_file=st.session_state.get("qa_report_token_file", "").strip() or "token.json"
                )
            st.session_state["qa_report_property_options"] = prop_df.to_dict("records")
            st.success(f"속성 {len(prop_df)}개를 불러왔습니다.")
        except Exception as exc:
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

    if st.button("API 목록 조회(최근 30일)", key="qa_report_fetch_api"):
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
            st.success(
                f"API 조회 완료: 이벤트 {len(api_events_df)}개, 매개변수 {len(api_params_df)}개"
            )
        except Exception as exc:
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

        except Exception as exc:
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
                    st.success("이슈를 resolved 상태로 저장했습니다.")
                    st.rerun()
                else:
                    st.error("issue_id를 찾지 못했습니다.")
    else:
        st.caption("현재 open 상태 이슈가 없습니다.")
