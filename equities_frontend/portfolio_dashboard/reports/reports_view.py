"""
Plotly chart factories for the portfolio dashboard.

Each function accepts a typed report model and returns a
``plotly.graph_objects.Figure`` ready for ``st.plotly_chart()``.
No Streamlit imports here — charts are pure data → figure transformations.
"""

from __future__ import annotations

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
from plotly.subplots import make_subplots

from portfolio_dashboard.reports.reports_model import (
    ALL_FACTOR_NAMES,
    STYLE_FACTOR_CATEGORIES,
    AttributionReport,
    FactorAttributionReport,
    SectorAttributionReport,
    TERiskFactorReport,
    TrackingErrorReport,
)

# Bloomberg GICS sector names as they appear in the TE Factors category field
_GICS_SECTORS: frozenset[str] = frozenset(
    {
        "Industrials",
        "Technology",
        "HealthCare",
        "Financials",
        "ConsumerDisc",
        "ConsumerStap",
        "RealEstate",
        "Energy",
        "Utilities",
        "Communications",
        "Materials",
    }
)


def _bar_range(values: list[float], pad: float = 0.18) -> list[float]:
    """
    Return an x-axis [min, max] range with extra padding so that
    'outside' text labels on the longest bars are never clipped.
    """
    mn, mx = min(values, default=0.0), max(values, default=0.0)
    span = (mx - mn) or 1.0
    return [mn - span * pad, mx + span * pad]


def _factor_group(category: str) -> str:
    """Map a Bloomberg factor category to a high-level risk group."""
    if category in STYLE_FACTOR_CATEGORIES:
        return "Style"
    if category in _GICS_SECTORS:
        return "Industry"
    if category == "Currency":
        return "Currency"
    if category == "Market":
        return "Market"
    return "Country"


# Professional financial dashboard palette — muted, print-safe colours
_GREEN = "#2d6a4f"  # forest green  — positive contribution / overweight
_RED = "#922b21"  # wine red      — negative / underweight
_BLUE = "#1a3c5e"  # deep navy     — primary / portfolio
_LIGHT_BLUE = "#2e6da4"  # steel blue    — secondary accent
_GREY = "#6b7a8d"  # slate grey    — benchmark / neutral
_ORANGE = "#9a5b13"  # burnt amber   — timing / interaction

_FONT_FAMILY = "Inter, -apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, sans-serif"

_LAYOUT_DEFAULTS = dict(
    plot_bgcolor="white",
    paper_bgcolor="white",
    font=dict(family=_FONT_FAMILY, size=11, color="#2c3e50"),
    margin=dict(l=210, r=130, t=50, b=36),
)


# ── attribution charts ─────────────────────────────────────────────────────────


def contributors_chart(report: AttributionReport | FactorAttributionReport, n: int = 15) -> go.Figure:
    """
    Horizontal bar chart: top-n active contributors + bottom-n active detractors,
    ranked by active CTR (CTR[D]).  Positive bars are green, negative red.
    """
    top = report.top_active_contributors(n)
    bottom = report.bottom_active_contributors(n)
    # deduplicate while preserving sort order (worst → best)
    seen: set[str] = set()
    combined: list = []
    for r in sorted(list(top) + list(bottom), key=lambda r: r.ctr_d or 0.0):
        if r.name not in seen:
            seen.add(r.name)
            combined.append(r)

    names = [r.name for r in combined]
    values = [r.ctr_d or 0.0 for r in combined]
    colors = [_GREEN if v >= 0 else _RED for v in values]

    fig = go.Figure(
        go.Bar(
            x=values,
            y=names,
            orientation="h",
            marker_color=colors,
            text=[f"{v:+.3f}%" for v in values],
            textposition="outside",
            hovertemplate="<b>%{y}</b><br>Active CTR: %{x:+.3f}%<extra></extra>",
        )
    )
    fig.update_layout(
        **_LAYOUT_DEFAULTS,
        title=dict(text=f"Top & Bottom {n} Active Contributors (CTR[D])", x=0),
        xaxis_title="Active CTR (%)",
        height=max(420, len(combined) * 22),
        xaxis=dict(zeroline=True, zerolinewidth=1.5, zerolinecolor="#333"),
    )
    return fig


def active_weight_scatter(report: AttributionReport | FactorAttributionReport) -> go.Figure:
    """
    Scatter of active weight (x-axis) vs active CTR (y-axis).
    Bubble size = portfolio weight.  Colour scale: red → green through zero.

    Interpretation quadrants:
      Q1 (top-right): overweight + added value  → best
      Q3 (bottom-left): underweight + avoided loss → also good
    """
    rows = [r for r in report.securities if r.avg_wgt_d is not None and r.ctr_d is not None]
    df = pd.DataFrame(
        {
            "name": [r.name for r in rows],
            "active_wgt": [r.avg_wgt_d for r in rows],
            "ctr_d": [r.ctr_d for r in rows],
            "portfolio_wgt": [r.avg_wgt_p or 0.1 for r in rows],
        }
    )

    fig = px.scatter(
        df,
        x="active_wgt",
        y="ctr_d",
        size=df["portfolio_wgt"].clip(lower=0.05),
        hover_name="name",
        color="ctr_d",
        color_continuous_scale="RdYlGn",
        color_continuous_midpoint=0,
        labels={
            "active_wgt": "Active Weight (%)",
            "ctr_d": "Active CTR (%)",
        },
        title="Active Weight vs Active Contribution",
    )
    fig.add_hline(y=0, line_width=1, line_dash="dot", line_color=_GREY)
    fig.add_vline(x=0, line_width=1, line_dash="dot", line_color=_GREY)
    fig.update_layout(
        **{k: v for k, v in _LAYOUT_DEFAULTS.items() if k != "margin"},
        margin=dict(l=60, r=60, t=55, b=50),
        coloraxis_showscale=False,
    )
    return fig


# ── sector charts ──────────────────────────────────────────────────────────────


