"""
Financial-model-aware extractor.

``EquitiesExtractor`` subclasses the avb_search ``Extractor`` and intercepts
only Bloomberg model files (``MODL_*.xlsx``), routing them to the structured
``extract_financial_model`` parser. Every other file type -- PDF, DOCX, PPTX,
Markdown, plain text, and generic spreadsheets -- falls straight through to the
parent class. avb_search itself is never modified.
"""

from __future__ import annotations

import logging
from typing import Any

from avb_search.extractor import Extractor
from avb_search.extractor._base import ExtractFigureSegment, ExtractSegment, ExtractTableSegment
from vectorization.financial_model.financial_model_controller import (
    DEFAULT_DATA_START_COL,
    extract_financial_model,
)

logger = logging.getLogger(__name__)


class EquitiesExtractor(Extractor):
    """
    avb_search ``Extractor`` plus structured financial-model handling.

    Args:
        financial_model_prefix: Filename prefix that triggers structured
            extraction (e.g. ``"MODL_"``). When ``None``, all spreadsheets use
            the parent class's generic extractor.
        per_page_pdf: When ``True``, request one segment per PDF page rather
            than the parent's default chunking. Consumed here so it is never
            forwarded to ``Extractor``, which does not accept it.
        data_start_col: Index of the first period-value column in a Bloomberg
            model sheet, forwarded to ``extract_financial_model``.
        **kwargs: Forwarded verbatim to ``Extractor`` (currently only
            ``allowed_file_extensions``).
    """

    def __init__(
        self,
        financial_model_prefix: str | None = None,
        per_page_pdf: bool = False,
        data_start_col: int = DEFAULT_DATA_START_COL,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.financial_model_prefix = financial_model_prefix
        self.per_page_pdf = per_page_pdf
        self.data_start_col = data_start_col

    def extract(
        self,
        data: bytes,
        file_extension: str,
        *,
        display_name: str | None = None,
    ) -> list[ExtractSegment | ExtractTableSegment | ExtractFigureSegment]:
        """
        Extract segments, defaulting every segment's ``sourceType`` to
        ``"document"`` when nothing upstream already set one.

        Neither avb_search's own extractors nor this class's financial-model
        path (which sets ``sourceType: "financial_model"`` itself, so
        ``setdefault`` here is a no-op for those) ever populate ``sourceType``
        for a regular document -- it was left null in the index for every
        non-financial-model file. Azure AI Search's OData ``ne`` comparison
        against a null field doesn't match (SQL-style three-valued logic), so
        those documents were silently excluded from anything filtering on
        ``sourceType`` -- notably ``database.cache.sync_from_azure``'s own
        index-sync query and Chat's "Documents only" filter -- even though
        they were fully findable through plain unfiltered search/chat.

        Args:
            data (bytes): Raw bytes of the file to extract content from.
            file_extension (str): The file extension (e.g. ".pdf", ".docx").
            display_name (str | None): Optional human-readable file name.

        Returns:
            list[ExtractSegment | ExtractTableSegment | ExtractFigureSegment]:
            The parent extractor's segments, each with ``sourceType`` set.
        """
        segments = super().extract(data=data, file_extension=file_extension, display_name=display_name)
        for segment in segments:
            segment.meta.setdefault("sourceType", "document")
        return segments

    def _extract_xlsx(self, data: bytes, title: str | None) -> list[ExtractSegment]:
        """Route model files to the structured parser; defer everything else."""
        if self._is_financial_model(title):
            return self._extract_financial_model(data=data, title=title)
        return super()._extract_xlsx(data=data, title=title)

    def _is_financial_model(self, title: str | None) -> bool:
        prefix = self.financial_model_prefix
        return bool(prefix and title and title.startswith(prefix))

    def _extract_financial_model(self, data: bytes, title: str | None) -> list[ExtractSegment]:
        """
        Parse a Bloomberg model into one segment per metric x period.

        Falls back to the parent generic extractor when the file does not match
        the expected structure (``extract_financial_model`` returns no records).
        """
        filename = title or "unknown.xlsx"
        try:
            records = extract_financial_model(data=data, filename=filename, data_start_col=self.data_start_col)
        except Exception as exc:  # noqa: BLE001 - never break the run on one bad file
            logger.error("Financial model extraction failed for %s: %s", filename, exc)
            records = []

        if not records:
            logger.warning("No model records for %s; using generic xlsx extractor", filename)
            return super()._extract_xlsx(data=data, title=title)

        latest_reported = records[0]["latest_reported_date"]
        segments = [self._segment_from_record(record, title, latest_reported) for record in records]
        logger.info(
            "Produced %d segments from model %s (latest reported: %s)",
            len(segments),
            filename,
            latest_reported,
        )
        return segments

    @staticmethod
    def _segment_from_record(
        record: dict[str, Any],
        title: str | None,
        latest_reported: str | None,
    ) -> ExtractSegment:
        """
        Convert one extractor record into an ``ExtractSegment``.

        Company and document fields are taken straight from the model header,
        which is more reliable than LLM enrichment for spreadsheets, so the
        enricher can skip these files. ``documentDate`` is the latest reported
        period, indicating how current the model's actuals are.
        """
        meta = {
            "metric": record.get("metric"),
            "periodLabel": record.get("period_label"),
            "periodEndDate": record.get("period_end_date"),
            "isEstimate": record.get("is_estimate"),
            "periodicity": record.get("periodicity"),
            "currency": record.get("currency"),
            "unit": record.get("unit"),
            "segment": record.get("segment"),
            "companies": [record["company_name"]] if record.get("company_name") else [],
            "tickers": [record["ticker"]] if record.get("ticker") else [],
            "documentType": "financial_model",
            "sourceType": "financial_model",
            "documentDate": latest_reported,
        }
        return ExtractSegment(title=title, content=record["content"], meta=meta)
