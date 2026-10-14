"""Portfolio Attribution tab view."""

from __future__ import annotations

import pandas as pd
import streamlit as st

from portfolio_dashboard.dashboard.dashboard_view import render_chart
from portfolio_dashboard.datalake.silver_controller import SilverStore
from portfolio_dashboard.reports.reports_model import (
    AttributionReport,
    FactorAttributionReport,
    SectorAttributionReport,
)
from portfolio_dashboard.reports.reports_view import (
    active_weight_scatter,
    contributors_chart,
    factor_selection_chart,
    performance_history_chart,
    sector_brinson_chart,
    sector_contributors_chart,
)


def render_attribution_tab(
    *,
    attr_report: AttributionReport | None,
    factor_report: FactorAttributionReport | None,
    sector_report: SectorAttributionReport | None,
    store: SilverStore | None = None,
    selected_portfolio: str | None = None,
) -> None:
    """Render the Portfolio Attribution tab.

    Args:
        attr_report: Portfolio attribution report, if loaded.
        factor_report: Factor attribution report, if loaded.
        sector_report: Sector attribution report, if loaded.
        store: Silver read store for loading performance history.
        selected_portfolio: Active portfolio display name.
    """
    # ── Performance history chart ────────────────────────────────────────────
    if store is not None and selected_portfolio is not None:
        df_hist = store.summary_history(selected_portfolio)
        df_hist = df_hist.dropna(subset=["port_return", "bench_return"])
        if len(df_hist) >= 2:
            st.subheader("Performance History")
            _hist_dates = pd.to_datetime(df_hist["as_of"])
            _min_d = _hist_dates.min().date()
            _max_d = _hist_dates.max().date()
            if _min_d < _max_d:
                _d_from, _d_to = st.slider(
                    "Periode",
                    min_value=_min_d,
                    max_value=_max_d,
                    value=(_min_d, _max_d),
                    format="DD/MM/YY",
                    key="attr_perf_dates",
                )
            else:
                _d_from, _d_to = _min_d, _max_d
            _mask = (_hist_dates.dt.date >= _d_from) & (_hist_dates.dt.date <= _d_to)
            df_filt = df_hist[_mask.to_numpy()].copy()
            if not df_filt.empty:
                render_chart(performance_history_chart(df_filt))
            st.divider()

    _sec_src = attr_report or factor_report

    if _sec_src is not None:
        st.subheader("Security-Level Active Contributors")
        n_contrib = st.slider("Contributors to show (top / bottom)", 5, 30, 15, key="n_contrib")
        render_chart(contributors_chart(_sec_src, n=n_contrib))
        render_chart(active_weight_scatter(_sec_src))
        st.divider()
    else:
        st.info("Upload a Port Attribution or Factor Attribution file to view security-level data.")

    if sector_report:
        st.subheader("Sector Attribution")
        col_sa1, col_sa2 = st.columns(2)
        with col_sa1:
            render_chart(sector_contributors_chart(sector_report))
        with col_sa2:
            render_chart(sector_brinson_chart(sector_report))

    if _sec_src is not None:
        st.divider()
        st.subheader("Full Attribution Table")
        rows = sorted(_sec_src.securities, key=lambda r: r.ctr_d or 0.0, reverse=True)
        df_attr = pd.DataFrame(
            {
                "Name": [r.name for r in rows],
                "Wgt P (%)": [r.avg_wgt_p for r in rows],
                "Wgt B (%)": [r.avg_wgt_b for r in rows],
                "Active Wgt (%)": [r.avg_wgt_d for r in rows],
                "Rtn P (%)": [r.tot_rtn_p for r in rows],
                "Rtn B (%)": [r.tot_rtn_b for r in rows],
                "CTR P (%)": [r.ctr_p for r in rows],
                "Active CTR (%)": [r.ctr_d for r in rows],
            }
        )
        num_cols = [c for c in df_attr.columns if c != "Name"]
        st.dataframe(
            df_attr.style.format({c: "{:+.3f}" for c in num_cols}, na_rep="—").background_gradient(
                subset=["Active CTR (%)"], cmap="RdYlGn", vmin=-0.5, vmax=0.5
            ),
            width="stretch",
            height=420,
        )

    if factor_report:
        st.divider()
        st.subheader("Stock Selection Alpha")
        st.caption(
            "Selectie-bijdrage = totale actieve CTR minus alle systematische factoreffecten. "
            "Dit zijn de pure aandelenselectie-keuzes."
        )
        n_sel = st.slider("Toon top / bottom", 5, 20, 10, key="n_sel")
        render_chart(factor_selection_chart(factor_report, n=n_sel))