def sector_brinson_chart(report: SectorAttributionReport) -> go.Figure:
    """
    Stacked horizontal bar: Brinson allocation / selection / interaction per sector,
    sorted by total attribution.  Zero line marks the boundary between help and hurt.
    """
    sectors = sorted(
        report.sectors,
        key=lambda s: s.total_attribution or 0.0,
    )
    names = [s.name for s in sectors]

    alloc_vals = [s.allocation or 0.0 for s in sectors]
    select_vals = [s.selection or 0.0 for s in sectors]
    inter_vals = [s.interaction or 0.0 for s in sectors]
    total_vals = [s.total_attribution or 0.0 for s in sectors]

    fig = go.Figure()
    for label, values, color in [
        ("Allocation", alloc_vals, _BLUE),
        ("Selection", select_vals, _GREEN),
        ("Interaction", inter_vals, _ORANGE),
    ]:
        fig.add_trace(
            go.Bar(
                name=label,
                x=values,
                y=names,
                orientation="h",
                marker_color=color,
                text=[f"{v:+.2f}%" if abs(v) >= 0.02 else "" for v in values],
                textposition="inside",
                insidetextanchor="middle",
                hovertemplate=f"<b>%{{y}}</b><br>{label}: %{{x:+.2f}}%<extra></extra>",
            )
        )

    # Invisible trace just to draw the total attribution label outside the stack
    fig.add_trace(
        go.Bar(
            name="Total",
            x=[0.0] * len(sectors),
            y=names,
            orientation="h",
            marker_color="rgba(0,0,0,0)",
            text=[f"∑ {v:+.2f}%" for v in total_vals],
            textposition="outside",
            hoverinfo="skip",
            showlegend=False,
            customdata=[[v] for v in total_vals],
            hovertemplate="<b>%{y}</b><br>Total: %{customdata[0]:+.2f}%<extra></extra>",
        )
    )

    all_values = alloc_vals + select_vals + inter_vals + total_vals
    fig.update_layout(
        **_LAYOUT_DEFAULTS,
        barmode="relative",
        title=dict(text="Brinson Attribution by GICS Sector", x=0),
        xaxis_title="Attribution Effect (%)",
        height=max(360, len(sectors) * 32),
        xaxis=dict(
            zeroline=True,
            zerolinewidth=1.5,
            zerolinecolor="#333",
            range=_bar_range(all_values),
        ),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, x=0),
    )
    return fig


def sector_weights_chart(report: SectorAttributionReport) -> go.Figure:
    """
    Grouped horizontal bar: portfolio weight vs benchmark weight per sector.
    Sorted by benchmark weight descending (largest benchmark sectors first).
    """
    sectors = sorted(
        report.sectors,
        key=lambda s: s.avg_wgt_b or 0.0,
        reverse=True,
    )
    names = [s.name for s in sectors]

    fig = go.Figure()
    fig.add_trace(
        go.Bar(
            name="Portfolio",
            x=[s.avg_wgt_p or 0.0 for s in sectors],
            y=names,
            orientation="h",
            marker_color=_BLUE,
            hovertemplate="<b>%{y}</b><br>Portfolio: %{x:.2f}%<extra></extra>",
        )
    )
    fig.add_trace(
        go.Bar(
            name="Benchmark",
            x=[s.avg_wgt_b or 0.0 for s in sectors],
            y=names,
            orientation="h",
            marker_color=_GREY,
            hovertemplate="<b>%{y}</b><br>Benchmark: %{x:.2f}%<extra></extra>",
        )
    )
    fig.update_layout(
        **_LAYOUT_DEFAULTS,
        barmode="group",
        title=dict(text="Sector Weights: Portfolio vs Benchmark", x=0),
        xaxis_title="Avg Weight (%)",
        height=max(360, len(sectors) * 38),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, x=0),
    )
    return fig


def sector_active_return_chart(report: SectorAttributionReport) -> go.Figure:
    """
    Horizontal bar of active return (Tot Rtn[D]) per sector.
    """
    sectors = sorted(
        report.sectors,
        key=lambda s: s.tot_rtn_d or 0.0,
    )
    names = [s.name for s in sectors]
    values = [s.tot_rtn_d or 0.0 for s in sectors]
    colors = [_GREEN if v >= 0 else _RED for v in values]

    fig = go.Figure(
        go.Bar(
            x=values,
            y=names,
            orientation="h",
            marker_color=colors,
            text=[f"{v:+.2f}%" for v in values],
            textposition="outside",
            hovertemplate="<b>%{y}</b><br>Active Return: %{x:+.2f}%<extra></extra>",
        )
    )
    fig.update_layout(
        **_LAYOUT_DEFAULTS,
        title=dict(text="Sector Active Returns (Portfolio − Benchmark)", x=0),
        xaxis_title="Active Return (%)",
        height=max(360, len(sectors) * 32),
        xaxis=dict(zeroline=True, zerolinewidth=1.5, zerolinecolor="#333"),
    )
    return fig


# ── risk / TE charts ───────────────────────────────────────────────────────────


def te_contribution_chart(report: TrackingErrorReport, n: int = 20) -> go.Figure:
    """
    Horizontal bar of Non-Factor Contrib (%) — idiosyncratic CTEV — for the top-n names.
    Falls back to Contribution (%) for older TE reports that lack the column.
    """
    top = list(reversed(report.top_risk_contributors(n)))
    has_nfc = any(r.non_factor_contrib is not None for r in top)
    names = [r.name for r in top]
    values = [(r.non_factor_contrib or 0.0) if has_nfc else (r.contribution_p or 0.0) for r in top]
    colors = [_BLUE if v >= 0 else _RED for v in values]
    x_label = "Non-Factor Contrib (%)" if has_nfc else "Contribution (%)"

    fig = go.Figure(
        go.Bar(
            x=values,
            y=names,
            orientation="h",
            marker_color=colors,
            text=[f"{v:+.2f}%" for v in values],
            textposition="outside",
            hovertemplate=f"<b>%{{y}}</b><br>{x_label}: %{{x:+.2f}}%<extra></extra>",
        )
    )
    fig.update_layout(
        **_LAYOUT_DEFAULTS,
        title=dict(text=f"Top {n} CTEV (%) by Idiosyncratic", x=0),
        xaxis_title=x_label,
        height=max(420, n * 26),
        xaxis=dict(zeroline=True, zerolinewidth=1, zerolinecolor="#aaa"),
    )
    return fig


def active_weight_chart(report: TrackingErrorReport, n: int = 25) -> go.Figure:
    """
    Horizontal signed bar of active weights — largest absolute positions shown first.
    Positive = overweight (green), negative = underweight (red).
    """
    # Reversed: plotly draws first item at bottom, so ascending order puts
    # the largest absolute position at the top.
    positions = list(reversed(report.largest_active_positions(n)))
    names = [r.name for r in positions]
    values = [r.wgt_d or 0.0 for r in positions]
    colors = [_GREEN if v >= 0 else _RED for v in values]

    fig = go.Figure(
        go.Bar(
            x=values,
            y=names,
            orientation="h",
            marker_color=colors,
            hovertemplate="<b>%{y}</b><br>Active Weight: %{x:+.2f}%<extra></extra>",
        )
    )
    fig.update_layout(
        **_LAYOUT_DEFAULTS,
        title=dict(text=f"Largest {n} Active Positions", x=0),
        xaxis_title="Active Weight (%)",
        height=max(420, n * 22),
        xaxis=dict(zeroline=True, zerolinewidth=1.5, zerolinecolor="#333"),
    )
    return fig


