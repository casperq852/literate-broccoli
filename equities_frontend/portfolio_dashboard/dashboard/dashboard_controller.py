"""Dashboard controller: the Streamlit page orchestrator.

This is the a.s.r. Controller layer for the dashboard topic. ``DashboardController``
bundles the silver read store and the medallion refresher, and its :meth:`run`
method is the Streamlit entry-point body: it executes top-to-bottom on every
rerun and wires together the model (classification), the view (page chrome and
tabs), and the datalake refresh. It is the untestable UI boundary; all reusable,
type-checked logic lives in the sibling model, view, and helper modules.
"""

from __future__ import annotations

import logging

import streamlit as st

from portfolio_dashboard.config import load_app_config
from portfolio_dashboard.dashboard.dashboard_model import (
    group_portfolios_by_family,
    is_master_portfolio,
)
from portfolio_dashboard.dashboard.page_view import (
    configure_page,
    render_environment_badge,
    render_identity_banner,
    render_key_metrics,
    render_portfolio_switcher,
    render_sidebar_status,
)
from portfolio_dashboard.dashboard.tabs.attribution_view import render_attribution_tab
from portfolio_dashboard.dashboard.tabs.factor_view import render_factor_tab
from portfolio_dashboard.dashboard.tabs.positioning_view import render_positioning_tab
from portfolio_dashboard.dashboard.tabs.risk_view import render_risk_tab
from portfolio_dashboard.dashboard.tabs.transactions_view import render_transactions_tab
from portfolio_dashboard.dashboard.tabs.trends_view import render_trends_tab
from portfolio_dashboard.dashboard.tabs.weights_view import render_weights_tab
from portfolio_dashboard.datalake.medallion_controller import MedallionRefresher, RefreshResult
from portfolio_dashboard.datalake.silver_controller import SilverStore
from portfolio_dashboard.reports.reports_model import (
    AttributionReport,
    FactorAttributionReport,
    SectorAttributionReport,
    TERiskFactorReport,
    TrackingErrorReport,
)

logging.basicConfig(level=logging.INFO)
# matplotlib logs an INFO line while (re)building its font cache; keep the
# third-party logger at WARNING so it never reaches the Streamlit console.
logging.getLogger("matplotlib").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

# Mapping from Bloomberg portfolio-name substrings to canonical fund-family
# label. AEIIAEF uses "AEIIAEF" in the master name but "AEIIEAF" (letters
# transposed) in sub-portfolio names from Excel Detail tabs; both map alike.
_FAMILY_MAP: dict[str, str] = load_app_config()["dashboard"]["family_map"]

# All known fund-code substrings (used for master/fonds detection).
_MASTER_CODES = set(_FAMILY_MAP.keys())


