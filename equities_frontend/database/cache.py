"""Local SQLite cache for indexed document metadata.

Stores one row per document path so the Index Browser can render instantly
without querying Azure AI Search on every page load.

Populated two ways:
  - ``post_ingest_update`` — called automatically after each ingest run.
  - ``sync_from_azure``    — full re-sync from Azure; available as a manual
                             "Sync" button in the Index Browser.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

_logger = logging.getLogger(__name__)


# Azure Files (SMB) doesn't support POSIX file locking — SQLite must live on local disk.
# /tmp is local to the compute node; fall back to a data/ subfolder only in dev.
def _resolve_db_path() -> Path:
    local_tmp = Path("/tmp/equities_rag")  # noqa: S108
    try:
        local_tmp.mkdir(parents=True, exist_ok=True)
        return local_tmp / "documents.db"
    except OSError:
        fallback = Path(__file__).parent.parent / "data"
        fallback.mkdir(parents=True, exist_ok=True)
        return fallback / "documents.db"


_DB_PATH = _resolve_db_path()

_DOCUMENTS_DDL = """
CREATE TABLE IF NOT EXISTS documents (
    path          TEXT PRIMARY KEY,
    filename      TEXT,
    companies     TEXT,        -- JSON list of tickers
    company_names TEXT,        -- JSON list
    publisher     TEXT,
    document_date TEXT,        -- YYYY-MM-DD or NULL
    document_type TEXT,
    source_type   TEXT,
    ingested_at   TEXT         -- ISO-8601 UTC timestamp or NULL if synced from Azure
);
"""


_COMPANIES_DDL = """
CREATE TABLE IF NOT EXISTS companies (
    ticker              TEXT PRIMARY KEY,
    display_name        TEXT,
    region              TEXT,
    gics_sector         TEXT,
    doc_count           INTEGER,
    has_financial_model INTEGER,        -- 0/1
    first_document_date TEXT,           -- YYYY-MM-DD or NULL
    last_document_date  TEXT,           -- YYYY-MM-DD or NULL
    classified_at       TEXT            -- ISO-8601 UTC timestamp, NULL if not yet classified
);
"""


_PORTFOLIO_METRICS_DDL = """
CREATE TABLE IF NOT EXISTS portfolio_metrics (
    portfolio        TEXT NOT NULL,
    as_of_date       TEXT NOT NULL,    -- YYYY-MM-DD
    benchmark        TEXT,
    base_currency    TEXT,
    portfolio_return REAL,
    active_return    REAL,
    portfolio_te     REAL,             -- tracking error, %
    portfolio_beta   REAL,
    holdings          TEXT,            -- JSON [{name, weight_pct}, ...] every position, whole portfolio
    risk_contributors TEXT,            -- JSON [{name, total_risk_d, contribution_pct}, ...] every position
    factor_exposures  TEXT,            -- JSON [{factor_label, category, active_exposure, contribution_pct}, ...]
    synced_at        TEXT,             -- ISO-8601 UTC timestamp
    PRIMARY KEY (portfolio, as_of_date)
);
"""


_PORTFOLIO_REPORTS_RAW_DDL = """
CREATE TABLE IF NOT EXISTS portfolio_reports_raw (
    portfolio    TEXT NOT NULL,
    report_type  TEXT NOT NULL,   -- 'attr' | 'te' | 'sector' | 'factor' | 'te_factors'
    report_blob  BLOB NOT NULL,   -- portfolio_dashboard.datalake.silver_model.report_to_parquet_bytes(report)
    synced_at    TEXT,            -- ISO-8601 UTC timestamp
    PRIMARY KEY (portfolio, report_type)
);
"""


_PORTFOLIO_SUMMARY_HISTORY_DDL = """
CREATE TABLE IF NOT EXISTS portfolio_summary_history (
    portfolio  TEXT NOT NULL,
    as_of      TEXT NOT NULL,   -- as returned by SilverStore.summary_history's 'as_of' column
    row_json   TEXT NOT NULL,   -- JSON {benchmark, te_pct, beta, port_return, bench_return, active_return, end_mkt_val}
    synced_at  TEXT,            -- ISO-8601 UTC timestamp
    PRIMARY KEY (portfolio, as_of)
);
"""


# An authoritative reference of real company names, sourced from the
# Portfolio Dashboard's actual holdings data (see
# portfolio_dashboard/chat_context_model.py::distinct_security_names) rather
# than noisy per-document LLM extraction -- deliberately a separate table
# from `companies` (not a column on it): `companies` means "seen in at least
# one ingested document" everywhere else in this app, and this app has no
# schema-migration mechanism for adding a column to that already-populated,
# already-in-production table (see companies/company_cleanup_model.py for
# how the two tables are used together).
_COMPANY_REFERENCE_DDL = """
CREATE TABLE IF NOT EXISTS company_reference (
    name           TEXT PRIMARY KEY,   -- exact Bloomberg holdings display name, e.g. "ASML HOLDING NV"
    ticker         TEXT,               -- LLM-resolved, e.g. "ASML"; NULL until resolved
    canonical_name TEXT,               -- LLM-resolved legal name, e.g. "ASML Holding N.V."; NULL until resolved
    resolved_at    TEXT                -- ISO-8601 UTC timestamp, NULL if resolution hasn't run yet
);
"""


# LLM-proposed company merges that couldn't be confirmed against
# company_reference (see companies/company_cleanup_model.py::gate_merge_groups)
# -- held here for manual approval on the Logs page instead of applying
# automatically.
_COMPANY_MERGE_SUGGESTIONS_DDL = """
CREATE TABLE IF NOT EXISTS company_merge_suggestions (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    tickers        TEXT NOT NULL,   -- JSON list of tickers proposed to merge
    canonical_name TEXT,            -- LLM's proposed canonical display name
    reason         TEXT,            -- LLM's stated reasoning, shown to the reviewer
    status         TEXT NOT NULL DEFAULT 'pending',   -- 'pending' | 'approved' | 'rejected'
    created_at     TEXT             -- ISO-8601 UTC timestamp
);
"""


# Single-row marker of the last date the daily LLM company-cleanup pass
# completed -- same once-a-day gating shape as
# portfolio_dashboard.silver_cache_model.synced_today(), just for a step
# that isn't itself backed by a report/summary table to derive a "last
# synced" date from.
_COMPANY_CLEANUP_STATE_DDL = """
CREATE TABLE IF NOT EXISTS company_cleanup_state (
    id            INTEGER PRIMARY KEY CHECK (id = 1),
    last_run_date TEXT
);
"""


# A periodically-uploaded Bloomberg factor/crowding screen (see
# companies/factor_screen_model.py) -- one dated snapshot per upload, kept
# (not overwritten) so a company's factor percentiles over time are
# preserved, matching the portfolio_summary_history precedent above.
_FACTOR_SCREEN_DDL = """
CREATE TABLE IF NOT EXISTS factor_screen (
    as_of_date           TEXT NOT NULL,   -- YYYY-MM-DD, parsed from the uploaded filename
    name                 TEXT NOT NULL,   -- Bloomberg display name, e.g. "MONGODB INC"
    symbol                TEXT,
    country               TEXT,
    bloomberg_identifier  TEXT,
    sedol                 TEXT,
    overall_factor_pct    REAL,
    quality_factor_pct    REAL,
    low_vol_factor_pct    REAL,
    growth_factor_pct     REAL,
    momentum_factor_pct   REAL,
    valuation_factor_pct  REAL,
    crowding_factor       REAL,           -- empty in every row of the file seen so far; kept for when it's populated
    uploaded_at           TEXT,           -- ISO-8601 UTC timestamp
    PRIMARY KEY (as_of_date, name)
);
"""


# ── connection & initialization ───────────────────────────────────────────────


@contextmanager
def _conn():
    """Open a fresh connection for one operation, then close it."""
    c = sqlite3.connect(str(_DB_PATH), check_same_thread=False, timeout=30)
    c.row_factory = sqlite3.Row
    try:
        yield c
    finally:
        c.close()


def _initialize() -> None:
    """Create tables on a fresh database; retries 30 s if another process is writing."""
    _logger.debug("initialising SQLite at %s", _DB_PATH)
    t0 = time.perf_counter()
    for attempt in range(60):
        try:
            # timeout=1 so a locked DB fails fast; the retry loop provides the total wait budget
            c = sqlite3.connect(str(_DB_PATH), timeout=1)
            try:
                c.execute("PRAGMA journal_mode=WAL")
                c.execute(_DOCUMENTS_DDL)
                c.execute(_COMPANIES_DDL)
                c.execute(_PORTFOLIO_METRICS_DDL)
                c.execute(_PORTFOLIO_REPORTS_RAW_DDL)
                c.execute(_PORTFOLIO_SUMMARY_HISTORY_DDL)
                c.execute(_COMPANY_REFERENCE_DDL)
                c.execute(_COMPANY_MERGE_SUGGESTIONS_DDL)
                c.execute(_COMPANY_CLEANUP_STATE_DDL)
                c.execute(_FACTOR_SCREEN_DDL)
                c.commit()
            finally:
                c.close()
            _logger.debug("SQLite ready (%.2fs)", time.perf_counter() - t0)
            return
        except sqlite3.OperationalError as exc:
            if "database is locked" not in str(exc) or attempt == 59:
                raise
            if attempt == 0:
                _logger.warning("SQLite locked, retrying...")
            time.sleep(0.5)


_cache_available = False


def _try_initialize() -> None:
    global _cache_available  # noqa: PLW0603
    try:
        _initialize()
        _cache_available = True
    except Exception as exc:
        _logger.warning("SQLite cache disabled: %s", exc)


# Run once per process at import time; failure is non-fatal.
_try_initialize()


# ── document cache ────────────────────────────────────────────────────────────


def upsert_documents(records: list[dict]) -> None:
    """Insert or replace document rows (keyed on path)."""
    if not records or not _cache_available:
        return
    now = datetime.now(timezone.utc).isoformat()
    with _conn() as c:
        c.executemany(
            """
            INSERT OR REPLACE INTO documents
                (path, filename, companies, company_names, publisher,
                 document_date, document_type, source_type, ingested_at)
            VALUES (:path, :filename, :companies, :company_names, :publisher,
                    :document_date, :document_type, :source_type, :ingested_at)
            """,
            [
                {
                    "path": r.get("path", ""),
                    "filename": r.get("filename") or os.path.basename(r.get("path", "")),
                    "companies": json.dumps(r.get("companies") or []),
                    "company_names": json.dumps(r.get("company_names") or []),
                    "publisher": r.get("publisher") or None,
                    "document_date": (r.get("document_date") or "")[:10] or None,
                    "document_type": r.get("document_type") or None,
                    "source_type": r.get("source_type") or None,
                    "ingested_at": r.get("ingested_at") or now,
                }
                for r in records
            ],
        )
        c.commit()


def get_all_documents() -> list[dict]:
    """Return all cached documents, most-recently ingested first."""
    if not _cache_available:
        return []
    with _conn() as c:
        rows = c.execute("SELECT * FROM documents ORDER BY ingested_at DESC NULLS LAST").fetchall()
    return [_row_to_dict(r) for r in rows]


def get_documents_by_paths(paths: list[str]) -> list[dict]:
    """Return cached documents matching the given Data Lake paths."""
    if not paths or not _cache_available:
        return []
    placeholders = ",".join("?" * len(paths))
    with _conn() as c:
        rows = c.execute(f"SELECT * FROM documents WHERE path IN ({placeholders})", paths).fetchall()  # noqa: S608
    return [_row_to_dict(r) for r in rows]


def get_recent_documents(limit: int = 15) -> list[dict]:
    """Return the *limit* most-recently ingested documents."""
    if not _cache_available:
        return []
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM documents WHERE ingested_at IS NOT NULL ORDER BY ingested_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def get_distinct_publishers() -> list[str]:
    if not _cache_available:
        return []
    with _conn() as c:
        rows = c.execute(
            "SELECT DISTINCT publisher FROM documents WHERE publisher IS NOT NULL AND publisher != '' ORDER BY publisher"
        ).fetchall()
    return [r[0] for r in rows]


def get_distinct_companies() -> list[str]:
    """Return sorted unique company tickers across all documents."""
    if not _cache_available:
        return []
    with _conn() as c:
        rows = c.execute("SELECT companies FROM documents WHERE companies IS NOT NULL").fetchall()
    tickers: set[str] = set()
    for r in rows:
        tickers.update(json.loads(r[0] or "[]"))
    return sorted(t for t in tickers if t)


def get_documents_missing_companies() -> list[dict]:
    """Return every cached document whose companies list is empty, most-recently ingested first."""
    if not _cache_available:
        return []
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM documents WHERE companies IS NULL OR companies = '[]' ORDER BY ingested_at DESC NULLS LAST"
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def get_companies_with_financial_model() -> list[str]:
    """Return sorted unique company tickers that have an ingested Bloomberg financial model."""
    if not _cache_available:
        return []
    with _conn() as c:
        rows = c.execute(
            "SELECT companies FROM documents WHERE companies IS NOT NULL AND source_type = 'financial_model'"
        ).fetchall()
    tickers: set[str] = set()
    for r in rows:
        tickers.update(json.loads(r[0] or "[]"))
    return sorted(t for t in tickers if t)


def delete_documents(paths: list[str]) -> None:
    """Delete specific document rows by path, e.g. after confirming their Data Lake blob is permanently gone."""
    if not paths or not _cache_available:
        return
    with _conn() as c:
        placeholders = ",".join("?" * len(paths))
        c.execute(f"DELETE FROM documents WHERE path IN ({placeholders})", paths)  # noqa: S608
        c.commit()


def document_count() -> int:
    if not _cache_available:
        return 0
    with _conn() as c:
        return c.execute("SELECT COUNT(*) FROM documents").fetchone()[0]


# ── backup & restore ──────────────────────────────────────────────────────────
# Lets the scheduler (equities_frontend/scheduler/) push a durable copy of this
# otherwise-local-only cache to the Data Lake, and restore it on a cold start
# (e.g. after a container redeploy wipes /tmp). Bytes in, bytes out -- no
# Data Lake/avb_core import here, keeping this module Azure-agnostic.


def get_db_path() -> Path:
    """Public accessor for the local SQLite file's path."""
    return _DB_PATH