def risk_contribution_scatter(report: TrackingErrorReport) -> go.Figure:
    """
    Scatter: active weight (x) vs Non-Factor Contribution % to portfolio TE (y).
    Uses the same idiosyncratic Non-Factor Contrib (%) as the "Top CTEV by
    Idiosyncratic" chart, falling back to Contribution (%) for older TE
    reports that lack the column. Top 10 contributors are labelled; bubble
    size = |portfolio weight|.
    """
    has_nfc = any(r.non_factor_contrib is not None for r in report.securities)
    ctr_field = "non_factor_contrib" if has_nfc else "contribution_p"
    rows = [r for r in report.securities if r.wgt_d is not None and getattr(r, ctr_field) is not None]
    df = pd.DataFrame(
        {
            "name": [r.name for r in rows],
            "active_wgt": [r.wgt_d for r in rows],
            "contribution": [getattr(r, ctr_field) for r in rows],
            "portfolio_wgt": [abs(r.wgt_p or 0.1) for r in rows],
        }
    )

    label_threshold = df["contribution"].abs().nlargest(10).min()
    df["label"] = df.apply(
        lambda x: x["name"] if abs(x["contribution"]) >= label_threshold else "",
        axis=1,
    )

    ctr_label = "Non-Factor Contrib (%)" if has_nfc else "Contribution (%)"
    fig = px.scatter(
        df,
        x="active_wgt",
        y="contribution",
        size=df["portfolio_wgt"].clip(lower=0.1),
        hover_name="name",
        text="label",
        color="contribution",
        color_continuous_scale="RdBu",
        color_continuous_midpoint=0,
        hover_data={
            "portfolio_wgt": ":.2f",
            "active_wgt": ":.2f",
            "contribution": ":.2f",
            "label": False,
        },
        labels={
            "active_wgt": "Active Weight (%)",
            "contribution": ctr_label,
            "portfolio_wgt": "Portfolio Weight (%)",
        },
        title=f"Active Weight vs TE {ctr_label} — bubble = portfolio weight, labelled = top 10",
    )
    fig.add_vline(x=0, line_width=1, line_dash="dot", line_color=_GREY)
    fig.add_hline(y=0, line_width=1, line_dash="dot", line_color=_GREY)
    fig.update_traces(textposition="top center", textfont_size=9, textfont_color="#333")
    fig.update_layout(
        **{k: v for k, v in _LAYOUT_DEFAULTS.items() if k != "margin"},
        margin=dict(l=70, r=120, t=70, b=60),
        coloraxis_showscale=False,
    )
    return fig


# ── overview / positioning charts ──────────────────────────────────────────────


def top_bottom_weights_chart(report: TrackingErrorReport, n: int = 10) -> go.Figure:
    """
    Combined bar: top n overweights (green, top half) and bottom n underweights
    (red, bottom half), sorted so the most extreme positions are closest to zero.
    Uses current active weights (wgt_d) from the TE report.
    """
    all_recs = [r for r in report.securities if r.wgt_d is not None]
    top_n = sorted(all_recs, key=lambda r: r.wgt_d or 0.0, reverse=True)[:n]
    bot_n = sorted(all_recs, key=lambda r: r.wgt_d or 0.0)[:n]

    combined = list(reversed(bot_n)) + list(reversed(top_n))
    names = [r.name for r in combined]
    values = [r.wgt_d or 0.0 for r in combined]
    colors = [_GREEN if v >= 0 else _RED for v in values]

    fig = go.Figure(
        go.Bar(
            x=values,
            y=names,
            orientation="h",
            marker_color=colors,
            text=[f"{v:+.2f}%" for v in values],
            textposition="outside",
            hovertemplate="<b>%{y}</b><br>Active Weight: %{x:+.2f}%<extra></extra>",
        )
    )
    fig.update_layout(
        **_LAYOUT_DEFAULTS,
        title=dict(text=f"Top {n} Overweights & Bottom {n} Underweights", x=0),
        xaxis_title="Active Weight (%)",
        height=max(500, len(combined) * 22),
        xaxis=dict(
            zeroline=True,
            zerolinewidth=1,
            zerolinecolor="#aaa",
            range=_bar_range(values),
        ),
    )
    return fig


def factor_sector_weights_chart(report: FactorAttributionReport) -> go.Figure:
    """
    Sector active weights derived from the Factor Attribution report.
    Mirrors sector_positioning_chart but uses FactorAttributionReport.sectors
    so it can be shown in the Factor Attribution tab without a separate sector report.
    """
    sectors = sorted(report.sectors, key=lambda s: s.avg_wgt_d or 0.0)
    names = [s.name for s in sectors]
    values = [s.avg_wgt_d or 0.0 for s in sectors]
    colors = [_GREEN if v >= 0 else _RED for v in values]

    fig = go.Figure(
        go.Bar(
            x=values,
            y=names,
            orientation="h",
            marker_color=colors,
            text=[f"{v:+.1f}%" for v in values],
            textposition="outside",
            customdata=[[s.avg_wgt_p or 0, s.avg_wgt_b or 0] for s in sectors],
            hovertemplate=(
                "<b>%{y}</b><br>"
                "Portfolio: %{customdata[0]:.1f}%<br>"
                "Benchmark: %{customdata[1]:.1f}%<br>"
                "Active: %{x:+.1f}%"
                "<extra></extra>"
            ),
        )
    )
    fig.update_layout(
        **_LAYOUT_DEFAULTS,
        title=dict(text="Sector Active Weights (Portfolio − Benchmark)", x=0),
        xaxis_title="Active Weight (%)",
        height=max(360, len(sectors) * 34),
        xaxis=dict(
            zeroline=True,
            zerolinewidth=1,
            zerolinecolor="#aaa",
            range=_bar_range(values) if values else [0, 1],
        ),
    )
    return fig


def sector_positioning_chart(report: SectorAttributionReport) -> go.Figure:
    """
    Horizontal bar of active weights per sector, sorted from most underweight to most
    overweight.  Quick read on where the portfolio tilts vs the benchmark.
    """
    sectors = sorted(report.sectors, key=lambda s: s.avg_wgt_d or 0.0)
    names = [s.name for s in sectors]
    values = [s.avg_wgt_d or 0.0 for s in sectors]
    colors = [_GREEN if v >= 0 else _RED for v in values]

    fig = go.Figure(
        go.Bar(
            x=values,
            y=names,
            orientation="h",
            marker_color=colors,
            text=[f"{v:+.1f}%" for v in values],
            textposition="outside",
            customdata=[[s.avg_wgt_p or 0, s.avg_wgt_b or 0] for s in sectors],
            hovertemplate=(
                "<b>%{y}</b><br>"
                "Portfolio: %{customdata[0]:.1f}%<br>"
                "Benchmark: %{customdata[1]:.1f}%<br>"
                "Active: %{x:+.1f}%"
                "<extra></extra>"
            ),
        )
    )
    fig.update_layout(
        **_LAYOUT_DEFAULTS,
        title=dict(text="Sector Active Weights (Portfolio − Benchmark)", x=0),
        xaxis_title="Active Weight (%)",
        height=max(360, len(sectors) * 34),
        xaxis=dict(
            zeroline=True,
            zerolinewidth=1,
            zerolinecolor="#aaa",
            range=_bar_range(values),
        ),
    )
    return fig


