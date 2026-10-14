"""
Data models for Bloomberg portfolio reports.

Follows the same frozen-dataclass pattern as equities_search: immutable value
objects (record rows, metadata) are frozen; mutable aggregates (reports that
accumulate many records) are regular dataclasses.

All weight and return fields are in percent (e.g. 2.5 means 2.5 %).
None signals that Bloomberg left the cell empty — typically because the
security is absent from the portfolio or benchmark leg.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from portfolio_dashboard.config import load_app_config

_APP_CONFIG = load_app_config()

# ── shared metadata ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ReportMetadata:
    """Immutable header extracted from any Bloomberg report export."""

    report_name: str
    portfolio: str
    benchmark: str
    run_date: str
    base_currency: str
    period_start: str | None = None  # e.g. "12/31/2025" — attribution only
    period_end: str | None = None  # e.g. "06/19/2026"
    as_of: str | None = None  # e.g. "06/19/2026" — risk report only
    classification: str | None = None


# ── attribution report ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class AttributionRecord:
    """
    One security row from a Bloomberg portfolio attribution export.

    Suffix conventions: _p = portfolio, _b = benchmark,
    _d = active (difference = portfolio − benchmark).
    """

    level: int  # 0 = portfolio total, 1 = individual security
    name: str

    # Average weights (%)
    avg_wgt_p: float | None
    avg_wgt_b: float | None
    avg_wgt_d: float | None  # active weight = P − B

    # Total returns (%)
    tot_rtn_p: float | None
    tot_rtn_b: float | None
    tot_rtn_d: float | None

    # Factor-return contribution (%)
    factor_p: float | None
    factor_b: float | None
    factor_d: float | None

    # Selection-return contribution (%)
    selection_p: float | None
    selection_b: float | None
    selection_d: float | None

    # Timing-return contribution (%)
    timing_p: float | None
    timing_b: float | None
    timing_d: float | None

    # Contribution to return — the primary ranking metric (%)
    ctr_p: float | None
    ctr_b: float | None
    ctr_d: float | None


@dataclass
class AttributionReport:
    """Full Bloomberg portfolio attribution report."""

    metadata: ReportMetadata
    total: AttributionRecord  # Level-0 portfolio aggregate
    securities: list[AttributionRecord] = field(default_factory=list)

    @property
    def portfolio_return(self) -> float | None:
        """Portfolio total return in percent, taken from the level-0 total."""
        return self.total.tot_rtn_p

    @property
    def benchmark_return(self) -> float | None:
        """Benchmark total return in percent, taken from the level-0 total."""
        return self.total.tot_rtn_b

    @property
    def active_return(self) -> float | None:
        """Active return (portfolio minus benchmark) in percent."""
        return self.total.tot_rtn_d

    def portfolio_holdings(self) -> list[AttributionRecord]:
        """Securities with a non-zero portfolio weight."""
        return [r for r in self.securities if r.avg_wgt_p is not None]

    def top_active_contributors(self, n: int = 10) -> list[AttributionRecord]:
        """Return the ``n`` securities with the highest active contribution.

        Args:
            n: Number of securities to return.

        Returns:
            list[AttributionRecord]: Highest active contributors, descending.
        """
        return sorted(self.securities, key=lambda r: r.ctr_d or 0.0, reverse=True)[:n]

    def bottom_active_contributors(self, n: int = 10) -> list[AttributionRecord]:
        """Return the ``n`` securities with the lowest active contribution.

        Args:
            n: Number of securities to return.

        Returns:
            list[AttributionRecord]: Lowest active contributors, ascending.
        """
        return sorted(self.securities, key=lambda r: r.ctr_d or 0.0)[:n]


# ── tracking-error / risk report ───────────────────────────────────────────────


@dataclass(frozen=True)
class TrackingErrorRecord:
    """
    One security row from a Bloomberg ex-ante tracking-error report.

    ``total_risk_d`` is the stock's contribution (in %) to the total portfolio TE.
    """

    level: int
    name: str
    wgt_p: float | None  # current portfolio weight (%)
    wgt_d: float | None  # active weight, signed (%)
    gross_active_wgt: float | None  # |active weight|, always ≥ 0
    total_risk_d: float | None  # Total Risk — absolute risk of this position (%)
    contribution_p: float | None  # Contribution (%) — share of portfolio TE
    beta_p: float | None  # ex-ante beta
    non_factor_contrib: float | None = None  # Non-Factor Contrib (%) — idiosyncratic CTEV


@dataclass
class TrackingErrorReport:
    """Full Bloomberg tracking-error / risk report.

    Supports both the legacy 2-level format (L0=total, L1=security) and the
    current 4-level GICS format (L0=total, L1=sector, L2=industry group, L3=security).
    """

    metadata: ReportMetadata
    total: TrackingErrorRecord
    all_records: list[TrackingErrorRecord] = field(default_factory=list)  # every row, all levels
    sectors: list[TrackingErrorRecord] = field(default_factory=list)  # level 1 — GICS sectors
    industry_groups: list[TrackingErrorRecord] = field(default_factory=list)  # level 2 — industry groups
    securities: list[TrackingErrorRecord] = field(default_factory=list)  # level 3 (or level 1 in legacy)

    @property
    def portfolio_te(self) -> float | None:
        """Portfolio ex-ante tracking error in percent."""
        return self.total.total_risk_d

    @property
    def portfolio_beta(self) -> float | None:
        """Portfolio ex-ante beta.

        Falls back to a weight-weighted average of the individual securities
        when the portfolio-total beta cell is empty.

        Returns:
            float | None: The portfolio beta, or ``None`` when unavailable.
        """
        if self.total.beta_p is not None:
            return self.total.beta_p
        # Fallback: weighted-average from individual securities when the
        # portfolio-total beta cell is empty (e.g. formula with no cached value)
        pairs = [(r.beta_p, r.wgt_p) for r in self.all_records if r.level >= 1 and r.beta_p is not None and r.wgt_p]
        if not pairs:
            return None
        total_w = sum(w for _, w in pairs)
        return sum(b * w for b, w in pairs) / total_w if total_w else None

    def top_risk_contributors(self, n: int = 15) -> list[TrackingErrorRecord]:
        """Names ranked by absolute Non-Factor Contrib (%) when available, else Contribution (%).

        Non-Factor Contrib (%) is the idiosyncratic CTEV contribution, added in
        the updated TE Report Excel format. Falls back to Contribution (%) for
        older reports that do not yet carry the column.
        """
        has_nfc = any(r.non_factor_contrib is not None for r in self.securities)
        candidates = [
            r
            for r in self.securities
            if r.non_factor_contrib is not None or r.contribution_p is not None or r.wgt_p is not None
        ]
        if has_nfc:
            return sorted(
                candidates,
                key=lambda r: abs(r.non_factor_contrib or 0.0),
                reverse=True,
            )[:n]
        return sorted(
            candidates,
            key=lambda r: abs(r.contribution_p or 0.0),
            reverse=True,
        )[:n]

    def largest_active_positions(self, n: int = 25) -> list[TrackingErrorRecord]:
        """Return the ``n`` securities with the largest absolute active weight.

        Args:
            n: Number of securities to return.

        Returns:
            list[TrackingErrorRecord]: Largest active positions, descending.
        """
        return sorted(
            self.securities,
            key=lambda r: abs(r.wgt_d or 0.0),
            reverse=True,
        )[:n]

    def children_of(self, parent_name: str, parent_level: int) -> list[TrackingErrorRecord]:
        """Return direct children (parent_level + 1) of the named node, in CSV order."""
        child_level = parent_level + 1
        in_parent = False
        result: list[TrackingErrorRecord] = []
        for r in self.all_records:
            if r.level == parent_level and r.name == parent_name:
                in_parent = True
                continue
            if in_parent:
                if r.level <= parent_level:
                    break
                if r.level == child_level:
                    result.append(r)
        return result


# ── sector attribution report ──────────────────────────────────────────────────


@dataclass(frozen=True)
class SectorAttributionRecord:
    """
    One row from a Bloomberg sector (GICS) attribution report.

    level 0 = portfolio total, level 1 = GICS sector, level 2 = individual security.

    Brinson identity (at level-1): total_attribution ≈ allocation + selection + interaction.
    """

    level: int
    name: str

    avg_wgt_p: float | None
    avg_wgt_b: float | None
    avg_wgt_d: float | None

    ctr_p: float | None
    ctr_b: float | None
    ctr_d: float | None  # active contribution to return

    tot_rtn_p: float | None
    tot_rtn_b: float | None
    tot_rtn_d: float | None

    # Brinson decomposition — meaningful for level-1 sector rows
    allocation: float | None
    selection: float | None
    interaction: float | None
    total_attribution: float | None


@dataclass
class SectorAttributionReport:
    """Full Bloomberg sector attribution report."""

    metadata: ReportMetadata
    total: SectorAttributionRecord
    sectors: list[SectorAttributionRecord] = field(default_factory=list)  # level 1
    securities: list[SectorAttributionRecord] = field(default_factory=list)  # level 2


# ── factor attribution report ──────────────────────────────────────────────────

# EU model factors shown in charts (most relevant for European equity portfolios)
EU_FACTOR_NAMES: list[str] = _APP_CONFIG["factor_names"]["eu"]

# All individual factor columns present in Bloomberg factor attribution exports
ALL_FACTOR_NAMES: list[str] = _APP_CONFIG["factor_names"]["all"]


@dataclass
class FactorAttributionRecord:
    """One row from a Bloomberg factor attribution export."""

    level: int
    name: str
    avg_wgt_p: float | None
    avg_wgt_b: float | None
    avg_wgt_d: float | None
    tot_rtn_p: float | None
    tot_rtn_b: float | None
    tot_rtn_d: float | None
    ctr_p: float | None
    ctr_b: float | None
    ctr_d: float | None
    factor_d: float | None  # Factor Rtn (Contr)[D] — systematic component
    selection_d: float | None  # Selection Rtn (Contr)[D] — stock-specific alpha
    timing_d: float | None  # Time Rtn (Contr)[D]
    end_mkt_val: float | None = None  # End Mkt Val — total portfolio market value at report end-of-day
    factor_ctrs_d: dict = field(default_factory=dict)  # {factor_name: active_ctr}


@dataclass
class FactorAttributionReport:
    """Full Bloomberg multi-factor attribution report (GICS Sector classification)."""

    metadata: ReportMetadata
    total: FactorAttributionRecord
    sectors: list[FactorAttributionRecord] = field(default_factory=list)  # level 1
    securities: list[FactorAttributionRecord] = field(default_factory=list)  # level 2

    @property
    def portfolio_return(self) -> float | None:
        """Portfolio total return in percent, taken from the level-0 total."""
        return self.total.tot_rtn_p

    @property
    def active_return(self) -> float | None:
        """Active return (portfolio minus benchmark) in percent."""
        return self.total.tot_rtn_d

    @property
    def portfolio_end_mkt_val(self) -> float | None:
        """Total portfolio market value at end of day, from the level-0 total row."""
        return self.total.end_mkt_val

    def top_selectors(self, n: int = 10) -> list[FactorAttributionRecord]:
        """Top securities by pure stock-selection contribution (factor effects removed)."""
        held = [r for r in self.securities if r.avg_wgt_p is not None]
        return sorted(held, key=lambda r: r.selection_d or 0.0, reverse=True)[:n]

    def bottom_selectors(self, n: int = 10) -> list[FactorAttributionRecord]:
        """Return the ``n`` securities with the lowest stock-selection contribution.

        Args:
            n: Number of securities to return.

        Returns:
            list[FactorAttributionRecord]: Weakest selectors, ascending.
        """
        held = [r for r in self.securities if r.avg_wgt_p is not None]
        return sorted(held, key=lambda r: r.selection_d or 0.0)[:n]

    def top_active_contributors(self, n: int = 10) -> list[FactorAttributionRecord]:
        """Return the ``n`` securities with the highest active contribution.

        Args:
            n: Number of securities to return.

        Returns:
            list[FactorAttributionRecord]: Highest active contributors, descending.
        """
        return sorted(self.securities, key=lambda r: r.ctr_d or 0.0, reverse=True)[:n]

    def bottom_active_contributors(self, n: int = 10) -> list[FactorAttributionRecord]:
        """Return the ``n`` securities with the lowest active contribution.

        Args:
            n: Number of securities to return.

        Returns:
            list[FactorAttributionRecord]: Lowest active contributors, ascending.
        """
        return sorted(self.securities, key=lambda r: r.ctr_d or 0.0)[:n]


# ── TE factor risk decomposition ───────────────────────────────────────────────

# Style/systematic risk factor categories in Bloomberg's European risk model
STYLE_FACTOR_CATEGORIES: set[str] = set(_APP_CONFIG["style_factor_categories"])


@dataclass(frozen=True)
class TERiskFactorRecord:
    """One row from a Bloomberg TE factor risk decomposition report."""

    factor_name: str  # full "Category:Label" string, e.g. "Beta:EU Beta"
    category: str  # e.g. "Beta"
    factor_label: str  # e.g. "EU Beta"
    portfolio_exposure: float | None
    benchmark_exposure: float | None
    active_exposure: float | None
    factor_vol: float | None  # Factor Vol (Std %)
    tot_active_std: float | None  # Tot. Active (Std)
    marginal: float | None  # Marginal (X100)
    contribution_pct: float | None  # Contribution %


@dataclass
class TERiskFactorReport:
    """Bloomberg TE factor risk decomposition report (factor-level TE attribution)."""

    metadata: ReportMetadata
    factors: list[TERiskFactorRecord] = field(default_factory=list)

    def top_te_contributors(self, n: int = 15) -> list[TERiskFactorRecord]:
        """Return the ``n`` factors contributing most to tracking error.

        Args:
            n: Number of factors to return.

        Returns:
            list[TERiskFactorRecord]: Largest TE contributors, descending.
        """
        return sorted(self.factors, key=lambda r: r.contribution_pct or 0.0, reverse=True)[:n]

    def bottom_te_contributors(self, n: int = 15) -> list[TERiskFactorRecord]:
        """Return the ``n`` factors contributing least to tracking error.

        Args:
            n: Number of factors to return.

        Returns:
            list[TERiskFactorRecord]: Smallest TE contributors, ascending.
        """
        return sorted(self.factors, key=lambda r: r.contribution_pct or 0.0)[:n]

    def style_factors(self) -> list[TERiskFactorRecord]:
        """Returns only style/systematic factors, excluding country/industry/currency."""
        return [r for r in self.factors if r.category in STYLE_FACTOR_CATEGORIES]


# ── public type alias ──────────────────────────────────────────────────────────

# Any concrete report object produced by the parsing layer. All members expose a
# ``metadata`` attribute and the level-0 aggregate used by the storage layer.
Report = AttributionReport | TrackingErrorReport | SectorAttributionReport | FactorAttributionReport | TERiskFactorReport