def is_cache_empty() -> bool:
    """True if there are no cached documents, companies, or portfolio metrics."""
    return document_count() == 0 and not get_all_companies() and portfolio_metrics_count() == 0


def backup_to_bytes() -> bytes | None:
    """
    Return a consistent point-in-time snapshot of the local cache as bytes.

    Uses sqlite3's online-backup API rather than a raw file read: the DB runs
    in WAL mode, so a raw read risks capturing the main file mid-checkpoint
    while committed data still sits in the separate -wal file.
    """
    if not _cache_available:
        return None
    fd, tmp_name = tempfile.mkstemp(dir=_DB_PATH.parent, suffix=".db")
    os.close(fd)
    tmp_path = Path(tmp_name)
    try:
        dest = sqlite3.connect(str(tmp_path))
        try:
            with _conn() as source:
                source.backup(dest)
        finally:
            dest.close()
        return tmp_path.read_bytes()
    except Exception as exc:  # noqa: BLE001
        _logger.warning("SQLite backup failed: %s", exc)
        return None
    finally:
        tmp_path.unlink(missing_ok=True)


def restore_from_bytes(data: bytes, *, force: bool = False) -> bool:
    """
    Restore the local cache from a previously-backed-up snapshot.

    Refuses to run unless the local cache is currently empty (or force=True),
    so a warm restart never clobbers real local data with a stale snapshot.
    """
    if not _cache_available:
        return False
    if not force and not is_cache_empty():
        _logger.info("Skipping cache restore: local cache is not empty")
        return False
    fd, tmp_name = tempfile.mkstemp(dir=_DB_PATH.parent, suffix=".db")
    os.close(fd)
    tmp_path = Path(tmp_name)
    try:
        tmp_path.write_bytes(data)
        source = sqlite3.connect(str(tmp_path))
        try:
            with _conn() as dest:
                source.backup(dest)
        finally:
            source.close()
        return True
    except Exception as exc:  # noqa: BLE001
        _logger.warning("SQLite restore failed: %s", exc)
        return False
    finally:
        tmp_path.unlink(missing_ok=True)