def te_active_weight_bar(
    records: list,
    title: str,
    subtitle: str = "",
) -> go.Figure:
    """
    Horizontal bar chart of active weights for a list of TrackingErrorRecord.
    Used at every level of the sector look-through drill-down.
    Sorted from most underweight to most overweight.
    Setting clickmode='event+select' in the layout lets Streamlit's on_select
    capture bar clicks for drill-down navigation.
    """
    recs = sorted(
        [r for r in records if r.wgt_d is not None],
        key=lambda r: r.wgt_d or 0.0,
    )
    if not recs:
        recs = list(records)

    names = [r.name for r in recs]
    values = [r.wgt_d or 0.0 for r in recs]
    port_wgts = [r.wgt_p or 0.0 for r in recs]
    colors = [_GREEN if v >= 0 else _RED for v in values]

    full_title = title
    if subtitle:
        full_title = f"{title}" f"<br><span style='font-size:10px;color:#888'>{subtitle}</span>"

    fig = go.Figure(
        go.Bar(
            x=values,
            y=names,
            orientation="h",
            marker_color=colors,
            text=[f"{v:+.2f}%" for v in values],
            textposition="outside",
            customdata=[[pw] for pw in port_wgts],
            hovertemplate=(
                "<b>%{y}</b><br>"
                "Actief gewicht: %{x:+.2f}%<br>"
                "Portefeuillegewicht: %{customdata[0]:.2f}%"
                "<extra></extra>"
            ),
            selected=dict(marker=dict(opacity=1.0)),
            unselected=dict(marker=dict(opacity=0.4)),
        )
    )
    fig.update_layout(
        **_LAYOUT_DEFAULTS,
        title=dict(text=full_title, x=0),
        xaxis_title="Actief gewicht (%)",
        height=max(360, len(recs) * 30),
        xaxis=dict(
            zeroline=True,
            zerolinewidth=1,
            zerolinecolor="#aaa",
            range=_bar_range(values),
        ),
        clickmode="event+select",
    )
    return fig


def te_active_weight_treemap(report: TrackingErrorReport) -> go.Figure:
    """
    Drill-down treemap built from the 4-level TE report hierarchy:
      root → sector (L1) → industry group (L2) → security (L3).

    Size  = Gross Active Weight (always ≥ 0).
    Colour = signed active weight (green = overweight, red = underweight).

    Click any tile to zoom in; use the breadcrumb path bar at the top to navigate back.
    """
    ids: list[str] = []
    labels: list[str] = []
    parents: list[str] = []
    values: list[float] = []
    colors: list[float] = []
    custom: list[list[float]] = []  # [active_wgt, portfolio_wgt]

    current_l1: str | None = None
    current_l2_uid: str | None = None

    for r in report.all_records:
        if r.level == 0:
            continue
        gross = r.gross_active_wgt or 0.0
        active = r.wgt_d or 0.0
        port_w = r.wgt_p or 0.0

        if r.level == 1:
            current_l1 = r.name
            current_l2_uid = None
            uid = r.name
            parent = ""
        elif r.level == 2:
            current_l2_uid = f"{current_l1}||{r.name}"
            uid = current_l2_uid
            parent = current_l1 or ""
        else:  # level 3
            uid = f"{current_l2_uid or current_l1}||{r.name}"
            parent = current_l2_uid or current_l1 or ""

        ids.append(uid)
        labels.append(r.name)
        parents.append(parent)
        values.append(gross)
        colors.append(active)
        custom.append([active, port_w])

    # Symmetric colour range centred on 0 for a balanced diverging scale
    max_abs = max((abs(c) for c in colors), default=1.0)

    fig = go.Figure(
        go.Treemap(
            ids=ids,
            labels=labels,
            parents=parents,
            values=values,
            branchvalues="total",
            customdata=custom,
            marker=dict(
                colors=colors,
                colorscale=[
                    [0.00, "#8B0000"],
                    [0.35, "#D4807C"],
                    [0.50, "#F5F5F0"],
                    [0.65, "#7DC4A8"],
                    [1.00, "#1B5E20"],
                ],
                cmin=-max_abs,
                cmax=max_abs,
                showscale=True,
                colorbar=dict(
                    title=dict(text="Actief<br>gewicht (%)", side="right"),
                    thickness=14,
                    len=0.55,
                    tickformat="+.1f",
                    x=1.01,
                ),
            ),
            texttemplate="<b>%{label}</b><br>%{customdata[0]:+.1f}%",
            hovertemplate=(
                "<b>%{label}</b><br>"
                "Actief gewicht: %{customdata[0]:+.2f}%<br>"
                "Portefeuillegewicht: %{customdata[1]:.2f}%<br>"
                "Bruto actief: %{value:.2f}%"
                "<extra></extra>"
            ),
            root_color="#FAFAFA",
            pathbar=dict(
                visible=True,
                edgeshape=">",
                thickness=20,
                textfont=dict(size=11),
            ),
            tiling=dict(packing="squarify"),
        )
    )

    fig.update_layout(
        title=dict(
            text=(
                "Active Weights — Sector Look-Through  "
                "<span style='font-size:11px;color:#666'>"
                "klik om in te zoomen · klik pad bovenin om terug</span>"
            ),
            x=0,
            font=dict(size=13),
        ),
        margin=dict(t=60, l=0, r=85, b=5),
        height=580,
        plot_bgcolor="white",
        paper_bgcolor="white",
        font=dict(family=_FONT_FAMILY, size=11, color="#2c3e50"),
    )
    return fig


