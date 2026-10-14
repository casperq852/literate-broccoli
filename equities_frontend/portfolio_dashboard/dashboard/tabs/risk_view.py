"""Risk & Tracking Error tab view."""

from __future__ import annotations

import pandas as pd
import streamlit as st

from portfolio_dashboard.dashboard.dashboard_view import render_chart
from portfolio_dashboard.reports.reports_model import (
    STYLE_FACTOR_CATEGORIES,
    TERiskFactorReport,
    TrackingErrorReport,
)
from portfolio_dashboard.reports.reports_view import (
    active_weight_chart,
    risk_contribution_scatter,
    te_category_bar,
    te_contribution_chart,
    te_factor_contribution_chart,
)


def render_risk_tab(
    *,
    te_report: TrackingErrorReport | None,
    factor_te_report: TERiskFactorReport | None,
    is_master: bool,
) -> None:
    """Render the Risk & Tracking Error tab.

    Args:
        te_report: Tracking-error report, if loaded.
        factor_te_report: Tracking-error factor report, if loaded.
        is_master: Whether the active portfolio is a master/fund-level portfolio.
    """
    if te_report is None and factor_te_report is None:
        st.info("Upload a TE Report or TE Factors CSV to view risk analysis.")
    else:
        if te_report:
            # ── Risk Overview Card ──────────────────────────────────────────────────
            _te_v = te_report.portfolio_te
            _beta_v = te_report.portfolio_beta
            _has_nfc_ov = any(r.non_factor_contrib is not None for r in te_report.securities)
            _fac_pct = _idio_pct = None
            if _has_nfc_ov and not is_master:
                _idio_s = sum(r.non_factor_contrib or 0.0 for r in te_report.securities)
                _tot_s = sum(r.contribution_p or 0.0 for r in te_report.securities)
                if abs(_tot_s) > 0.001:
                    _idio_pct = min(max(_idio_s / _tot_s * 100.0, 0.0), 100.0)
                    _fac_pct = 100.0 - _idio_pct

            _gics_cats_ov = {
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
            _group_ctev: dict[str, float] = {}
            if factor_te_report:
                for _r in factor_te_report.factors:
                    _grp = (
                        "Style"
                        if _r.category in STYLE_FACTOR_CATEGORIES
                        else (
                            "Industry"
                            if _r.category in _gics_cats_ov
                            else (
                                "Currency"
                                if _r.category == "Currency"
                                else "Market" if _r.category == "Market" else "Country"
                            )
                        )
                    )
                    _group_ctev[_grp] = _group_ctev.get(_grp, 0.0) + (_r.contribution_pct or 0.0)

            _top_secs_ov = (
                []
                if is_master
                else sorted(
                    te_report.securities,
                    key=lambda r: (r.non_factor_contrib if _has_nfc_ov else r.contribution_p) or 0.0,
                    reverse=True,
                )[:6]
            )

            _ov_c1, _ov_c2, _ov_c3 = st.columns([1, 1, 1.5])
            with _ov_c1:
                st.metric("Total Active Risk (TE)", f"{_te_v:.2f}%" if _te_v else "—")
                st.metric("Beta (Ex-Ante)", f"{_beta_v:.3f}x" if _beta_v else "—")
                if _fac_pct is not None:
                    _fp = int(round(_fac_pct))
                    _ip = 100 - _fp
                    st.markdown(
                        f"<div style='margin-top:6px'>"
                        f"<div style='font-size:0.7rem;color:#546e7a;text-transform:uppercase;"
                        f"letter-spacing:0.06em;margin-bottom:5px'>Factor / Idiosyncratisch</div>"
                        f"<div style='display:flex;height:16px;border-radius:4px;overflow:hidden'>"
                        f"<div style='width:{_fp}%;background:#1a3c5e'></div>"
                        f"<div style='width:{_ip}%;background:#2e6da4'></div>"
                        f"</div>"
                        f"<div style='display:flex;justify-content:space-between;"
                        f"font-size:0.73rem;margin-top:3px;color:#546e7a'>"
                        f"<span>Factor {_fac_pct:.1f}%</span>"
                        f"<span>Idio {_idio_pct:.1f}%</span>"
                        f"</div></div>",
                        unsafe_allow_html=True,
                    )
            with _ov_c2:
                if _group_ctev:
                    st.markdown(
                        "<div style='font-size:0.7rem;color:#546e7a;text-transform:uppercase;"
                        "letter-spacing:0.06em;margin-bottom:6px'>CTEV per Factor Groep</div>",
                        unsafe_allow_html=True,
                    )
                    for _grp_n, _grp_v in sorted(_group_ctev.items(), key=lambda x: -x[1]):
                        _clr = "#1a3c5e" if _grp_v >= 0 else "#922b21"
                        st.markdown(
                            f"<div style='display:flex;justify-content:space-between;"
                            f"align-items:center;padding:3px 0;border-bottom:1px solid #f0f3f7'>"
                            f"<span style='font-size:0.82rem'>{_grp_n}</span>"
                            f"<span style='font-size:0.82rem;font-weight:600;color:{_clr}'>"
                            f"{_grp_v:+.2f}%</span></div>",
                            unsafe_allow_html=True,
                        )
                else:
                    st.caption("Upload TE Factors voor factor-groep CTEV breakdown.")
            with _ov_c3:
                if _top_secs_ov:
                    _ctev_hdr = "Non-Factor Contrib" if _has_nfc_ov else "Contribution"
                    st.markdown(
                        f"<div style='font-size:0.7rem;color:#546e7a;text-transform:uppercase;"
                        f"letter-spacing:0.06em;margin-bottom:6px'>Top CTEV — {_ctev_hdr} (%)</div>",
                        unsafe_allow_html=True,
                    )
                    for _sec_r in _top_secs_ov:
                        _sec_v = (_sec_r.non_factor_contrib if _has_nfc_ov else _sec_r.contribution_p) or 0.0
                        _sec_nm = _sec_r.name[:28] + "…" if len(_sec_r.name) > 28 else _sec_r.name
                        st.markdown(
                            f"<div style='display:flex;justify-content:space-between;"
                            f"align-items:center;padding:3px 0;border-bottom:1px solid #f0f3f7'>"
                            f"<span style='font-size:0.82rem'>{_sec_nm}</span>"
                            f"<span style='font-size:0.82rem;font-weight:600;color:#1a3c5e'>"
                            f"{_sec_v:.2f}%</span></div>",
                            unsafe_allow_html=True,
                        )

            st.divider()
            if te_report.portfolio_beta is None:
                with st.expander("⚠️ Beta niet gevonden — klik voor diagnose"):
                    tot = te_report.total
                    st.write(
                        "**Portfolio totaal rij:**",
                        {
                            "name": tot.name,
                            "total_risk_d": tot.total_risk_d,
                            "beta_p": tot.beta_p,
                            "wgt_p": tot.wgt_p,
                        },
                    )
                    beta_in_secs = [(r.name, r.beta_p) for r in te_report.all_records if r.beta_p is not None][:5]
                    st.write("**Beta-waarden in onderliggende rijen (max 5):**", beta_in_secs or "geen")
                    st.caption(
                        "Als de bovenstaande waarden leeg zijn, bevat het Excel-bestand formule-cellen "
                        "zonder gecachte waarden. Sla het bestand opnieuw op in Excel (Ctrl+S) "
                        "zodat de berekende waarden worden opgeslagen."
                    )

            if not is_master:
                n_risk = st.slider("Positions to show", 5, 40, 20, key="n_risk")

                # Rendered full-width, stacked: side-by-side half columns left too
                # little horizontal room for the outside-positioned percentage
                # labels on charts with up to 40 bars, cutting them off.
                render_chart(te_contribution_chart(te_report, n=n_risk))
                render_chart(active_weight_chart(te_report, n=n_risk))

                render_chart(risk_contribution_scatter(te_report))
        if factor_te_report:
            st.divider()
            st.subheader("Factor Risk Decomposition")
            st.caption(
                "Contribution of each risk category and factor to the portfolio's ex-ante "
                "tracking error. Positive values increase TE; negative values reduce it."
            )
            st.caption("⏱ Gebaseerd op Factor Attributie (T-1) — factormodel-data loopt één dag achter.")
            cat_col, detail_col = st.columns([1, 2])
            with cat_col:
                render_chart(te_category_bar(factor_te_report))
            with detail_col:
                render_chart(te_factor_contribution_chart(factor_te_report, n=20))

            st.subheader("Factor Risk Table")
            frows = sorted(
                factor_te_report.factors,
                key=lambda r: r.contribution_pct or 0.0,
                reverse=True,
            )
            df_te_fac = pd.DataFrame(
                {
                    "Factor": [r.factor_name for r in frows],
                    "Category": [r.category for r in frows],
                    "Port. Exposure": [r.portfolio_exposure for r in frows],
                    "Bench. Exposure": [r.benchmark_exposure for r in frows],
                    "Active Exposure": [r.active_exposure for r in frows],
                    "Factor Vol (%)": [r.factor_vol for r in frows],
                    "Active Std": [r.tot_active_std for r in frows],
                    "Contribution (%)": [r.contribution_pct for r in frows],
                }
            )
            num_cols_tf = [c for c in df_te_fac.columns if c not in ("Factor", "Category")]
            # Ensure all numeric columns are float so None → NaN and na_rep works
            df_te_fac[num_cols_tf] = df_te_fac[num_cols_tf].apply(pd.to_numeric, errors="coerce")
            _ctr_vals = df_te_fac["Contribution (%)"].dropna()
            _ctr_abs = float(_ctr_vals.abs().max()) if not _ctr_vals.empty else 1.0
            _fmt_exp = {c: "{:.4f}" for c in num_cols_tf if "Exposure" in c or "Std" in c}
            _fmt_pct = {c: "{:+.2f}" for c in num_cols_tf if c not in _fmt_exp}
            st.dataframe(
                df_te_fac.style.format({**_fmt_exp, **_fmt_pct}, na_rep="—").background_gradient(
                    subset=["Contribution (%)"],
                    cmap="RdYlGn_r",
                    vmin=-_ctr_abs,
                    vmax=_ctr_abs,
                ),
                width="stretch",
                height=420,
                hide_index=True,
            )
            st.caption("⏱ Gebaseerd op Factor Attributie (T-1) — factormodel-data loopt één dag achter.")

        if te_report and not is_master:
            st.divider()
            st.subheader("Security Risk Table")
            _has_nfc_r = any(r.non_factor_contrib is not None for r in te_report.securities)
            _ctr_col = "Non-Factor Contrib (%)" if _has_nfc_r else "Contribution (%)"
            recs = sorted(
                te_report.securities,
                key=lambda r: abs((r.non_factor_contrib if _has_nfc_r else r.contribution_p) or 0.0),
                reverse=True,
            )
            df_risk = pd.DataFrame(
                {
                    "Name": [r.name for r in recs],
                    "Port. Wgt (%)": [r.wgt_p for r in recs],
                    "Active Wgt (%)": [r.wgt_d for r in recs],
                    "Total Risk": [r.total_risk_d for r in recs],
                    _ctr_col: [(r.non_factor_contrib if _has_nfc_r else r.contribution_p) for r in recs],
                    "Beta (ex-ante)": [r.beta_p for r in recs],
                }
            )
            num_cols_r = [c for c in df_risk.columns if c != "Name"]
            _ctr_vals_r = df_risk[_ctr_col].dropna()
            _ctr_abs_r = float(_ctr_vals_r.abs().max()) if not _ctr_vals_r.empty else 1.0
            st.dataframe(
                df_risk.style.format({c: "{:.3f}" for c in num_cols_r}, na_rep="—").background_gradient(
                    subset=[_ctr_col],
                    cmap="RdBu_r",
                    vmin=-_ctr_abs_r,
                    vmax=_ctr_abs_r,
                ),
                width="stretch",
                height=420,
                hide_index=True,
            )