# ── company cache ─────────────────────────────────────────────────────────────


def upsert_companies(records: list[dict]) -> None:
    """Insert or replace company rows (keyed on ticker)."""
    if not records or not _cache_available:
        return
    with _conn() as c:
        c.executemany(
            """
            INSERT OR REPLACE INTO companies
                (ticker, display_name, region, gics_sector, doc_count,
                 has_financial_model, first_document_date, last_document_date, classified_at)
            VALUES (:ticker, :display_name, :region, :gics_sector, :doc_count,
                    :has_financial_model, :first_document_date, :last_document_date, :classified_at)
            """,
            [
                {
                    "ticker": r["ticker"],
                    "display_name": r.get("display_name") or r["ticker"],
                    "region": r.get("region") or "Unclassified",
                    "gics_sector": r.get("gics_sector") or "Unclassified",
                    "doc_count": r.get("doc_count", 0),
                    "has_financial_model": 1 if r.get("has_financial_model") else 0,
                    "first_document_date": r.get("first_document_date") or None,
                    "last_document_date": r.get("last_document_date") or None,
                    "classified_at": r.get("classified_at") or None,
                }
                for r in records
            ],
        )
        c.commit()


def get_all_companies() -> list[dict]:
    """Return all cached companies, alphabetically by ticker."""
    if not _cache_available:
        return []
    with _conn() as c:
        rows = c.execute("SELECT * FROM companies ORDER BY ticker ASC").fetchall()
    return [_company_row_to_dict(r) for r in rows]


