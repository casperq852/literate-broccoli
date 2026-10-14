"""
Background hourly loop: restore the SQLite cache on a cold start, then every
tick classify newly-seen companies (region/GICS sector), merge any company
entries that turned out to be the same real company under different tickers
(``companies.company_controller.CompanyController.deduplicate_companies``),
back the cache up to the Data Lake, and back the rotating log file up too (so
it's readable without Azure Portal/CLI log-stream access -- see
logs/logs_view.py). The
Portfolio Dashboard's own data sync runs once immediately on every container
start (so a fresh deploy never sits with an empty cache until the next
scheduled window), then is checked every tick but only actually runs once a
day after that (see ``_run_portfolio_sync``) -- it's the one step here that
does an expensive recursive Data Lake directory listing that grows with the
size of the silver-layer archive, so it deliberately doesn't share the hourly
cadence the other steps use. A daily LLM company-cleanup pass runs 30 minutes
after the portfolio sync's own target time (``_run_company_cleanup``):
refreshes an authoritative company reference from Portfolio Dashboard
holdings, reviews the company list for duplicates an LLM can catch that the
rapidfuzz-only dedup above can't, and backfills a bounded batch of documents
with no company tagged at all.

Started once from main.py -- the only place in the codebase that runs exactly
once per container lifetime (app.py/version_selector.py re-execute per
Streamlit rerun/session). Runs as a daemon thread in the same process as the
Streamlit subprocess main.py launches; both share the local filesystem, so
both open equities_frontend's local SQLite cache concurrently -- WAL mode plus
cache.py's own lock-retry loop already handles that.

No cross-instance coordination if the deployment ever scales to multiple
containers: every instance restores/classifies/backs up independently, every
hour. This is safe because upsert_companies is idempotent (keyed on ticker)
and sync_companies only calls the LLM for tickers that instance's own cache
doesn't yet have a classified_at for, so duplicate work is bounded to
genuinely-new companies discovered within the hour -- and a "only one instance
runs" guard would leave the others permanently stale on classification and on
the fuzzy-match coverage universe (see company_resolution_controller.py),
which is worse than some redundant LLM calls.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime
from pathlib import PurePosixPath
from typing import Any
from zoneinfo import ZoneInfo

from portfolio_dashboard import silver_cache_model
from portfolio_dashboard.chat_context_model import summarize_portfolio
from portfolio_dashboard.datalake.medallion_controller import MedallionRefresher
from portfolio_dashboard.datalake.silver_controller import SilverStore

from companies.company_controller import CompanyController
from database import cache
from helpers import LOG_FILE_PATH, download_from_datalake, get_datalake_manager, upload_to_datalake
from scheduler.scheduler_model import (
    TICK_INTERVAL_SECONDS,
    load_app_config,
    load_equities_config,
    resolve_datalake_account,
)

logger = logging.getLogger(__name__)

_shutdown = threading.Event()

# avb_core's DataLakeManager.upload_file(overwrite=True) archives the previous
# file into a sibling archive/ folder on every call instead of truly
# overwriting it -- with an hourly backup tick that folder grows by one file
# forever unless pruned. Keep roughly a week's worth as a rollback safety net.
_MAX_BACKUP_ARCHIVE_VERSIONS = 168

# Local time (not UTC -- new Bloomberg PORT exports land ~07:00 desk-local,
# so the target has to track local time, including DST, or it'd drift an hour
# off twice a year) the Portfolio Dashboard's own data sync is allowed to run
# at, once per calendar day. Checking more often than daily was exactly the
# "too many requests" load that made the underlying recursive Data Lake
# listing time out in production, so this deliberately doesn't share the
# other steps' hourly cadence.
_PORTFOLIO_SYNC_TZ = ZoneInfo("Europe/Amsterdam")
_PORTFOLIO_SYNC_HOUR = 7
_PORTFOLIO_SYNC_MINUTE = 30

# Local time the daily LLM company-cleanup pass is allowed to run at, once
# per calendar day -- 30 minutes after the portfolio sync's own target time,
# so CompanyController.sync_company_reference has the freshest possible
# portfolio holdings data to resolve against, not yesterday's.
_COMPANY_CLEANUP_TZ = ZoneInfo("Europe/Amsterdam")
_COMPANY_CLEANUP_HOUR = 8
_COMPANY_CLEANUP_MINUTE = 0


def start_scheduler_thread() -> threading.Thread:
    """Start the hourly loop as a daemon thread. Safe to call exactly once, from main.py."""
    thread = threading.Thread(target=run_forever, name="equities-scheduler", daemon=True)
    thread.start()
    return thread


def stop_scheduler() -> None:
    """Signal the loop to exit promptly instead of waiting out its current sleep."""
    _shutdown.set()


def run_forever() -> None:
    """Restore the cache once if empty, then classify + back up every ``TICK_INTERVAL_SECONDS``."""
    try:
        app_config = load_app_config()
    except Exception:  # noqa: BLE001
        logger.exception("Scheduler could not load app_config.yaml; scheduler will not run")
        return

    equities_cfg = app_config["equities"]
    account_name = resolve_datalake_account(app_config)

    _restore_cache_if_empty(account_name, equities_cfg)
    # Run once immediately on every container start, regardless of the daily
    # gate below -- otherwise a fresh deploy sits with an empty portfolio
    # cache until the next 07:30 Amsterdam window, however far off that is.
    # force=True also lets this dedupe naturally with the loop's first
    # iteration: once this succeeds, synced_today() makes that call a no-op.
    _run_portfolio_sync(force=True)

    while not _shutdown.is_set():
        _run_classification(equities_cfg)
        _run_company_dedup(equities_cfg)
        _run_portfolio_sync()
        _run_company_cleanup(equities_cfg)
        _run_backup(account_name, equities_cfg)
        _run_log_backup(account_name, equities_cfg)
        _shutdown.wait(timeout=TICK_INTERVAL_SECONDS)


def _restore_cache_if_empty(account_name: str, equities_cfg: dict[str, Any]) -> None:
    """Pull the latest cache backup down on a cold start. Never fatal -- a first-ever deploy has no backup yet."""
    if not cache.is_cache_empty():
        return
    try:
        data = download_from_datalake(
            account_name=account_name,
            file_path=equities_cfg["cache_backup_path"],
            _filesystem_name=equities_cfg["data_layer"],
        )
        restored = cache.restore_from_bytes(data)
        logger.info("Cache restore from Data Lake backup: %s", "ok" if restored else "skipped")
    except Exception as exc:  # noqa: BLE001
        logger.info("No cache backup restored (expected on first deploy): %s", exc)


def _run_classification(equities_cfg: dict[str, Any]) -> None:
    """One classification pass: find companies without a region/GICS sector yet, classify them."""
    try:
        newly_classified = CompanyController(equities_cfg["index_name"]).sync_companies(equities_cfg)
        logger.info("Scheduler classification tick: %d newly classified", newly_classified)
    except Exception:  # noqa: BLE001
        logger.exception("Scheduler classification tick failed")


def _run_company_dedup(equities_cfg: dict[str, Any]) -> None:
    """
    One company-dedup pass: merge cached company entries that turned out to
    be the same real company under different tickers (see
    ``CompanyController.deduplicate_companies``). Runs right after
    classification, since classification is what fills in the region/GICS
    sector a merge needs to decide which side of a merge to keep.
    """
    try:
        merged = CompanyController(equities_cfg["index_name"]).deduplicate_companies()
        logger.info("Scheduler company dedup tick: %d alias ticker(s) merged", merged)
    except Exception:  # noqa: BLE001
        logger.exception("Scheduler company dedup tick failed")


def run_company_dedup_now() -> int:
    """
    Public, on-demand company-dedup pass -- lets failures raise instead of
    logging them, for a UI button to report on directly. Used by the Logs
    page's manual "Merge duplicate companies now" button, e.g. right after
    noticing a split like the one this whole mechanism exists to fix,
    without waiting for the next hourly tick.

    Resolves its own config the same standalone way the scheduler thread
    does (``scheduler_model.load_equities_config``), not
    ``helpers.equities_config()``'s ``st.session_state`` lookup -- so this
    stays callable from a plain background thread too, not just from inside
    a live Streamlit session.

    Returns:
        int: Number of alias tickers merged away.

    Raises:
        Exception: Whatever the underlying cache read/write raises --
            deliberately not swallowed here, unlike the scheduled tick, so
            the caller can show exactly what went wrong.
    """
    equities_cfg = load_equities_config()
    return CompanyController(equities_cfg["index_name"]).deduplicate_companies()


def _run_company_cleanup(equities_cfg: dict[str, Any], *, force: bool = False) -> None:
    """
    One daily LLM-driven company cleanup pass, gated to once a day (see
    ``_COMPANY_CLEANUP_HOUR``/``_COMPANY_CLEANUP_MINUTE``): refreshes the
    authoritative company reference from Portfolio Dashboard holdings,
    reviews the company list for likely duplicates an LLM can catch that
    rapidfuzz alone can't (see ``CompanyController.run_llm_cleanup``), and
    re-ingests a bounded batch of documents that have no company tagged at
    all (see ``CompanyController.backfill_untagged_documents``).

    Args:
        force: Skip the once-a-day gate. Used by ``run_once()``'s manual
            testing entry point -- the regular hourly loop always respects it.
    """
    if not force:
        now_local = datetime.now(_COMPANY_CLEANUP_TZ)
        target_today = now_local.replace(
            hour=_COMPANY_CLEANUP_HOUR, minute=_COMPANY_CLEANUP_MINUTE, second=0, microsecond=0
        )
        if now_local < target_today:
            return
        if cache.company_cleanup_synced_today():
            return
    try:
        controller = CompanyController(equities_cfg["index_name"])
        result = controller.run_llm_cleanup(equities_cfg)
        backfilled = controller.backfill_untagged_documents(equities_cfg)
        cache.record_company_cleanup_run()
        logger.info(
            "Scheduler company cleanup tick: %d name(s) resolved, %d merged, %d queued for review, "
            "%d document(s) backfilled",
            result["reference_resolved"],
            result["auto_merged"],
            result["queued_for_review"],
            backfilled,
        )
    except Exception:  # noqa: BLE001
        logger.exception("Scheduler company cleanup tick failed")


def run_company_cleanup_now() -> dict[str, int]:
    """
    Public, on-demand company cleanup pass -- bypasses the once-a-day gate
    and lets failures raise instead of logging them. Used by the Logs page's
    manual "Run company cleanup now" button.

    Returns:
        dict[str, int]: ``{"reference_resolved", "auto_merged",
        "queued_for_review", "backfilled"}``.

    Raises:
        Exception: Whatever the underlying LLM/cache call raises --
            deliberately not swallowed here, unlike the scheduled tick, so
            the caller can show exactly what went wrong.
    """
    equities_cfg = load_equities_config()
    controller = CompanyController(equities_cfg["index_name"])
    result = controller.run_llm_cleanup(equities_cfg)
    backfilled = controller.backfill_untagged_documents(equities_cfg)
    cache.record_company_cleanup_run()
    return {**result, "backfilled": backfilled}


def _run_portfolio_sync(*, force: bool = False) -> None:
    """
    One portfolio-data sync pass, gated to once a day: refresh the colleague's
    Portfolio Dashboard medallion pipeline (their real, unmodified
    landing->bronze->silver refresh), summarize each portfolio's latest
    reports into this app's own SQLite cache for Chat to query directly (see
    chat_context_model.py), and persist the full, unsummarized reports via
    silver_cache_model so the Portfolio Dashboard page
    (portfolio_dashboard_view.py) can read them locally instead of
    independently repeating this same expensive recursive Data Lake listing
    on every page load.

    Both ``MedallionRefresher.refresh()`` (its own landing-vs-bronze diff) and
    ``SilverStore.load_reports_by_portfolio()`` (the silver-layer listing) walk
    the whole ``EQT/portfolio`` tree -- with hundreds of files and growing,
    that's expensive enough to time out (confirmed in production: too many
    requests, not a network/firewall issue), so this only actually runs once
    per calendar day, at ``_PORTFOLIO_SYNC_HOUR``:``_PORTFOLIO_SYNC_MINUTE``
    local time (new PORT exports land ~07:00 desk-local). Checked every
    hourly tick, fires on the first tick at or after that local time each
    day -- since ticks land on whatever minute the container happened to
    start, not on the hour, this can run up to ~1 hour late relative to the
    target, never early.

    Failures here are logged and swallowed, same as every other scheduled
    step -- see ``run_portfolio_sync_now()`` for the manual-trigger entry
    point that lets them raise instead, for a UI button to report on directly.

    Args:
        force: Skip the once-a-day gate. Used on every container start and by
            ``run_once()``'s manual testing entry point -- the regular hourly
            loop always respects it.
    """
    if not force:
        now_local = datetime.now(_PORTFOLIO_SYNC_TZ)
        target_today = now_local.replace(
            hour=_PORTFOLIO_SYNC_HOUR, minute=_PORTFOLIO_SYNC_MINUTE, second=0, microsecond=0
        )
        if now_local < target_today:
            return
        if silver_cache_model.synced_today():
            return
    try:
        synced = _do_portfolio_sync()
        logger.info("Scheduler portfolio sync tick: %d portfolios synced", synced)
    except Exception:  # noqa: BLE001
        logger.exception("Scheduler portfolio sync tick failed")


def _do_portfolio_sync() -> int:
    """
    The actual portfolio-sync work, raising on failure instead of catching it.

    Shared by ``_run_portfolio_sync`` (the gated, exception-swallowing
    scheduled tick) and ``run_portfolio_sync_now`` (the ungated, exception-
    raising manual trigger) so the two entry points can't drift onto
    different logic.

    Also caches ``store.summary_history(portfolio)`` for every portfolio --
    the sidebar's report-date picker's data source, rendered on every
    Portfolio Dashboard page load, not just when a Trends tab is opened.
    Reuses the same ``store`` (and therefore its already-warmed
    ``_agg_bytes``) the reports-by-portfolio read above just populated, so
    this is cheap: no repeat download for components already fetched.

    Needs no equities_cfg/account_name -- MedallionRefresher/SilverStore
    resolve their own Data Lake config independently via
    portfolio_dashboard.config.load_app_config(), unrelated to this app's own
    equities config.

    Returns:
        int: Number of portfolios synced.
    """
    MedallionRefresher().refresh()
    store = SilverStore()
    reports_by_portfolio = store.load_reports_by_portfolio()
    silver_cache_model.store_reports(reports_by_portfolio)
    synced = 0
    for portfolio, reports in reports_by_portfolio.items():
        record = summarize_portfolio(portfolio, reports)
        if record:
            cache.upsert_portfolio_metrics([record])
            synced += 1
        silver_cache_model.store_summary_history(portfolio, store.summary_history(portfolio))
    return synced


def run_portfolio_sync_now() -> int:
    """
    Public, on-demand portfolio sync -- bypasses the once-a-day gate and lets
    failures raise instead of logging them. Used by the Logs page's manual
    "Run portfolio sync now" button, e.g. to populate the cache for the first
    time without waiting for the next scheduled window.

    Returns:
        int: Number of portfolios synced.

    Raises:
        Exception: Whatever the underlying Data Lake refresh/read raises --
            deliberately not swallowed here, unlike the scheduled tick, so
            the caller can show exactly what went wrong.
    """
    return _do_portfolio_sync()


def _run_backup(account_name: str, equities_cfg: dict[str, Any]) -> None:
    """Push a fresh SQLite snapshot to the Data Lake. Runs regardless of the classification step's outcome."""
    try:
        data = cache.backup_to_bytes()
        if data is None:
            logger.warning("Scheduler backup tick: cache unavailable, nothing to back up")
            return
        upload_to_datalake(
            account_name=account_name,
            file_path=equities_cfg["cache_backup_path"],
            data=data,
            _filesystem_name=equities_cfg["data_layer"],
        )
        logger.info("Scheduler backup tick: uploaded %d bytes", len(data))
        _prune_backup_archives(account_name, equities_cfg["data_layer"], equities_cfg["cache_backup_path"], ".db")
    except Exception:  # noqa: BLE001
        logger.exception("Scheduler backup tick failed")


