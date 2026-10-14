"""
Tests for post-enrichment company-name canonicalization.

The false-positive regressions below (``test_*_regression_*``) each encode a
production incident documented in the module's own comments -- a legal-entity
or generic-descriptor suffix shared with an unrelated, already-known company
was enough to silently mis-canonicalize a batch of research notes onto the
wrong ticker. These must never start passing the fuzzy threshold again.
"""

from __future__ import annotations

from vectorization.company_resolution import (
    DEFAULT_FUZZY_THRESHOLD,
    company_name_similarity,
    normalize_ticker,
    resolve_companies,
)


class TestCompanyNameSimilarity:
    def test_matches_true_variant_spellings(self) -> None:
        assert company_name_similarity("Nike, Inc.", "NIKE Incorporated") >= DEFAULT_FUZZY_THRESHOLD

    def test_regression_ing_vs_just_eat_takeaway_shared_nv_suffix(self) -> None:
        score = company_name_similarity("ING Groep N.V.", "Just Eat Takeaway.com N.V.")
        assert score < DEFAULT_FUZZY_THRESHOLD

    def test_regression_novonesis_vs_netcompany_shared_jurisdiction_suffix(self) -> None:
        score = company_name_similarity("Novonesis A/S", "Netcompany Group A/S")
        assert score < DEFAULT_FUZZY_THRESHOLD

    def test_regression_yangzhou_yangjie_vs_microchip_shared_technology_suffix(self) -> None:
        score = company_name_similarity(
            "Yangzhou Yangjie Electronic Technology Co., Ltd.",
            "Microchip Technology",
        )
        assert score < DEFAULT_FUZZY_THRESHOLD

    def test_unrelated_names_score_low(self) -> None:
        assert company_name_similarity("Nike, Inc.", "Adidas AG") < DEFAULT_FUZZY_THRESHOLD


class TestNormalizeTicker:
    def test_strips_known_exchange_suffix(self) -> None:
        assert normalize_ticker("SRT GR") == "SRT"

    def test_leaves_unknown_suffix_untouched(self) -> None:
        assert normalize_ticker("SRT ZZ") == "SRT ZZ"

    def test_leaves_bare_ticker_untouched(self) -> None:
        assert normalize_ticker("AAPL") == "AAPL"


class TestResolveCompanies:
    def test_exact_ticker_match_replaces_name_and_ticker(self) -> None:
        known = {"NKE": "Nike, Inc."}
        names, tickers = resolve_companies(["Nike Inc (garbled OCR)"], ["NKE"], known)
        assert names == ["Nike, Inc."]
        assert tickers == ["NKE"]

    def test_ticker_normalized_before_lookup(self) -> None:
        known = {"NKE": "Nike, Inc."}
        names, tickers = resolve_companies(["Nike"], ["NKE US"], known)
        assert names == ["Nike, Inc."]
        assert tickers == ["NKE"]

    def test_fuzzy_name_match_when_ticker_absent(self) -> None:
        known = {"NKE": "Nike, Inc."}
        names, tickers = resolve_companies(["NIKE Incorporated"], [""], known)
        assert names == ["Nike, Inc."]
        assert tickers == ["NKE"]

    def test_no_match_leaves_entry_as_produced(self) -> None:
        known = {"NKE": "Nike, Inc."}
        names, tickers = resolve_companies(["Totally Unrelated Corp"], ["XYZ"], known)
        assert names == ["Totally Unrelated Corp"]
        assert tickers == ["XYZ"]

    def test_regression_shared_suffix_does_not_cause_cross_company_match(self) -> None:
        known = {"TKWY": "Just Eat Takeaway.com N.V."}
        names, tickers = resolve_companies(["ING Groep N.V."], [""], known)
        assert names == ["ING Groep N.V."]
        assert tickers == [""]

    def test_empty_known_companies_leaves_everything_unchanged(self) -> None:
        names, tickers = resolve_companies(["Some Company"], ["SC"], {})
        assert names == ["Some Company"]
        assert tickers == ["SC"]

    def test_shorter_tickers_list_treated_as_missing_ticker(self) -> None:
        known = {"NKE": "Nike, Inc."}
        names, tickers = resolve_companies(["NIKE Incorporated", "Totally Unrelated Corp"], ["NKE"], known)
        assert names == ["Nike, Inc.", "Totally Unrelated Corp"]
        assert tickers == ["NKE", ""]

    def test_custom_fuzzy_threshold_is_respected(self) -> None:
        # "Nike Athletics" vs "Nike, Inc." scores 90 -- above the default
        # threshold (80) but below a stricter custom one (95), proving the
        # parameter, not just the default, controls the cutoff.
        known = {"NKE": "Nike, Inc."}
        loose_names, _ = resolve_companies(["Nike Athletics"], [""], known, fuzzy_threshold=80)
        strict_names, strict_tickers = resolve_companies(["Nike Athletics"], [""], known, fuzzy_threshold=95)
        assert loose_names == ["Nike, Inc."]
        assert strict_names == ["Nike Athletics"]
        assert strict_tickers == [""]