def get_classified_tickers() -> set[str]:
    """Return the set of tickers that already have a region/sector classification."""
    if not _cache_available:
        return set()
    with _conn() as c:
        rows = c.execute("SELECT ticker FROM companies WHERE classified_at IS NOT NULL").fetchall()
    return {r[0] for r in rows}


def delete_all_companies() -> None:
    """Clear the companies cache, ahead of a full rebuild from the documents cache."""
    if not _cache_available:
        return
    with _conn() as c:
        c.execute("DELETE FROM companies")
        c.commit()


def delete_companies(tickers: list[str]) -> None:
    """Delete specific company rows by ticker, e.g. after company_dedup_model folds them into another entry."""
    if not tickers or not _cache_available:
        return
    with _conn() as c:
        placeholders = ",".join("?" * len(tickers))
        c.execute(f"DELETE FROM companies WHERE ticker IN ({placeholders})", tickers)  # noqa: S608
        c.commit()


def _company_row_to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["has_financial_model"] = bool(d["has_financial_model"])
    return d


def _record_from_azure_doc(path: str, doc: dict, ingested_at: str | None) -> dict:
    """
    Map a raw Azure AI Search document into a ``upsert_documents`` record.

    The index's ``companies`` field holds full company names and ``tickers``
    holds ticker codes (search_index_configuration.fields in configs/config.yaml)
    -- the opposite of this cache's own column names, which predate that
    schema. Flipped here, once, at the Azure-facing boundary, so every
    downstream reader (company_model.aggregate_companies etc.) can keep
    assuming "companies" = tickers, "company_names" = display names.
    """
    return {
        "path": path,
        "filename": os.path.basename(path),
        "companies": doc.get("tickers") or [],
        "company_names": doc.get("companies") or [],
        "publisher": doc.get("publisher"),
        "document_date": doc.get("documentDate"),
        "document_type": doc.get("documentType"),
        "source_type": doc.get("sourceType"),
        "ingested_at": ingested_at,
    }


_SYNC_SELECT_FIELDS = ["path", "documentDate", "publisher", "documentType", "sourceType", "companies", "tickers"]
_SYNC_PAGE_SIZE = 1000  # Azure AI Search's maximum $top per request
_SYNC_MAX_SKIP = 100_000  # Azure AI Search's hard $skip ceiling -- not configurable