def _run_log_backup(account_name: str, equities_cfg: dict[str, Any]) -> None:
    """
    Upload the current rotating log file to the Data Lake, so it's readable
    without Azure Portal/CLI log-stream access (see logs/logs_view.py, which
    reads the same file locally). Skipped, not fatal, before the file exists
    -- e.g. the very first tick, which can race the Streamlit subprocess's own
    file-logging setup in version_selector.py.
    """
    log_backup_path = equities_cfg.get("log_backup_path")
    if not log_backup_path or not LOG_FILE_PATH.exists():
        return
    try:
        data = LOG_FILE_PATH.read_bytes()
        upload_to_datalake(
            account_name=account_name,
            file_path=log_backup_path,
            data=data,
            _filesystem_name=equities_cfg["data_layer"],
        )
        logger.info("Scheduler log backup tick: uploaded %d bytes", len(data))
        _prune_backup_archives(account_name, equities_cfg["data_layer"], log_backup_path, ".log")
    except Exception:  # noqa: BLE001
        logger.exception("Scheduler log backup tick failed")


def _prune_backup_archives(account_name: str, filesystem_name: str, backup_path: str, extension: str) -> None:
    """
    Delete archived versions of one backup file beyond ``_MAX_BACKUP_ARCHIVE_VERSIONS``.

    Mirrors ``chat/session_store.py``'s ``_prune_old`` for chat sessions:
    list the archive directory, keep the newest ``_MAX_BACKUP_ARCHIVE_VERSIONS``
    entries, delete the rest. Never fatal -- a pruning failure (e.g. an
    unexpected ``DataLakeManager`` API shape, or a listing permission issue)
    should not fail the backup tick that already succeeded above.

    Args:
        backup_path: The live file's Data Lake path (e.g. ``cache_backup_path``
            or ``log_backup_path``) -- its sibling ``archive/`` directory is
            what avb_core's ``upload_file(overwrite=True)`` actually grows.
        extension: Only archived files ending in this extension are counted --
            keeps this call from pruning a different backup's archives that
            happen to share the same directory.
    """
    archive_dir = str(PurePosixPath(backup_path).parent / "archive")
    try:
        manager = get_datalake_manager(account_name=account_name)
        entries: list[tuple[str, str]] = []
        for path_props in manager.list_paths(filesystem_name=filesystem_name, path=archive_dir, recursive=False):
            if getattr(path_props, "is_directory", False):
                continue
            name = path_props.name
            if not name.endswith(extension):
                continue
            modified = getattr(path_props, "last_modified", None)
            entries.append((str(modified) if modified is not None else "", name))
        if len(entries) <= _MAX_BACKUP_ARCHIVE_VERSIONS:
            return
        entries.sort(key=lambda e: e[0], reverse=True)
        for _modified, name in entries[_MAX_BACKUP_ARCHIVE_VERSIONS:]:
            manager.delete_file(filesystem_name, name)
        logger.info(
            "Scheduler backup tick: pruned %d old %s archive(s) in %s, kept %d",
            len(entries) - _MAX_BACKUP_ARCHIVE_VERSIONS,
            extension,
            archive_dir,
            _MAX_BACKUP_ARCHIVE_VERSIONS,
        )
    except Exception as exc:  # noqa: BLE001
        logger.info("Could not prune backup archives in %s: %s", archive_dir, exc)


def run_once() -> None:
    """
    Manual test hook: run one restore/classify/backup tick immediately, without
    the sleep loop. Forces the portfolio sync regardless of the once-a-day
    gate (see ``_run_portfolio_sync``) -- a manual test run should actually
    run, not silently no-op because it isn't yet the scheduled local time.
    """
    app_config = load_app_config()
    equities_cfg = app_config["equities"]
    account_name = resolve_datalake_account(app_config)
    _restore_cache_if_empty(account_name, equities_cfg)
    _run_classification(equities_cfg)
    _run_company_dedup(equities_cfg)
    _run_portfolio_sync(force=True)
    _run_company_cleanup(equities_cfg, force=True)
    _run_backup(account_name, equities_cfg)
    _run_log_backup(account_name, equities_cfg)
