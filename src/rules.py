from __future__ import annotations

from typing import Dict, List, Sequence

import pandas as pd

from src.funnel import analyze_funnel_sequence


def _is_missing(series: pd.Series) -> pd.Series:
    if series.dtype == "object":
        return series.isna() | (series.astype(str).str.strip() == "")
    return series.isna()


def _add_result(
    out: List[dict],
    rule_id: str,
    rule_name: str,
    status: str,
    detail: str,
    fail_count: int,
    total_count: int,
) -> None:
    out.append(
        {
            "rule_id": rule_id,
            "rule_name": rule_name,
            "status": status,
            "detail": detail,
            "fail_count": int(fail_count),
            "total_count": int(total_count),
        }
    )


def run_qa_rules(
    df: pd.DataFrame,
    required_events: Sequence[str],
    required_params_by_event: Dict[str, Sequence[str]],
    null_threshold: float = 0.2,
    funnel_steps: Sequence[str] | None = None,
) -> pd.DataFrame:
    results: List[dict] = []
    work = df.copy()

    for col in ["event_name", "user_pseudo_id", "transaction_id", "value", "currency", "event_timestamp"]:
        if col not in work.columns:
            work[col] = pd.NA

    required_events_clean = [e.strip() for e in required_events if e.strip()]

    # 1) Required event existence.
    for event_name in required_events_clean:
        count = int((work["event_name"] == event_name).sum())
        status = "PASS" if count > 0 else "FAIL"
        _add_result(
            results,
            f"required_event:{event_name}",
            f"필수 이벤트 존재: {event_name}",
            status,
            f"{count}건",
            0 if status == "PASS" else 1,
            1,
        )

    # 2) Required params by event.
    for event_name, params in required_params_by_event.items():
        event_rows = work[work["event_name"] == event_name]
        total = len(event_rows)
        for param in [p.strip() for p in params if p.strip()]:
            if param not in work.columns:
                fail_count = total if total > 0 else 1
                status = "FAIL"
                detail = f"컬럼 없음 ({event_name} 기준)"
            else:
                missing = _is_missing(event_rows[param]).sum() if total > 0 else 1
                fail_count = int(missing)
                if total == 0:
                    status = "FAIL"
                    detail = f"{event_name} 이벤트 없음"
                else:
                    null_ratio = missing / total
                    status = "PASS" if missing == 0 else "FAIL"
                    detail = f"null {missing}/{total} ({null_ratio:.1%})"

            _add_result(
                results,
                f"required_param:{event_name}:{param}",
                f"필수 param 체크: {event_name}.{param}",
                status,
                detail,
                fail_count,
                total if total > 0 else 1,
            )

    # 3) Null ratio warnings for core fields.
    for field in ["transaction_id", "value", "currency", "user_pseudo_id"]:
        total = len(work)
        if total == 0:
            _add_result(
                results,
                f"null_ratio:{field}",
                f"null 비율 체크: {field}",
                "WARN",
                "데이터 없음",
                1,
                1,
            )
            continue

        missing = int(_is_missing(work[field]).sum())
        ratio = missing / total
        if ratio > null_threshold:
            status = "WARN"
        else:
            status = "PASS"

        _add_result(
            results,
            f"null_ratio:{field}",
            f"null 비율 체크: {field}",
            status,
            f"null {missing}/{total} ({ratio:.1%})",
            missing,
            total,
        )

    # 4) Value type check (purchase rows should be numeric where present).
    purchase_rows = work[work["event_name"] == "purchase"]
    purchase_total = len(purchase_rows)
    if purchase_total == 0:
        _add_result(
            results,
            "value_numeric",
            "value 타입 체크 (purchase)",
            "PASS",
            "purchase 이벤트 없음",
            0,
            1,
        )
    else:
        value_series = purchase_rows["value"]
        present_mask = ~_is_missing(value_series)
        present_values = value_series[present_mask]
        parsed = pd.to_numeric(present_values, errors="coerce")
        invalid = int(parsed.isna().sum())
        total_present = int(present_mask.sum())
        status = "PASS" if invalid == 0 else "FAIL"
        _add_result(
            results,
            "value_numeric",
            "value 타입 체크 (purchase)",
            status,
            f"비정상 값 {invalid}/{total_present}",
            invalid,
            total_present if total_present > 0 else 1,
        )

    # 5) Duplicate transaction_id.
    transaction_series = work["transaction_id"].dropna().astype(str).str.strip()
    transaction_series = transaction_series[transaction_series != ""]
    duplicate_mask = transaction_series.duplicated(keep=False)
    duplicate_rows = int(duplicate_mask.sum())
    duplicate_values = int(transaction_series[duplicate_mask].nunique()) if duplicate_rows else 0
    _add_result(
        results,
        "duplicate_transaction_id",
        "중복 transaction_id 체크",
        "PASS" if duplicate_rows == 0 else "FAIL",
        "중복 없음" if duplicate_rows == 0 else f"중복 값 {duplicate_values}개, 행 {duplicate_rows}건",
        duplicate_rows,
        len(transaction_series) if len(transaction_series) > 0 else 1,
    )

    # 6) Purchase without value.
    if purchase_total == 0:
        _add_result(
            results,
            "purchase_without_value",
            "purchase 있는데 value 없음",
            "PASS",
            "purchase 이벤트 없음",
            0,
            1,
        )
    else:
        missing_value = int(_is_missing(purchase_rows["value"]).sum())
        _add_result(
            results,
            "purchase_without_value",
            "purchase 있는데 value 없음",
            "PASS" if missing_value == 0 else "FAIL",
            f"{missing_value}/{purchase_total}건",
            missing_value,
            purchase_total,
        )

    # 7) purchase without add_to_cart.
    purchase_users = set(
        purchase_rows["user_pseudo_id"].dropna().astype(str).str.strip().tolist()
    )
    add_to_cart_users = set(
        work.loc[work["event_name"] == "add_to_cart", "user_pseudo_id"]
        .dropna()
        .astype(str)
        .str.strip()
        .tolist()
    )
    missing_add_to_cart = sorted(u for u in purchase_users if u and u not in add_to_cart_users)
    _add_result(
        results,
        "purchase_without_add_to_cart",
        "add_to_cart 없이 purchase 발생",
        "PASS" if len(missing_add_to_cart) == 0 else "FAIL",
        "없음" if len(missing_add_to_cart) == 0 else f"{len(missing_add_to_cart)}명",
        len(missing_add_to_cart),
        len(purchase_users) if len(purchase_users) > 0 else 1,
    )

    # 8) purchase without sign_up.
    sign_up_users = set(
        work.loc[work["event_name"] == "sign_up", "user_pseudo_id"]
        .dropna()
        .astype(str)
        .str.strip()
        .tolist()
    )
    missing_sign_up = sorted(u for u in purchase_users if u and u not in sign_up_users)
    _add_result(
        results,
        "purchase_without_sign_up",
        "sign_up 없이 purchase 발생",
        "PASS" if len(missing_sign_up) == 0 else "FAIL",
        "없음" if len(missing_sign_up) == 0 else f"{len(missing_sign_up)}명",
        len(missing_sign_up),
        len(purchase_users) if len(purchase_users) > 0 else 1,
    )

    # 9) Simple funnel order check.
    steps = list(funnel_steps) if funnel_steps else ["view_item", "add_to_cart", "begin_checkout", "purchase"]
    if purchase_users:
        sequence_df = work[work["user_pseudo_id"].astype(str).isin(purchase_users)]
        funnel = analyze_funnel_sequence(sequence_df, steps)
        funnel_status = "PASS" if funnel.failed_users == 0 else "FAIL"
        detail = (
            f"실패 {funnel.failed_users}/{funnel.total_users}명 ({funnel.fail_rate:.1%})"
            if funnel.total_users > 0
            else "검사 대상 없음"
        )
        _add_result(
            results,
            "funnel_order",
            "간단 퍼널 순서 체크",
            funnel_status,
            detail,
            funnel.failed_users,
            funnel.total_users if funnel.total_users > 0 else 1,
        )
    else:
        _add_result(
            results,
            "funnel_order",
            "간단 퍼널 순서 체크",
            "PASS",
            "purchase 유저 없음",
            0,
            1,
        )

    # 10) Event timestamp completeness.
    ts_missing = int(_is_missing(work["event_timestamp"]).sum())
    ts_total = len(work)
    ts_ratio = (ts_missing / ts_total) if ts_total else 1.0
    _add_result(
        results,
        "missing_event_timestamp",
        "event_timestamp 누락 체크",
        "PASS" if ts_ratio <= null_threshold else "WARN",
        f"null {ts_missing}/{ts_total} ({ts_ratio:.1%})",
        ts_missing,
        ts_total if ts_total > 0 else 1,
    )

    return pd.DataFrame(results)
