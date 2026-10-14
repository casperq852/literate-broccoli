"""Controller for the Logs page: wires the on-disk log file and in-memory capture to the view."""

from __future__ import annotations

from collections.abc import Callable

from logs import logs_model as model
from scheduler.scheduler_controller import run_company_cleanup_now as _run_company_cleanup_now
from scheduler.scheduler_controller import run_company_dedup_now as _run_company_dedup_now
from scheduler.scheduler_controller import run_portfolio_sync_now as _run_portfolio_sync_now

from companies import company_dedup_model as dedup_model
from companies.company_controller import CompanyController
from database.cache import (
    delete_documents,
    get_all_companies,
    get_all_documents,
    get_pending_company_merge_suggestions,
    set_company_merge_suggestion_status,
)
from helpers import LOG_FILE_PATH, equities_config, get_captured_logs
from index.index_controller import IndexController
from upload.upload_controller import UploadController


class LogsController:
    """Reads the rotating log file and the in-memory capture buffer for display."""

    @property
    def log_file_path(self) -> str:
        """Absolute path to the on-disk rotating log file, for display."""
        return str(LOG_FILE_PATH)

    @property
    def log_file_exists(self) -> bool:
        return LOG_FILE_PATH.exists()

    def read_file_lines(self) -> list[str]:
        """Return every line currently in the on-disk rotating log file, oldest first."""
        if not LOG_FILE_PATH.exists():
            return []
        text = LOG_FILE_PATH.read_text(encoding="utf-8", errors="replace")
        return model.parse_lines(text)

    def read_captured_lines(self) -> list[str]:
        """Return the in-memory captured log lines (this process only), oldest first."""
        return get_captured_logs()

    def file_bytes(self) -> bytes:
        """Return the full on-disk log file's raw bytes, for the download button."""
        if not LOG_FILE_PATH.exists():
            return b""
        return LOG_FILE_PATH.read_bytes()

    def run_portfolio_sync_now(self) -> tuple[bool, str]:
        """
        Manually trigger the Portfolio Dashboard's data sync right now,
        bypassing its once-a-day schedule (see
        scheduler/scheduler_controller.py) -- for bootstrapping the local
        cache the first time, without waiting for the next scheduled
        07:30 Amsterdam-time tick.

        Returns:
            tuple[bool, str]: (success, message) for the view to display.
        """
        try:
            synced = _run_portfolio_sync_now()
        except Exception as exc:  # noqa: BLE001
            return False, f"Portfolio sync failed: {exc}"
        return True, f"Synced {synced} portfolio(s) into the local cache."

    def run_company_dedup_now(self) -> tuple[bool, str]:
        """
        Manually trigger the company-dedup pass right now (see
        ``companies.company_controller.CompanyController.deduplicate_companies``),
        merging any cached companies that turned out to be the same real
        company under different tickers, without waiting for the next
        hourly tick.

        Returns:
            tuple[bool, str]: (success, message) for the view to display.
        """
        try:
            merged = _run_company_dedup_now()
        except Exception as exc:  # noqa: BLE001
            return False, f"Company dedup failed: {exc}"
        if merged:
            return True, f"Merged {merged} alias ticker(s) into their canonical company."
        return True, "No duplicate companies found."

    def run_company_cleanup_now(self) -> tuple[bool, str]:
        """
        Manually trigger the daily LLM company-cleanup pass right now (see
        ``scheduler.scheduler_controller.run_company_cleanup_now``):
        refreshes the authoritative company reference from Portfolio
        Dashboard holdings, reviews the company list for LLM-caught
        duplicates (auto-applying only ones confirmed against that
        reference, queuing the rest for review below), and backfills a
        bounded batch of documents with no company tagged at all.

        Returns:
            tuple[bool, str]: (success, message) for the view to display.
        """
        try:
            result = _run_company_cleanup_now()
        except Exception as exc:  # noqa: BLE001
            return False, f"Company cleanup failed: {exc}"
        return True, (
            f"Resolved {result['reference_resolved']} reference name(s), merged {result['auto_merged']} "
            f"automatically, queued {result['queued_for_review']} for review, backfilled "
            f"{result['backfilled']} document(s)."
        )

    def list_pending_merge_suggestions(self) -> list[dict]:
        """Return every LLM-proposed company merge still awaiting manual review."""
        return get_pending_company_merge_suggestions()

    def approve_merge_suggestion(self, suggestion_id: int, tickers: list[str]) -> tuple[bool, str]:
        """
        Approve a queued merge suggestion.

        Executes it through the exact same
        ``CompanyController.deduplicate_companies`` path the cleanup pass's
        own auto-apply case uses (via
        ``company_dedup_model.groups_from_tickers``) -- there is exactly one
        code path that ever actually merges companies, whether the merge was
        auto-applied or human-approved.

        Args:
            suggestion_id (int): The suggestion's row id.
            tickers (list[str]): The tickers this suggestion proposed merging.

        Returns:
            tuple[bool, str]: (success, message) for the view to display.
        """
        try:
            config = equities_config()
            merge_groups = dedup_model.groups_from_tickers([tickers], get_all_companies())
            if not merge_groups:
                return False, "Could not apply -- one or more of these tickers no longer exists."
            merged = CompanyController(config["index_name"]).deduplicate_companies(merge_groups=merge_groups)
            set_company_merge_suggestion_status(suggestion_id, "approved")
        except Exception as exc:  # noqa: BLE001
            return False, f"Approve failed: {exc}"
        return True, f"Merged {merged} ticker(s)."

    def reject_merge_suggestion(self, suggestion_id: int) -> None:
        """Mark a queued merge suggestion rejected, without changing any company data."""
        set_company_merge_suggestion_status(suggestion_id, "rejected")

    def reindex_all_documents(self, on_progress: Callable[[str, int, int], None] | None = None) -> dict[str, int]:
        """
        Re-ingest every currently-indexed document through today's ingestion
        methodology -- extraction, LLM enrichment, and company resolution
        (``vectorization.company_resolution.resolve_companies``) -- so
        documents indexed before that methodology existed (or before a given
        fix to it landed) pick up correct companies/tickers.

        Syncs the local document cache from Azure first, so the path list
        this re-ingests is the index's actual current contents rather than a
        possibly-stale local cache, then delegates to the same
        ``UploadController.ingest_documents`` the Upload and Bulk Upload
        pages already use for path-based ingestion into the existing index
        -- this is a wider run of exactly that, not a separate mechanism.
        Newly resolved companies are classified and cached the same way an
        ordinary upload's are (``ingestion_model.ingest_paths``'s
        post-ingest cache refresh); run "Merge duplicate companies now"
        afterwards to fold together any tickers this newly reveals are the
        same company under a different spelling than what was already cached.

        Args:
            on_progress: Optional progress callback, forwarded to
                ``UploadController.ingest_documents`` -- fired once per file
                as it completes.

        Returns:
            dict[str, int]: Mapping of path -> number of segments ingested,
            for every currently-indexed document (empty if the index has
            none). A value of 0 means that file yielded nothing this time.
        """
        config = equities_config()
        IndexController(config["index_name"]).sync_from_azure()
        paths = [doc["path"] for doc in get_all_documents() if doc.get("path")]
        if not paths:
            return {}
        return UploadController(config).ingest_documents(paths, on_progress=on_progress)

    def restore_missing_from_archive(
        self, candidate_paths: list[str], on_progress: Callable[[str, int, int], None] | None = None
    ) -> dict[str, int]:
        """
        Recover documents whose live Data Lake blob is gone from a sibling
        ``archive/`` folder, then re-ingest whatever was recovered (see
        ``UploadController.find_and_upload_archived_versions`` for why the
        archive folder exists and how the highest version is chosen).

        Meant as a targeted follow-up to ``reindex_all_documents``: pass it
        that call's "yielded no content" paths (a 0-segment result there can
        have several causes, of which a deleted live blob is only one -- so
        this re-checks each one rather than assuming) to recover whichever
        of them turn out to have an archived version, and re-ingest those
        through the current methodology, same as a normal upload would.

        Args:
            candidate_paths (list[str]): Paths to check for a recoverable
                archived version.
            on_progress (Callable[[str, int, int], None] | None): Optional
                progress callback, forwarded to
                ``UploadController.find_and_upload_archived_versions`` --
                fired once per candidate as it finishes being checked.

        Returns:
            dict[str, int]: original live_path -> number of segments
            ingested for whichever candidates had a recoverable archived
            version (candidates with none are simply absent from the
            result, not present with a 0).
        """
        config = equities_config()
        upload_controller = UploadController(config)
        recovered = upload_controller.find_and_upload_archived_versions(candidate_paths, on_progress=on_progress)
        if not recovered:
            return {}
        ingest_results = upload_controller.ingest_documents(list(recovered.values()))
        return {live_path: ingest_results.get(dest_path, 0) for live_path, dest_path in recovered.items()}

    def remove_ghost_documents(
        self, candidate_paths: list[str], on_progress: Callable[[str, int, int], None] | None = None
    ) -> list[str]:
        """
        Remove permanently-unrecoverable documents (see
        ``UploadController.find_ghost_documents``) from the *local* cache
        only, so they stop cluttering the Index Browser/Companies page while
        you track down and manually re-upload real replacements.

        This is deliberately local-only, and deliberately not a content-loss
        risk: it never touches Azure AI Search, so each document's existing,
        already-indexed content stays fully retrievable in Chat exactly as
        before -- only its row in this app's own document list disappears.
        The trade-off is that the removal isn't permanent: syncing the Index
        Browser from Azure again (``IndexController.sync_from_azure``)
        rebuilds the local cache from Azure AI Search from scratch, which
        still has the stale entry, so it will reappear. Deleting the
        underlying Azure AI Search entry itself isn't implemented yet --
        this app has no existing pattern for that, and doing it safely needs
        the index's key field name, which isn't visible from here.

        Args:
            candidate_paths (list[str]): Paths to check -- typically the
                "yielded no content" set from a prior ``ingest_documents``
                run.
            on_progress (Callable[[str, int, int], None] | None): Optional
                progress callback, forwarded to
                ``UploadController.find_ghost_documents`` -- fired once per
                candidate as it finishes being checked.

        Returns:
            list[str]: The paths confirmed permanently gone and removed
            from the local cache.
        """
        config = equities_config()
        ghosts = UploadController(config).find_ghost_documents(candidate_paths, on_progress=on_progress)
        if ghosts:
            delete_documents(ghosts)
        return ghosts