def sector_contributors_chart(report: SectorAttributionReport) -> go.Figure:
    """
    Horizontal bar of active CTR[D] per sector.
    Since there are only ~11 GICS sectors, all are shown with no slider needed.
    """
    sectors = sorted(report.sectors, key=lambda s: s.ctr_d or 0.0)
    names = [s.name for s in sectors]
    values = [s.ctr_d or 0.0 for s in sectors]
    colors = [_GREEN if v >= 0 else _RED for v in values]

    fig = go.Figure(
        go.Bar(
            x=values,
            y=names,
            orientation="h",
            marker_color=colors,
            text=[f"{v:+.2f}%" for v in values],
            textposition="outside",
            hovertemplate="<b>%{y}</b><br>Active CTR: %{x:+.2f}%<extra></extra>",
        )
    )
    fig.update_layout(
        **_LAYOUT_DEFAULTS,
        title=dict(text="Sector Active Contributors (CTR[D])", x=0),
        xaxis_title="Active CTR (%)",
        height=max(360, len(sectors) * 38),
        xaxis=dict(
            zeroline=True,
            zerolinewidth=1.5,
            zerolinecolor="#333",
            range=_bar_range(values),
        ),
    )
    return fig


# ── factor attribution charts ──────────────────────────────────────────────────


def factor_contributions_bar(report: FactorAttributionReport) -> go.Figure:
    """
    Horizontal bar of portfolio-level EU factor active contributions.
    Shows which systematic risk factors helped (+) or hurt (−) active return.
    The title includes the total factor vs selection split.
    """
    _raw_ctrs = report.total.factor_ctrs_d
    # When both "EU X" and "X" are present (global portfolio with European sub-model),
    # prefer the non-EU version to avoid double-counting the same systematic risk.
    factor_ctrs = {fn: v for fn, v in _raw_ctrs.items() if not (fn.startswith("EU ") and fn[3:] in _raw_ctrs)}
    # "Style" is the sum of all other style sub-factors; exclude it to avoid double-counting.
    _ordered = [f for f in ALL_FACTOR_NAMES if f != "Style"]
    factors = [(f, factor_ctrs[f]) for f in _ordered if f in factor_ctrs]
    _seen = set(_ordered) | {"Style"}
    for _fn, _fv in sorted(factor_ctrs.items()):
        if _fn not in _seen:
            factors.append((_fn, _fv))
    factors.sort(key=lambda x: x[1])

    names = [f[0] for f in factors]
    values = [f[1] for f in factors]
    colors = [_GREEN if v >= 0 else _RED for v in values]

    total_factor = report.total.factor_d or 0.0
    total_selection = report.total.selection_d or 0.0

    fig = go.Figure(
        go.Bar(
            x=values,
            y=names,
            orientation="h",
            marker_color=colors,
            text=[f"{v:+.3f}%" for v in values],
            textposition="outside",
            hovertemplate="<b>%{y}</b><br>Factor Active CTR: %{x:+.3f}%<extra></extra>",
        )
    )
    fig.update_layout(
        **_LAYOUT_DEFAULTS,
        title=dict(
            text=(
                f"Factor Contributions to Active Return — "
                f"Factor: {total_factor:+.2f}%  |  Selection: {total_selection:+.2f}%"
            ),
            x=0,
        ),
        xaxis_title="Active CTR (%)",
        height=max(480, len(factors) * 32),
        xaxis=dict(zeroline=True, zerolinewidth=1.5, zerolinecolor="#333"),
    )
    return fig


def factor_selection_chart(report: FactorAttributionReport, n: int = 10) -> go.Figure:
    """
    Top n / Bottom n securities by pure selection contribution (Selection Rtn (Contr)[D]).
    Selection = total CTR minus all factor effects: these are the stock-specific alpha calls.
    """
    top = report.top_selectors(n)
    bottom = report.bottom_selectors(n)

    seen: set[str] = set()
    combined: list = []
    for r in sorted(list(top) + list(bottom), key=lambda r: r.selection_d or 0.0):
        if r.name not in seen:
            seen.add(r.name)
            combined.append(r)

    names = [r.name for r in combined]
    values = [r.selection_d or 0.0 for r in combined]
    colors = [_GREEN if v >= 0 else _RED for v in values]

    fig = go.Figure(
        go.Bar(
            x=values,
            y=names,
            orientation="h",
            marker_color=colors,
            text=[f"{v:+.3f}%" for v in values],
            textposition="outside",
            hovertemplate="<b>%{y}</b><br>Selection CTR: %{x:+.3f}%<extra></extra>",
        )
    )
    fig.update_layout(
        **_LAYOUT_DEFAULTS,
        title=dict(
            text=f"Top & Bottom {n} — Pure Selection Alpha (total CTR minus factor effects)",
            x=0,
        ),
        xaxis_title="Selection Contribution (%)",
        height=max(420, len(combined) * 22),
        xaxis=dict(zeroline=True, zerolinewidth=1.5, zerolinecolor="#333"),
    )
    return fig


def factor_exposure_contribution_bar(
    factor_report: FactorAttributionReport,
    te_factor_report: TERiskFactorReport | None = None,
) -> go.Figure:
    """
    Vertical grouped bar: active factor exposure (from TE factor report) and
    contribution (%) per factor (from factor attribution report).
    """
    _raw_ctrs = factor_report.total.factor_ctrs_d
    factor_ctrs = {fn: v for fn, v in _raw_ctrs.items() if not (fn.startswith("EU ") and fn[3:] in _raw_ctrs)}
    _exclude = {"Style", "Currency"}
    _ordered = [f for f in ALL_FACTOR_NAMES if f not in _exclude]
    factors: list[tuple[str, float]] = [(f, factor_ctrs[f]) for f in _ordered if f in factor_ctrs]
    _seen = set(_ordered) | _exclude
    for fn, fv in sorted(factor_ctrs.items()):
        if fn not in _seen:
            factors.append((fn, fv))

    factor_names = [f[0] for f in factors]
    ctr_values = [f[1] for f in factors]

    _DISPLAY: dict[str, str] = {
        "MidCap": "Mid-Cap",
        "DivYld": "DvdYield",
        "LtReversal": "LongRev",
        "EarnVariab": "Variability",
        "ResidualVol": "ResVol",
    }
    x_labels = [_DISPLAY.get(fn, fn) for fn in factor_names]

    exp_map: dict[str, float] = {}
    if te_factor_report:
        for r in te_factor_report.factors:
            if r.category in STYLE_FACTOR_CATEGORIES:
                exp_map[r.category] = exp_map.get(r.category, 0.0) + (r.active_exposure or 0.0)
    exp_values = [exp_map.get(fn) for fn in factor_names]
    has_exposures = any(v is not None for v in exp_values)

    total_factor = factor_report.total.factor_d or 0.0
    total_selection = factor_report.total.selection_d or 0.0

    fig = go.Figure()
    if has_exposures:
        fig.add_trace(
            go.Bar(
                name="Exposure",
                x=x_labels,
                y=exp_values,
                marker_color=_ORANGE,
                hovertemplate="<b>%{x}</b><br>Actief exposure: %{y:+.3f}<extra></extra>",
            )
        )
    fig.add_trace(
        go.Bar(
            name="Contributie (%)",
            x=x_labels,
            y=ctr_values,
            marker_color="#b5338a",
            hovertemplate="<b>%{x}</b><br>Contributie: %{y:+.3f}%<extra></extra>",
        )
    )
    fig.add_hline(y=0, line_width=1, line_color="#aaa", line_dash="dot")
    fig.update_layout(
        **{k: v for k, v in _LAYOUT_DEFAULTS.items() if k != "margin"},
        margin=dict(l=60, r=60, t=70, b=90),
        barmode="group",
        title=dict(
            text=(f"Factor Contributie (%) — " f"Factor: {total_factor:+.2f}% | Selection: {total_selection:+.2f}%"),
            x=0,
        ),
        yaxis_title="Waarde",
        xaxis=dict(tickangle=-30),
        legend=dict(orientation="h", y=-0.22, x=0.5, xanchor="center"),
        height=420,
    )
    return fig


