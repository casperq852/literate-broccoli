"""Controller for the upload / bulk-upload topic: Data Lake write + search-index ingestion wiring."""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import PurePosixPath
from typing import Any

from azure.core.exceptions import ResourceNotFoundError

from helpers import datalake_account, download_from_datalake, get_datalake_manager, upload_to_datalake
from upload import archive_restore_model as archive_model
from upload import ingestion_model
from upload import upload_model as model

logger = logging.getLogger(__name__)


class UploadController:
    """Wires uploaded files into the Data Lake and the search index for one deployment."""

    def __init__(self, config: dict[str, Any]) -> None:
        self._config = config

    @property
    def allowed_extensions(self) -> list[str]:
        """File extensions accepted for upload (e.g. [".pdf", ".xlsx"])."""
        return self._config["allowed_extensions"]

    def upload_document(self, filename: str, data: bytes) -> str:
        """
        Write one file's bytes to the Data Lake.

        Args:
            filename (str): The uploaded file's name.
            data (bytes): The file's raw bytes.

        Returns:
            str: The Data Lake destination path the file was written to.
        """
        dest_path = model.build_destination_path(self._config["index_path"], filename)
        upload_to_datalake(account_name=datalake_account(), file_path=dest_path, data=data)
        return dest_path

    def ingest_documents(
        self,
        paths: list[str],
        on_progress: Callable[[str, int, int], None] | None = None,
        overwrite: bool = False,
    ) -> dict[str, int]:
        """
        Ingest one or more previously-uploaded Data Lake paths into the search index.

        Args:
            paths (list[str]): Data Lake paths to ingest.
            on_progress (Callable[[str, int, int], None] | None): Optional
                progress callback, fired as each file completes -- see
                ``ingestion_model.ingest_paths``.
            overwrite (bool): Delete each path's existing indexed chunks before
                re-ingesting, so a shrunk document doesn't leave orphaned chunks
                behind. Defaults to False.

        Returns:
            dict[str, int]: Mapping of path -> number of segments ingested.
        """
        return ingestion_model.ingest_paths(paths, on_progress=on_progress, overwrite=overwrite)

    def supported_archive_members(self, namelist: list[str]) -> list[str]:
        """Filter a zip archive's member names to those this deployment can ingest."""
        return model.supported_archive_members(namelist, self.allowed_extensions)

    def find_and_upload_archived_versions(
        self,
        candidate_paths: list[str],
        on_progress: Callable[[str, int, int], None] | None = None,
    ) -> dict[str, str]:
        """
        Recover documents whose live blob is gone from a sibling ``archive/`` folder.

        ``avb_core``'s ``upload_file(overwrite=True)`` never truly overwrites
        a live file in place -- it renames the previous live blob into a
        sibling ``archive/`` folder as ``"<stem>_v<NNN><ext>"`` before
        writing new content to the live path (see
        ``upload.archive_restore_model`` for the full explanation and
        ``scheduler.scheduler_controller._prune_backup_archives``, which
        relies on the same behaviour). If a live blob was later deleted
        outright, only those versioned copies survive. For each candidate
        whose blob 404s, this finds the highest-numbered version in its
        archive/ folder and uploads that content fresh to today's canonical
        ``index_path`` location -- not the stale original path.

        Does not ingest the recovered files -- callers batch that separately
        via ``ingest_documents`` once every candidate has been checked, so
        one ``IndexPipeline`` gets built for the whole batch instead of once
        per file.

        Args:
            candidate_paths (list[str]): Data Lake paths to check --
                typically the "yielded no content" set from a prior
                ``ingest_documents`` run (a 404 is only one of several
                reasons that could happen, so this re-checks each one rather
                than assuming).
            on_progress (Callable[[str, int, int], None] | None): Optional
                progress callback, fired once per candidate as it finishes
                being checked (restored or not) -- (path, completed, total).

        Returns:
            dict[str, str]: original live_path -> new Data Lake path the
            recovered content was uploaded to. Candidates whose blob
            downloaded fine (nothing to restore), or whose archive/ folder
            had no matching version, are omitted.
        """
        missing = self._probe_missing(candidate_paths, on_progress=on_progress)
        if not missing:
            return {}
        plan = self._plan_from_archive(missing)

        account_name = datalake_account()
        filesystem_name = self._config["data_layer"]
        recovered: dict[str, str] = {}
        for live_path, archive_path in plan.items():
            try:
                data = download_from_datalake(
                    account_name=account_name, file_path=archive_path, _filesystem_name=filesystem_name
                )
                recovered[live_path] = self.upload_document(PurePosixPath(live_path).name, data)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Could not restore %s from archived version %s: %s", live_path, archive_path, exc)
        return recovered

    def find_ghost_documents(
        self,
        candidate_paths: list[str],
        on_progress: Callable[[str, int, int], None] | None = None,
    ) -> list[str]:
        """
        Identify candidates that are permanently unrecoverable from Data Lake.

        A "ghost" here is a path that 404s on download *and* has no matching
        versioned entry in its sibling ``archive/`` folder either (see
        ``find_and_upload_archived_versions`` for what that folder is) --
        there is nothing left anywhere in Data Lake to restore it from. The
        only way to get its content back is a manual re-upload of the
        original file from wherever it actually came from (email, a
        broker's portal, etc.), not anything this app can do on its own.

        Args:
            candidate_paths (list[str]): Data Lake paths to check --
                typically the "yielded no content" set from a prior
                ``ingest_documents`` run.
            on_progress (Callable[[str, int, int], None] | None): Optional
                progress callback, fired once per candidate as it finishes
                being checked -- (path, completed, total).

        Returns:
            list[str]: The subset of ``candidate_paths`` confirmed to be
            permanently gone.
        """
        missing = self._probe_missing(candidate_paths, on_progress=on_progress)
        if not missing:
            return []
        plan = self._plan_from_archive(missing)
        return [path for path in missing if path not in plan]

    def _probe_missing(
        self,
        candidate_paths: list[str],
        on_progress: Callable[[str, int, int], None] | None = None,
    ) -> list[str]:
        """Download-check every candidate; return the ones that 404 (azure.core.exceptions.ResourceNotFoundError)."""
        account_name = datalake_account()
        filesystem_name = self._config["data_layer"]

        missing: list[str] = []
        total = len(candidate_paths)
        for completed, path in enumerate(candidate_paths, start=1):
            try:
                download_from_datalake(account_name=account_name, file_path=path, _filesystem_name=filesystem_name)
            except ResourceNotFoundError:
                missing.append(path)
            except Exception as exc:  # noqa: BLE001 - any other download failure isn't this feature's concern
                logger.debug("Probe skipped %s (not a 404): %s", path, exc)
            if on_progress:
                on_progress(path, completed, total)
        return missing

    def _plan_from_archive(self, missing: list[str]) -> dict[str, str]:
        """Given confirmed-missing paths, map whichever have a recoverable archived version (see archive_restore_model)."""
        if not missing:
            return {}
        account_name = datalake_account()
        filesystem_name = self._config["data_layer"]
        manager = get_datalake_manager(account_name=account_name)

        archive_listing_by_dir: dict[str, list[str]] = {}
        for archive_dir in {archive_model.archive_dir_for(path) for path in missing}:
            try:
                archive_listing_by_dir[archive_dir] = [
                    PurePosixPath(entry.name).name
                    for entry in manager.list_paths(filesystem_name=filesystem_name, path=archive_dir, recursive=False)
                    if not getattr(entry, "is_directory", False)
                ]
            except Exception as exc:  # noqa: BLE001 - no archive folder here, nothing to restore from
                logger.debug("No archive folder at %s: %s", archive_dir, exc)
                archive_listing_by_dir[archive_dir] = []

        return archive_model.build_restore_plan(missing, archive_listing_by_dir)