def _paginate_search_paths(search_client, filter_expr: str, seen: dict[str, dict]) -> bool:
    """
    Walk every page of ``filter_expr`` via explicit ``skip``, merging distinct
    paths into ``seen``.

    Deliberately does NOT rely on plain iteration ("for r in
    search_client.search(...)") to auto-continue across pages -- confirmed by
    hand that it silently stops well short of the true result count for a
    query this size, for reasons that don't trace back to any documented
    ``azure-search-documents`` limitation. Explicit ``skip``-based paging,
    already used below for financial-model rows, is the only approach proven
    to walk the full result set reliably; every pass now uses it, not just
    the row-heavy one.

    Args:
        search_client: The Azure AI Search client to query.
        filter_expr (str): The OData filter for this pass.
        seen (dict[str, dict]): Accumulator of path -> raw document, mutated
            in place so multiple passes can share one dedup set.

    Returns:
        bool: True if this pass was truncated by Azure AI Search's ``$skip``
        ceiling before exhausting all matching rows.
    """
    skip = 0
    while skip <= _SYNC_MAX_SKIP:
        page = list(
            search_client.search(
                search_text="*",
                filter=filter_expr,
                select=_SYNC_SELECT_FIELDS,
                top=_SYNC_PAGE_SIZE,
                skip=skip,
            )
        )
        if not page:
            return False
        for r in page:
            path = r.get("path") or ""
            if path and path not in seen:
                seen[path] = dict(r)
        if len(page) < _SYNC_PAGE_SIZE:
            return False
        skip += _SYNC_PAGE_SIZE
    return True


def sync_from_azure(search_client) -> int:
    """
    Full re-sync: fetch every document from Azure AI Search (wildcard query),
    deduplicate by path, replace the local cache, and return the document count.

    Runs two passes instead of one query, because financial models expand
    into one search-index row per metric and period (see
    vectorization/extraction/extraction_controller.py) -- a handful of
    uploaded models is already enough rows to push the index's *total* row
    count past Azure AI Search's hard ``$skip`` ceiling of 100,000, even
    though the number of distinct *documents* stays small. Mixing both kinds
    of rows into one paginated walk means the row-heavy financial-model rows
    burn through most of that 100,000-row budget before the walk ever reaches
    the (far more numerous) distinct regular documents sitting behind them --
    splitting them into separate passes, each with its own independent
    ``$skip`` budget, avoids that. Only ``path`` and a few document-level
    fields are ever selected here, never the per-metric fields, so a single
    row is all any document needs to be represented. Both passes page via
    ``_paginate_search_paths`` with an explicit ``skip`` loop -- see that
    function's docstring for why plain iteration isn't trusted to auto-
    continue here.

    The first pass excludes financial-model rows
    (``sourceType ne 'financial_model' or sourceType eq null``). The
    ``or ... eq null`` half of that filter matters: nothing upstream of this
    ever set ``sourceType`` for a regular (non-financial-model) document
    until
    ``vectorization.extraction.extraction_controller.EquitiesExtractor.extract``
    started defaulting it, so every document ingested before that fix has a
    null ``sourceType`` in the index. Azure AI Search's OData ``ne`` doesn't
    match a null field (SQL-style three-valued logic), so without the
    explicit null check this pass would silently drop every one of those
    documents -- they were fully findable through plain unfiltered
    search/chat, just invisible to this sync and to Chat's "Documents only"
    filter. The second pass is the financial-model rows. If either pass is
    truncated by the ``$skip`` ceiling, the sync still returns everything
    found up to the limit (logged as a warning) instead of crashing and
    leaving the local cache exactly as stale as before.
    """
    seen: dict[str, dict] = {}
    truncated = _paginate_search_paths(search_client, "sourceType ne 'financial_model' or sourceType eq null", seen)
    truncated = _paginate_search_paths(search_client, "sourceType eq 'financial_model'", seen) or truncated

    if truncated:
        _logger.warning(
            "sync_from_azure: a pass exceeded Azure AI Search's %d $skip limit; some documents may "
            "be missing from the local cache until this is paginated differently (e.g. by date range).",
            _SYNC_MAX_SKIP,
        )

    if not seen:
        return 0

    records = [_record_from_azure_doc(path, doc, ingested_at=None) for path, doc in seen.items()]

    with _conn() as c:
        c.execute("DELETE FROM documents")
        c.commit()

    upsert_documents(records)
    return len(records)


def post_ingest_update(file_paths: list[str], search_client) -> None:
    """
    After ingesting files, query Azure for their metadata and update SQLite.
    Uses filename keyword search to find each document; falls back to storing
    path + timestamp only if the search returns nothing.
    """
    now = datetime.now(timezone.utc).isoformat()
    for path in file_paths:
        filename = os.path.basename(path)
        try:
            results = list(
                search_client.search(
                    search_text=filename,
                    select=[
                        "path",
                        "documentDate",
                        "publisher",
                        "documentType",
                        "sourceType",
                        "companies",
                        "tickers",
                    ],
                    top=10,
                )
            )
            matched = next((r for r in results if r.get("path") == path), None)
            if matched:
                upsert_documents([_record_from_azure_doc(path, matched, ingested_at=now)])
                continue
        except Exception as exc:  # noqa: BLE001
            _logger.debug("post_ingest_update metadata lookup failed for %s: %s", path, exc)
        upsert_documents(
            [
                {
                    "path": path,
                    "filename": filename,
                    "companies": [],
                    "company_names": [],
                    "ingested_at": now,
                }
            ]
        )