class DashboardController:
    """Bundle the silver store and refresher and drive the Streamlit page.

    Attributes:
        store: The silver-layer read store backing the current-period reports
            and the trend-history frames.
        refresher: The medallion pipeline that pulls new datalake exports.
    """

    def __init__(self, store: SilverStore | None = None, refresher: MedallionRefresher | None = None) -> None:
        """Wire the store and refresher, creating defaults when not supplied.

        Args:
            store: An existing silver store, or ``None`` to construct the default.
            refresher: An existing refresher, or ``None`` to construct the default.
        """
        self.store: SilverStore = store if store is not None else SilverStore()
        self.refresher: MedallionRefresher = refresher if refresher is not None else MedallionRefresher()

    # ── datalake actions ─────────────────────────────────────────────────────

    def refresh_data(self) -> RefreshResult:
        """Pull new exports through the medallion pipeline and reload silver.

        Runs the incremental landing-zone -> bronze -> silver refresh, then
        invalidates the silver read cache so the next render reflects the newly
        promoted reports.

        Returns:
            RefreshResult: Counts and paths describing what changed.
        """
        result = self.refresher.refresh()
        self.store.invalidate()
        return result

    # ── page orchestration ───────────────────────────────────────────────────

    def run(self) -> None:
        """Render the full Portfolio Analytics page.

        Executes on every Streamlit rerun: configures the page, reads the silver
        layer, resolves the active portfolio, draws the banner/switcher/metrics,
        and renders the six analysis tabs.
        """
        configure_page()

        # Manual "Refresh Data" sidebar control intentionally not rendered here:
        # equities_frontend's own hourly scheduler already calls
        # MedallionRefresher().refresh() on this same silver layer (see
        # scheduler/scheduler_controller.py), so a manual refresh is both
        # redundant and, on a resource-constrained host, an easy way for a
        # user to trigger duplicate heavy work while the scheduler is doing
        # the same thing in the background.

        with st.spinner("Loading portfolios..."):
            reports_by_portfolio = self.store.load_reports_by_portfolio()

        # ── portfolio selector (fund family → sub-portfolio) ──
        portfolio_names = sorted(reports_by_portfolio.keys())
        fund_groups = group_portfolios_by_family(portfolio_names, _FAMILY_MAP)
        families = sorted(fund_groups.keys())

        if families:
            sel_family = st.session_state.get("banner_family")
            if sel_family not in families:
                sel_family = families[0]
                st.session_state["banner_family"] = sel_family

            # Exclude Cash sub-portfolios from the switcher; they add no analysis.
            family_ports = sorted(p for p in fund_groups[sel_family] if "cash" not in p.lower())

            sel_port = st.session_state.get("banner_pills")
            if sel_port not in family_ports:
                fonds = [
                    p
                    for p in family_ports
                    if "alpha" not in p.lower() and "core" not in p.lower() and "cash" not in p.lower()
                ]
                sel_port = fonds[0] if fonds else family_ports[0]
                st.session_state["banner_pills"] = sel_port

            selected_portfolio = sel_port
            with st.sidebar:
                if len(portfolio_names) == 1:
                    st.divider()
                    st.caption(f"Portfolio: **{selected_portfolio}**")
        else:
            selected_portfolio = None
            family_ports = []

        if selected_portfolio:
            port_data = reports_by_portfolio[selected_portfolio]
            attr_report: AttributionReport | None = port_data.get("attr")
            te_report: TrackingErrorReport | None = port_data.get("te")
            sector_report: SectorAttributionReport | None = port_data.get("sector")
            factor_report: FactorAttributionReport | None = port_data.get("factor")
            factor_te_report: TERiskFactorReport | None = port_data.get("te_factors")
        else:
            attr_report = te_report = sector_report = factor_report = factor_te_report = None

        render_sidebar_status(
            selected_portfolio=selected_portfolio,
            store=self.store,
            attr_report=attr_report,
            te_report=te_report,
            sector_report=sector_report,
            factor_report=factor_report,
            factor_te_report=factor_te_report,
        )

        # Environment badge sits at the very bottom of the sidebar.
        render_environment_badge(environment=self.store.environment)

        # ── page header ──
        st.title("Portfolio Analytics")

        if not reports_by_portfolio:
            st.info(
                "Nog geen data geladen.  \n"
                "De achtergrondtaak haalt elk uur automatisch de laatste Bloomberg PORT-exports "
                "op uit de datalake en verwerkt ze naar de silver-laag."
            )
            st.stop()

        any_report = attr_report or te_report or sector_report or factor_report or factor_te_report
        if any_report is None:
            st.info(
                f"Geen rapporten gevonden voor **{selected_portfolio}**. " "Selecteer een ander portfolio in de zijbalk."
            )
            st.stop()

        # ── identity banner + switcher + key metrics ──
        metadata = any_report.metadata
        port_name = metadata.portfolio or selected_portfolio or "—"
        bench_name = metadata.benchmark or "—"
        period_str = (
            f"{metadata.period_start} – {metadata.period_end}"
            if metadata.period_start
            else (metadata.as_of or metadata.run_date or "—")
        )
        ccy = metadata.base_currency or "—"
        is_master = is_master_portfolio(port_name, _MASTER_CODES)

        render_identity_banner(
            portfolio_name=port_name,
            benchmark_name=bench_name,
            period=period_str,
            currency=ccy,
            has_switcher=len(portfolio_names) > 1,
        )
        render_portfolio_switcher(families=families, family_ports=family_ports)
        render_key_metrics(attr_report=attr_report, factor_report=factor_report, te_report=te_report)

        # ── tabs ──
        tab_positioning, tab_attribution, tab_factor, tab_risk, tab_trends, tab_weights, tab_transactions = st.tabs(
            [
                "📍 Positionering",
                "📊 Portfolio Attributie",
                "🧮 Factor Attributie",
                "⚡ Risk & Tracking Error",
                "📈 Trends",
                "⚖️ Gewichten",
                "💱 Transacties",
            ]
        )

        with tab_positioning:
            render_positioning_tab(
                attr_report=attr_report,
                te_report=te_report,
                sector_report=sector_report,
                factor_report=factor_report,
                factor_te_report=factor_te_report,
                is_master=is_master,
                selected_portfolio=selected_portfolio,
            )

        with tab_attribution:
            render_attribution_tab(
                attr_report=attr_report,
                factor_report=factor_report,
                sector_report=sector_report,
                store=self.store,
                selected_portfolio=selected_portfolio,
            )

        with tab_factor:
            render_factor_tab(factor_report=factor_report, factor_te_report=factor_te_report)

        with tab_risk:
            render_risk_tab(te_report=te_report, factor_te_report=factor_te_report, is_master=is_master)

        with tab_trends:
            render_trends_tab(store=self.store, selected_portfolio=selected_portfolio)

        with tab_weights:
            render_weights_tab(te_report=te_report, is_master=is_master)

        with tab_transactions:
            render_transactions_tab(store=self.store)
