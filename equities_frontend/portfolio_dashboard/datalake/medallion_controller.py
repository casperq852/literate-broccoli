"""Incremental landing-zone -> bronze -> silver refresh orchestration.

:class:`MedallionRefresher` promotes newly delivered Bloomberg exports through the
medallion layers exactly once: raw files are copied to the bronze layer under an
``EQT/portfolio/<code>/YYYY/MM/DD`` folder, parsed into report objects, and written
to the
silver layer as parquet. Promotion is incremental (a file whose bronze copy already
exists is skipped) and gated on the sibling status file when one is present, so a
sidebar button press only processes data that has not been parsed yet.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from portfolio_dashboard.datalake import helpers
from portfolio_dashboard.datalake.datalake_controller import DatalakeClient, run_async
from portfolio_dashboard.datalake.silver_model import build_aggregation_parquet_bytes, report_to_parquet_bytes
from portfolio_dashboard.reports.parsing_model import detect_and_load, detect_and_load_excel


@dataclass
class RefreshResult:
    """Outcome counts and paths from one medallion refresh run.

    Attributes:
        promoted: Number of landing files copied to the bronze layer.
        parsed_reports: Number of report objects written to the silver layer.
        skipped_existing: Landing files skipped because a bronze copy exists.
        skipped_not_ready: Landing files skipped because their status file was
            missing a success marker.
        bronze_paths: Bronze paths written this run.
        silver_paths: Silver paths written this run.
        aggregation_paths: Per-portfolio aggregation paths rebuilt this run.
        errors: ``(filename, message)`` pairs for files that failed to process.
    """

    promoted: int = 0
    parsed_reports: int = 0
    skipped_existing: int = 0
    skipped_not_ready: int = 0
    bronze_paths: list[str] = field(default_factory=list)
    silver_paths: list[str] = field(default_factory=list)
    aggregation_paths: list[str] = field(default_factory=list)
    errors: list[tuple[str, str]] = field(default_factory=list)


class MedallionRefresher:
    """Promote landing-zone exports into the bronze and silver layers.

    Attributes:
        client: The Data Lake facade used for all container I/O.
    """

    def __init__(self, client: DatalakeClient | None = None) -> None:
        """Wire the refresher to a Data Lake client.

        Args:
            client: An existing :class:`DatalakeClient`, or ``None`` to build one.
        """
        self.client: DatalakeClient = client if client is not None else DatalakeClient()

    def refresh(self) -> RefreshResult:
        """Run one incremental landing -> bronze -> silver promotion pass.

        This is the synchronous entry point for the Streamlit sidebar: it drives the
        async Data Lake manager to completion inside a single event loop.

        Returns:
            RefreshResult: Counts and paths describing what changed.
        """
        return run_async(self.refresh_async())

    async def refresh_async(self) -> RefreshResult:
        """Run one incremental promotion pass using the async Data Lake manager.

        Returns:
            RefreshResult: Counts and paths describing what changed.
        """
        result = RefreshResult()
        await self.client.open()
        try:
            landing_names = await self.client.list_landing_names()
            basenames = {name.rsplit("/", 1)[-1] for name in landing_names}
            existing_bronze = set(await self.client.list_files("bronze"))

            for basename in sorted(basenames):
                landing = helpers.parse_landing_filename(basename)
                if landing is None:
                    continue

                bronze_path = helpers.build_bronze_path(landing)
                if bronze_path in existing_bronze:
                    result.skipped_existing += 1
                    continue

                try:
                    if not await self._is_ready(landing, basenames):
                        result.skipped_not_ready += 1
                        continue
                    await self._promote_one(landing, bronze_path, result)
                except Exception as error:  # noqa: BLE001 - report and continue per file
                    result.errors.append((basename, f"{type(error).__name__}: {error}"))

            await self._rebuild_aggregations(result)
        finally:
            await self.client.close()

        return result

    async def _rebuild_aggregations(self, result: RefreshResult) -> None:
        """Rebuild the per-portfolio aggregation parquet for combos touched this run.

        For every ``(code, slug, key)`` component that received a new silver file,
        this re-reads *all* of that component's date-partitioned silver parquet and
        rewrites a single stacked aggregation file at
        ``EQT/portfolio/<code>/aggregation/<slug>__<key>.parquet``. Rebuilding from
        the full silver listing keeps each aggregation complete even though the
        promotion loop only wrote the current run's dates.

        Args:
            result: The accumulating run result; touched components are read from
                ``silver_paths`` and rebuilt aggregation paths are appended to
                ``aggregation_paths``.
        """
        affected: set[tuple[str, str, str]] = set()
        for silver_path in result.silver_paths:
            entry = helpers.parse_silver_path(silver_path)
            if entry is not None:
                affected.add((entry.code, entry.slug, entry.key))
        if not affected:
            return

        by_combo: dict[tuple[str, str, str], list[helpers.SilverEntry]] = {}
        for path in await self.client.list_files("silver"):
            entry = helpers.parse_silver_path(path)
            if entry is None:
                continue
            combo = (entry.code, entry.slug, entry.key)
            if combo in affected:
                by_combo.setdefault(combo, []).append(entry)

        for (code, slug, key), entries in by_combo.items():
            entries.sort(key=lambda entry: entry.date)
            payloads = await asyncio.gather(*(self.client.download("silver", entry.path) for entry in entries))
            aggregation_bytes = build_aggregation_parquet_bytes(
                [(entry.date, payload) for entry, payload in zip(entries, payloads)]
            )
            aggregation_path = helpers.build_aggregation_path(code, slug, key)
            await self.client.upload("silver", aggregation_path, aggregation_bytes)
            result.aggregation_paths.append(aggregation_path)

    async def _is_ready(self, landing: helpers.LandingFile, basenames: set[str]) -> bool:
        """Return whether a landing file is ready to promote.

        A file is ready when it has no sibling status file, or when its status file
        contains the configured success marker.

        Args:
            landing: The parsed landing data file.
            basenames: All landing-zone file names, for status-sibling lookup.

        Returns:
            bool: ``True`` when the file may be promoted.
        """
        status_name = helpers.status_filename_for(landing)
        if status_name not in basenames:
            return True
        content = await self.client.download("landing", f"{self.client.root}/{status_name}")
        return helpers.is_status_success(content.decode("utf-8", errors="replace"))

    async def _promote_one(self, landing: helpers.LandingFile, bronze_path: str, result: RefreshResult) -> None:
        """Copy one landing file to bronze and write its silver parquet tables.

        Args:
            landing: The parsed landing data file.
            bronze_path: The destination bronze path.
            result: The accumulating run result to update in place.
        """
        raw = await self.client.download("landing", f"{self.client.root}/{landing.filename}")
        await self.client.upload("bronze", bronze_path, raw)
        result.promoted += 1
        result.bronze_paths.append(bronze_path)

        reports = detect_and_load_excel(raw) if landing.ext == "xlsx" else [detect_and_load(raw)]
        for report in reports:
            storage_key = helpers.report_storage_key(report)
            silver_path = helpers.build_silver_path(landing, report.metadata.portfolio, storage_key)
            await self.client.upload("silver", silver_path, report_to_parquet_bytes(report))
            result.parsed_reports += 1
            result.silver_paths.append(silver_path)
