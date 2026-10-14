"""
Tests for the pure formatting/filter-building helpers in ``search.search_model``.

Excludes everything that talks to Azure AI Search or Azure AI Foundry
(``retrieve``, ``condense_query``, ``answer``, ``validate_answer``, client
construction) -- those need a live index/deployment and belong in the RAG
eval harness, not a fast unit suite.
"""

from __future__ import annotations

from datetime import date

from search.search_model import (
    Chunk,
    _chunk_from_result,
    build_odata_filter,
    format_context,
    format_investment_context,
    format_source_label,
    normalize_publisher,
)


class TestNormalizePublisher:
    def test_none_passes_through(self) -> None:
        assert normalize_publisher(None) is None

    def test_known_alias_is_resolved_case_insensitively(self) -> None:
        assert normalize_publisher("Big Data Federation Inc") == "Financial Model"
        assert normalize_publisher("  BigData Federation  ") == "Financial Model"

    def test_unknown_publisher_is_returned_unchanged(self) -> None:
        assert normalize_publisher("Morgan Stanley") == "Morgan Stanley"


class TestFormatSourceLabel:
    def test_all_parts_present(self) -> None:
        label = format_source_label(
            "Morgan Stanley",
            "research_note",
            "2025-06-01T00:00:00Z",
            companies=("ASML",),
            humanize_type=True,
        )
        assert label == "ASML · Morgan Stanley · research note · 2025-06-01"

    def test_humanize_type_false_keeps_underscores(self) -> None:
        label = format_source_label("Morgan Stanley", "research_note", "2025-06-01T00:00:00Z")
        assert label == "Morgan Stanley · research_note · 2025-06-01"

    def test_multiple_companies_are_comma_joined(self) -> None:
        label = format_source_label(None, None, None, companies=("ASML", "ASMI"))
        assert label == "ASML, ASMI"

    def test_publisher_alias_is_normalized(self) -> None:
        label = format_source_label("big data federation", "financial_model", "2025-01-01")
        assert label == "Financial Model · financial_model · 2025-01-01"

    def test_no_parts_falls_back_to_unknown_source(self) -> None:
        assert format_source_label(None, None, None) == "Unknown source"


class TestBuildODataFilter:
    def test_no_criteria_returns_none(self) -> None:
        assert build_odata_filter(None) is None

    def test_document_source_type_includes_null_for_legacy_records(self) -> None:
        assert build_odata_filter("document") == "(sourceType eq 'document' or sourceType eq null)"

    def test_other_source_type_is_a_plain_equality(self) -> None:
        assert build_odata_filter("financial_model") == "sourceType eq 'financial_model'"

    def test_publishers_are_ored_together(self) -> None:
        result = build_odata_filter(None, publishers=["Morgan Stanley", "UBS"])
        assert result == "(publisher eq 'Morgan Stanley' or publisher eq 'UBS')"

    def test_document_types_are_ored_together(self) -> None:
        result = build_odata_filter(None, document_types=["research_note"])
        assert result == "(documentType eq 'research_note')"

    def test_date_range_bounds(self) -> None:
        result = build_odata_filter(None, date_from=date(2025, 1, 1), date_to=date(2025, 12, 31))
        assert result == "documentDate ge 2025-01-01T00:00:00Z and documentDate le 2025-12-31T23:59:59Z"

    def test_multiple_criteria_are_anded_together(self) -> None:
        result = build_odata_filter("document", publishers=["UBS"], date_from=date(2025, 1, 1))
        assert result == (
            "(sourceType eq 'document' or sourceType eq null) and (publisher eq 'UBS') "
            "and documentDate ge 2025-01-01T00:00:00Z"
        )


def _chunk(**overrides) -> Chunk:
    defaults = dict(
        content="Revenue grew 10% year over year.",
        metric=None,
        period_label=None,
        period_end_date=None,
        is_estimate=None,
        document_type=None,
        document_date=None,
        publisher=None,
        score=1.0,
    )
    defaults.update(overrides)
    return Chunk(**defaults)


class TestFormatContext:
    def test_empty_chunks_yields_empty_string(self) -> None:
        assert format_context([]) == ""

    def test_chunk_with_tags(self) -> None:
        chunk = _chunk(document_type="research_note", publisher="Morgan Stanley", document_date="2025-06-01T00:00:00Z")
        assert format_context([chunk]) == (
            "[1] Revenue grew 10% year over year.  [research_note, Morgan Stanley, as of 2025-06-01]"
        )

    def test_chunk_without_tags_has_no_suffix(self) -> None:
        chunk = _chunk()
        assert format_context([chunk]) == "[1] Revenue grew 10% year over year."

    def test_multiple_chunks_are_numbered_in_order(self) -> None:
        chunks = [_chunk(content="first"), _chunk(content="second")]
        assert format_context(chunks) == "[1] first\n[2] second"


class TestFormatInvestmentContext:
    def test_chunk_with_metadata_header(self) -> None:
        chunk = _chunk(
            content="Revenue grew 10%.",
            document_type="research_note",
            document_date="2025-06-01T00:00:00Z",
            publisher="big data federation",
        )
        assert format_investment_context([chunk]) == (
            "[1] Financial Model | research_note | 2025-06-01\nRevenue grew 10%."
        )

    def test_chunk_without_metadata_has_bare_header(self) -> None:
        chunk = _chunk(content="Some content.")
        assert format_investment_context([chunk]) == "[1]\nSome content."


class TestChunkFromResult:
    def test_full_result_maps_every_field(self) -> None:
        result = {
            "content": "text",
            "metric": "Revenue",
            "periodLabel": "2025 Q1 (Rep)",
            "periodEndDate": "2025-03-31",
            "isEstimate": "reported",
            "documentType": "financial_model",
            "companies": ["ASML"],
            "documentDate": "2025-06-01T00:00:00Z",
            "publisher": "Bloomberg",
            "@search.score": 4.2,
            "segment": "EUV",
            "currency": "EUR",
            "unit": "Millions",
        }
        chunk = _chunk_from_result(result)
        assert chunk == Chunk(
            content="text",
            metric="Revenue",
            period_label="2025 Q1 (Rep)",
            period_end_date="2025-03-31",
            is_estimate="reported",
            document_type="financial_model",
            companies=("ASML",),
            document_date="2025-06-01T00:00:00Z",
            publisher="Bloomberg",
            score=4.2,
            segment="EUV",
            currency="EUR",
            unit="Millions",
        )

    def test_missing_fields_default_sensibly(self) -> None:
        chunk = _chunk_from_result({})
        assert chunk.content == ""
        assert chunk.companies == ()
        assert chunk.score == 0.0
        assert chunk.metric is None
