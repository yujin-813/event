from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from typing import Dict

import pandas as pd


def _connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=30)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    return conn


def init_test_log_db(db_path: Path) -> None:
    with _connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS qa_sessions (
                session_id TEXT PRIMARY KEY,
                target_url TEXT NOT NULL,
                status TEXT NOT NULL,
                started_at TEXT NOT NULL,
                ended_at TEXT,
                captured_events INTEGER NOT NULL DEFAULT 0,
                last_error TEXT,
                tester_name TEXT,
                tester_note TEXT,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS qa_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                captured_at TEXT NOT NULL,
                source TEXT,
                event_name TEXT,
                params_json TEXT,
                page_url TEXT,
                measurement_id TEXT,
                client_id TEXT,
                request_method TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS qa_ui_actions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                action_at TEXT NOT NULL,
                session_id TEXT,
                action_type TEXT NOT NULL,
                actor_role TEXT,
                actor_name TEXT,
                detail_json TEXT,
                remote_addr_hash TEXT,
                user_agent TEXT
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_qa_events_session_time ON qa_events(session_id, captured_at)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_qa_ui_actions_time ON qa_ui_actions(action_at)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_qa_ui_actions_session_time ON qa_ui_actions(session_id, action_at)"
        )


def upsert_session(db_path: Path, session_payload: Dict[str, object]) -> None:
    now_iso = datetime.now(timezone.utc).isoformat()
    with _connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO qa_sessions (
                session_id, target_url, status, started_at, ended_at,
                captured_events, last_error, tester_name, tester_note, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(session_id) DO UPDATE SET
                target_url=excluded.target_url,
                status=excluded.status,
                started_at=excluded.started_at,
                ended_at=excluded.ended_at,
                captured_events=excluded.captured_events,
                last_error=excluded.last_error,
                tester_name=excluded.tester_name,
                tester_note=excluded.tester_note
            """,
            (
                str(session_payload.get("session_id", "")).strip(),
                str(session_payload.get("target_url", "")).strip(),
                str(session_payload.get("status", "")).strip(),
                str(session_payload.get("started_at", "")).strip(),
                str(session_payload.get("ended_at", "")).strip(),
                int(session_payload.get("captured_events", 0) or 0),
                str(session_payload.get("last_error", "")).strip(),
                str(session_payload.get("tester_name", "")).strip(),
                str(session_payload.get("tester_note", "")).strip(),
                now_iso,
            ),
        )


def append_event(db_path: Path, payload: Dict[str, object]) -> None:
    session_id = str(payload.get("session_id", "")).strip()
    if not session_id:
        return
    params = payload.get("params")
    params_json = json.dumps(params, ensure_ascii=False, sort_keys=True) if isinstance(params, dict) else "{}"
    with _connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO qa_events (
                session_id, captured_at, source, event_name, params_json,
                page_url, measurement_id, client_id, request_method
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session_id,
                str(payload.get("captured_at", "")).strip(),
                str(payload.get("source", "")).strip(),
                str(payload.get("event_name", "")).strip(),
                params_json,
                str(payload.get("page_url", "")).strip(),
                str(payload.get("measurement_id", "")).strip(),
                str(payload.get("client_id", "")).strip(),
                str(payload.get("request_method", "")).strip(),
            ),
        )


def list_recent_sessions(db_path: Path, limit: int = 50) -> pd.DataFrame:
    if not Path(db_path).exists():
        return pd.DataFrame(
            columns=[
                "session_id",
                "status",
                "tester_name",
                "target_url",
                "captured_events",
                "started_at",
                "ended_at",
            ]
        )
    with _connect(db_path) as conn:
        df = pd.read_sql_query(
            """
            SELECT
                session_id, status, tester_name, target_url,
                captured_events, started_at, ended_at
            FROM qa_sessions
            ORDER BY started_at DESC
            LIMIT ?
            """,
            conn,
            params=(int(limit),),
        )
    return df


def get_session(db_path: Path, session_id: str) -> Dict[str, object]:
    sid = str(session_id or "").strip()
    if not sid or not Path(db_path).exists():
        return {}
    with _connect(db_path) as conn:
        cur = conn.execute(
            """
            SELECT
                session_id, status, target_url, started_at, ended_at,
                captured_events, last_error, tester_name, tester_note
            FROM qa_sessions
            WHERE session_id = ?
            LIMIT 1
            """,
            (sid,),
        )
        row = cur.fetchone()
    if not row:
        return {}
    return {
        "session_id": row[0] or "",
        "status": row[1] or "",
        "target_url": row[2] or "",
        "started_at": row[3] or "",
        "ended_at": row[4] or "",
        "captured_events": int(row[5] or 0),
        "last_error": row[6] or "",
        "tester_name": row[7] or "",
        "tester_note": row[8] or "",
    }


def append_ui_action(db_path: Path, payload: Dict[str, object]) -> None:
    action_type = str(payload.get("action_type", "")).strip()
    if not action_type:
        return
    action_at = str(payload.get("action_at", "")).strip() or datetime.now(timezone.utc).isoformat()
    detail = payload.get("detail")
    detail_json = json.dumps(detail, ensure_ascii=False, sort_keys=True) if isinstance(detail, dict) else "{}"
    with _connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO qa_ui_actions (
                action_at, session_id, action_type, actor_role, actor_name,
                detail_json, remote_addr_hash, user_agent
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                action_at,
                str(payload.get("session_id", "")).strip(),
                action_type,
                str(payload.get("actor_role", "")).strip(),
                str(payload.get("actor_name", "")).strip(),
                detail_json,
                str(payload.get("remote_addr_hash", "")).strip(),
                str(payload.get("user_agent", "")).strip(),
            ),
        )
