"""
Local SQLite cache of the vendored Portfolio Dashboard's full report objects
(``AttributionReport``, ``TrackingErrorReport``, etc.) -- the whole thing the
dashboard's tabs need to render, not just the summarized LLM-facing subset in
``chat_context_model.py``.

Exists so the dashboard page never has to independently re-walk the entire
Data Lake silver-layer directory tree (``SilverStore``'s own recursive
``list_paths``, see ``datalake/silver_controller.py``) on page load: with
hundreds of files under ``EQT/portfolio`` and growing, that listing alone can
time out under load, and doing it once an hour compounds the problem further
(confirmed in production -- too many redundant requests, not a network/
firewall issue). The scheduler now reads it only once a day (see
``scheduler/scheduler_controller.py::_run_portfolio_sync``) and persists the
result here; the dashboard page always reads from here instead.

Serialization reuses the vendored package's own tested round-trip
(``portfolio_dashboard.datalake.silver_model.report_to_parquet_bytes`` /
``parquet_bytes_to_report``) rather than a hand-rolled scheme -- storing the
exact same parquet bytes their own silver layer already produces, just in a
local SQLite BLOB column instead of a Data Lake file. That keeps this cache
automatically correct if the vendored report dataclasses ever gain or lose
fields upstream, since we never have to know their shape ourselves.

Also caches ``SilverStore.summary_history()``'s per-date summary metrics (the
sidebar's report-date picker's data source, rendered on *every* dashboard
page load, not just when a Trends tab is opened) -- these are plain scalars,
not dataclasses, so they're just stored as JSON rows rather than going
through the parquet round-trip above. ``pandas`` is used directly here (the
one exception to this app's own polars-first convention for new code) purely
to match ``summary_history()``'s existing return shape exactly, so the cached
version is a real drop-in replacement, not a lookalike.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd

from database import cache
from portfolio_dashboard.datalake.silver_model import parquet_bytes_to_report, report_to_parquet_bytes


def store_reports(reports_by_portfolio: dict[str, dict[str, object]]) -> None:
    """Serialize and persist every portfolio's reports to the local SQLite cache."""
    records = [
        {"portfolio": portfolio, "report_type": report_type, "report_blob": report_to_parquet_bytes(report)}
        for portfolio, reports in reports_by_portfolio.items()
        for report_type, report in reports.items()
        if report is not None
    ]
    cache.upsert_portfolio_report_blobs(records)


def load_reports() -> dict[str, dict[str, object]] | None:
    """
    Rebuild every portfolio's reports from the local SQLite cache.

    Returns:
        dict[str, dict[str, object]] | None: The same shape
        ``SilverStore.load_reports_by_portfolio()`` returns, or ``None`` if
        the cache is empty (e.g. no daily sync has completed yet).
    """
    portfolios = cache.get_portfolio_report_portfolios()
    if not portfolios:
        return None
    return {
        portfolio: {
            report_type: parquet_bytes_to_report(blob)
            for report_type, blob in cache.get_portfolio_report_blobs(portfolio).items()
        }
        for portfolio in portfolios
    }


def synced_today() -> bool:
    """True if the cache already holds a sync from today (UTC)."""
    today = datetime.now(timezone.utc).date().isoformat()
    return cache.portfolio_reports_last_synced_date() == today


def store_summary_history(portfolio: str, frame: pd.DataFrame) -> None:
    """Persist one portfolio's ``summary_history()`` rows to the local SQLite cache."""
    if frame.empty:
        return
    records = [
        {
            "portfolio": portfolio,
            "as_of": row["as_of"],
            "row": {k: v for k, v in row.items() if k not in ("as_of", "portfolio")},
        }
        for row in frame.to_dict("records")
    ]
    cache.upsert_portfolio_summary_history(records)


def load_summary_history(portfolio: str) -> pd.DataFrame | None:
    """
    Rebuild one portfolio's summary-history frame from the local SQLite cache.

    Returns:
        pd.DataFrame | None: The same shape ``SilverStore.summary_history()``
        returns, or ``None`` if this portfolio has never been cached (e.g. no
        daily sync has completed yet).
    """
    if not cache.has_portfolio_summary_history(portfolio):
        return None
    return pd.DataFrame(cache.get_portfolio_summary_history(portfolio))
