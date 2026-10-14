"""Per-tab view components for the portfolio dashboard.

Each module exposes a single ``render_*_tab`` function that draws one Streamlit
tab. The functions receive already-parsed reports (or the storage handle) and
contain only visualization logic, keeping :mod:`portfolio_dashboard.app` a thin
orchestrator.
"""
