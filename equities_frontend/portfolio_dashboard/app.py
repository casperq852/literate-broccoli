"""Portfolio Analytics Dashboard — Streamlit entry-point.

Thin launcher script. It prepares ``sys.path`` so the dashboard runs both as a
package module and as a bare Streamlit script, then delegates all rendering to
``portfolio_dashboard.dashboard.dashboard_controller.DashboardController.run``.
Use the sidebar **Refresh Data** button to pull the latest Bloomberg PORT
exports from the Azure Data Lake through the medallion pipeline (landing zone ->
bronze -> silver); the dashboard reads the silver layer for multi-day,
multi-portfolio trend analysis.

Run
---
    python portfolio_dashboard/main.py
    python -m portfolio_dashboard
    PYTHONPATH=. streamlit run portfolio_dashboard/app.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

# matplotlib is pulled in transitively by the dashboard's plotting stack and, on
# import, tries to create its cache under $HOME, which is read-only on the a.s.r.
# Azure ML compute ("mkdir failed / Permission denied"). Point it at a writable,
# stable temp dir before any such import so the warning is gone and the font cache
# persists across runs.
if not os.environ.get("MPLCONFIGDIR"):
    os.environ["MPLCONFIGDIR"] = str(Path(tempfile.gettempdir()) / "portfolio_dashboard_mpl")
Path(os.environ["MPLCONFIGDIR"]).mkdir(parents=True, exist_ok=True)

sys.path.insert(0, str(Path(__file__).parent.parent))

import streamlit as st

from portfolio_dashboard.dashboard.dashboard_controller import DashboardController

# Cache the controller for the session so the silver read store's downloaded
# reports persist across reruns; a Refresh Data click reloads via invalidate().
if "controller" not in st.session_state:
    st.session_state["controller"] = DashboardController()

st.session_state["controller"].run()
