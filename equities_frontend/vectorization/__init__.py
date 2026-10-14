"""
vectorization

Extension layer on top of avb_search for the Equities desk. Adds:

  * Structured Bloomberg financial-model extraction (``extract_financial_model``)

Custom extraction is wired into avb_search by subclassing -- ``EquitiesExtractor``
-- so avb_search itself is never modified. Document metadata is produced by the
native avb_search field-driven ``MetadataEnricher`` (configured via the
``enrichment`` block in ``configs/config.yaml``).

Run the ingestion pipeline with::

    python -m vectorization --config_file equities_frontend/configs/config.yaml ...
"""

from __future__ import annotations

from vectorization.extraction.extraction_controller import EquitiesExtractor
from vectorization.financial_model.financial_model_controller import extract_financial_model

__all__ = [
    "EquitiesExtractor",
    "extract_financial_model",
]
