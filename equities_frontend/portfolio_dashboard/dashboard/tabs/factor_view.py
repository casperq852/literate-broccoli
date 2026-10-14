"""Factor Attribution & Positioning tab view."""

from __future__ import annotations

import pandas as pd
import streamlit as st

from portfolio_dashboard.dashboard.dashboard_view import render_chart
from portfolio_dashboard.reports.reports_model import FactorAttributionReport, TERiskFactorReport
from portfolio_dashboard.reports.reports_view import factor_exposure_contribution_bar


def render_factor_tab(
    *,
    factor_report: FactorAttributionReport | None,
    factor_te_report: TERiskFactorReport | None,
) -> None:
    """Render the Factor Attribution & Positioning tab.

    Args:
        factor_report: Factor attribution report, if loaded.
        factor_te_report: Tracking-error factor report, if loaded.
    """
    if factor_report is None:
        st.info(
            "Upload the Factor Attribution CSV (Classification: GICS Sector) "
            "to view factor return decomposition and sector positioning."
        )
    else:
        render_chart(factor_exposure_contribution_bar(factor_report, factor_te_report))

        st.divider()
        st.subheader("Sector Summary")
        secs = sorted(factor_report.sectors, key=lambda s: s.ctr_d or 0.0, reverse=True)
        if secs:
            df_fac_sec = pd.DataFrame(
                {
                    "Sector": [s.name for s in secs],
                    "Wgt P (%)": [s.avg_wgt_p for s in secs],
                    "Active Wgt (%)": [s.avg_wgt_d for s in secs],
                    "Active CTR (%)": [s.ctr_d for s in secs],
                    "Factor CTR (%)": [s.factor_d for s in secs],
                    "Selection CTR (%)": [s.selection_d for s in secs],
                    "Timing CTR (%)": [s.timing_d for s in secs],
                }
            )
            num_fac = [c for c in df_fac_sec.columns if c != "Sector"]
            st.dataframe(
                df_fac_sec.style.format({c: "{:+.3f}" for c in num_fac}, na_rep="—").background_gradient(
                    subset=["Selection CTR (%)"], cmap="RdYlGn", vmin=-0.3, vmax=0.3
                ),
                width="stretch",
            )

        st.subheader("Security Detail")
        secu = sorted(factor_report.securities, key=lambda r: r.ctr_d or 0.0, reverse=True)
        if secu:
            df_fac = pd.DataFrame(
                {
                    "Name": [r.name for r in secu],
                    "Wgt P (%)": [r.avg_wgt_p for r in secu],
                    "Active Wgt (%)": [r.avg_wgt_d for r in secu],
                    "Active CTR (%)": [r.ctr_d for r in secu],
                    "Factor CTR (%)": [r.factor_d for r in secu],
                    "Selection CTR (%)": [r.selection_d for r in secu],
                    "Timing CTR (%)": [r.timing_d for r in secu],
                }
            )
            num_cols_f = [c for c in df_fac.columns if c != "Name"]
            st.dataframe(
                df_fac.style.format({c: "{:+.3f}" for c in num_cols_f}, na_rep="—").background_gradient(
                    subset=["Selection CTR (%)"], cmap="RdYlGn", vmin=-0.3, vmax=0.3
                ),
                width="stretch",
                height=420,
            )
