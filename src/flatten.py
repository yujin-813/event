from __future__ import annotations

from typing import Dict, Iterable, List

import pandas as pd


def normalize_param_name(name: str) -> str:
    key = name.strip()
    map_name = {
        "eventName": "event_name",
        "date": "event_date",
        "dateHourMinute": "event_timestamp",
        "pageLocation": "page_location",
        "userPseudoId": "user_pseudo_id",
        "userId": "user_pseudo_id",
        "transactionId": "transaction_id",
        "eventCount": "event_count",
        "transaction_id": "transaction_id",
        "user_pseudo_id": "user_pseudo_id",
    }

    if key in map_name:
        return map_name[key]
    if key.startswith("customEvent:"):
        return key.split(":", 1)[1]
    return key


def flatten_events(df: pd.DataFrame, requested_params: Iterable[str]) -> pd.DataFrame:
    if df.empty:
        base_columns = [
            "event_name",
            "event_date",
            "event_timestamp",
            "user_pseudo_id",
            "event_count",
        ]
        requested = [normalize_param_name(p) for p in requested_params if p.strip()]
        all_cols = list(dict.fromkeys(base_columns + requested))
        return pd.DataFrame(columns=all_cols)

    rename_map: Dict[str, str] = {col: normalize_param_name(col) for col in df.columns}
    out = df.rename(columns=rename_map).copy()

    if "event_date" in out.columns:
        out["event_date"] = pd.to_datetime(
            out["event_date"], format="%Y%m%d", errors="coerce"
        ).dt.date

    if "event_timestamp" in out.columns:
        out["event_timestamp"] = pd.to_datetime(
            out["event_timestamp"], format="%Y%m%d%H%M", errors="coerce"
        )

    for col in ["event_count"]:
        if col in out.columns:
            out[col] = pd.to_numeric(out[col], errors="coerce")

    expected_columns = [
        "event_name",
        "event_date",
        "event_timestamp",
        "user_pseudo_id",
        "event_count",
    ]
    expected_columns.extend(normalize_param_name(p) for p in requested_params if p.strip())

    for col in expected_columns:
        if col not in out.columns:
            out[col] = pd.NA

    # Keep a stable order for key columns first.
    ordered = list(dict.fromkeys(expected_columns + list(out.columns)))
    return out[ordered]