def _row_to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["companies"] = json.loads(d.get("companies") or "[]")
    d["company_names"] = json.loads(d.get("company_names") or "[]")
    return d


# ── portfolio metrics ────────────────────────────────────────────────────────
# Populated hourly by the scheduler from the colleague's Portfolio Dashboard's
# own silver-layer data (equities_frontend/portfolio_dashboard/), so Chat can
# ground portfolio questions in a fast local lookup instead of a live Data
# Lake read. See portfolio_dashboard/chat_context_model.py.


def upsert_portfolio_metrics(records: list[dict]) -> None:
    """Insert or replace portfolio-metrics rows (keyed on portfolio + as_of_date)."""
    if not records or not _cache_available:
        return
    with _conn() as c:
        c.executemany(
            """
            INSERT OR REPLACE INTO portfolio_metrics
                (portfolio, as_of_date, benchmark, base_currency, portfolio_return,
                 active_return, portfolio_te, portfolio_beta, holdings, risk_contributors,
                 factor_exposures, synced_at)
            VALUES (:portfolio, :as_of_date, :benchmark, :base_currency, :portfolio_return,
                    :active_return, :portfolio_te, :portfolio_beta, :holdings, :risk_contributors,
                    :factor_exposures, :synced_at)
            """,
            [
                {
                    "portfolio": r["portfolio"],
                    "as_of_date": r["as_of_date"],
                    "benchmark": r.get("benchmark"),
                    "base_currency": r.get("base_currency"),
                    "portfolio_return": r.get("portfolio_return"),
                    "active_return": r.get("active_return"),
                    "portfolio_te": r.get("portfolio_te"),
                    "portfolio_beta": r.get("portfolio_beta"),
                    "holdings": json.dumps(r.get("holdings") or []),
                    "risk_contributors": json.dumps(r.get("risk_contributors") or []),
                    "factor_exposures": json.dumps(r.get("factor_exposures") or []),
                    "synced_at": r.get("synced_at") or datetime.now(timezone.utc).isoformat(),
                }
                for r in records
            ],
        )
        c.commit()


def get_portfolio_metrics(portfolio: str, as_of_date: str | None = None) -> dict | None:
    """
    Return one portfolio's metrics row, for a specific date or the latest available.

    Args:
        portfolio: Portfolio name, as stored by the sync job.
        as_of_date: A specific ``YYYY-MM-DD`` date, or ``None`` for the most
            recent date on record for this portfolio.

    Returns:
        dict | None: The row (JSON columns already decoded), or ``None`` if
        this portfolio has no cached metrics for the requested date.
    """
    if not _cache_available:
        return None
    with _conn() as c:
        if as_of_date:
            row = c.execute(
                "SELECT * FROM portfolio_metrics WHERE portfolio = ? AND as_of_date = ?",
                (portfolio, as_of_date),
            ).fetchone()
        else:
            row = c.execute(
                "SELECT * FROM portfolio_metrics WHERE portfolio = ? ORDER BY as_of_date DESC LIMIT 1",
                (portfolio,),
            ).fetchone()
    return _portfolio_row_to_dict(row) if row else None


def get_portfolio_names() -> list[str]:
    """Return sorted distinct portfolio names with at least one cached metrics row."""
    if not _cache_available:
        return []
    with _conn() as c:
        rows = c.execute("SELECT DISTINCT portfolio FROM portfolio_metrics ORDER BY portfolio").fetchall()
    return [r[0] for r in rows]


def portfolio_metrics_count() -> int:
    if not _cache_available:
        return 0
    with _conn() as c:
        return c.execute("SELECT COUNT(*) FROM portfolio_metrics").fetchone()[0]


def _portfolio_row_to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["holdings"] = json.loads(d.get("holdings") or "[]")
    d["risk_contributors"] = json.loads(d.get("risk_contributors") or "[]")
    d["factor_exposures"] = json.loads(d.get("factor_exposures") or "[]")
    return d


# ── portfolio reports (raw) ──────────────────────────────────────────────────
# Populated once a day by the scheduler (not hourly, unlike portfolio_metrics
# above): the full report objects the Portfolio Dashboard page needs to render
# its tabs, stored as the same parquet bytes the colleague's own silver layer
# already uses (portfolio_dashboard.datalake.silver_model.report_to_parquet_bytes),
# so reconstructing them on read is a reuse of their own tested round-trip, not
# a hand-rolled serialization scheme. This module only stores/returns raw
# blobs and knows nothing about what's inside them -- see
# portfolio_dashboard/silver_cache_model.py, the only caller of these
# functions, for the serialize/deserialize side.


def upsert_portfolio_report_blobs(records: list[dict]) -> None:
    """Insert or replace raw report-blob rows (keyed on portfolio + report_type)."""
    if not records or not _cache_available:
        return
    with _conn() as c:
        c.executemany(
            """
            INSERT OR REPLACE INTO portfolio_reports_raw (portfolio, report_type, report_blob, synced_at)
            VALUES (:portfolio, :report_type, :report_blob, :synced_at)
            """,
            [
                {
                    "portfolio": r["portfolio"],
                    "report_type": r["report_type"],
                    "report_blob": r["report_blob"],
                    "synced_at": r.get("synced_at") or datetime.now(timezone.utc).isoformat(),
                }
                for r in records
            ],
        )
        c.commit()


