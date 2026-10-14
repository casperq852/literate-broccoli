"""Current Positioning tab view."""

from __future__ import annotations

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from portfolio_dashboard.dashboard.dashboard_view import CHART_CONFIG, render_chart
from portfolio_dashboard.reports.reports_model import (
    AttributionReport,
    FactorAttributionReport,
    SectorAttributionReport,
    TERiskFactorReport,
    TrackingErrorReport,
)
from portfolio_dashboard.reports.reports_view import (
    attribution_waterfall,
    sector_positioning_chart,
    te_active_weight_bar,
    te_factor_exposure_chart,
    top_bottom_weights_chart,
)


def render_positioning_tab(
    *,
    attr_report: AttributionReport | None,
    te_report: TrackingErrorReport | None,
    sector_report: SectorAttributionReport | None,
    factor_report: FactorAttributionReport | None,
    factor_te_report: TERiskFactorReport | None,
    is_master: bool,
    selected_portfolio: str | None,
) -> None:
    """Render the Current Positioning tab.

    Args:
        attr_report: Portfolio attribution report, if loaded.
        te_report: Tracking-error report, if loaded.
        sector_report: Sector attribution report, if loaded.
        factor_report: Factor attribution report, if loaded.
        factor_te_report: Tracking-error factor report, if loaded.
        is_master: Whether the active portfolio is a master/fund-level portfolio.
        selected_portfolio: Name of the active portfolio, used for widget keys.
    """
    col_l, col_r = st.columns(2)

    with col_l:
        if is_master:
            if te_report and te_report.sectors:
                render_chart(
                    te_active_weight_bar(
                        te_report.sectors,
                        "Sector Active Weights",
                        "Active weight = portfolio − benchmark",
                    )
                )
        elif te_report:
            render_chart(top_bottom_weights_chart(te_report, n=10))
        elif attr_report:
            recs = [r for r in attr_report.securities if r.avg_wgt_d is not None]
            top10 = sorted(recs, key=lambda r: r.avg_wgt_d or 0.0, reverse=True)[:10]
            bot10 = sorted(recs, key=lambda r: r.avg_wgt_d or 0.0)[:10]
            combined = list(reversed(bot10)) + list(reversed(top10))
            vals = [r.avg_wgt_d or 0.0 for r in combined]
            fig_w = go.Figure(
                go.Bar(
                    x=vals,
                    y=[r.name for r in combined],
                    orientation="h",
                    marker_color=["#2d6a4f" if v >= 0 else "#922b21" for v in vals],
                    text=[f"{v:+.2f}%" for v in vals],
                    textposition="outside",
                    textfont=dict(size=9),
                    hovertemplate="<b>%{y}</b><br>Active weight: %{x:+.2f}%<extra></extra>",
                )
            )
            fig_w.update_layout(
                plot_bgcolor="white",
                paper_bgcolor="white",
                font=dict(family="Inter, sans-serif", size=11, color="#2c3e50"),
                margin=dict(l=210, r=130, t=50, b=36),
                title=dict(text="Top 10 Overweights & Bottom 10 Underweights", x=0),
                xaxis_title="Avg active weight (%)",
                height=max(500, len(combined) * 22),
                xaxis=dict(zeroline=True, zerolinewidth=1, zerolinecolor="#aaa"),
            )
            render_chart(fig_w)
        else:
            st.info("Upload a TE or attribution report to see active weight positioning.")

    with col_r:
        if te_report and te_report.sectors:
            # ── Sector look-through drill-down ─────────────────────────────────
            _dd_key = f"te_drilldown_{selected_portfolio}"
            if _dd_key not in st.session_state:
                st.session_state[_dd_key] = []
            _dd_path: list[str] = st.session_state[_dd_key]

            # Breadcrumb + reset button
            if _dd_path:
                _bc_col, _rst_col = st.columns([4, 1])
                _bc_col.caption("Sectoren › " + " › ".join(_dd_path))
                with _rst_col:
                    if st.button("↩ Reset", key=f"te_rst_{selected_portfolio}"):
                        st.session_state[_dd_key] = []
                        st.rerun()

            # Determine records and title for current drill-down level
            if len(_dd_path) == 0:
                _dd_recs = te_report.sectors
                _dd_title = "Actieve gewichten per sector"
                _dd_sub = "klik op een sector om industrie-uitsplitsing te zien"
                _dd_can_drill = True
            elif len(_dd_path) == 1:
                _dd_recs = te_report.children_of(_dd_path[0], parent_level=1)
                _dd_title = f"Industrie — {_dd_path[0]}"
                _dd_sub = "klik op een industrie om de aandelen te zien"
                _dd_can_drill = True
            else:
                _dd_recs = te_report.children_of(_dd_path[1], parent_level=2)
                _dd_title = f"Aandelen — {_dd_path[1]}"
                _dd_sub = ""
                _dd_can_drill = False

            _dd_fig = te_active_weight_bar(_dd_recs, _dd_title, _dd_sub)
            _dd_chart_key = f"te_dd_{selected_portfolio}_{len(_dd_path)}"
            _dd_ev = st.plotly_chart(
                _dd_fig,
                config=CHART_CONFIG,
                width="stretch",
                on_select="rerun",
                selection_mode="points",
                key=_dd_chart_key,
            )

            # Handle click → drill one level deeper
            if _dd_can_drill:
                _dd_pts = getattr(getattr(_dd_ev, "selection", None), "points", [])
                if _dd_pts:
                    _clicked = _dd_pts[0].get("y")
                    if _clicked:
                        st.session_state[_dd_key] = _dd_path + [_clicked]
                        st.rerun()

        elif sector_report:
            render_chart(sector_positioning_chart(sector_report))
        else:
            st.info("Upload een TE Report voor de sector look-through grafiek.")

    if factor_te_report:
        st.divider()
        st.subheader("Style Factor Tilts")
        st.caption(
            "Active factor exposures (portfolio minus benchmark). "
            "Positive values indicate the portfolio has more exposure than the benchmark."
        )
        render_chart(te_factor_exposure_chart(factor_te_report))

    st.divider()

    dec_col, risk_col = st.columns(2)

    with dec_col:
        st.subheader("Attribution Decomposition")
        if factor_report:
            render_chart(attribution_waterfall(factor_report))
            st.caption(
                "Bron: Factor Attributie (T-1) — Factor/Selection/Timing decomposities zijn niet beschikbaar in Port Attributie."
            )
        elif attr_report:
            render_chart(attribution_waterfall(attr_report))
        else:
            st.info("Upload an attribution or factor attribution report.")

    with risk_col:
        st.subheader("Risk Snapshot")
        if te_report:
            snap: dict = {
                "Ex-ante TE (%)": te_report.portfolio_te,
                "Portfolio beta": te_report.portfolio_beta,
            }
            if not is_master:
                snap["Overweight names"] = float(sum(1 for r in te_report.securities if (r.wgt_d or 0) > 0))
                snap["Underweight names"] = float(sum(1 for r in te_report.securities if (r.wgt_d or 0) < 0))
            st.dataframe(
                pd.DataFrame({"Value": snap}).style.format("{:.2f}", na_rep="—"),
                width="stretch",
            )
        else:
            st.info("Upload a TE report.")
