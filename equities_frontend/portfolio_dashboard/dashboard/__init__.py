"""Dashboard topic: Streamlit entry-point support (controller, view, helpers).

The Streamlit UI body lives in :mod:`portfolio_dashboard.app` because Streamlit
executes its script top-to-bottom, rendering widgets inline. This package holds
the parts that can be cleanly separated from that flow: pure formatting helpers,
the plotly rendering wrapper (view), and the wiring controller.
"""