def get_portfolio_report_blobs(portfolio: str) -> dict[str, bytes]:
    """Return ``{report_type: blob}`` for every cached raw report of one portfolio."""
    if not _cache_available:
        return {}
    with _conn() as c:
        rows = c.execute(
            "SELECT report_type, report_blob FROM portfolio_reports_raw WHERE portfolio = ?",
            (portfolio,),
        ).fetchall()
    return {row["report_type"]: row["report_blob"] for row in rows}


def get_portfolio_report_portfolios() -> list[str]:
    """Return sorted distinct portfolio names with at least one cached raw report."""
    if not _cache_available:
        return []
    with _conn() as c:
        rows = c.execute("SELECT DISTINCT portfolio FROM portfolio_reports_raw ORDER BY portfolio").fetchall()
    return [r[0] for r in rows]


def portfolio_reports_last_synced_date() -> str | None:
    """Return the UTC date (``YYYY-MM-DD``) of the most recent raw-report sync, or ``None`` if never synced."""
    if not _cache_available:
        return None
    with _conn() as c:
        row = c.execute("SELECT MAX(synced_at) FROM portfolio_reports_raw").fetchone()
    if not row or not row[0]:
        return None
    return str(row[0])[:10]


# ── portfolio summary history ────────────────────────────────────────────────
# Populated alongside portfolio_reports_raw by the same once-a-day scheduler
# sync -- the per-date summary metrics SilverStore.summary_history() computes
# (tracking error, beta, returns, market value), which the Portfolio Dashboard
# page's sidebar reads on every render, not just when a Trends tab is opened.
# See portfolio_dashboard/silver_cache_model.py for the serialize/deserialize
# side; this module only stores/returns plain rows.


def upsert_portfolio_summary_history(records: list[dict]) -> None:
    """Insert or replace summary-history rows (keyed on portfolio + as_of)."""
    if not records or not _cache_available:
        return
    with _conn() as c:
        c.executemany(
            """
            INSERT OR REPLACE INTO portfolio_summary_history (portfolio, as_of, row_json, synced_at)
            VALUES (:portfolio, :as_of, :row_json, :synced_at)
            """,
            [
                {
                    "portfolio": r["portfolio"],
                    "as_of": r["as_of"],
                    "row_json": json.dumps(r["row"]),
                    "synced_at": r.get("synced_at") or datetime.now(timezone.utc).isoformat(),
                }
                for r in records
            ],
        )
        c.commit()


def get_portfolio_summary_history(portfolio: str) -> list[dict]:
    """Return every cached summary-history row for one portfolio, ordered by ``as_of``."""
    if not _cache_available:
        return []
    with _conn() as c:
        rows = c.execute(
            "SELECT as_of, row_json FROM portfolio_summary_history WHERE portfolio = ? ORDER BY as_of",
            (portfolio,),
        ).fetchall()
    return [{"as_of": row["as_of"], "portfolio": portfolio, **json.loads(row["row_json"])} for row in rows]


def has_portfolio_summary_history(portfolio: str) -> bool:
    """True if at least one summary-history row is cached for this portfolio."""
    if not _cache_available:
        return False
    with _conn() as c:
        row = c.execute(
            "SELECT 1 FROM portfolio_summary_history WHERE portfolio = ? LIMIT 1",
            (portfolio,),
        ).fetchone()
    return row is not None


# ── company reference ────────────────────────────────────────────────────────
# An authoritative list of real company names sourced from the Portfolio
# Dashboard's own holdings data, kept fresh by the daily company-cleanup
# scheduler step (companies/company_controller.py::sync_company_reference).
# See the _COMPANY_REFERENCE_DDL comment above for why this is a separate
# table from `companies`.


def upsert_company_reference_names(names: list[str]) -> None:
    """
    Insert newly-seen reference names, unresolved (ticker/canonical_name NULL).

    ``INSERT OR IGNORE`` so re-running this for a name that's already been
    resolved never clobbers its resolution back to NULL.
    """
    if not names or not _cache_available:
        return
    with _conn() as c:
        c.executemany(
            "INSERT OR IGNORE INTO company_reference (name) VALUES (?)",
            [(name,) for name in names],
        )
        c.commit()


def get_unresolved_company_reference_names() -> list[str]:
    """Return reference names that have never been through LLM ticker/name resolution."""
    if not _cache_available:
        return []
    with _conn() as c:
        rows = c.execute("SELECT name FROM company_reference WHERE resolved_at IS NULL").fetchall()
    return [r[0] for r in rows]


def upsert_company_reference_resolutions(records: list[dict]) -> None:
    """Fill in ticker/canonical_name/resolved_at for already-inserted reference names."""
    if not records or not _cache_available:
        return
    now = datetime.now(timezone.utc).isoformat()
    with _conn() as c:
        c.executemany(
            """
            UPDATE company_reference
            SET ticker = :ticker, canonical_name = :canonical_name, resolved_at = :resolved_at
            WHERE name = :name
            """,
            [
                {
                    "name": r["name"],
                    "ticker": r.get("ticker"),
                    "canonical_name": r.get("canonical_name"),
                    "resolved_at": r.get("resolved_at") or now,
                }
                for r in records
            ],
        )
        c.commit()


