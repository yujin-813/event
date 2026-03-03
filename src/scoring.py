from __future__ import annotations

from typing import Dict

import pandas as pd


def _score_from_status(status: str) -> float:
    if status == "PASS":
        return 1.0
    if status == "WARN":
        return 0.5
    return 0.0


def _risk_level(total_score: int) -> str:
    if total_score >= 85:
        return "Low"
    if total_score >= 60:
        return "Medium"
    return "High"


def compute_integrity_score(results_df: pd.DataFrame) -> Dict[str, int | str]:
    if results_df.empty:
        return {
            "coverage_score": 0,
            "param_score": 0,
            "anomaly_score": 0,
            "total_score": 0,
            "risk_level": "High",
        }

    coverage_rules = results_df[results_df["rule_id"].str.startswith("required_event:")]
    param_rules = results_df[results_df["rule_id"].str.startswith("required_param:")]
    anomaly_rules = results_df[
        ~results_df["rule_id"].str.startswith("required_event:")
        & ~results_df["rule_id"].str.startswith("required_param:")
    ]

    coverage_ratio = (
        coverage_rules["status"].map(_score_from_status).mean()
        if not coverage_rules.empty
        else 0.0
    )
    param_ratio = (
        param_rules["status"].map(_score_from_status).mean()
        if not param_rules.empty
        else 0.0
    )
    anomaly_ratio = (
        anomaly_rules["status"].map(_score_from_status).mean()
        if not anomaly_rules.empty
        else 0.0
    )

    coverage_score = round(coverage_ratio * 40)
    param_score = round(param_ratio * 30)
    anomaly_score = round(anomaly_ratio * 30)
    total_score = int(coverage_score + param_score + anomaly_score)

    return {
        "coverage_score": int(coverage_score),
        "param_score": int(param_score),
        "anomaly_score": int(anomaly_score),
        "total_score": total_score,
        "risk_level": _risk_level(total_score),
    }
