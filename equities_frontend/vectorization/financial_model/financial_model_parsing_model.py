"""
Pure parsing and formatting functions for Bloomberg financial-model sheets.

Every function here takes plain openpyxl rows/worksheets or primitive values
and returns plain values. Nothing here holds state, performs I/O, or knows how
the caller assembles a full extraction record -- that orchestration lives in
``financial_model_controller``.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from vectorization.financial_model.types import ModelHeader, Period

# Rows 3 and 4 carry the period labels and end dates respectively.
_HEADER_ROW_COUNT = 2

# A period label looks like "2025 Q1 (Rep)", "2026 A (Fwd)", or, for
# semi-annual reporters (Bloomberg's "Periodicity:S", e.g. RELX/REL LN),
# "2023 S1 (Rep)".
_PERIOD_LABEL_RE = re.compile(r"\d{4}\s+(Q\d|S\d|A\b)")
_ESTIMATE_RE = re.compile(r"\b(Fwd|Est|E\))", re.IGNORECASE)
_UNIT_RE = re.compile(r"In\s+(Millions?|Billions?|Thousands?)", re.IGNORECASE)


def parse_header(sheet: Any) -> ModelHeader:
    """
    Parse company name, ticker, periodicity and currency from rows 1-2.

    Args:
        sheet (Any): An openpyxl read-only worksheet positioned at the model sheet.

    Returns:
        ModelHeader: The identifying metadata found at the top of the sheet, with
        sensible defaults ("Q" periodicity, "USD" currency) when a field is absent.
    """
    rows = list(sheet.iter_rows(min_row=1, max_row=2, max_col=5, values_only=True))
    company_name = ticker = ""
    periodicity, currency = "Q", "USD"

    if rows and rows[0][0]:
        company_name = re.sub(r"\s*-\s*Company Financial.*$", "", str(rows[0][0])).strip()

    if len(rows) > 1 and rows[1][0]:
        meta = str(rows[1][0])
        if match := re.match(r"^(\S+)", meta):
            ticker = match.group(1)
        if match := re.search(r"Periodicity:(\w+)", meta):
            periodicity = match.group(1)
        if match := re.search(r"Currency:(\w+)", meta):
            currency = match.group(1)

    return ModelHeader(company_name, ticker, periodicity, currency)


def parse_periods(sheet: Any, data_start_col: int) -> dict[int, Period]:
    """
    Map each data column index to its Period from the label (row 3) and date (row 4).

    Args:
        sheet (Any): An openpyxl read-only worksheet positioned at the model sheet.
        data_start_col (int): Index of the first period-value column.

    Returns:
        dict[int, Period]: Column index -> Period, for every column whose row-4
        value is a date and whose row-3 label matches the expected period pattern.
    """
    rows = list(sheet.iter_rows(min_row=3, max_row=4, values_only=True))
    if len(rows) < _HEADER_ROW_COUNT:
        return {}

    labels, dates = rows[0], rows[1]
    periods: dict[int, Period] = {}
    for col in range(data_start_col, max(len(labels), len(dates))):
        label = labels[col] if col < len(labels) else None
        date = dates[col] if col < len(dates) else None
        if not isinstance(date, datetime) or not label or not _PERIOD_LABEL_RE.search(str(label)):
            continue
        text = str(label).strip()
        periods[col] = Period(label=text, end_date=date, is_estimate=bool(_ESTIMATE_RE.search(text)))
    return periods


def parse_unit(sheet: Any) -> str:
    """
    Read the unit from row 3 col 0, e.g. "In Millions of USD" -> "Millions".

    Args:
        sheet (Any): An openpyxl read-only worksheet positioned at the model sheet.

    Returns:
        str: The detected unit ("Millions", "Billions", "Thousands"), or "Units"
        when no unit could be detected.
    """
    row = next(sheet.iter_rows(min_row=3, max_row=3, max_col=1, values_only=True), (None,))
    if row[0] and (match := _UNIT_RE.search(str(row[0]))):
        return match.group(1).rstrip("s").capitalize() + "s"
    return "Units"


def indent_depth(raw: str) -> int:
    """
    Return the number of leading spaces in ``raw``, used as the hierarchy level.

    Args:
        raw (str): The raw, un-stripped cell value from column 0 of a data row.

    Returns:
        int: Number of leading space characters.
    """
    return len(raw) - len(raw.lstrip(" "))


def build_metric_path(parents: list[str], leaf: str) -> str:
    """
    Join parent sections and the leaf into a breadcrumb, dropping repeats.

    Args:
        parents (list[str]): Ancestor section names, outermost first.
        leaf (str): The metric's own name.

    Returns:
        str: A " > "-joined breadcrumb, e.g. "Segment Revenue > Data Center".
    """
    parts = [*parents, leaf]
    deduped = [parts[0]]
    for part in parts[1:]:
        if part != deduped[-1]:
            deduped.append(part)
    return " > ".join(deduped)


def format_metric_value(
    value: float,
    metric_path: str,
    currency: str,
    unit: str,
    ratio_tokens: tuple[str, ...],
) -> tuple[str, str]:
    """
    Format a metric value for display, with ratios rendered as bare 2dp numbers.

    Args:
        value (float): The raw numeric cell value.
        metric_path (str): The metric's full breadcrumb path, checked against
            ``ratio_tokens`` to decide whether it is a ratio/percentage.
        currency (str): The model's reporting currency (e.g. "USD").
        unit (str): The model's reporting unit (e.g. "Millions").
        ratio_tokens (tuple[str, ...]): Substrings that mark a metric path as a
            ratio/percentage (e.g. "margin", "eps") rather than a currency amount.

    Returns:
        tuple[str, str]: The formatted value string and its unit suffix (empty
        for ratios).
    """
    if any(token in metric_path.lower() for token in ratio_tokens):
        return f"{value:.2f}", ""
    suffix = f" {currency} {unit}"
    if isinstance(value, float) and value == int(value):
        return f"{int(value):,}", suffix
    if isinstance(value, float):
        return f"{value:,.2f}", suffix
    return f"{value:,}", suffix


def is_row_section_header(row: tuple, period_columns: dict[int, Period]) -> bool:
    """
    Determine whether a data row is a section header rather than a metric row.

    Args:
        row (tuple): The raw row values from the sheet.
        period_columns (dict[int, Period]): Column index -> Period, used to check
            whether any period column in this row holds a numeric value.

    Returns:
        bool: True if the row has a name but no Bloomberg field (column 1) and
        no numeric value in any period column.
    """
    if len(row) > 1 and row[1]:
        return False
    return all(not (col < len(row) and isinstance(row[col], (int, float))) for col in period_columns)


def stamp_latest_reported_date(records: list[dict[str, Any]]) -> None:
    """
    Set ``latest_reported_date`` on every record to the newest reported period.

    Args:
        records (list[dict[str, Any]]): Extraction records, mutated in place.
    """
    reported = [record["period_end_date"] for record in records if record["is_estimate"] == "reported"]
    latest = max(reported) if reported else None
    for record in records:
        record["latest_reported_date"] = latest