def factor_active_exposure_snapshot_chart(
    df: pd.DataFrame,
    dates: list,
    factors: list[str],
) -> go.Figure:
    """
    Vertical grouped bar comparing active factor exposures across up to 4 selected dates.
    Each date gets a distinct colour; factors on the x-axis.
    df columns: as_of, factor_label, active_exposure.
    """
    _DATE_COLORS = ["#e6b84f", "#c0395a", "#7a8a99", "#5bb3d5"]
    df = df.copy()
    df["as_of"] = pd.to_datetime(df["as_of"])

    fig = go.Figure()
    for i, d in enumerate(dates[:4]):
        d_ts = pd.Timestamp(d)
        sub = df[df["as_of"] == d_ts]
        label = f"{d_ts:%d %b %Y}"
        vals: list[float | None] = []
        for fn in factors:
            row = sub[sub["factor_label"] == fn]
            vals.append(float(row["active_exposure"].iloc[0]) if not row.empty else None)
        fig.add_trace(
            go.Bar(
                name=label,
                x=factors,
                y=vals,
                marker_color=_DATE_COLORS[i % len(_DATE_COLORS)],
                hovertemplate="<b>%{x}</b><br>" + label + ": %{y:+.3f}<extra></extra>",
            )
        )

    fig.add_hline(y=0, line_width=1, line_color="#aaa", line_dash="dot")
    fig.update_layout(
        **{k: v for k, v in _LAYOUT_DEFAULTS.items() if k != "margin"},
        margin=dict(l=60, r=60, t=60, b=90),
        barmode="group",
        title=dict(text="Actief factorexposure — snapshot vergelijking", x=0),
        yaxis_title="Actief exposure",
        xaxis=dict(tickangle=-30),
        legend=dict(orientation="h", y=-0.22, x=0.5, xanchor="center"),
        height=420,
    )
    return fig


# ── attribution waterfall ──────────────────────────────────────────────────────


def attribution_waterfall(report) -> go.Figure:
    """
    Vertical waterfall: Factor → Selection → Timing → Total Active Return.
    Works with AttributionReport and FactorAttributionReport (duck-typed).
    """
    t = report.total
    factor = t.factor_d or 0.0
    selection = t.selection_d or 0.0
    timing = t.timing_d or 0.0
    total_active = t.tot_rtn_d or 0.0

    fig = go.Figure(
        go.Waterfall(
            orientation="v",
            measure=["relative", "relative", "relative", "total"],
            x=["Factor return", "Selection", "Timing", "Active return"],
            y=[factor, selection, timing, total_active],
            connector={"line": {"color": "#d0d7de", "width": 1, "dash": "dot"}},
            increasing={"marker": {"color": _GREEN}},
            decreasing={"marker": {"color": _RED}},
            totals={"marker": {"color": _BLUE}},
            text=[f"{v:+.3f}%" for v in [factor, selection, timing, total_active]],
            textposition="outside",
            hovertemplate="<b>%{x}</b><br>%{y:+.3f}%<extra></extra>",
        )
    )
    fig.update_layout(
        **{k: v for k, v in _LAYOUT_DEFAULTS.items() if k != "margin"},
        margin=dict(l=60, r=80, t=60, b=40),
        title=dict(text="Active Return Decomposition", x=0),
        yaxis_title="Active Return (%)",
        yaxis=dict(zeroline=True, zerolinewidth=1.5, zerolinecolor="#333"),
        height=360,
        showlegend=False,
    )
    return fig


# ── TE category breakdown ──────────────────────────────────────────────────────


def te_category_bar(report: TERiskFactorReport) -> go.Figure:
    """
    Horizontal bar of total TE contribution grouped by risk category
    (Style / Country / Industry / Currency / Market).
    Quickly shows whether portfolio risk is driven by factor tilts, country
    bets, sector bets, or currency exposure.
    """
    groups: dict[str, float] = {}
    for r in report.factors:
        grp = _factor_group(r.category)
        groups[grp] = groups.get(grp, 0.0) + (r.contribution_pct or 0.0)

    items = sorted(groups.items(), key=lambda x: x[1])
    names = [i[0] for i in items]
    values = [i[1] for i in items]
    colors = [_BLUE if v >= 0 else _GREEN for v in values]

    fig = go.Figure(
        go.Bar(
            x=values,
            y=names,
            orientation="h",
            marker_color=colors,
            text=[f"{v:+.1f}%" for v in values],
            textposition="outside",
            hovertemplate="<b>%{y}</b><br>TE contribution: %{x:+.1f}%<extra></extra>",
        )
    )
    fig.update_layout(
        **{k: v for k, v in _LAYOUT_DEFAULTS.items() if k != "margin"},
        margin=dict(l=100, r=140, t=55, b=40),
        title=dict(text="TE Contribution by Risk Category", x=0),
        xaxis_title="Contribution to TE (%)",
        height=max(260, len(groups) * 52),
        xaxis=dict(
            zeroline=True,
            zerolinewidth=1.5,
            zerolinecolor="#333",
            range=_bar_range(values),
        ),
    )
    return fig


# ── TE factor risk decomposition charts ────────────────────────────────────────


