"""Dashboard page view: Streamlit chrome for the analytics shell.

This is the a.s.r. View layer for the dashboard shell. It owns page
configuration, the portfolio identity banner, the fund switcher, and the
key-metrics row. These functions render UI only and hold no analytical logic.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pandas as pd
import streamlit as st

from portfolio_dashboard.dashboard.dashboard_model import sub_portfolio_label
from portfolio_dashboard.dashboard.helpers import format_metric, report_display_date
from portfolio_dashboard.reports.reports_model import (
    AttributionReport,
    FactorAttributionReport,
    SectorAttributionReport,
    TERiskFactorReport,
    TrackingErrorReport,
)

if TYPE_CHECKING:
    from portfolio_dashboard.dashboard.dashboard_controller import DashboardController
    from portfolio_dashboard.datalake.medallion_controller import RefreshResult
    from portfolio_dashboard.datalake.silver_controller import SilverStore

_LOGO_PATH = Path(__file__).parent.parent / "assets" / "asr_vermogensbeheer_logo.png"

_PAGE_STYLE = """
    <style>
    html, body, [class*="css"] {
        font-family: Inter, -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
    }
    h1 {
        color: #1a3c5e !important;
        font-size: 1.45rem !important;
        font-weight: 700 !important;
        letter-spacing: -0.02em !important;
        margin-bottom: 0.1rem !important;
    }
    h2 {
        color: #1a3c5e !important;
        font-size: 1.05rem !important;
        font-weight: 600 !important;
        letter-spacing: -0.01em !important;
        margin-top: 1.2rem !important;
    }
    h3 {
        color: #2c3e50 !important;
        font-size: 0.9rem !important;
        font-weight: 600 !important;
        letter-spacing: 0.01em !important;
        text-transform: uppercase !important;
    }
    [data-testid="stMetricValue"] {
        font-size: 1.25rem !important;
        font-weight: 700 !important;
        color: #1a3c5e !important;
        letter-spacing: -0.01em !important;
    }
    [data-testid="stMetricLabel"] {
        font-size: 0.68rem !important;
        font-weight: 600 !important;
        color: #546e7a !important;
        text-transform: uppercase !important;
        letter-spacing: 0.07em !important;
    }
    [data-testid="stMetricDelta"] { font-size: 0.78rem !important; }

    /* a.s.r. house style: MultiSelect width */
    .stMultiSelect [data-baseweb=select] span {
        max-width: 1000px;
        font-size: 0.9rem;
    }

    /* a.s.r. house style: cream sidebar */
    [data-testid="stSidebar"] { background-color: #fff8e3 !important; border-right: 1px solid #e9ddb5; }
    [data-testid="stSidebar"] > div { background-color: #fff8e3; }
    [data-testid="stSidebar"] h2 { font-size: 0.9rem !important; text-transform: uppercase !important; }

    /* a.s.r. house style: white main canvas */
    [data-testid="stAppViewContainer"] { background-color: #ffffff; }
    [data-testid="stMain"] { background-color: #ffffff; }

    /* a.s.r. house style: olive tab bar with oker-yellow tabs */
    .stTabs [data-baseweb="tab-list"] {
        gap: 0.25rem !important;
        background-color: #d6dfb7 !important;
        border-radius: 0.6rem !important;
        padding: 0.25rem 0.5rem !important;
        border-bottom: none !important;
    }
    .stTabs [data-baseweb="tab"] {
        font-size: 0.8rem !important;
        font-weight: 600 !important;
        color: #1f2a1f !important;
        letter-spacing: 0.02em !important;
        padding: 0.5rem 1.1rem !important;
        margin-right: 0.25rem !important;
        border: 1px solid #9cab6f !important;
        border-radius: 0.5rem !important;
        background: #f7f2d0 !important;
        transition: background 0.15s !important;
    }
    .stTabs [data-baseweb="tab"]:hover {
        color: #1f2a1f !important;
        background: #efe6b8 !important;
    }
    .stTabs [aria-selected="true"] {
        color: #1f2a1f !important;
        background: #efe6b8 !important;
        border-color: #7e8f52 !important;
    }
    hr { border-color: #e9ecef !important; margin: 1rem 0 !important; }
    .stCaption, small { color: #6c757d !important; font-size: 0.78rem !important; }
    .stAlert { border-radius: 4px !important; font-size: 0.85rem !important; }
    </style>
    """

_PILLS_STYLE = """
<style>
[data-testid="stPills"] {
    background: #1a3c5e;
    padding: 6px 20px 8px 20px !important;
    margin-top: 0 !important;
    margin-bottom: 0 !important;
    border-radius: 0 !important;
}
</style>
"""


def configure_page() -> None:
    """Set the Streamlit page configuration and inject the shared a.s.r. stylesheet."""
    st.set_page_config(
        page_title="Portfolio Analytics",
        page_icon="💼",
        layout="wide",
        initial_sidebar_state="expanded",
    )
    st.markdown(_PAGE_STYLE, unsafe_allow_html=True)
    # Sidebar logo intentionally not re-rendered here: this dashboard is
    # embedded as one page of equities_frontend, whose own version_selector.py
    # already renders the a.s.r. logo once at the top of the (single, shared)
    # sidebar for every page. Rendering it again here duplicated it.


def _sub_portfolio_badge(portfolio_name: str) -> str:
    """Return the coloured alpha/core badge HTML for a portfolio name.

    Args:
        portfolio_name: The portfolio name shown in the banner.

    Returns:
        str: An HTML ``span`` for alpha/core portfolios, or an empty string.
    """
    lower = portfolio_name.lower()
    if "alpha" in lower:
        return (
            '<span style="background:#1565c0;color:white;padding:2px 10px;border-radius:10px;'
            'font-size:0.68rem;font-weight:700;letter-spacing:0.05em;margin-left:10px">ALPHA</span>'
        )
    if "core" in lower:
        return (
            '<span style="background:#2e7d32;color:white;padding:2px 10px;border-radius:10px;'
            'font-size:0.68rem;font-weight:700;letter-spacing:0.05em;margin-left:10px">CORE</span>'
        )
    return ""


def render_identity_banner(
    *,
    portfolio_name: str,
    benchmark_name: str,
    period: str,
    currency: str,
    has_switcher: bool,
) -> None:
    """Render the gradient portfolio identity banner.

    Args:
        portfolio_name: Active portfolio name.
        benchmark_name: Benchmark name.
        period: Formatted reporting period string.
        currency: Base currency label.
        has_switcher: Whether the fund switcher renders directly below, in which
            case the banner keeps square bottom corners so the two connect.
    """
    badge = _sub_portfolio_badge(portfolio_name)
    banner_radius = "8px 8px 0 0" if has_switcher else "8px"
    banner_margin = "0" if has_switcher else "14px"
    st.markdown(
        f"""
<div style="background:linear-gradient(135deg,#1a3c5e 0%,#2c5282 100%);
            color:white;padding:14px 20px;border-radius:{banner_radius};margin-bottom:{banner_margin}">
  <div style="display:flex;align-items:stretch;gap:0;flex-wrap:wrap">
    <div style="flex:2;min-width:160px;padding-right:20px">
      <div style="font-size:0.62rem;text-transform:uppercase;letter-spacing:0.12em;
                  opacity:0.65;margin-bottom:3px">Portfolio</div>
      <div style="font-size:1.2rem;font-weight:700;line-height:1.2">
        {portfolio_name}{badge}
      </div>
    </div>
    <div style="width:1px;background:rgba(255,255,255,0.2);margin:0 20px"></div>
    <div style="flex:2;min-width:160px;padding-right:20px">
      <div style="font-size:0.62rem;text-transform:uppercase;letter-spacing:0.12em;
                  opacity:0.65;margin-bottom:3px">Benchmark</div>
      <div style="font-size:0.95rem;font-weight:500">{benchmark_name}</div>
    </div>
    <div style="width:1px;background:rgba(255,255,255,0.2);margin:0 20px"></div>
    <div style="flex:2;min-width:140px;padding-right:20px">
      <div style="font-size:0.62rem;text-transform:uppercase;letter-spacing:0.12em;
                  opacity:0.65;margin-bottom:3px">Periode</div>
      <div style="font-size:0.88rem">{period}</div>
    </div>
    <div style="width:1px;background:rgba(255,255,255,0.2);margin:0 20px"></div>
    <div style="flex:1;min-width:60px">
      <div style="font-size:0.62rem;text-transform:uppercase;letter-spacing:0.12em;
                  opacity:0.65;margin-bottom:3px">Valuta</div>
      <div style="font-size:0.88rem;font-weight:500">{currency}</div>
    </div>
  </div>
</div>
""",
        unsafe_allow_html=True,
    )


def render_portfolio_switcher(*, families: list[str], family_ports: list[str]) -> None:
    """Render the two-level (fund family + sub-portfolio) pills switcher.

    The pills write ``banner_family`` and ``banner_pills`` into session state;
    their defaults are seeded by the page orchestrator before this call.

    Args:
        families: Available fund-family labels.
        family_ports: Sub-portfolios within the selected family.
    """
    show_family_row = len(families) > 1
    show_sub_row = len(family_ports) > 1
    if not (show_family_row or show_sub_row):
        return

    st.markdown(_PILLS_STYLE, unsafe_allow_html=True)

    if show_family_row:
        st.pills(
            "Fonds",
            options=families,
            key="banner_family",
            label_visibility="collapsed",
        )

    if show_family_row and show_sub_row:
        st.markdown(
            '<div style="background:#1a3c5e;padding:0 20px">'
            '<div style="height:1px;background:rgba(255,255,255,0.15)"></div></div>',
            unsafe_allow_html=True,
        )

    if show_sub_row:
        st.pills(
            "Sub-portfolio",
            options=family_ports,
            format_func=sub_portfolio_label,
            key="banner_pills",
            label_visibility="collapsed",
        )

    st.markdown(
        '<div style="background:#1a3c5e;height:12px;border-radius:0 0 8px 8px;margin-bottom:14px"></div>',
        unsafe_allow_html=True,
    )


def render_key_metrics(
    *,
    attr_report: AttributionReport | None,
    factor_report: FactorAttributionReport | None,
    te_report: TrackingErrorReport | None,
) -> None:
    """Render the five-column key-metrics row and its divider.

    Args:
        attr_report: Portfolio attribution report, preferred performance source.
        factor_report: Factor attribution report, fallback performance source.
        te_report: Tracking-error report supplying ex-ante TE and beta.
    """
    mc1, mc2, mc3, mc4, mc5 = st.columns(5)

    source = attr_report or factor_report
    if source:
        active_rtn = source.active_return
        mc1.metric("Portfolio Return", format_metric(source.portfolio_return))
        mc2.metric("Benchmark Return", format_metric(source.total.tot_rtn_b))
        mc3.metric(
            "Active Return",
            format_metric(active_rtn),
            delta=f"{active_rtn:+.2f}%" if active_rtn is not None else None,
        )
        perf_label = "Port Attributie (T)" if isinstance(source, AttributionReport) else "Factor Attributie (T-1)"
        st.caption(f"Performancebron: {perf_label}")
    else:
        mc1.metric("Portfolio Return", "—")
        mc2.metric("Benchmark Return", "—")
        mc3.metric("Active Return", "—")

    if te_report:
        mc4.metric("Ex-ante TE", format_metric(te_report.portfolio_te))
        mc5.metric("Beta", format_metric(te_report.portfolio_beta, decimals=3, suffix="x"))
    else:
        mc4.metric("Ex-ante TE", "—")
        mc5.metric("Beta", "—")

    st.divider()


def render_environment_badge(*, environment: str) -> None:
    """Render a coloured datalake-environment badge in the sidebar.

    The colour scheme matches the a.s.r. IO Dashboard: red for development, blue
    for acceptance, green for production. The badge reflects the environment that
    backs the silver layer, i.e. the data the dashboard is currently reading.

    Args:
        environment: The environment name, for example ``development``,
            ``acceptance`` or ``production``.
    """
    palette: dict[str, tuple[str, str]] = {
        "development": ("#ff4b4b", "#d33"),
        "acceptance": ("#0083b8", "#005f87"),
        "production": ("#4caf50", "#388e3c"),
    }
    background, border = palette.get(environment.lower(), ("#6c757d", "#495057"))
    with st.sidebar:
        st.markdown(
            f"<div style='background-color:{background};color:white;padding:10px;"
            "border-radius:5px;text-align:center;font-weight:bold;margin-bottom:20px;"
            f"border:1px solid {border}'>{environment.upper()}</div>",
            unsafe_allow_html=True,
        )


def render_sidebar_refresh(*, controller: DashboardController) -> None:
    """Render the datalake refresh control in the sidebar.

    Presents a single "Refresh Data" action that pulls newly delivered Bloomberg
    PORT exports through the medallion pipeline (landing zone -> bronze -> silver)
    and reloads the dashboard from the refreshed silver layer.

    Args:
        controller: The dashboard controller exposing :meth:`refresh_data`.
    """
    with st.sidebar:
        st.header("Data")
        st.caption(
            "Haal de laatste Bloomberg PORT-exports op uit de datalake. "
            "Nieuwe bestanden worden automatisch verwerkt naar de silver-laag."
        )
        if st.button("\U0001f504 Refresh Data", width="stretch"):
            with st.spinner("Datalake verversen\u2026"):
                result = controller.refresh_data()
            _render_refresh_result(result)
        st.divider()


def _render_refresh_result(result: RefreshResult) -> None:
    """Summarise a medallion refresh run in the sidebar.

    Args:
        result: The counts and paths returned by :meth:`DashboardController.refresh_data`.
    """
    if result.promoted:
        st.success(f"{result.promoted} nieuw rapport(en) verwerkt naar silver.")
    else:
        st.info("Geen nieuwe rapporten gevonden.")

    context: list[str] = []
    if result.skipped_existing:
        context.append(f"{result.skipped_existing} reeds aanwezig")
    if result.skipped_not_ready:
        context.append(f"{result.skipped_not_ready} nog niet gereed")
    if context:
        st.caption(" \u00b7 ".join(context))

    if result.errors:
        with st.expander(f"\u26a0\ufe0f {len(result.errors)} fout(en)", expanded=False):
            for filename, message in result.errors:
                st.caption(f"{filename}: {message}")


def render_sidebar_status(
    *,
    selected_portfolio: str | None,
    store: SilverStore,
    attr_report: AttributionReport | None,
    te_report: TrackingErrorReport | None,
    sector_report: SectorAttributionReport | None,
    factor_report: FactorAttributionReport | None,
    factor_te_report: TERiskFactorReport | None,
) -> None:
    """Render the sidebar report-status list, diagnostics, and report-date picker.

    Args:
        selected_portfolio: The active portfolio, or ``None``.
        store: The silver store backing the history frames.
        attr_report: Active portfolio attribution report.
        te_report: Active tracking-error report.
        sector_report: Active sector attribution report.
        factor_report: Active factor attribution report.
        factor_te_report: Active TE risk-factor report.
    """
    with st.sidebar:
        st.caption("Geladen rapporten")
        for label, report in [
            ("Sector Attribution", sector_report),
            ("TE / Risk", te_report),
            ("Factor Attribution", factor_report),
            ("TE Factors", factor_te_report),
            ("Port Attribution", attr_report),
        ]:
            if report:
                st.markdown(
                    f"<span style='color:#2d6a4f;font-weight:600'>&#10003; {label}</span>"
                    f"<br><span style='color:#6c757d;font-size:0.75rem'>{report_display_date(report)}</span>",
                    unsafe_allow_html=True,
                )
            else:
                st.markdown(
                    f"<span style='color:#adb5bd'>&#8212; {label}</span>",
                    unsafe_allow_html=True,
                )

        if selected_portfolio and store.has_data():
            hist = store.summary_history(selected_portfolio)
            if not hist.empty:
                avail = sorted(pd.to_datetime(hist["as_of"]).dt.date.unique())
                st.divider()
                st.caption("**Rapportdatum**")
                if len(avail) == 1:
                    st.caption(avail[0].strftime("%d %b %Y"))
                else:
                    st.select_slider(
                        "Rapportdatum",
                        options=avail,
                        value=avail[-1],
                        format_func=lambda d: d.strftime("%d %b %Y"),
                        key="selected_report_date",
                        label_visibility="collapsed",
                    )
                    st.caption(
                        f"{len(avail)} datums beschikbaar  \n"
                        f"{avail[0].strftime('%d %b %Y')} \u2013 {avail[-1].strftime('%d %b %Y')}"
                    )