def get_all_company_reference() -> list[dict]:
    """Return every reference row, resolved or not."""
    if not _cache_available:
        return []
    with _conn() as c:
        rows = c.execute("SELECT * FROM company_reference ORDER BY name").fetchall()
    return [dict(r) for r in rows]


# ── company merge suggestions ────────────────────────────────────────────────
# LLM-proposed merges the daily cleanup pass couldn't confirm against
# company_reference -- held for manual approval on the Logs page instead of
# applying automatically (companies/company_cleanup_model.py::gate_merge_groups).


def add_company_merge_suggestion(tickers: list[str], canonical_name: str | None, reason: str | None) -> None:
    """Queue one proposed merge for manual review."""
    if not _cache_available:
        return
    with _conn() as c:
        c.execute(
            "INSERT INTO company_merge_suggestions (tickers, canonical_name, reason, status, created_at) "
            "VALUES (?, ?, ?, 'pending', ?)",
            (json.dumps(tickers), canonical_name, reason, datetime.now(timezone.utc).isoformat()),
        )
        c.commit()


def get_pending_company_merge_suggestions() -> list[dict]:
    """Return every merge suggestion still awaiting review, oldest first."""
    if not _cache_available:
        return []
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM company_merge_suggestions WHERE status = 'pending' ORDER BY created_at"
        ).fetchall()
    return [{**dict(r), "tickers": json.loads(r["tickers"])} for r in rows]


def set_company_merge_suggestion_status(suggestion_id: int, status: str) -> None:
    """Mark a merge suggestion 'approved' or 'rejected'."""
    if not _cache_available:
        return
    with _conn() as c:
        c.execute("UPDATE company_merge_suggestions SET status = ? WHERE id = ?", (status, suggestion_id))
        c.commit()


# ── company cleanup state ────────────────────────────────────────────────────


def company_cleanup_synced_today() -> bool:
    """True if the daily LLM company-cleanup pass has already completed today (UTC)."""
    if not _cache_available:
        return False
    today = datetime.now(timezone.utc).date().isoformat()
    with _conn() as c:
        row = c.execute("SELECT last_run_date FROM company_cleanup_state WHERE id = 1").fetchone()
    return bool(row and row[0] == today)


def record_company_cleanup_run() -> None:
    """Mark the daily LLM company-cleanup pass as completed today (UTC)."""
    if not _cache_available:
        return
    today = datetime.now(timezone.utc).date().isoformat()
    with _conn() as c:
        c.execute(
            "INSERT INTO company_cleanup_state (id, last_run_date) VALUES (1, ?) "
            "ON CONFLICT(id) DO UPDATE SET last_run_date = excluded.last_run_date",
            (today,),
        )
        c.commit()


# ── factor screen ─────────────────────────────────────────────────────────────
# A periodically-uploaded Bloomberg factor/crowding screen (see
# companies/factor_screen_model.py and CompanyController.upload_factor_screen)
# -- one dated snapshot per upload, kept rather than overwritten.

_FACTOR_SCREEN_COLUMNS = (
    "symbol",
    "country",
    "bloomberg_identifier",
    "sedol",
    "overall_factor_pct",
    "quality_factor_pct",
    "low_vol_factor_pct",
    "growth_factor_pct",
    "momentum_factor_pct",
    "valuation_factor_pct",
    "crowding_factor",
)


def upsert_factor_screen_rows(as_of_date: str, rows: list[dict]) -> None:
    """Insert or replace one dated snapshot's factor-screen rows (keyed on as_of_date + name)."""
    if not rows or not _cache_available:
        return
    now = datetime.now(timezone.utc).isoformat()
    with _conn() as c:
        c.executemany(
            f"""
            INSERT OR REPLACE INTO factor_screen
                (as_of_date, name, {", ".join(_FACTOR_SCREEN_COLUMNS)}, uploaded_at)
            VALUES
                (:as_of_date, :name, {", ".join(f":{col}" for col in _FACTOR_SCREEN_COLUMNS)}, :uploaded_at)
            """,  # noqa: S608
            [
                {
                    "as_of_date": as_of_date,
                    "name": row["name"],
                    "uploaded_at": now,
                    **{col: row.get(col) for col in _FACTOR_SCREEN_COLUMNS},
                }
                for row in rows
            ],
        )
        c.commit()


def get_latest_factor_screen_date() -> str | None:
    """Return the most recent as_of_date with any cached factor-screen rows, or None if never uploaded."""
    if not _cache_available:
        return None
    with _conn() as c:
        row = c.execute("SELECT MAX(as_of_date) FROM factor_screen").fetchone()
    return row[0] if row and row[0] else None


def get_factor_screen_rows(as_of_date: str) -> list[dict]:
    """Return every cached factor-screen row for one dated snapshot."""
    if not _cache_available:
        return []
    with _conn() as c:
        rows = c.execute("SELECT * FROM factor_screen WHERE as_of_date = ?", (as_of_date,)).fetchall()
    return [dict(r) for r in rows]
