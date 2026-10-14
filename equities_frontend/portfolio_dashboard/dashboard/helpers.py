"""Technical helpers for the dashboard entry-point.

Pure, side-effect-light utilities shared by the Streamlit app: metric
formatting, report date derivation, and upload archiving. None of these
functions render UI; keep Streamlit calls in the view layer.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from portfolio_dashboard.reports.reports_model import Report

# Bloomberg date formats, tried in order when normalising a raw date string.
_DATE_FORMATS: tuple[str, ...] = ("%m/%d/%Y", "%Y-%m-%d", "%d/%m/%Y")


def format_metric(value: float | None, decimals: int = 2, suffix: str = "%") -> str:
    """Format a numeric metric for display with an explicit sign.

    Args:
        value: The value to format, or ``None`` when unavailable.
        decimals: Number of decimal places to show.
        suffix: Unit suffix appended after the number (e.g. ``"%"`` or ``"x"``).

    Returns:
        str: The formatted string, or an em dash when ``value`` is ``None``.
    """
    if value is None:
        return "—"
    sign = "+" if value > 0 else ""
    return f"{sign}{value:.{decimals}f}{suffix}"


def report_display_date(report: Report) -> str:
    """Return the human-facing effective date of a report.

    Prefers the period end, then the risk report ``as_of``, then the run date.

    Args:
        report: Any parsed report exposing ``metadata``.

    Returns:
        str: The chosen date string, or an empty string when none is set.
    """
    metadata = report.metadata
    return metadata.period_end or metadata.as_of or metadata.run_date or ""


def report_date_key(report: Report) -> str:
    """Return an ISO (YYYY-MM-DD) sort key for a report.

    Uses ``run_date`` first — the Bloomberg generation date is unique per
    calendar day even when ``period_end`` is identical across multiple daily
    exports of the same period. Falls back to ``as_of`` then ``period_end``.

    Args:
        report: Any parsed report exposing ``metadata``.

    Returns:
        str: The ISO date string, or the stripped raw value when unparsable.
    """
    metadata = report.metadata
    raw = metadata.run_date or metadata.as_of or metadata.period_end or ""
    if not raw:
        return ""
    for date_format in _DATE_FORMATS:
        try:
            return datetime.strptime(raw.strip(), date_format).date().isoformat()
        except ValueError:
            continue
    return raw.strip()


def save_uploaded_file(raw: bytes, data_dir: Path, report_type: str, filename: str) -> None:
    """Archive an uploaded file under ``data_dir / report_type``.

    Args:
        raw: The raw bytes of the uploaded file.
        data_dir: Root directory for archived uploads.
        report_type: Sub-directory naming the detected report type.
        filename: Name to store the file under.
    """
    save_dir = data_dir / report_type
    save_dir.mkdir(parents=True, exist_ok=True)
    (save_dir / filename).write_bytes(raw)
