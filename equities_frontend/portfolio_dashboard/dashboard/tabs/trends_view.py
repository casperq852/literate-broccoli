"""Trends tab view."""

from __future__ import annotations

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from portfolio_dashboard.dashboard.dashboard_view import render_chart
from portfolio_dashboard.datalake.silver_controller import SilverStore
from portfolio_dashboard.reports.reports_model import STYLE_FACTOR_CATEGORIES
from portfolio_dashboard.reports.reports_view import (
    beta_te_trend_chart,
    factor_active_exposure_snapshot_chart,
    factor_exposure_trend_chart,
    sector_weight_trend_chart,
)


def render_trends_tab(*, store: SilverStore, selected_portfolio: str | None) -> None:
    """Render the Trends tab.

    Args:
        store: Portfolio history store backing the trend charts.
        selected_portfolio: Name of the active portfolio, used as the default.
    """
    if not store.has_data():
        st.info(
            "Nog geen historische data.  \n"
            "Upload Bloomberg-rapporten en klik op **Sla op in geschiedenis** in de zijbalk "
            "om de tijdreeks-database op te bouwen. Elke dag dat je rapporten opslaat, "
            "groeit de trend-analyse automatisch."
        )
    else:
        # Exclude cash portfolios from the dropdown
        port_list = [p for p in store.portfolios() if "cash" not in p.lower()]

        # Auto-sync: follow the main portfolio selector unless the user explicitly
        # picked a different portfolio in the Trends tab dropdown. We track the last
        # known main selection; when it changes we push the new value into the widget.
        _prev_main = st.session_state.get("_trend_last_main_portfolio")
        if _prev_main != selected_portfolio:
            st.session_state["_trend_last_main_portfolio"] = selected_portfolio
            if selected_portfolio in port_list:
                st.session_state["trend_portfolio"] = selected_portfolio

        # Fallback: first open or previous selection no longer valid
        if st.session_state.get("trend_portfolio") not in port_list:
            st.session_state["trend_portfolio"] = (
                selected_portfolio if selected_portfolio in port_list else (port_list[0] if port_list else None)
            )

        t_col1, t_col2 = st.columns([1, 3])
        with t_col1:
            selected_port = st.selectbox("Portfolio", port_list, key="trend_portfolio")

        df_summary = store.summary_history(selected_port)
        df_factors = store.factor_history(selected_port)
        df_sectors = store.sector_history(selected_port)

        with st.expander("🔍 Debug: factor data in silver layer", expanded=False):
            if df_factors.empty:
                st.warning("Geen factor data (te_factors) gevonden voor dit portfolio.")
            else:
                st.write(f"**Rijen:** {len(df_factors)} | **Datums:** {sorted(df_factors['as_of'].unique())}")
                st.write("**Unieke factor_label → category:**")
                st.dataframe(
                    df_factors[["factor_label", "category"]].drop_duplicates().sort_values("factor_label"),
                    use_container_width=True,
                    height=200,
                )

        if df_summary.empty:
            st.warning(f"Geen data gevonden voor {selected_port}.")
        else:
            dates = pd.to_datetime(df_summary["as_of"])
            min_d, max_d = dates.min().date(), dates.max().date()

            if min_d == max_d:
                st.caption(
                    f"Één datapunt beschikbaar ({min_d.strftime('%d %b %Y')}). "
                    "Sla dagelijks rapporten op om trends te zien."
                )
                d_from, d_to = min_d, max_d
            else:
                with t_col2:
                    d_from, d_to = st.slider(
                        "Periode",
                        min_value=min_d,
                        max_value=max_d,
                        value=(min_d, max_d),
                        format="DD/MM/YY",
                        key="trend_dates",
                    )

            mask = (dates.dt.date >= d_from) & (dates.dt.date <= d_to)
            df_s = df_summary[mask.to_numpy()].copy()

            # ── Beta & Tracking Error ──────────────────────────────────────────
            if df_s[["te_pct", "beta"]].notna().any().any():
                st.subheader("Beta & Tracking Error")
                render_chart(beta_te_trend_chart(df_s))

            # ── Cross-portfolio TE vergelijking ────────────────────────────────
            if len(port_list) > 1:
                with st.expander("TE-vergelijking alle portfolios", expanded=False):
                    _cmp_frames = []
                    _cmp_first_dates: list[pd.Timestamp] = []
                    for _cpn in port_list:
                        _cdf = store.summary_history(_cpn)
                        if not _cdf.empty and _cdf["te_pct"].notna().any():
                            _cdf = _cdf[["as_of", "te_pct"]].copy()
                            _cdf["as_of"] = pd.to_datetime(_cdf["as_of"])
                            _cdf["portfolio"] = _cpn
                            _cmp_frames.append(_cdf)
                            _cmp_first_dates.append(_cdf["as_of"].min())
                    if _cmp_frames:
                        df_cmp = pd.concat(_cmp_frames, ignore_index=True)
                        # Start from the first date where ALL portfolios have a data point
                        if _cmp_first_dates:
                            _cmp_start = max(_cmp_first_dates)
                            df_cmp = df_cmp[df_cmp["as_of"] >= _cmp_start]
                        fig_te_cmp = go.Figure()
                        for _cpn in df_cmp["portfolio"].unique():
                            _pdat = df_cmp[df_cmp["portfolio"] == _cpn].sort_values("as_of")
                            fig_te_cmp.add_scatter(
                                x=_pdat["as_of"],
                                y=_pdat["te_pct"],
                                mode="lines+markers",
                                name=_cpn,
                                hovertemplate="%{x|%d %b %Y}<br>TE: %{y:.2f}%<extra>" + _cpn + "</extra>",
                            )
                        fig_te_cmp.update_layout(
                            title=dict(text="Ex-ante Tracking Error — alle portfolios (excl. cash)", x=0),
                            xaxis_title="Datum",
                            yaxis_title="TE (%)",
                            margin=dict(l=60, r=60, t=55, b=40),
                            legend=dict(orientation="h", y=1.1, x=0),
                            hovermode="x unified",
                            plot_bgcolor="rgba(0,0,0,0)",
                            paper_bgcolor="rgba(0,0,0,0)",
                        )
                        render_chart(fig_te_cmp)

            # ── Sector actieve gewichten over tijd ─────────────────────────────
            if not df_sectors.empty:
                st.divider()
                st.subheader("Sector Positionering")
                s_dates = pd.to_datetime(df_sectors["as_of"]).dt.date
                df_sec = df_sectors[(s_dates >= d_from) & (s_dates <= d_to)]
                if not df_sec.empty:
                    render_chart(sector_weight_trend_chart(df_sec))

            # ── Factor exposure trends ─────────────────────────────────────────
            if not df_factors.empty:
                st.divider()
                st.subheader("Factor Exposures")

                # Split available labels into style factors vs industries/countries.
                # Regional prefixes (GL, EU, US, …) are stripped before matching so that
                # "GL Beta", "US Momentum" etc. are recognised as style factors alongside
                # bare "Beta" / "EU Beta" entries.
                _fl_all = sorted(df_factors["factor_label"].unique())
                _cat_map = (
                    df_factors[["factor_label", "category"]]
                    .drop_duplicates()
                    .set_index("factor_label")["category"]
                    .to_dict()
                )
                _REGION_PREFIXES = ("EU ", "US ", "GL ", "NA ", "EM ", "DM ", "AP ")

                def _is_style(cat: str) -> bool:
                    if cat in STYLE_FACTOR_CATEGORIES:
                        return True
                    for _pfx in _REGION_PREFIXES:
                        if cat.startswith(_pfx) and cat[len(_pfx) :] in STYLE_FACTOR_CATEGORIES:
                            return True
                    return False

                _style_labels = [f for f in _fl_all if _is_style(_cat_map.get(f, ""))]
                _ind_labels = [f for f in _fl_all if f not in _style_labels]

                # Determine which regional model dominates for the selected portfolio
                # (GL for ADWAF, EU for ADAAF/AEIIAEF, US for US-only portfolios) so
                # the defaults always open with the right factor set preselected.
                _pfx_counts: dict[str, int] = {}
                for _lbl in _style_labels:
                    for _pfx in _REGION_PREFIXES:
                        if _lbl.startswith(_pfx):
                            _pfx_counts[_pfx] = _pfx_counts.get(_pfx, 0) + 1
                            break
                _dom_pfx = max(_pfx_counts, key=_pfx_counts.get) if _pfx_counts else ""

                # Defaults: prefer dominant-region factors for Momentum, Valuation, Profit, EarnYield
                _style_defaults = []
                for _kw in ["Momentum", "Valuation", "Profit", "EarnYield"]:
                    _m = next(
                        (f for f in _style_labels if f.startswith(_dom_pfx) and _kw.lower() in f.lower()),
                        None,
                    ) or next((f for f in _style_labels if _kw.lower() in f.lower()), None)
                    if _m and _m not in _style_defaults:
                        _style_defaults.append(_m)
                _style_defaults = _style_defaults or _style_labels[:4]

                _fe_c1, _fe_c2 = st.columns(2)
                with _fe_c1:
                    _all_style = st.checkbox("Alle stijlfactoren", key="trend_all_style")
                    _sel_style: list[str] = (
                        _style_labels
                        if _all_style
                        else st.multiselect(
                            "Stijlfactoren",
                            _style_labels,
                            default=[f for f in _style_defaults if f in _style_labels],
                            key="trend_style_factors",
                        )
                    )
                with _fe_c2:
                    _all_ind = st.checkbox("Alle industrieën", key="trend_all_ind")
                    _sel_ind: list[str] = (
                        _ind_labels
                        if _all_ind
                        else st.multiselect(
                            "Industrieën / landen",
                            _ind_labels,
                            default=[],
                            key="trend_ind_factors",
                        )
                    )

                _sel_combined = _sel_style + [f for f in _sel_ind if f not in _sel_style]
                if _sel_combined:
                    f_dates = pd.to_datetime(df_factors["as_of"]).dt.date
                    df_f = df_factors[(f_dates >= d_from) & (f_dates <= d_to)]
                    if _sel_style:
                        render_chart(factor_exposure_trend_chart(df_f, _sel_style))
                    if _sel_ind:
                        render_chart(factor_exposure_trend_chart(df_f, _sel_ind))

                # ── Factor Exposure Snapshot (multi-date vergelijking) ──────────────
                st.divider()
                st.subheader("Factor Exposure Snapshot")
                st.caption(
                    "Vergelijk actieve factorexposure op maximaal 4 datums naast elkaar. "
                    "Gebruikt de factorenselect hierboven."
                )
                _snap_avail = sorted(pd.to_datetime(df_factors["as_of"]).dt.date.unique())
                _snap_default = _snap_avail[-min(4, len(_snap_avail)) :]
                _snap_dates = st.multiselect(
                    "Selecteer datums (max 4)",
                    options=_snap_avail,
                    default=_snap_default,
                    format_func=lambda d: d.strftime("%d %b %Y") if hasattr(d, "strftime") else str(d),
                    key="trend_snap_dates",
                )
                if _snap_dates and _sel_combined:
                    render_chart(factor_active_exposure_snapshot_chart(df_factors, _snap_dates[:4], _sel_combined))
                elif not _sel_combined:
                    st.caption("Selecteer stijlfactoren of industrieën hierboven.")
