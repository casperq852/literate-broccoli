"""
Controller for the Companies topic: local cache aggregation, region/GICS
sector classification, and per-company document/financial-data wiring for
the Companies browser and detail screen.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any

import pandas as pd
from portfolio_dashboard import chat_context_model, silver_cache_model

from companies import company_cleanup_model as cleanup_model
from companies import company_dedup_model as dedup_model
from companies import company_model as model
from companies import company_reference_model as reference_model
from companies import factor_screen_model
from database import cache
from dcf.dcf_controller import DcfController
from index.index_controller import IndexController
from search import search_model
from vectorization.company_resolution import DEFAULT_FUZZY_THRESHOLD, company_name_similarity

logger = logging.getLogger(__name__)

# Cap on how many untagged documents backfill_untagged_documents() re-ingests
# per (automatic, daily) run -- see that method's docstring.
_BACKFILL_BATCH_LIMIT = 50


class CompanyController:
    """Wires the local company cache, LLM classification, and per-company data lookups."""

    def __init__(self, index_name: str) -> None:
        self._index_name = index_name

    # ── browser ──────────────────────────────────────────────────────────────────

    def load_companies(self) -> pd.DataFrame:
        """
        Read cached company summaries (instant) as a DataFrame, sorted by region/sector/name.

        Returns:
            pd.DataFrame: One row per company, columns Region/Sector/Ticker/
            Company/Documents/Financial Model/Latest Date.
        """
        columns = ["Region", "Sector", "Ticker", "Company", "Documents", "Financial Model", "Latest Date"]
        companies = cache.get_all_companies()
        if not companies:
            return pd.DataFrame(columns=columns)

        rows = [
            {
                "Region": company["region"] or "Unclassified",
                "Sector": company["gics_sector"] or "Unclassified",
                "Ticker": company["ticker"],
                "Company": company["display_name"],
                "Documents": company["doc_count"],
                "Financial Model": "Yes" if company["has_financial_model"] else "",
                "Latest Date": company["last_document_date"] or "",
            }
            for company in companies
        ]
        dataframe = pd.DataFrame(rows)
        return dataframe.sort_values(["Region", "Sector", "Company"]).reset_index(drop=True)

    def sync_companies(self, config: dict[str, Any]) -> int:
        """
        Rebuild the company cache from the local document cache and classify anything new.

        Recomputes doc counts, date ranges, and financial-model availability
        for every company from ``database.cache.get_all_documents()``, then
        batch-classifies (region + GICS sector) only the tickers that don't
        already have a classification -- previously classified companies keep
        their region/sector even if their document stats changed.

        Args:
            config (dict[str, Any]): The equities app config block
                (chat_deployment, company_classification).

        Returns:
            int: Number of companies newly classified in this call.
        """
        aggregated = model.aggregate_companies(cache.get_all_documents())
        if not aggregated:
            cache.delete_all_companies()
            return 0
        return self._refresh_and_classify(aggregated, config)

    def upload_factor_screen(self, filename: str, data: bytes) -> tuple[str, int]:
        """
        Parse and store one dated snapshot of an uploaded Bloomberg factor/crowding screen.

        Args:
            filename (str): The uploaded file's name, e.g.
                ``"Momentum Screen_2026_Aug_24.xlsx"`` -- its date is the
                snapshot's key (see ``factor_screen_model.parse_screen_date``).
            data (bytes): The uploaded file's raw bytes.

        Returns:
            tuple[str, int]: (as_of_date, row_count) for the upload UI's
            success message.

        Raises:
            ValueError: If the filename's date can't be parsed, or the
                sheet doesn't look like a Bloomberg factor screen export
                (see ``factor_screen_model.parse_screen_date``/``parse_factor_screen``).
        """
        as_of_date = factor_screen_model.parse_screen_date(filename)
        rows = factor_screen_model.parse_factor_screen(data)
        cache.upsert_factor_screen_rows(as_of_date, rows)
        return as_of_date, len(rows)

    def sync_companies_incremental(self, tickers: list[str], config: dict[str, Any]) -> None:
        """
        Refresh the company cache for specific tickers right after an upload/ingest.

        A ticker seen for the first time is classified via the LLM so it
        appears fully populated immediately. A ticker that already has a
        classification only gets its doc-count/date-range/financial-model
        stats refreshed -- its region/sector are kept as-is and no LLM call
        is made, so re-uploading research for an already-covered company
        stays cheap.

        Args:
            tickers (list[str]): Tickers to refresh, typically the companies
                found in a just-ingested document.
            config (dict[str, Any]): The equities app config block
                (chat_deployment, company_classification).
        """
        if not tickers:
            return
        wanted = {ticker.strip().upper() for ticker in tickers}
        aggregated = model.aggregate_companies(cache.get_all_documents())
        targets = [company for company in aggregated if company["ticker"] in wanted]
        self._refresh_and_classify(targets, config)

    def _refresh_and_classify(self, targets: list[dict], config: dict[str, Any]) -> int:
        """
        Classify unclassified companies in ``targets`` and refresh stats for the rest.

        Shared by ``sync_companies`` (all companies) and
        ``sync_companies_incremental`` (a specific subset).

        Args:
            targets (list[dict]): Aggregated company summaries to process.
            config (dict[str, Any]): The equities app config block.

        Returns:
            int: Number of companies newly classified.
        """
        if not targets:
            return 0

        classified_tickers = cache.get_classified_tickers()
        existing = {company["ticker"]: company for company in cache.get_all_companies()}

        to_classify = model.companies_needing_classification(targets, classified_tickers)
        newly_classified = self._classify_and_upsert(to_classify, config) if to_classify else 0

        # Refresh doc stats for already-classified companies without re-running classification.
        already_classified = [company for company in targets if company["ticker"] in classified_tickers]
        if already_classified:
            refresh_records = [
                {
                    **company,
                    "region": existing.get(company["ticker"], {}).get("region", "Unclassified"),
                    "gics_sector": existing.get(company["ticker"], {}).get("gics_sector", "Unclassified"),
                    "classified_at": existing.get(company["ticker"], {}).get("classified_at"),
                }
                for company in already_classified
            ]
            cache.upsert_companies(refresh_records)

        return newly_classified

    def _classify_and_upsert(self, targets: list[dict], config: dict[str, Any]) -> int:
        """Batch-classify ``targets`` via the chat model and upsert the results into the cache."""
        classification_config = config["company_classification"]
        now = datetime.now(timezone.utc).isoformat()
        classified_count = 0

        for batch in model.batches(targets, classification_config["batch_size"]):
            prompt = model.build_classification_prompt(
                batch,
                classification_config["allowed_regions"],
                classification_config["allowed_gics_sectors"],
            )
            raw_text = ""
            try:
                raw_text = search_model.get_chat_client(config["chat_deployment"], _config=config).chat(
                    messages=[{"role": "user", "content": prompt}],
                    reasoning_effort="none",
                )
            except Exception:  # noqa: BLE001
                logger.exception("Company classification batch failed")

            tickers = [company["ticker"] for company in batch]
            classifications = model.parse_classification_response(
                raw_text,
                tickers,
                classification_config["allowed_regions"],
                classification_config["allowed_gics_sectors"],
            )
            records = [
                {
                    **company,
                    "region": classifications[company["ticker"]]["region"],
                    "gics_sector": classifications[company["ticker"]]["gics_sector"],
                    "classified_at": now,
                }
                for company in batch
            ]
            cache.upsert_companies(records)
            classified_count += len(batch)

        return classified_count

    def deduplicate_companies(self, merge_groups: list[dict[str, Any]] | None = None) -> int:
        """
        Merge cached company entries that turn out to be the same real
        company under different tickers.

        ``merge_groups`` lets a caller supply groups found some other way
        (e.g. ``run_llm_cleanup``'s LLM-reviewed, reference-confirmed
        merges, converted via ``company_dedup_model.groups_from_tickers``)
        instead of self-detecting them. Both paths execute through this same
        method -- there is exactly one place that ever actually merges
        companies, so a merge behaves identically regardless of how it was
        found. ``None`` (the default) detects groups via
        ``company_dedup_model.find_merge_groups`` -- Bloomberg exchange-code
        suffixes and fuzzy name matching, the same rules
        ``vectorization.company_resolution.resolve_companies`` applies at
        ingestion time.

        Rewrites every affected document's companies/company_names lists
        onto the canonical ticker/name, drops the now-empty alias rows from
        the companies table, then refreshes each canonical row's doc count
        and date range from the just-merged documents -- no LLM re-classification,
        since every canonical ticker here was already a known, previously
        classified company.

        This only fixes the local cache -- the underlying Azure AI Search
        index still holds each document's original (unresolved) ticker, so a
        full Index Browser sync (``index.index_controller.IndexController.sync_from_azure``)
        pulls the old split rows straight back in until the affected
        documents are themselves re-ingested. Safe to run repeatedly and
        interleaved with ingestion or a sync: with nothing left to merge,
        ``find_merge_groups`` simply returns no groups.

        Args:
            merge_groups: Externally-supplied merge groups (same shape
                ``find_merge_groups`` returns), or ``None`` to detect them
                via ``find_merge_groups`` as before.

        Returns:
            int: Number of alias tickers merged away.
        """
        groups = merge_groups if merge_groups is not None else dedup_model.find_merge_groups(cache.get_all_companies())
        if not groups:
            return 0

        alias_map = dedup_model.build_alias_map(groups)
        changed_documents = dedup_model.rewrite_document_tickers(cache.get_all_documents(), alias_map)
        if changed_documents:
            cache.upsert_documents(changed_documents)

        cache.delete_companies(list(alias_map.keys()))

        aggregated = {company["ticker"]: company for company in model.aggregate_companies(cache.get_all_documents())}
        refreshed = [
            {**aggregated[group["canonical"]["ticker"]], **dedup_model.best_classification(group)}
            for group in groups
            if group["canonical"]["ticker"] in aggregated
        ]
        cache.upsert_companies(refreshed)

        logger.info(
            "Company dedup: merged %d alias ticker(s) into %d canonical compan(y/ies)",
            len(alias_map),
            len(groups),
        )
        return len(alias_map)

    def sync_company_reference(self, config: dict[str, Any]) -> int:
        """
        Keep the authoritative company reference fresh from Portfolio Dashboard holdings.

        Extracts every distinct security name currently held across cached
        portfolios (``portfolio_dashboard.chat_context_model.distinct_security_names``,
        reading ``portfolio_dashboard.silver_cache_model.load_reports()`` --
        the already-locally-cached report objects the daily portfolio sync
        already produces, no live Data Lake read here), inserts any
        genuinely new names into ``database.cache``'s ``company_reference``
        table unresolved, then resolves (via LLM, the same batched
        chat-classification pattern ``_classify_and_upsert`` uses for
        region/sector) only the names that don't already have a resolution
        -- cheap in steady state, since most runs see zero new names.

        Args:
            config (dict[str, Any]): The equities app config block
                (chat_deployment, company_classification.batch_size).

        Returns:
            int: Number of names newly resolved in this call.
        """
        try:
            reports_by_portfolio = silver_cache_model.load_reports() or {}
        except Exception:  # noqa: BLE001
            logger.exception("Could not load cached portfolio reports for company reference sync")
            return 0

        names = chat_context_model.distinct_security_names(reports_by_portfolio)
        cache.upsert_company_reference_names(names)

        unresolved = cache.get_unresolved_company_reference_names()
        if not unresolved:
            return 0

        classification_config = config["company_classification"]
        resolved_count = 0
        for batch in model.batches(unresolved, classification_config["batch_size"]):
            prompt = reference_model.build_reference_prompt(batch)
            raw_text = ""
            try:
                raw_text = search_model.get_chat_client(config["chat_deployment"], _config=config).chat(
                    messages=[{"role": "user", "content": prompt}],
                    reasoning_effort="none",
                )
            except Exception:  # noqa: BLE001
                logger.exception("Company reference resolution batch failed")

            resolutions = reference_model.parse_reference_response(raw_text, batch)
            records = [
                {"name": name, "ticker": r["ticker"], "canonical_name": r["canonical_name"]}
                for name, r in resolutions.items()
            ]
            cache.upsert_company_reference_resolutions(records)
            resolved_count += len(batch)

        return resolved_count

    def run_llm_cleanup(self, config: dict[str, Any]) -> dict[str, int]:
        """
        Periodic LLM-driven review of the company list for likely duplicates.

        Smarter than the rapidfuzz-only ``deduplicate_companies``: an LLM
        has the world knowledge to tell two companies that merely share a
        legal suffix apart, which is exactly what rapidfuzz alone got wrong
        in production (see ``company_cleanup_model``'s module docstring).

        Refreshes the company reference first (``sync_company_reference``),
        then asks the LLM to propose merge groups across the full current
        company list, and splits its proposals via
        ``company_cleanup_model.gate_merge_groups``: groups where every
        member is independently confirmed against the same authoritative
        reference entry merge automatically, through the same
        ``deduplicate_companies`` execution path the rapidfuzz pass uses;
        everything else is queued in ``company_merge_suggestions`` for
        manual review on the Logs page.

        Args:
            config (dict[str, Any]): The equities app config block
                (chat_deployment, company_classification.batch_size).

        Returns:
            dict[str, int]: ``{"reference_resolved", "auto_merged", "queued_for_review"}``.
        """
        reference_resolved = self.sync_company_reference(config)

        companies = cache.get_all_companies()
        if len(companies) < 2:  # noqa: PLR2004
            return {"reference_resolved": reference_resolved, "auto_merged": 0, "queued_for_review": 0}

        prompt = cleanup_model.build_merge_review_prompt(companies)
        raw_text = ""
        try:
            raw_text = search_model.get_chat_client(config["chat_deployment"], _config=config).chat(
                messages=[{"role": "user", "content": prompt}],
                reasoning_effort="none",
            )
        except Exception:  # noqa: BLE001
            logger.exception("Company merge review call failed")

        valid_tickers = {company["ticker"] for company in companies}
        groups = cleanup_model.parse_merge_review_response(raw_text, valid_tickers)
        if not groups:
            return {"reference_resolved": reference_resolved, "auto_merged": 0, "queued_for_review": 0}

        reference = cache.get_all_company_reference()
        auto_apply, needs_review = cleanup_model.gate_merge_groups(groups, companies, reference)

        auto_merged = 0
        if auto_apply:
            ticker_groups = [group["tickers"] for group in auto_apply]
            merge_groups = dedup_model.groups_from_tickers(ticker_groups, companies)
            auto_merged = self.deduplicate_companies(merge_groups=merge_groups)

        for group in needs_review:
            cache.add_company_merge_suggestion(group["tickers"], group.get("canonical_name"), group.get("reason"))

        logger.info(
            "Company LLM cleanup: %d name(s) resolved, %d merged automatically, %d queued for review",
            reference_resolved,
            auto_merged,
            len(needs_review),
        )
        return {
            "reference_resolved": reference_resolved,
            "auto_merged": auto_merged,
            "queued_for_review": len(needs_review),
        }

    def backfill_untagged_documents(self, config: dict[str, Any], *, max_documents: int = _BACKFILL_BATCH_LIMIT) -> int:
        """
        Re-ingest documents whose companies list is empty.

        No new extraction logic here -- the win comes entirely from
        ``known_companies`` now including any newly reference-confirmed
        entries when ``vectorization.company_resolution.resolve_companies``
        runs during ingestion. Same underlying mechanism as the manual
        "Re-ingest ALL indexed documents" Danger Zone action, just scoped to
        documents that actually have nothing tagged rather than the whole
        index.

        Capped at ``max_documents`` per run (default 50) -- this runs
        automatically once a day, unlike the manually-triggered, unbounded
        Danger Zone action, so an unexpectedly large untagged backlog
        doesn't silently consume a large, unbounded amount of LLM/embedding
        budget in one unattended run; any remainder is simply picked up on
        the next day's run.

        Args:
            config (dict[str, Any]): The equities app config block (index_name).
            max_documents: Maximum documents to re-ingest in this call.

        Returns:
            int: Number of documents that picked up at least one company this run.
        """
        # Local import: upload/ingestion_model.py imports CompanyController, so importing
        # UploadController at module level here would be circular.
        from upload.upload_controller import UploadController  # noqa: PLC0415

        missing = cache.get_documents_missing_companies()
        if not missing:
            return 0
        paths = [doc["path"] for doc in missing if doc.get("path")][:max_documents]
        if not paths:
            return 0

        UploadController(config).ingest_documents(paths)
        refreshed = cache.get_documents_by_paths(paths)
        return sum(1 for doc in refreshed if doc.get("companies"))

    # ── detail screen ────────────────────────────────────────────────────────────

    def get_company(self, ticker: str) -> dict | None:
        """Return the cached summary for one ticker, or None if it isn't cached."""
        for company in cache.get_all_companies():
            if company["ticker"] == ticker:
                return company
        return None

    def documents_for_company(self, ticker: str) -> pd.DataFrame:
        """
        Return this company's cached documents, newest first.

        Reuses ``IndexController.load_documents()``'s row shaping and filters
        to rows whose "Companies" column includes ``ticker`` as a whole
        entry (not a substring match of another ticker).

        Args:
            ticker (str): The company ticker to filter to.

        Returns:
            pd.DataFrame: The subset of the Index Browser's document table
            belonging to this company.
        """
        all_documents = IndexController(self._index_name).load_documents()
        if all_documents.empty:
            return all_documents
        pattern = rf"(?:^|,\s*){re.escape(ticker)}(?:\s*,|$)"
        mask = all_documents["Companies"].str.contains(pattern, case=False, na=False, regex=True)
        return all_documents[mask].reset_index(drop=True)

    def financial_summary(self, ticker: str, config: dict[str, Any]) -> list[dict] | None:
        """
        Build a compact historical financial summary from this company's ingested Bloomberg model.

        Auto-maps the highest-ranked candidate metric per canonical DCF line
        item (see ``DcfController.candidate_mapping``) rather than asking the
        user to confirm mappings -- this is a read-only snapshot, not the DCF
        Model Builder itself.

        Args:
            ticker (str): The company ticker to retrieve.
            config (dict[str, Any]): The equities app config block
                (index_name, dcf).

        Returns:
            list[dict] | None: Long-format summary rows (see
            ``company_model.financial_summary_rows``), or None if this
            company has no ingested financial model.
        """
        dcf_controller = DcfController()
        records = dcf_controller.retrieve_records(ticker, config["index_name"], config["dcf"]["max_bulk_records"])
        if not records:
            return None

        candidates = dcf_controller.candidate_mapping(records)
        mapping: dict[str, str | None] = {
            item_id: (item_candidates[0] if item_candidates else None) for item_id, item_candidates in candidates.items()
        }
        historicals = dcf_controller.historical_series(records, mapping)
        item_labels = {item_id: rule["label"] for item_id, rule in dcf_controller.canonical_line_items.items()}
        return model.financial_summary_rows(historicals, item_labels)

    def portfolio_positions(self, company_name: str) -> list[dict]:
        """
        Find this company's positions across every portfolio with cached metrics.

        Same lookup ``investment_case_controller.py``'s
        ``_portfolio_positioning_chunk`` already does (kept as a separate,
        near-identical implementation there rather than refactored into a
        shared function -- that method grounds an LLM prompt chunk, not a UI
        table, and reshaping a controller two features now depend on is a
        bigger risk than a ~10-line duplication).

        Args:
            company_name (str): The company's display name.

        Returns:
            list[dict]: One entry per portfolio holding this company,
            ``{"portfolio", "as_of_date", "matched_name", "active_weight_pct",
            "weight_pct", "total_risk_d", "contribution_pct",
            "contribution_label"}`` (see
            ``portfolio_dashboard.chat_context_model.find_company_position``).
            Empty if the company isn't held anywhere with cached metrics.
        """
        if not company_name:
            return []
        positions = []
        for portfolio in cache.get_portfolio_names():
            record = cache.get_portfolio_metrics(portfolio)
            if not record:
                continue
            position = chat_context_model.find_company_position(company_name, record)
            if position:
                positions.append({"portfolio": portfolio, "as_of_date": record["as_of_date"], **position})
        return positions

    def factor_screen_for_company(self, company_name: str, fuzzy_threshold: int = DEFAULT_FUZZY_THRESHOLD) -> dict | None:
        """
        Find this company's row in the most recently uploaded factor/crowding screen.

        Fuzzy-matches ``company_name`` against the latest snapshot's
        ``name`` column using the same legal-suffix-aware scorer
        ``vectorization.company_resolution.company_name_similarity`` uses
        elsewhere in this app (the fix for this session's ING/JET
        false-positive-merge incident) -- deliberately not a second, ad-hoc
        matching heuristic.

        Args:
            company_name (str): The company's display name.
            fuzzy_threshold (int): Minimum rapidfuzz score (0-100) to accept
                a match.

        Returns:
            dict | None: The matched screen row plus ``as_of_date``, or
            ``None`` if no factor screen has ever been uploaded, or nothing
            in the latest snapshot clears ``fuzzy_threshold``.
        """
        if not company_name:
            return None
        as_of_date = cache.get_latest_factor_screen_date()
        if as_of_date is None:
            return None
        rows = cache.get_factor_screen_rows(as_of_date)
        if not rows:
            return None

        best_row: dict | None = None
        best_score = 0.0
        for row in rows:
            score = company_name_similarity(company_name, row["name"])
            if score >= fuzzy_threshold and score > best_score:
                best_row, best_score = row, score
        if best_row is None:
            return None
        return {**best_row, "as_of_date": as_of_date}
