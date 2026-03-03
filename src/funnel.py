from __future__ import annotations

from dataclasses import dataclass
from typing import List, Sequence

import pandas as pd


@dataclass
class FunnelSummary:
    steps: List[str]
    total_users: int
    passed_users: int
    failed_users: int
    fail_rate: float
    failed_user_ids: List[str]


def _has_ordered_steps(events: Sequence[str], steps: Sequence[str]) -> bool:
    step_idx = 0
    for event in events:
        if event == steps[step_idx]:
            step_idx += 1
            if step_idx == len(steps):
                return True
    return False


def analyze_funnel_sequence(df: pd.DataFrame, steps: Sequence[str]) -> FunnelSummary:
    if df.empty or "user_pseudo_id" not in df.columns or "event_name" not in df.columns:
        return FunnelSummary(
            steps=list(steps),
            total_users=0,
            passed_users=0,
            failed_users=0,
            fail_rate=0.0,
            failed_user_ids=[],
        )

    if not steps:
        return FunnelSummary(
            steps=[],
            total_users=0,
            passed_users=0,
            failed_users=0,
            fail_rate=0.0,
            failed_user_ids=[],
        )

    work = df.copy()
    if "event_timestamp" in work.columns:
        work = work.sort_values(["user_pseudo_id", "event_timestamp"], na_position="last")
    else:
        work = work.sort_values(["user_pseudo_id"])

    failed_users: List[str] = []
    total_users = 0
    passed_users = 0

    for user_id, group in work.groupby("user_pseudo_id", dropna=True):
        total_users += 1
        events = group["event_name"].astype(str).tolist()
        if _has_ordered_steps(events, steps):
            passed_users += 1
        else:
            failed_users.append(str(user_id))

    failed_count = len(failed_users)
    fail_rate = (failed_count / total_users) if total_users else 0.0

    return FunnelSummary(
        steps=list(steps),
        total_users=total_users,
        passed_users=passed_users,
        failed_users=failed_count,
        fail_rate=fail_rate,
        failed_user_ids=failed_users[:20],
    )
