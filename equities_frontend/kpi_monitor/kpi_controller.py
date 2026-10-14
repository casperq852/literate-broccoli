"""
Controller for the KPI Monitor topic.

Wires one company's ingested Bloomberg model and its earnings transcripts /
other documents into a single LLM review, and wires file uploads into the
existing Data Lake + search-index ingestion. Owns every network call; the
series maths, prompt text, and response validation live in ``kpi_model``.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any

from dcf import dcf_parsing_model
from kpi_monitor import kpi_export_model as export_model
from kpi_monitor import kpi_model as model
from kpi_monitor import review_store as store
from kpi_monitor.kpi_model import KpiAnalysis, SourceFile
from search import search_model
from search.search_model import Chunk

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class UploadOutcome:
    """The result of uploading and ingesting one file."""

    filename: str
    kind: str  # 'financial_model' | 'document'
    segments: int = 0
    path: str = ""
    error: str | None = None
    warnings: list[str] = field(default_factory=list)


class KpiController:
    """Wires KPI review retrieval, LLM judgement, and source uploads together."""

    def __init__(self) -> None:
        self._content: dict[str, Any] = model.load_content()

    # ── data ───────────────────────────────────────────────────────────────────

    def load_series(self, ticker: str, config: dict[str, Any], company: str = "") -> list[model.KpiSeries]:
        """Retrieve and group this company's whole ingested model into per-metric series."""
        kpi_cfg = config["kpi_monitor"]
        chunks = search_model.retrieve_financial_model_bulk(
            config["index_name"], ticker, kpi_cfg["max_bulk_records"], company or None
        )
        records = dcf_parsing_model.parse_chunks(chunks)
        return model.build_series(records, kpi_cfg["max_reported_periods"], kpi_cfg["max_estimate_periods"])

    def load_documents(
        self,
        company: str,
        ticker: str,
        config: dict[str, Any],
        document_types: list[str] | None = None,
    ) -> list[Chunk]:
        """
        Retrieve transcript / presentation / report passages that may explain the numbers.

        Runs each configured query, drops duplicate passages, and returns the
        newest ``max_documents`` (the financial model itself is excluded).

        Args:
            company (str): The company's display name.
            ticker (str): The company ticker, used to anchor retrieval.
            config (dict[str, Any]): The equities app config block.
            document_types (list[str] | None): Restrict to these document types;
                defaults to everything except the financial model.

        Returns:
            list[Chunk]: Passages ordered newest-first.
        """
        kpi_cfg = config["kpi_monitor"]
        types = document_types or [t for t in search_model.ALL_DOCUMENT_TYPES if t != "financial_model"]
        seen: set[str] = set()
        chunks: list[Chunk] = []
        for query in self._content["retrieval_queries"]:
            try:
                results = search_model.retrieve(
                    query=query,
                    index_name=config["index_name"],
                    top_k=kpi_cfg["queries_top_k"],
                    company=ticker or company,
                    document_types=types,
                )
            except Exception:  # noqa: BLE001 - one failed query should not sink the review
                logger.exception("KPI document query failed: %s", query)
                continue
            for chunk in results:
                if chunk.content not in seen:
                    seen.add(chunk.content)
                    chunks.append(chunk)
        chunks.sort(key=lambda chunk: chunk.document_date or "", reverse=True)
        return chunks[: kpi_cfg["max_documents"]]

    # ── review ─────────────────────────────────────────────────────────────────

    def analyze(
        self,
        company: str,
        ticker: str,
        config: dict[str, Any],
        focus_notes: str = "",
        document_types: list[str] | None = None,
        sources: tuple[SourceFile, ...] = (),
    ) -> KpiAnalysis | None:
        """
        Review this company's latest model against its transcripts and documents.

        Args:
            company (str): The company's display name.
            ticker (str): The company ticker.
            config (dict[str, Any]): The equities app config block.
            focus_notes (str): Optional free-text steer from the analyst.
            document_types (list[str] | None): Restrict document retrieval to these types.
            sources (tuple[SourceFile, ...]): Files the analyst added since the previous review, recorded with this one.

        Returns:
            KpiAnalysis | None: None if the company has no usable model data.

        Raises:
            ValueError: If the LLM's response could not be parsed into a review.
        """
        series, omitted = model.select_series(
            self.load_series(ticker, config, company), config["kpi_monitor"]["max_series"]
        )
        if not series:
            return None
        as_of = model.latest_reported_period(series) or "unknown"
        documents = self.load_documents(company, ticker, config, document_types)

        review_content = self._content["review"]
        prompt = model.build_user_prompt(
            review_content["user_prompt_template"],
            company=company,
            ticker=ticker,
            as_of=as_of,
            series_table=model.format_series_table(series),
            omitted_note=(f"({omitted} smaller-moving metrics were left out to fit.)" if omitted else ""),
            documents=search_model.format_investment_context(documents),
            focus_notes=focus_notes,
        )
        raw = (
            search_model.get_chat_client(config["chat_deployment"])
            .chat(
                messages=[
                    {"role": "system", "content": review_content["system_prompt"]},
                    {"role": "user", "content": prompt},
                ],
            )
            .strip()
        )
        review = model.parse_review(raw, n_documents=len(documents))
        if review is None:
            logger.warning("KPI review for %s was not valid JSON: %.300s", ticker, raw)
            msg = "The model's response could not be read as a KPI review."
            raise ValueError(msg)
        return KpiAnalysis(
            review=review,
            as_of=as_of,
            series=series,
            documents=documents,
            omitted_series=omitted,
            focus_notes=focus_notes.strip(),
            sources=sources,
            created_at=datetime.now(timezone.utc).isoformat(),
        )

    def source_labels(self, documents: list[Chunk]) -> dict[int, str]:
        """Number each document passage the way the prompt did, with a citation label."""
        return {
            index: search_model.format_source_label(
                chunk.publisher,
                chunk.document_type,
                chunk.document_date,
                companies=chunk.companies,
                humanize_type=True,
            )
            for index, chunk in enumerate(documents, start=1)
        }

    def build_report(self, analysis: KpiAnalysis, ticker: str, company: str) -> bytes:
        """Build the shareable Word report for a review (fresh or reopened)."""
        return export_model.build_report(
            analysis, company, ticker, self.source_labels(analysis.documents), date.today().isoformat()
        )

    # ── sources ────────────────────────────────────────────────────────────────

    def upload_sources(
        self,
        files: list[tuple[str, bytes]],
        config: dict[str, Any],
        on_progress: Callable[[str], None] | None = None,
    ) -> list[UploadOutcome]:
        """
        Upload MODL workbooks, transcripts and other documents, then ingest each into the index.

        One file failing does not stop the others.

        Args:
            files (list[tuple[str, bytes]]): (filename, bytes) pairs.
            config (dict[str, Any]): The equities app config block.
            on_progress (Callable[[str], None] | None): Called with a filename as its upload starts.

        Returns:
            list[UploadOutcome]: One outcome per file, in order.
        """
        # Imported here: the upload stack pulls in the full ingestion SDK, which a review doesn't need.
        from upload.upload_controller import UploadController  # noqa: PLC0415

        upload_controller = UploadController(config)
        outcomes: list[UploadOutcome] = []
        for filename, data in files:
            kind = model.classify_upload(filename)
            warnings: list[str] = []
            if filename.lower().endswith(".xlsx") and kind == "document":
                warnings.append(
                    f"Not named {model.MODL_FILENAME_PREFIX}*.xlsx, so it is indexed as a generic document and "
                    "its metrics will not appear in the KPI review."
                )
            if on_progress:
                on_progress(filename)
            try:
                dest_path = upload_controller.upload_document(filename, data)
                segments = upload_controller.ingest_documents([dest_path]).get(dest_path, 0)
            except Exception as error:  # noqa: BLE001
                logger.exception("KPI source upload failed for %s", filename)
                outcomes.append(UploadOutcome(filename, kind, error=str(error), warnings=warnings))
                continue
            outcomes.append(UploadOutcome(filename, kind, segments=segments, path=dest_path, warnings=warnings))
        return outcomes

    # ── saved reviews ──────────────────────────────────────────────────────────

    def save_review(
        self, analysis: KpiAnalysis, ticker: str, company: str, config: dict[str, Any], account_name: str
    ) -> str:
        """
        Store a review (output, evidence, document passages, source files) in the Data Lake.

        Keeps the newest ``kpi_monitor.max_saved_reviews`` per company.

        Returns:
            str: The saved review's id.
        """
        review_id = model.make_review_id(analysis.created_at, analysis.as_of)
        store.save_review(
            account_name=account_name,
            filesystem_name=config["data_layer"],
            folder=model.storage_folder(ticker),
            review_id=review_id,
            payload=model.analysis_to_dict(analysis, ticker, company),
            keep=config["kpi_monitor"]["max_saved_reviews"],
        )
        return review_id

    def list_reviews(self, ticker: str, config: dict[str, Any], account_name: str) -> list[str]:
        """Saved review ids for a company, newest first (empty if none or the store is unreachable)."""
        return store.list_reviews(account_name, config["data_layer"], model.storage_folder(ticker))

    def load_review(self, ticker: str, review_id: str, config: dict[str, Any], account_name: str) -> KpiAnalysis:
        """Reopen a saved review exactly as it was saved."""
        data = store.load_review(account_name, config["data_layer"], model.storage_folder(ticker), review_id)
        return model.analysis_from_dict(data)

    def delete_review(self, ticker: str, review_id: str, config: dict[str, Any], account_name: str) -> None:
        """Delete one saved review."""
        store.delete_review(account_name, config["data_layer"], model.storage_folder(ticker), review_id)