def te_factor_contribution_chart(report: TERiskFactorReport, n: int = 20) -> go.Figure:
    """
    Horizontal bar of the top n + bottom n factors by contribution to portfolio TE.
    Positive = factor increases tracking error; negative = factor reduces it.
    Sorted worst → best so the most TE-adding factors are at the top.
    """
    top = report.top_te_contributors(n)
    bottom = report.bottom_te_contributors(5)

    seen: set[str] = set()
    combined: list = []
    for r in sorted(set(top) | set(bottom), key=lambda r: r.contribution_pct or 0.0):
        if r.factor_name not in seen:
            seen.add(r.factor_name)
            combined.append(r)

    # Use "Category: Label" display name, trimming the "EU " prefix where possible
    labels = [f"{r.category}: {r.factor_label}" for r in combined]
    values = [r.contribution_pct or 0.0 for r in combined]
    colors = [_BLUE if v >= 0 else _GREEN for v in values]

    fig = go.Figure(
        go.Bar(
            x=values,
            y=labels,
            orientation="h",
            marker_color=colors,
            text=[f"{v:+.2f}%" for v in values],
            textposition="outside",
            hovertemplate=("<b>%{y}</b><br>TE Contribution: %{x:+.2f}%<extra></extra>"),
        )
    )
    fig.update_layout(
        **_LAYOUT_DEFAULTS,
        title=dict(text=f"Factor Contributions to Portfolio Tracking Error (top {n})", x=0),
        xaxis_title="Contribution to TE (%)",
        height=max(480, len(combined) * 26),
        xaxis=dict(
            zeroline=True,
            zerolinewidth=1,
            zerolinecolor="#aaa",
            range=_bar_range(values),
        ),
    )
    return fig


def te_factor_exposure_chart(report: TERiskFactorReport) -> go.Figure:
    """
    Horizontal bar of active factor exposures for the major style/risk factors.
    Positive = portfolio has more exposure than benchmark (e.g. higher beta, larger size).
    Negative = portfolio has less exposure (e.g. lower valuation, lower dividend yield).
    Used in the Current Positioning tab to show factor tilts at a glance.
    """
    style = sorted(report.style_factors(), key=lambda r: r.active_exposure or 0.0)

    labels = [r.factor_label for r in style]
    values = [r.active_exposure or 0.0 for r in style]
    colors = [_BLUE if v >= 0 else _RED for v in values]

    fig = go.Figure(
        go.Bar(
            x=values,
            y=labels,
            orientation="h",
            marker_color=colors,
            text=[f"{v:+.3f}" for v in values],
            textposition="outside",
            customdata=[[r.portfolio_exposure or 0, r.benchmark_exposure or 0] for r in style],
            hovertemplate=(
                "<b>%{y}</b><br>"
                "Active: %{x:+.3f}<br>"
                "Portfolio: %{customdata[0]:.3f}<br>"
                "Benchmark: %{customdata[1]:.3f}"
                "<extra></extra>"
            ),
        )
    )
    fig.update_layout(
        **_LAYOUT_DEFAULTS,
        title=dict(text="Style Factor Tilts — Active Exposure (Portfolio − Benchmark)", x=0),
        xaxis_title="Active Factor Exposure",
        height=max(420, len(style) * 34),
        xaxis=dict(
            zeroline=True,
            zerolinewidth=1,
            zerolinecolor="#aaa",
            range=_bar_range(values),
        ),
    )
    return fig


# ── trend / historical charts ──────────────────────────────────────────────────


def beta_te_trend_chart(df: pd.DataFrame) -> go.Figure:
    """
    Dual-axis line chart: ex-ante TE (%) on the left axis, beta on the right.
    df columns: as_of, te_pct, beta.
    """
    df = df.dropna(subset=["te_pct", "beta"], how="all").copy()
    df["as_of"] = pd.to_datetime(df["as_of"])

    fig = go.Figure()
    if "te_pct" in df.columns and df["te_pct"].notna().any():
        fig.add_trace(
            go.Scatter(
                x=df["as_of"],
                y=df["te_pct"],
                name="Ex-ante TE (%)",
                line=dict(color=_BLUE, width=2),
                yaxis="y",
                hovertemplate="%{x|%d %b %Y} — TE: %{y:.2f}%<extra></extra>",
            )
        )
    if "beta" in df.columns and df["beta"].notna().any():
        fig.add_trace(
            go.Scatter(
                x=df["as_of"],
                y=df["beta"],
                name="Beta",
                line=dict(color=_ORANGE, width=2, dash="dot"),
                yaxis="y2",
                hovertemplate="%{x|%d %b %Y} — Beta: %{y:.3f}x<extra></extra>",
            )
        )
    fig.update_layout(
        **{k: v for k, v in _LAYOUT_DEFAULTS.items() if k != "margin"},
        margin=dict(l=60, r=80, t=55, b=40),
        title=dict(text="Ex-ante Tracking Error & Beta Over Time", x=0),
        xaxis=dict(title="Date", showgrid=False),
        yaxis=dict(title="TE (%)", side="left", showgrid=True),
        yaxis2=dict(title="Beta", side="right", overlaying="y", showgrid=False),
        legend=dict(orientation="h", y=1.1, x=0),
        hovermode="x unified",
    )
    return fig


def active_return_trend_chart(df: pd.DataFrame) -> go.Figure:
    """
    Bar chart of periodic active return per date.
    df columns: as_of, active_return.
    """
    df = df.dropna(subset=["active_return"]).copy()
    df["as_of"] = pd.to_datetime(df["as_of"])
    colors = [_GREEN if v >= 0 else _RED for v in df["active_return"]]

    fig = go.Figure(
        go.Bar(
            x=df["as_of"],
            y=df["active_return"],
            marker_color=colors,
            text=[f"{v:+.2f}%" for v in df["active_return"]],
            textposition="outside",
            hovertemplate="%{x|%d %b %Y} — Active return: %{y:+.2f}%<extra></extra>",
        )
    )
    fig.add_hline(y=0, line_width=1, line_color="#aaa")
    fig.update_layout(
        **{k: v for k, v in _LAYOUT_DEFAULTS.items() if k != "margin"},
        margin=dict(l=60, r=60, t=55, b=40),
        title=dict(text="Active Return Over Time", x=0),
        yaxis_title="Active Return (%)",
        xaxis_title="Date",
    )
    return fig


def factor_exposure_trend_chart(df: pd.DataFrame, factors: list[str]) -> go.Figure:
    """
    Multi-line chart of active factor exposures over time for the selected factors.
    df columns: as_of, factor_label, active_exposure.
    """
    _COLORS = [_BLUE, _RED, _GREEN, _ORANGE, _GREY, _LIGHT_BLUE]
    df = df[df["factor_label"].isin(factors)].copy()
    df["as_of"] = pd.to_datetime(df["as_of"])

    fig = go.Figure()
    for i, fac in enumerate(factors):
        sub = df[df["factor_label"] == fac].sort_values("as_of")
        if sub.empty:
            continue
        fig.add_trace(
            go.Scatter(
                x=sub["as_of"],
                y=sub["active_exposure"],
                name=fac,
                line=dict(color=_COLORS[i % len(_COLORS)], width=2),
                hovertemplate=f"{fac}  %{{x|%d %b %Y}}: %{{y:+.3f}}<extra></extra>",
            )
        )
    fig.add_hline(y=0, line_width=1, line_color="#aaa")
    fig.update_layout(
        **{k: v for k, v in _LAYOUT_DEFAULTS.items() if k != "margin"},
        margin=dict(l=60, r=60, t=55, b=40),
        title=dict(text="Factor Active Exposure Trends", x=0),
        yaxis_title="Active Exposure",
        xaxis_title="Date",
        legend=dict(orientation="h", y=1.1, x=0),
        hovermode="x unified",
    )
    return fig


