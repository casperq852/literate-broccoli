"""Dashboard model: pure portfolio grouping and classification helpers.

This is the a.s.r. Model layer for the dashboard topic. Every function here is
a pure calculation over report metadata or portfolio names, without Streamlit or
I/O side effects, so it can be reused and unit-tested in isolation.
"""

from __future__ import annotations

from portfolio_dashboard.reports.reports_model import (
    AttributionReport,
    FactorAttributionReport,
    Report,
    SectorAttributionReport,
    TERiskFactorReport,
    TrackingErrorReport,
)


def report_storage_key(report: Report) -> str | None:
    """Return the per-portfolio slot name for a parsed report.

    The isinstance order is significant: more specific report types are matched
    before their base types so a report never lands in the wrong slot.

    Args:
        report: Any parsed report.

    Returns:
        str | None: The slot key (``"te_factors"``, ``"attr"``, ``"te"``,
        ``"sector"`` or ``"factor"``), or ``None`` when the report type has no
        dedicated slot.
    """
    if isinstance(report, TERiskFactorReport):
        return "te_factors"
    if isinstance(report, AttributionReport):
        return "attr"
    if isinstance(report, TrackingErrorReport):
        return "te"
    if isinstance(report, SectorAttributionReport):
        return "sector"
    if isinstance(report, FactorAttributionReport):
        return "factor"
    return None


def group_portfolios_by_family(portfolio_names: list[str], family_map: dict[str, str]) -> dict[str, list[str]]:
    """Group portfolio names into fund families using code substrings.

    The longest family code is matched first to avoid partial matches. Names
    with no matching code form their own single-entry group.

    Args:
        portfolio_names: Portfolio names to group.
        family_map: Mapping of lowercase fund-code substrings to family labels.

    Returns:
        dict[str, list[str]]: Family label mapped to the portfolio names in it.
    """
    groups: dict[str, list[str]] = {}
    ordered = sorted(family_map.items(), key=lambda item: len(item[0]), reverse=True)
    for name in portfolio_names:
        lower = name.lower()
        family = next((canonical for code, canonical in ordered if code in lower), None)
        groups.setdefault(family or name, []).append(name)
    return groups


def is_master_portfolio(name: str, master_codes: set[str]) -> bool:
    """Return whether a portfolio is a fund/master level portfolio.

    Master portfolios match a known fund code but are not an alpha, core, or
    cash sub-portfolio. Master portfolios expose only sector and industry-group
    aggregates in their tracking-error report, not security-level data.

    Args:
        name: Portfolio name.
        master_codes: Known lowercase fund-code substrings.

    Returns:
        bool: ``True`` when ``name`` is a master/fund-level portfolio.
    """
    lower = name.lower()
    return (
        any(code in lower for code in master_codes)
        and "alpha" not in lower
        and "core" not in lower
        and "cash" not in lower
    )


def sub_portfolio_label(name: str) -> str:
    """Return the short switcher label for a sub-portfolio name.

    Args:
        name: Portfolio name.

    Returns:
        str: ``"Alpha"``, ``"Core"``, ``"Cash"`` or ``"Fonds"``.
    """
    lower = name.lower()
    if "alpha" in lower:
        return "Alpha"
    if "core" in lower:
        return "Core"
    if "cash" in lower:
        return "Cash"
    return "Fonds"
