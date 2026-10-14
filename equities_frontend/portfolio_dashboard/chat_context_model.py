"""
Summarizes the colleague's Portfolio Dashboard reports into a compact,
LLM-ready form, and detects/formats portfolio questions for Chat.

Pure functions only -- no Streamlit, no I/O. Deliberately plain Python (lists
of dicts, direct attribute access on the report dataclasses) rather than
pandas: the inputs here are a handful of already-typed record objects per
portfolio, not a tabular transformation, so a DataFrame would add nothing.
The colleague's own dashboard/silver-history code uses pandas at its own
Plotly-rendering boundary (see their docs/architecture.md) -- that's their
package, vendored unmodified; this module has no reason to follow suit.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from rapidfuzz import fuzz, process

_DATE_INPUT_FORMAT = "%m/%d/%Y"
_DEFAULT_FUZZY_THRESHOLD = 80


def _normalize_date(value: str | None) -> str | None:
    """Convert a Bloomberg "MM/DD/YYYY" date string to "YYYY-MM-DD", or None."""
    if not value:
        return None
    try:
        return datetime.strptime(value, _DATE_INPUT_FORMAT).strftime("%Y-%m-%d")
    except ValueError:
        return None


def _signed(value: float | None) -> str:
    """Format an active-weight/exposure value with an explicit +/- sign, or "n/a" if unavailable."""
    return f"{value:+.2f}" if value is not None else "n/a"


def summarize_portfolio(portfolio: str, reports: dict[str, Any]) -> dict[str, Any] | None:
    """
    Build one ``database.cache.upsert_portfolio_metrics`` record from a
    portfolio's latest reports.

    Args:
        portfolio: The portfolio's display name, as returned by
            ``SilverStore.load_reports_by_portfolio()``'s keys.
        reports: One portfolio's report dict, e.g. ``{"attr": AttributionReport,
            "te": TrackingErrorReport, "te_factors": TERiskFactorReport, ...}``
            -- the exact shape ``SilverStore.load_reports_by_portfolio()``
            returns per portfolio (see dashboard_controller.py's own usage).

    Returns:
        dict[str, Any] | None: A record ready for ``upsert_portfolio_metrics``,
        or ``None`` if none of the expected reports are present for this
        portfolio (nothing meaningful to store).
    """
    attr_report = reports.get("attr")
    te_report = reports.get("te")
    te_factor_report = reports.get("te_factors")

    any_report = attr_report or te_report or te_factor_report
    if any_report is None:
        return None

    metadata = any_report.metadata
    as_of_date = (
        _normalize_date(getattr(metadata, "as_of", None))
        or _normalize_date(getattr(metadata, "period_end", None))
        or _normalize_date(getattr(metadata, "run_date", None))
    )
    if not as_of_date:
        return None

    # Whole portfolio, not a top-N slice: an alpha portfolio only holds ~40-60
    # names, so every position is small enough to store and matters for
    # per-company lookups (see find_company_position) -- a top-15 cut would
    # silently drop names outside the largest positions.
    #
    # Sorted and led by ACTIVE weight (vs. the benchmark), not absolute
    # portfolio weight: a name can be a large absolute holding while being
    # benchmark-neutral (no risk story), or a small absolute holding that's
    # heavily over/underweight (the actual risk driver) -- desk convention is
    # to discuss positioning in benchmark-relative terms, not absolute terms.
    holdings = []
    if attr_report is not None:
        holdings = sorted(
            (
                {"name": r.name, "active_weight_pct": r.avg_wgt_d, "weight_pct": r.avg_wgt_p}
                for r in attr_report.portfolio_holdings()
            ),
            key=lambda h: abs(h["active_weight_pct"] or 0.0),
            reverse=True,
        )

    risk_contributors = []
    portfolio_te = portfolio_beta = None
    if te_report is not None:
        portfolio_te = te_report.portfolio_te
        portfolio_beta = te_report.portfolio_beta
        # Mirror TrackingErrorReport.top_risk_contributors()'s own field choice
        # (and the dashboard's own Security Risk Table, dashboard/tabs/risk_view.py):
        # newer TE Report exports carry Non-Factor Contrib -- the idiosyncratic
        # CTEV contribution -- which is preferred over the older Contribution
        # field when present, for the whole report uniformly. Using plain
        # contribution_p unconditionally (as this used to) can disagree with
        # what the dashboard itself shows for the same portfolio/date.
        has_non_factor_contrib = any(r.non_factor_contrib is not None for r in te_report.securities)
        risk_contributors = sorted(
            (
                {
                    "name": r.name,
                    "active_weight_pct": r.wgt_d,
                    "total_risk_d": r.total_risk_d,
                    "contribution_pct": (r.non_factor_contrib if has_non_factor_contrib else r.contribution_p),
                    "contribution_label": "Non-Factor Contrib" if has_non_factor_contrib else "Contribution",
                }
                for r in te_report.securities
            ),
            key=lambda r: abs(r["contribution_pct"] or 0.0),
            reverse=True,
        )

    factor_exposures = []
    if te_factor_report is not None:
        factor_exposures = [
            {
                "factor_label": r.factor_label,
                "category": r.category,
                "active_exposure": r.active_exposure,
                "contribution_pct": r.contribution_pct,
            }
            for r in te_factor_report.top_te_contributors(n=len(te_factor_report.factors))
        ]

    return {
        "portfolio": portfolio,
        "as_of_date": as_of_date,
        "benchmark": metadata.benchmark,
        "base_currency": metadata.base_currency,
        "portfolio_return": attr_report.portfolio_return if attr_report else None,
        "active_return": attr_report.active_return if attr_report else None,
        "portfolio_te": portfolio_te,
        "portfolio_beta": portfolio_beta,
        "holdings": holdings,
        "risk_contributors": risk_contributors,
        "factor_exposures": factor_exposures,
    }


def distinct_security_names(reports_by_portfolio: dict[str, dict[str, Any]]) -> list[str]:
    """
    Collect every distinct security display name held across all cached portfolios.

    Seeds ``companies.company_controller.CompanyController.sync_company_reference``'s
    authoritative company-name reference: unlike per-document LLM extraction,
    these names come straight from real Bloomberg holdings data, so a name
    appearing here is definitely a real, currently-covered company -- not
    ground truth for spelling (Bloomberg's own display convention, e.g. "ASML
    HOLDING NV"), just for existence.

    Reuses the exact same two accessors ``summarize_portfolio`` already reads
    per portfolio (``attr_report.portfolio_holdings()``, ``te_report.securities``)
    rather than a new report-reading path, run across every portfolio instead
    of just one.

    Args:
        reports_by_portfolio: ``{portfolio_name: {"attr": AttributionReport,
            "te": TrackingErrorReport, ...}}`` -- the shape
            ``SilverStore.load_reports_by_portfolio()`` (and
            ``silver_cache_model.load_reports()``) returns.

    Returns:
        list[str]: Sorted, deduplicated security display names.
    """
    names: set[str] = set()
    for reports in reports_by_portfolio.values():
        attr_report = reports.get("attr")
        if attr_report is not None:
            names.update(r.name for r in attr_report.portfolio_holdings())
        te_report = reports.get("te")
        if te_report is not None:
            names.update(r.name for r in te_report.securities)
    return sorted(name for name in names if name)


# Each fund family stores exactly 4 portfolios: the combined live fund
# ("<CODE> FONDS"), its cash sleeve ("CASH_<CODE>"), and its two model
# portfolios ("EQxx ALPHA_<CODE>" / "EQxx CORE_<CODE>", the latter a smart-beta
# strategy) -- confirmed against the real cache.get_portfolio_names() output:
# ['ADAAF FONDS', 'ADWAF FONDS', 'AEIIAEF FONDS', 'CASH_ADAAF', 'CASH_ADWAF',
#  'CASH_AEIIEAF', 'EQEU ALPHA_AEIIEAF', 'EQEU CORE_AEIIEAF', 'EQUS ALPHA_ADAAF',
#  'EQUS CORE_ADAAF', 'EQWW ALPHA_ADWAF', 'EQWW CORE_ADWAF'].
#
# The Europe family's code is spelled two ways in the data itself (AEIIAEF in
# the FONDS name, AEIIEAF everywhere else) -- both map to one canonical family.
_FAMILY_CODES: dict[str, tuple[str, ...]] = {
    "ADAAF": ("ADAAF",),
    "ADWAF": ("ADWAF",),
    "AEIIEAF": ("AEIIAEF", "AEIIEAF"),
}

# Natural-language region names a question might use instead of the raw code.
_FAMILY_NAME_ALIASES: dict[str, tuple[str, ...]] = {
    "ADAAF": ("verenigde staten", "united states"),
    "ADWAF": ("global", "wereld"),
    "AEIIEAF": ("europe", "europa"),
}

# Short region codes ("US", "WW") are only matched case-sensitively (against
# the original, un-lowered question) -- "us" lower-case is far too common an
# English pronoun to treat as a region reference.
_FAMILY_SHORT_CODES: dict[str, str] = {
    "ADAAF": "US",
    "ADWAF": "WW",
}

# Checked in this order so a specific sleeve mention always wins over the
# generic "fund"/"fonds" wording that also appears in ordinary phrasing.
_SLEEVE_KEYWORDS: list[tuple[str, tuple[str, ...]]] = [
    ("alpha", ("alpha",)),
    ("core", ("core", "smart beta", "smart-beta")),
    ("cash", ("cash",)),
    ("combined", ("combined", "fonds", "fund")),
]

# Analysts ask about the alpha sleeve by far the most often; assume it when a
# question names a family but doesn't say which sleeve.
_DEFAULT_SLEEVE = "alpha"


def _family_of(portfolio_name: str) -> str | None:
    """Return which fund family a stored portfolio name belongs to, or None."""
    name_upper = portfolio_name.upper()
    for family, codes in _FAMILY_CODES.items():
        if any(code in name_upper for code in codes):
            return family
    return None


def _sleeve_of(portfolio_name: str) -> str:
    """Return a stored portfolio name's sleeve: alpha, core, cash, or combined."""
    name_upper = portfolio_name.upper()
    if "CASH" in name_upper:
        return "cash"
    if "ALPHA" in name_upper:
        return "alpha"
    if "CORE" in name_upper:
        return "core"
    return "combined"  # the "<CODE> FONDS" shape has no sleeve keyword of its own


def _detect_family(question: str) -> str | None:
    """Return the fund family named or implied in a question, or None."""
    question_lower = question.lower()
    for family, codes in _FAMILY_CODES.items():
        if any(re.search(rf"\b{re.escape(code.lower())}\b", question_lower) for code in codes):
            return family
    for family, aliases in _FAMILY_NAME_ALIASES.items():
        if any(re.search(rf"\b{re.escape(alias)}\b", question_lower) for alias in aliases):
            return family
    for family, short_code in _FAMILY_SHORT_CODES.items():
        if re.search(rf"\b{re.escape(short_code)}\b", question):  # case-sensitive, see _FAMILY_SHORT_CODES
            return family
    return None


def _detect_sleeve(question_lower: str) -> str | None:
    """Return the sleeve named in a question (alpha/core/cash/combined), or None if unstated."""
    for sleeve, keywords in _SLEEVE_KEYWORDS:
        if any(re.search(rf"\b{re.escape(kw)}\b", question_lower) for kw in keywords):
            return sleeve
    return None


def detect_portfolio_question(question: str, known_portfolios: list[str]) -> str | None:
    """
    Return the known portfolio name implied by a question, or None.

    Args:
        question: The user's typed chat question.
        known_portfolios: Portfolio names currently in the cache (from
            ``database.cache.get_portfolio_names()``), e.g. Bloomberg's raw
            ``"ADAAF FONDS"`` / ``"EQUS ALPHA_ADAAF"``.

    Returns:
        str | None: The matched portfolio name (from ``known_portfolios``), or
        ``None`` if no known fund family is named or implied.

        Two passes: first, a literal mention of a stored name as a substring
        (the longest one wins, so a more specific match isn't shadowed by a
        shorter one). Otherwise, the question is classified into a fund
        family (by code, e.g. "ADAAF", or by region name/code, e.g. "US" /
        "Verenigde Staten" / "Europe") and a sleeve (alpha / core / cash /
        combined, from an explicit keyword -- defaulting to the alpha sleeve
        when the question names a family but not a sleeve, since that's what
        analysts ask about by far the most often).
    """
    question_lower = question.lower()
    verbatim_matches = [name for name in known_portfolios if name.lower() in question_lower]
    if verbatim_matches:
        return max(verbatim_matches, key=len)

    family = _detect_family(question)
    if not family:
        return None
    sleeve = _detect_sleeve(question_lower) or _DEFAULT_SLEEVE

    candidates = [name for name in known_portfolios if _family_of(name) == family and _sleeve_of(name) == sleeve]
    if not candidates:
        return None
    return max(candidates, key=len)


def format_portfolio_context(record: dict[str, Any]) -> str:
    """
    Render one ``database.cache.get_portfolio_metrics()`` row as a compact,
    citation-ready text block for injection into the chat prompt.

    The alpha sleeve is small enough (40-60 names) that the LLM should see
    every position, not just the largest ones -- ``summarize_portfolio``
    already stores the full portfolio, this is the only place it was still
    being cut down. The other sleeves (core/smart-beta, combined, cash) can be
    much larger, so they stay capped at the top 10 per section to keep the
    prompt from ballooning for sleeves nobody asked to see in full.

    Args:
        record: A row as returned by ``database.cache.get_portfolio_metrics()``.

    Returns:
        str: A compact Markdown-ish summary of the portfolio's latest metrics.
    """
    full_detail = _sleeve_of(record["portfolio"]) == "alpha"
    limit = None if full_detail else 10

    lines = [
        f"Portfolio: {record['portfolio']} (as of {record['as_of_date']})",
        "All positioning figures below are relative to the benchmark (active weight, active "
        "exposure) -- discuss holdings, risk, and factors in benchmark-relative terms, not "
        "absolute portfolio weight.",
        f"Benchmark: {record.get('benchmark') or 'n/a'} | Base currency: {record.get('base_currency') or 'n/a'}",
    ]
    if record.get("portfolio_return") is not None or record.get("active_return") is not None:
        lines.append(
            f"Portfolio return: {record.get('portfolio_return')}% | Active return: {record.get('active_return')}%"
        )
    if record.get("portfolio_te") is not None or record.get("portfolio_beta") is not None:
        lines.append(f"Tracking error: {record.get('portfolio_te')}% | Beta: {record.get('portfolio_beta')}")

    if record.get("holdings"):
        lines.append("Holdings by active weight vs. benchmark:" if full_detail else "Top active holdings vs. benchmark:")
        lines.extend(
            f"  - {w['name']}: {_signed(w['active_weight_pct'])}% active (portfolio weight {w['weight_pct']}%)"
            for w in record["holdings"][:limit]
        )

    if record.get("risk_contributors"):
        lines.append("Risk contributors:" if full_detail else "Top risk contributors:")
        lines.extend(
            f"  - {r['name']}: {r['contribution_pct']}% {r.get('contribution_label', 'Contribution')} to TE "
            f"(active weight {_signed(r['active_weight_pct'])}%, {r['total_risk_d']}% total position risk)"
            for r in record["risk_contributors"][:limit]
        )

    if record.get("factor_exposures"):
        lines.append("Factor exposures:" if full_detail else "Top factor exposures:")
        lines.extend(
            f"  - {f['factor_label']} ({f['category']}): active exposure {f['active_exposure']}, "
            f"{f['contribution_pct']}% of TE"
            for f in record["factor_exposures"][:limit]
        )

    return "\n".join(lines)


def find_company_position(
    company: str, record: dict[str, Any], fuzzy_threshold: int = _DEFAULT_FUZZY_THRESHOLD
) -> dict[str, Any] | None:
    """
    Find a company's position within one portfolio's cached holdings, or None if not held.

    Bloomberg security names (e.g. "ASML HOLDING NV") don't share a ticker
    with this app's own company records, so matching is fuzzy (same
    rapidfuzz.fuzz.WRatio approach as vectorization/company_resolution.py's
    name canonicalization) rather than an exact join.

    Args:
        company: The company's display name (or a close alias) to look for.
        record: A row as returned by ``database.cache.get_portfolio_metrics()``.
        fuzzy_threshold: Minimum rapidfuzz score (0-100) to accept a match.

    Returns:
        dict[str, Any] | None: ``{"matched_name", "active_weight_pct",
        "weight_pct", "total_risk_d", "contribution_pct", "contribution_label"}``,
        or ``None`` if this portfolio doesn't hold a name that matches ``company``.
    """
    holdings = record.get("holdings") or []
    if not company or not holdings:
        return None

    names = [h["name"] for h in holdings]
    match = process.extractOne(company, names, scorer=fuzz.WRatio, score_cutoff=fuzzy_threshold)
    if not match:
        return None

    matched_name = match[0]
    holding = next((h for h in holdings if h["name"] == matched_name), {})
    risk_entry = next(
        (r for r in (record.get("risk_contributors") or []) if r["name"] == matched_name),
        None,
    )
    return {
        "matched_name": matched_name,
        "active_weight_pct": holding.get("active_weight_pct"),
        "weight_pct": holding.get("weight_pct"),
        "total_risk_d": risk_entry["total_risk_d"] if risk_entry else None,
        "contribution_pct": risk_entry["contribution_pct"] if risk_entry else None,
        "contribution_label": risk_entry.get("contribution_label", "Contribution") if risk_entry else "Contribution",
    }


def format_company_portfolio_context(company: str, positions: list[dict[str, Any]]) -> str:
    """
    Render a company's positions across portfolios as a compact, citation-ready text block.

    Args:
        company: The company's display name, as passed to ``find_company_position``.
        positions: One entry per portfolio that holds ``company``, each
            ``{"portfolio", "as_of_date", **find_company_position() result}``.

    Returns:
        str: A compact summary, one line per portfolio holding the company, framed
        in benchmark-relative (active weight) terms first.
    """
    lines = [
        f"Portfolio positioning for {company} (relative to each portfolio's benchmark):",
    ]
    for p in positions:
        active = f"{_signed(p.get('active_weight_pct'))}% active"
        absolute = f"{p['weight_pct']}%" if p.get("weight_pct") is not None else "n/a"
        risk_bits = []
        if p.get("contribution_pct") is not None:
            risk_bits.append(f"{p['contribution_pct']}% {p.get('contribution_label', 'Contribution')} to TE")
        if p.get("total_risk_d") is not None:
            risk_bits.append(f"{p['total_risk_d']}% total position risk")
        risk = f", risk: {' / '.join(risk_bits)}" if risk_bits else ""
        lines.append(f"  - {p['portfolio']} (as of {p['as_of_date']}): {active} (portfolio weight {absolute}){risk}")
    return "\n".join(lines)