def sector_weight_trend_chart(df: pd.DataFrame) -> go.Figure:
    """
    Multi-line chart of sector active weights over time.
    df columns: as_of, sector, wgt_d.
    Each sector is a separate line; zero line marks benchmark-neutral.
    """
    _SECTOR_COLORS = [
        _BLUE,
        _RED,
        _GREEN,
        _ORANGE,
        _GREY,
        _LIGHT_BLUE,
        "#9b59b6",
        "#e67e22",
        "#1abc9c",
        "#e74c3c",
        "#34495e",
    ]
    df = df.copy()
    df["as_of"] = pd.to_datetime(df["as_of"])
    sectors = sorted(df["sector"].unique())

    fig = go.Figure()
    for i, sector in enumerate(sectors):
        sub = df[df["sector"] == sector].sort_values("as_of")
        fig.add_trace(
            go.Scatter(
                x=sub["as_of"],
                y=sub["wgt_d"],
                name=sector,
                mode="lines+markers",
                line=dict(color=_SECTOR_COLORS[i % len(_SECTOR_COLORS)], width=2),
                marker=dict(size=5),
                hovertemplate=f"<b>{sector}</b>  %{{x|%d %b %Y}}: %{{y:+.2f}}%<extra></extra>",
            )
        )

    fig.add_hline(y=0, line_width=1, line_color="#aaa", line_dash="dot")
    fig.update_layout(
        **{k: v for k, v in _LAYOUT_DEFAULTS.items() if k != "margin"},
        margin=dict(l=60, r=60, t=55, b=40),
        title=dict(text="Sector Active Weights Over Time", x=0),
        yaxis_title="Active Weight (%)",
        xaxis_title="Date",
        legend=dict(orientation="h", y=-0.25, x=0),
        hovermode="x unified",
        height=480,
    )
    return fig


# Validated categorical pair (light surface):
#   Portfolio #2e6da4 — benchmark #d4832e — all CVD checks pass.
_PORT_COLOR = "#2e6da4"
_BENCH_COLOR = "#d4832e"


def performance_history_chart(df: pd.DataFrame) -> go.Figure:
    """Dual-panel performance history: portfolio vs benchmark (top) + active return area (bottom).

    Args:
        df: Filtered summary-history rows with columns ``as_of``, ``port_return``,
            ``bench_return``, ``active_return``, ``portfolio``, ``benchmark``.

    Returns:
        go.Figure: Two-panel time-series figure ready for ``st.plotly_chart``.
    """
    dates = pd.to_datetime(df["as_of"])
    port = df["port_return"].tolist()
    bench = df["bench_return"].tolist()
    active_raw = [v if v is not None else float("nan") for v in df["active_return"].tolist()]

    # Rebase the active return to 0 at the first data point of the selected period.
    # This makes the bottom panel show "relative outperformance since period start"
    # rather than absolute active return since report inception.
    _baseline = next((v for v in active_raw if not pd.isna(v)), 0.0)
    active = [(v - _baseline) if not pd.isna(v) else float("nan") for v in active_raw]

    port_label = str(df["portfolio"].iloc[0]) if not df.empty else "Portfolio"
    bench_label = str(df["benchmark"].iloc[0]) if not df.empty else "Benchmark"

    fig = make_subplots(
        rows=2,
        cols=1,
        row_heights=[0.65, 0.35],
        shared_xaxes=True,
        vertical_spacing=0.03,
        subplot_titles=("", "Actief rendement"),
    )

    # ── Top panel: portfolio vs benchmark lines ────────────────────────────
    fig.add_trace(
        go.Scatter(
            x=dates,
            y=port,
            name=port_label,
            mode="lines",
            line=dict(color=_PORT_COLOR, width=2),
            hovertemplate="%{x|%d %b %Y}<br>Portefeuille: <b>%{y:.2f}%</b><extra></extra>",
        ),
        row=1,
        col=1,
    )
    fig.add_trace(
        go.Scatter(
            x=dates,
            y=bench,
            name=bench_label,
            mode="lines",
            line=dict(color=_BENCH_COLOR, width=2),
            hovertemplate="%{x|%d %b %Y}<br>Benchmark: <b>%{y:.2f}%</b><extra></extra>",
        ),
        row=1,
        col=1,
    )

    # ── Bottom panel: sign-coloured active return area ─────────────────────
    # Positive fill (green)
    fig.add_trace(
        go.Scatter(
            x=dates,
            y=[v if (v is not None and not pd.isna(v) and v >= 0) else 0 for v in active],
            fill="tozeroy",
            fillcolor="rgba(45,106,79,0.45)",
            line=dict(width=0),
            showlegend=False,
            hoverinfo="skip",
        ),
        row=2,
        col=1,
    )
    # Negative fill (red)
    fig.add_trace(
        go.Scatter(
            x=dates,
            y=[v if (v is not None and not pd.isna(v) and v < 0) else 0 for v in active],
            fill="tozeroy",
            fillcolor="rgba(146,43,33,0.45)",
            line=dict(width=0),
            showlegend=False,
            hoverinfo="skip",
        ),
        row=2,
        col=1,
    )
    # Active return line on top of fills
    fig.add_trace(
        go.Scatter(
            x=dates,
            y=active,
            name="Actief rendement",
            mode="lines",
            line=dict(color=_GREY, width=1.5),
            hovertemplate="%{x|%d %b %Y}<br>Actief: <b>%{y:+.2f}%</b><extra></extra>",
        ),
        row=2,
        col=1,
    )
    fig.add_hline(y=0, line_width=1, line_color="#ccc", line_dash="dot", row=2, col=1)

    fig.update_layout(
        **{k: v for k, v in _LAYOUT_DEFAULTS.items() if k != "margin"},
        margin=dict(l=60, r=80, t=50, b=40),
        legend=dict(orientation="h", y=1.06, x=0, font=dict(size=11)),
        hovermode="x unified",
        height=500,
    )
    fig.update_yaxes(ticksuffix="%", gridcolor="#f0f3f7", row=1, col=1)
    fig.update_yaxes(ticksuffix="%", tickformat="+.2f", gridcolor="#f0f3f7", row=2, col=1)
    fig.update_xaxes(showticklabels=True, row=2, col=1)
    # Subplot title font: make the "Actief rendement" label recessive
    for ann in fig.layout.annotations:
        ann.font = dict(size=10, color=_GREY)

    return fig
