"""
Pure discounted-cash-flow valuation math.

Every function here takes and returns plain floats/lists -- no Streamlit, no
LLM calls, no Azure access. This keeps the valuation arithmetic deterministic,
independently testable, and auditable: the same inputs always produce the
same outputs, and every intermediate step is a named, inspectable function
rather than one large calculation.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class DcfResult:
    """Full year-by-year DCF output, ready for display and Excel export."""

    projected_revenue: list[float]
    projected_ebit: list[float]
    projected_nopat: list[float]
    projected_fcf: list[float]
    discount_factors: list[float]
    pv_fcf: list[float]
    terminal_value: float
    pv_terminal_value: float
    enterprise_value: float
    equity_value: float
    value_per_share: float


def fade_growth_rate(initial_rate: float, terminal_rate: float, projection_years: int) -> list[float]:
    """
    Linearly fade the revenue growth rate from ``initial_rate`` to ``terminal_rate``.

    Args:
        initial_rate (float): Growth rate applied in year 1 (decimal fraction).
        terminal_rate (float): Growth rate the fade converges to by the final year.
        projection_years (int): Number of explicit projection years.

    Returns:
        list[float]: One growth rate per projection year, year 1 first.

    Raises:
        ValueError: If ``projection_years`` is less than 1.
    """
    if projection_years < 1:
        raise ValueError("projection_years must be at least 1.")
    if projection_years == 1:
        return [initial_rate]
    step = (terminal_rate - initial_rate) / (projection_years - 1)
    return [initial_rate + step * year for year in range(projection_years)]


def project_revenue(base_revenue: float, growth_rates: list[float]) -> list[float]:
    """
    Compound ``base_revenue`` forward by each year's growth rate.

    Args:
        base_revenue (float): The most recent actual (reported) revenue.
        growth_rates (list[float]): One growth rate per projection year.

    Returns:
        list[float]: Projected revenue, one value per projection year.
    """
    projected: list[float] = []
    prior = base_revenue
    for rate in growth_rates:
        prior = prior * (1 + rate)
        projected.append(prior)
    return projected


def project_ebit(projected_revenue: list[float], ebit_margins: list[float]) -> list[float]:
    """
    Apply each year's EBIT margin assumption to that year's projected revenue.

    Args:
        projected_revenue (list[float]): Projected revenue per year.
        ebit_margins (list[float]): EBIT margin assumption per year (decimal fraction).

    Returns:
        list[float]: Projected EBIT, one value per projection year.

    Raises:
        ValueError: If the two lists have different lengths.
    """
    if len(projected_revenue) != len(ebit_margins):
        raise ValueError("projected_revenue and ebit_margins must have the same length.")
    return [revenue * margin for revenue, margin in zip(projected_revenue, ebit_margins)]


def apply_percent_of_revenue(projected_revenue: list[float], percent: float) -> list[float]:
    """
    Derive a per-year value (e.g. D&A or Capex) as a flat percentage of projected revenue.

    A common simplification when the underlying model doesn't split D&A/Capex
    forecasts out explicitly -- the user sets one ratio and it scales with
    the projected revenue path.

    Args:
        projected_revenue (list[float]): Projected revenue per year.
        percent (float): The percentage of revenue (decimal fraction).

    Returns:
        list[float]: One value per projection year.
    """
    return [revenue * percent for revenue in projected_revenue]


def compute_nopat(projected_ebit: list[float], tax_rate: float) -> list[float]:
    """NOPAT = EBIT * (1 - tax_rate), applied to every projection year."""
    return [ebit * (1 - tax_rate) for ebit in projected_ebit]


def compute_unlevered_fcf(
    nopat: list[float],
    d_and_a: list[float],
    capex: list[float],
    change_in_nwc: list[float],
) -> list[float]:
    """
    Unlevered free cash flow = NOPAT + D&A - Capex - change in net working capital.

    Args:
        nopat (list[float]): Net operating profit after tax, per year.
        d_and_a (list[float]): Depreciation & amortization add-back, per year.
        capex (list[float]): Capital expenditure, per year.
        change_in_nwc (list[float]): Change in net working capital, per year
            (pass a list of zeros when this isn't derivable from the retrieved
            model -- a documented simplification, not a silent guess).

    Returns:
        list[float]: Unlevered FCF, one value per projection year.

    Raises:
        ValueError: If the input lists don't all have the same length.
    """
    lengths = {len(nopat), len(d_and_a), len(capex), len(change_in_nwc)}
    if len(lengths) != 1:
        raise ValueError("nopat, d_and_a, capex, and change_in_nwc must all have the same length.")
    return [
        year_nopat + year_d_and_a - year_capex - year_delta_nwc
        for year_nopat, year_d_and_a, year_capex, year_delta_nwc in zip(nopat, d_and_a, capex, change_in_nwc)
    ]


def discount_factors(discount_rate: float, n_years: int) -> list[float]:
    """
    End-of-year discount factors: 1 / (1 + discount_rate)^t for t = 1..n_years.

    Mid-year discounting convention is not applied here -- end-of-year
    discounting is the documented, simpler convention used throughout.

    Args:
        discount_rate (float): The discount rate / WACC (decimal fraction).
        n_years (int): Number of explicit projection years.

    Returns:
        list[float]: One discount factor per year.
    """
    return [1 / (1 + discount_rate) ** year for year in range(1, n_years + 1)]


def discount_cash_flows(cash_flows: list[float], factors: list[float]) -> list[float]:
    """
    Elementwise present value of each cash flow.

    Args:
        cash_flows (list[float]): Cash flows per year.
        factors (list[float]): Discount factors per year, same length as ``cash_flows``.

    Returns:
        list[float]: Present values, one per year.

    Raises:
        ValueError: If the two lists have different lengths.
    """
    if len(cash_flows) != len(factors):
        raise ValueError("cash_flows and factors must have the same length.")
    return [cash_flow * factor for cash_flow, factor in zip(cash_flows, factors)]


def terminal_value(final_year_fcf: float, discount_rate: float, terminal_growth_rate: float) -> float:
    """
    Gordon growth terminal value at the end of the explicit projection period.

    Args:
        final_year_fcf (float): Unlevered FCF in the last explicit projection year.
        discount_rate (float): The discount rate / WACC (decimal fraction).
        terminal_growth_rate (float): Perpetual growth rate beyond the explicit period.

    Returns:
        float: The undiscounted terminal value.

    Raises:
        ValueError: If ``discount_rate <= terminal_growth_rate`` (the Gordon
            growth formula is undefined/negative in that case -- this must
            never silently produce a nonsensical number).
    """
    if discount_rate <= terminal_growth_rate:
        raise ValueError(
            f"discount_rate ({discount_rate}) must be greater than terminal_growth_rate "
            f"({terminal_growth_rate}) for the Gordon growth terminal value to be defined."
        )
    return final_year_fcf * (1 + terminal_growth_rate) / (discount_rate - terminal_growth_rate)


def enterprise_value(pv_fcf: list[float], pv_terminal_value: float) -> float:
    """Sum of explicit-period present values plus the discounted terminal value."""
    return sum(pv_fcf) + pv_terminal_value


def equity_value(enterprise_value_amount: float, net_debt: float) -> float:
    """Enterprise value minus net debt."""
    return enterprise_value_amount - net_debt


def value_per_share(equity_value_amount: float, shares_outstanding: float) -> float:
    """
    Equity value divided by shares outstanding.

    Raises:
        ValueError: If ``shares_outstanding`` is not positive.
    """
    if shares_outstanding <= 0:
        raise ValueError("shares_outstanding must be positive.")
    return equity_value_amount / shares_outstanding


def dcf_valuation(
    base_revenue: float,
    revenue_growth_rates: list[float],
    ebit_margins: list[float],
    tax_rate: float,
    d_and_a: list[float],
    capex: list[float],
    change_in_nwc: list[float],
    discount_rate: float,
    terminal_growth_rate: float,
    net_debt: float,
    shares_outstanding: float,
) -> DcfResult:
    """
    Run the full 2-stage DCF from base revenue and per-year assumptions to a per-share value.

    Args:
        base_revenue (float): The most recent actual (reported) revenue.
        revenue_growth_rates (list[float]): One growth rate per projection year.
        ebit_margins (list[float]): One EBIT margin per projection year.
        tax_rate (float): Effective tax rate applied to EBIT (decimal fraction).
        d_and_a (list[float]): D&A add-back per projection year.
        capex (list[float]): Capital expenditure per projection year.
        change_in_nwc (list[float]): Change in net working capital per projection year.
        discount_rate (float): The discount rate / WACC (decimal fraction).
        terminal_growth_rate (float): Perpetual growth rate beyond the explicit period.
        net_debt (float): Total debt less cash and equivalents.
        shares_outstanding (float): Diluted shares outstanding.

    Returns:
        DcfResult: Every intermediate series plus the final per-share value.
    """
    projected_revenue = project_revenue(base_revenue, revenue_growth_rates)
    projected_ebit = project_ebit(projected_revenue, ebit_margins)
    nopat = compute_nopat(projected_ebit, tax_rate)
    fcf = compute_unlevered_fcf(nopat, d_and_a, capex, change_in_nwc)
    factors = discount_factors(discount_rate, len(fcf))
    pv_fcf = discount_cash_flows(fcf, factors)
    tv = terminal_value(fcf[-1], discount_rate, terminal_growth_rate)
    pv_tv = tv * factors[-1]
    ev = enterprise_value(pv_fcf, pv_tv)
    eq_value = equity_value(ev, net_debt)
    per_share = value_per_share(eq_value, shares_outstanding)

    return DcfResult(
        projected_revenue=projected_revenue,
        projected_ebit=projected_ebit,
        projected_nopat=nopat,
        projected_fcf=fcf,
        discount_factors=factors,
        pv_fcf=pv_fcf,
        terminal_value=tv,
        pv_terminal_value=pv_tv,
        enterprise_value=ev,
        equity_value=eq_value,
        value_per_share=per_share,
    )
