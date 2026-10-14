"""
Controller for the DCF Model Builder topic.

Wires company selection, bulk financial-model retrieval, metric-mapping
candidates, historical-trend defaults, LLM-assisted assumption suggestions,
DCF computation, and Excel export together. Owns every network call
(Azure Search, Azure OpenAI); the pure math and parsing live in
``dcf_parsing_model.py``, ``dcf_valuation_model.py``, ``dcf_llm_model.py``,
and ``dcf_export_model.py``.
"""

from __future__ import annotations

import logging
from typing import Any

from database import cache
from dcf import dcf_export_model as export_model
from dcf import dcf_llm_model as llm_model
from dcf import dcf_parsing_model as parsing_model
from dcf import dcf_valuation_model as valuation_model
from search import search_model

logger = logging.getLogger(__name__)


class DcfController:
    """Wires the DCF Model Builder's retrieval, mapping, valuation, and export."""

    def __init__(self) -> None:
        self._content: dict[str, Any] = parsing_model.load_content()

    # ── content access ─────────────────────────────────────────────────────────

    @property
    def canonical_line_items(self) -> dict[str, dict]:
        """Canonical DCF line-item ids -> {label, required, keywords, exclude_keywords}."""
        return self._content["canonical_line_items"]

    # ── company selection ────────────────────────────────────────────────────────

    def companies_with_financial_model(self) -> list[str]:
        """Return sorted tickers that have an ingested Bloomberg financial model."""
        return cache.get_companies_with_financial_model()

    # ── retrieval + parsing ──────────────────────────────────────────────────────

    def retrieve_records(self, ticker: str, index_name: str, max_records: int) -> list[parsing_model.ParsedRecord]:
        """
        Bulk-retrieve and parse every financial-model chunk for one company.

        Args:
            ticker (str): The company ticker to retrieve.
            index_name (str): The Azure AI Search index to query.
            max_records (int): Hard cap passed to ``retrieve_financial_model_bulk``.

        Returns:
            list[parsing_model.ParsedRecord]: Successfully parsed records.
        """
        chunks = search_model.retrieve_financial_model_bulk(index_name, ticker, max_records)
        return parsing_model.parse_chunks(chunks)

    def candidate_mapping(self, records: list[parsing_model.ParsedRecord]) -> dict[str, list[str]]:
        """
        Rank candidate metric breadcrumbs per canonical line item.

        Args:
            records (list[parsing_model.ParsedRecord]): Parsed records for one company.

        Returns:
            dict[str, list[str]]: canonical_item_id -> ranked candidate breadcrumbs
            (empty list if nothing matched).
        """
        metrics = parsing_model.distinct_metrics(records)
        return {
            item_id: parsing_model.rank_candidates(metrics, item_rule)
            for item_id, item_rule in self.canonical_line_items.items()
        }

    def historical_series(
        self,
        records: list[parsing_model.ParsedRecord],
        mapping: dict[str, str | None],
    ) -> dict[str, list[parsing_model.ParsedRecord]]:
        """
        Build each mapped canonical line item's historical time series.

        Args:
            records (list[parsing_model.ParsedRecord]): Parsed records for one company.
            mapping (dict[str, str | None]): canonical_item_id -> chosen metric
                breadcrumb (or None if the user left it unmapped).

        Returns:
            dict[str, list[parsing_model.ParsedRecord]]: canonical_item_id ->
            oldest-to-newest time series (empty list if unmapped).
        """
        return {
            item_id: (parsing_model.time_series_for_metric(records, metric_path) if metric_path else [])
            for item_id, metric_path in mapping.items()
        }

    # ── LLM-assisted defaults (read-only/advisory) ────────────────────────────────

    def suggest_assumptions_from_research(
        self,
        company: str,
        ticker: str,
        config: dict[str, Any],
    ) -> llm_model.LlmAssumptionSuggestion:
        """
        Ask the LLM for starting DCF assumptions grounded in research commentary.

        Retrieves with ``source_type="document"`` (explicitly excluding
        financial_model chunks) so the suggestion reflects analyst/broker
        views rather than restating numbers already parsed structurally.
        Read-only/advisory: never writes to session state itself -- the View
        decides whether/when to copy a suggested value into a widget.

        Args:
            company (str): The company name, used in the prompt.
            ticker (str): The company ticker, used to scope retrieval.
            config (dict[str, Any]): The equities app config block.

        Returns:
            llm_model.LlmAssumptionSuggestion: ``available=False`` if
            retrieval found nothing or the LLM response couldn't be parsed.
        """
        dcf_config = config["dcf"]
        chunks = search_model.retrieve(
            query=f"{company} outlook growth margin valuation risk",
            index_name=config["index_name"],
            top_k=dcf_config["research_top_k"],
            company=ticker or company,
            source_type="document",
        )
        if not chunks:
            return llm_model.LlmAssumptionSuggestion(available=False)

        chunks = sorted(chunks, key=lambda chunk: chunk.document_date or "", reverse=True)
        context = search_model.format_investment_context(chunks)

        llm_content = self._content["llm_suggestion"]
        prompt = llm_model.build_suggestion_prompt(llm_content["user_prompt_template"], company, context)

        try:
            raw_text = (
                search_model.get_chat_client(config["chat_deployment"])
                .chat(
                    messages=[
                        {"role": "system", "content": llm_content["system_prompt"]},
                        {"role": "user", "content": prompt},
                    ],
                )
                .strip()
            )
        except Exception:  # noqa: BLE001
            logger.exception("DCF assumption suggestion failed")
            return llm_model.LlmAssumptionSuggestion(available=False)

        parsed = llm_model.parse_llm_suggestion(raw_text)
        if parsed is None:
            return llm_model.LlmAssumptionSuggestion(available=False)

        cited = [chunks[i - 1] for i in parsed["cited_indices"] if 0 < i <= len(chunks)]
        return llm_model.LlmAssumptionSuggestion(
            available=True,
            revenue_growth_rate=parsed["revenue_growth_rate"],
            ebit_margin=parsed["ebit_margin"],
            discount_rate=parsed["discount_rate"],
            rationale=parsed["rationale"],
            sources=cited,
        )

    # ── valuation ──────────────────────────────────────────────────────────────

    def run_valuation(self, **kwargs: Any) -> valuation_model.DcfResult:
        """Run the DCF calculation. Forwards all arguments to ``dcf_valuation_model.dcf_valuation``."""
        return valuation_model.dcf_valuation(**kwargs)

    # ── export ─────────────────────────────────────────────────────────────────

    def build_workbook(self, **kwargs: Any) -> bytes:
        """Build the DCF Excel workbook. Forwards all arguments to ``dcf_export_model.build_workbook``."""
        return export_model.build_workbook(**kwargs)
