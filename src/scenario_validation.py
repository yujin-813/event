from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Sequence

import pandas as pd


@dataclass
class ScenarioValidationResult:
    status: str
    detail: str
    mode: str
    key_field: str
    key_value: str
    target_user_id: str
    expected_steps: List[str]
    observed_steps: List[str]
    missing_steps: List[str]
    order_ok: bool
    matched_event_rows: int
    step_counts: Dict[str, int]


def _normalize_steps(steps: Sequence[str]) -> List[str]:
    return [s.strip() for s in steps if s.strip()]


def _is_ordered_subset(observed: Sequence[str], expected: Sequence[str]) -> bool:
    if not expected:
        return True
    idx = 0
    for name in observed:
        if name == expected[idx]:
            idx += 1
            if idx == len(expected):
                return True
    return False


def _missing_steps(observed: Sequence[str], expected: Sequence[str]) -> List[str]:
    observed_set = set(observed)
    return [step for step in expected if step not in observed_set]


def _build_step_counts(events: pd.Series, steps: Sequence[str]) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for step in steps:
        counts[step] = int((events == step).sum())
    return counts


def validate_single_scenario(
    df: pd.DataFrame,
    expected_steps: Sequence[str],
    key_field: str = "none",
    key_value: str = "",
    user_id: str = "",
) -> ScenarioValidationResult:
    steps = _normalize_steps(expected_steps)
    key_field_clean = key_field.strip() if key_field else "none"
    key_value_clean = key_value.strip()
    user_id_clean = user_id.strip()

    if key_field_clean != "none" and not key_value_clean:
        return ScenarioValidationResult(
            status="FAIL",
            detail=f"식별 방식이 '{key_field_clean}'인 경우 식별값을 입력해야 합니다.",
            mode="key",
            key_field=key_field_clean,
            key_value=key_value_clean,
            target_user_id=user_id_clean,
            expected_steps=steps,
            observed_steps=[],
            missing_steps=steps,
            order_ok=False,
            matched_event_rows=0,
            step_counts={step: 0 for step in steps},
        )

    if df.empty:
        return ScenarioValidationResult(
            status="FAIL",
            detail="검증할 데이터가 없습니다.",
            mode="empty",
            key_field=key_field_clean,
            key_value=key_value_clean,
            target_user_id=user_id_clean,
            expected_steps=steps,
            observed_steps=[],
            missing_steps=steps,
            order_ok=False,
            matched_event_rows=0,
            step_counts={step: 0 for step in steps},
        )

    work = df.copy()
    for col in [
        "event_name",
        "event_timestamp",
        "user_pseudo_id",
        "transaction_id",
        "qa_debug_session_id",
    ]:
        if col not in work.columns:
            work[col] = pd.NA

    work = work.sort_values(["event_timestamp"], na_position="last")

    # 1) Key-based mode (transaction_id / qa_debug_session_id / user_pseudo_id)
    if key_field_clean != "none" and key_value_clean:
        if key_field_clean not in work.columns:
            return ScenarioValidationResult(
                status="FAIL",
                detail=f"키 컬럼 '{key_field_clean}'이 데이터에 없습니다.",
                mode="key",
                key_field=key_field_clean,
                key_value=key_value_clean,
                target_user_id=user_id_clean,
                expected_steps=steps,
                observed_steps=[],
                missing_steps=steps,
                order_ok=False,
                matched_event_rows=0,
                step_counts={step: 0 for step in steps},
            )

        matched_by_key = work[
            work[key_field_clean].astype(str).str.strip() == key_value_clean
        ]
        if matched_by_key.empty:
            not_found_detail = f"{key_field_clean}='{key_value_clean}' 값을 찾지 못했습니다."
            if key_field_clean == "qa_debug_session_id":
                not_found_detail += (
                    " URL 쿼리값이 이벤트 전송 시점까지 유지되는지, "
                    f"또는 이벤트 파라미터에 {key_field_clean}가 포함되는지 확인하세요. "
                    "키가 없으면 '키 없이 집계형' 검증을 사용하세요."
                )
            return ScenarioValidationResult(
                status="FAIL",
                detail=not_found_detail,
                mode="key",
                key_field=key_field_clean,
                key_value=key_value_clean,
                target_user_id=user_id_clean,
                expected_steps=steps,
                observed_steps=[],
                missing_steps=steps,
                order_ok=False,
                matched_event_rows=0,
                step_counts={step: 0 for step in steps},
            )

        inferred_user = user_id_clean
        if not inferred_user and key_field_clean != "user_pseudo_id":
            user_values = (
                matched_by_key["user_pseudo_id"].dropna().astype(str).str.strip()
            )
            user_values = user_values[user_values != ""]
            if not user_values.empty:
                inferred_user = user_values.iloc[0]

        if inferred_user:
            filtered = work[
                work["user_pseudo_id"].astype(str).str.strip() == inferred_user
            ]
        else:
            filtered = matched_by_key

        observed = filtered["event_name"].astype(str).tolist()
        observed_unique_ordered = list(dict.fromkeys(observed))
        missing_steps = _missing_steps(observed, steps)
        order_ok = _is_ordered_subset(observed, steps)
        step_counts = _build_step_counts(filtered["event_name"].astype(str), steps)

        status = "PASS" if (not missing_steps and order_ok) else "FAIL"
        if status == "PASS":
            detail = "시나리오 검증 통과 (키 기반)"
        else:
            parts: List[str] = []
            if missing_steps:
                parts.append(f"누락 step: {', '.join(missing_steps)}")
            if not order_ok:
                parts.append("step 순서 불일치")
            detail = " | ".join(parts)

        return ScenarioValidationResult(
            status=status,
            detail=detail,
            mode="key",
            key_field=key_field_clean,
            key_value=key_value_clean,
            target_user_id=inferred_user,
            expected_steps=steps,
            observed_steps=observed_unique_ordered,
            missing_steps=missing_steps,
            order_ok=order_ok,
            matched_event_rows=len(filtered),
            step_counts=step_counts,
        )

    # 2) User-based mode
    if user_id_clean:
        filtered = work[
            work["user_pseudo_id"].astype(str).str.strip() == user_id_clean
        ]
        if filtered.empty:
            return ScenarioValidationResult(
                status="FAIL",
                detail=f"user_pseudo_id='{user_id_clean}' 값을 찾지 못했습니다.",
                mode="user",
                key_field="user_pseudo_id",
                key_value=user_id_clean,
                target_user_id=user_id_clean,
                expected_steps=steps,
                observed_steps=[],
                missing_steps=steps,
                order_ok=False,
                matched_event_rows=0,
                step_counts={step: 0 for step in steps},
            )

        observed = filtered["event_name"].astype(str).tolist()
        observed_unique_ordered = list(dict.fromkeys(observed))
        missing_steps = _missing_steps(observed, steps)
        order_ok = _is_ordered_subset(observed, steps)
        step_counts = _build_step_counts(filtered["event_name"].astype(str), steps)

        status = "PASS" if (not missing_steps and order_ok) else "FAIL"
        detail = "시나리오 검증 통과 (사용자 기반)" if status == "PASS" else (
            (f"누락 step: {', '.join(missing_steps)}" if missing_steps else "")
            + (" | step 순서 불일치" if not order_ok else "")
        ).strip(" |")

        return ScenarioValidationResult(
            status=status,
            detail=detail,
            mode="user",
            key_field="user_pseudo_id",
            key_value=user_id_clean,
            target_user_id=user_id_clean,
            expected_steps=steps,
            observed_steps=observed_unique_ordered,
            missing_steps=missing_steps,
            order_ok=order_ok,
            matched_event_rows=len(filtered),
            step_counts=step_counts,
        )

    # 3) Aggregate mode (no key, no user)
    observed_all = work["event_name"].astype(str).tolist()
    observed_unique_ordered = list(dict.fromkeys(observed_all))
    missing_steps = _missing_steps(observed_all, steps)
    order_ok = _is_ordered_subset(observed_all, steps)
    step_counts = _build_step_counts(work["event_name"].astype(str), steps)

    status = "PASS" if (not missing_steps and order_ok) else "FAIL"
    if status == "PASS":
        detail = "집계형 시나리오 검증 통과"
    else:
        parts = []
        if missing_steps:
            parts.append(f"누락 step: {', '.join(missing_steps)}")
        if not order_ok:
            parts.append("전체 로그 기준 step 순서 불일치")
        detail = " | ".join(parts)

    return ScenarioValidationResult(
        status=status,
        detail=detail,
        mode="aggregate",
        key_field="none",
        key_value="",
        target_user_id="",
        expected_steps=steps,
        observed_steps=observed_unique_ordered,
        missing_steps=missing_steps,
        order_ok=order_ok,
        matched_event_rows=len(work),
        step_counts=step_counts,
    )
