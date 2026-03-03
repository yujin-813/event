from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import List
from uuid import uuid4

import pandas as pd


DEFAULT_ISSUE_PATH = Path("issues/qa_issues.json")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ensure_file(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text("[]", encoding="utf-8")


def load_issues(path: Path = DEFAULT_ISSUE_PATH) -> List[dict]:
    _ensure_file(path)
    return json.loads(path.read_text(encoding="utf-8"))


def save_issues(issues: List[dict], path: Path = DEFAULT_ISSUE_PATH) -> None:
    _ensure_file(path)
    path.write_text(json.dumps(issues, ensure_ascii=False, indent=2), encoding="utf-8")


def upsert_auto_issues(
    results_df: pd.DataFrame,
    property_id: str,
    issue_path: Path = DEFAULT_ISSUE_PATH,
) -> int:
    if results_df.empty:
        return 0

    issues = load_issues(issue_path)
    created = 0

    for _, row in results_df.iterrows():
        status = str(row.get("status", ""))
        if status not in {"FAIL", "WARN"}:
            continue

        rule_id = str(row.get("rule_id", ""))
        rule_name = str(row.get("rule_name", ""))
        detail = str(row.get("detail", ""))

        existing = next(
            (
                issue
                for issue in issues
                if issue.get("property_id") == property_id
                and issue.get("rule_id") == rule_id
                and issue.get("detail") == detail
                and issue.get("status") == "open"
            ),
            None,
        )

        if existing:
            existing["last_seen_at"] = _now_iso()
            existing["occurrences"] = int(existing.get("occurrences", 1)) + 1
            continue

        issues.append(
            {
                "issue_id": str(uuid4()),
                "property_id": property_id,
                "rule_id": rule_id,
                "rule_name": rule_name,
                "detail": detail,
                "status": "open",
                "resolution": "",
                "detected_at": _now_iso(),
                "last_seen_at": _now_iso(),
                "occurrences": 1,
            }
        )
        created += 1

    save_issues(issues, issue_path)
    return created


def resolve_issue(
    issue_id: str,
    resolution: str,
    issue_path: Path = DEFAULT_ISSUE_PATH,
) -> bool:
    issues = load_issues(issue_path)
    resolved = False

    for issue in issues:
        if issue.get("issue_id") == issue_id:
            issue["status"] = "resolved"
            issue["resolution"] = resolution.strip()
            issue["resolved_at"] = _now_iso()
            resolved = True
            break

    if resolved:
        save_issues(issues, issue_path)
    return resolved


def issues_to_dataframe(issue_path: Path = DEFAULT_ISSUE_PATH) -> pd.DataFrame:
    issues = load_issues(issue_path)
    if not issues:
        return pd.DataFrame(
            columns=[
                "issue_id",
                "property_id",
                "rule_name",
                "detail",
                "status",
                "occurrences",
                "detected_at",
                "last_seen_at",
                "resolution",
            ]
        )

    df = pd.DataFrame(issues)
    order = [
        "issue_id",
        "property_id",
        "rule_name",
        "detail",
        "status",
        "occurrences",
        "detected_at",
        "last_seen_at",
        "resolution",
    ]
    for col in order:
        if col not in df.columns:
            df[col] = ""
    return df[order].sort_values(by=["status", "last_seen_at"], ascending=[True, False])
