from __future__ import annotations

from datetime import datetime
from typing import Dict

import pandas as pd

try:
    from fpdf import FPDF
except ModuleNotFoundError:  # pragma: no cover - optional dependency guard
    FPDF = None


def _latin_safe(text: str) -> str:
    return str(text).encode("latin-1", errors="replace").decode("latin-1")


def results_to_csv_bytes(results_df: pd.DataFrame) -> bytes:
    return results_df.to_csv(index=False).encode("utf-8-sig")


def events_to_csv_bytes(events_df: pd.DataFrame) -> bytes:
    return events_df.to_csv(index=False).encode("utf-8-sig")


def results_to_pdf_bytes(
    results_df: pd.DataFrame,
    score: Dict[str, int | str],
    metadata: Dict[str, str],
) -> bytes:
    if FPDF is None:
        raise RuntimeError("PDF export requires 'fpdf2'. Run: pip install fpdf2")

    pdf = FPDF()
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.add_page()

    pdf.set_font("Helvetica", "B", 14)
    pdf.cell(0, 10, "GA4 QA Report", ln=True)

    pdf.set_font("Helvetica", size=10)
    for key, value in metadata.items():
        pdf.cell(0, 6, _latin_safe(f"{key}: {value}"), ln=True)

    pdf.ln(3)
    pdf.set_font("Helvetica", "B", 11)
    pdf.cell(0, 8, "Data Integrity Score", ln=True)

    pdf.set_font("Helvetica", size=10)
    pdf.cell(0, 6, _latin_safe(f"Total: {score['total_score']} / 100"), ln=True)
    pdf.cell(0, 6, _latin_safe(f"Risk Level: {score['risk_level']}"), ln=True)
    pdf.cell(0, 6, _latin_safe(f"Coverage: {score['coverage_score']} / 40"), ln=True)
    pdf.cell(0, 6, _latin_safe(f"Param Completeness: {score['param_score']} / 30"), ln=True)
    pdf.cell(0, 6, _latin_safe(f"Anomaly: {score['anomaly_score']} / 30"), ln=True)

    pdf.ln(4)
    pdf.set_font("Helvetica", "B", 11)
    pdf.cell(0, 8, "QA Rules", ln=True)

    pdf.set_font("Helvetica", size=9)
    for _, row in results_df.iterrows():
        rule = str(row.get("rule_name", ""))
        status = str(row.get("status", ""))
        detail = str(row.get("detail", ""))
        line = f"[{status}] {rule} - {detail}"
        pdf.multi_cell(0, 5, _latin_safe(line))

    return pdf.output(dest="S").encode("latin1")


def build_report_filename(prefix: str, suffix: str) -> str:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    return f"{prefix}_{ts}.{suffix}"
