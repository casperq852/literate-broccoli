"""Dashboard view helpers: thin Streamlit rendering wrappers.

Keeps plotly rendering configuration in one place so every chart in the app
shares the same toolbar behaviour and export settings.
"""

from __future__ import annotations

from typing import Any

import plotly.graph_objects as go
import streamlit as st

# Shared plotly toolbar configuration applied to every rendered chart.
CHART_CONFIG: dict[str, Any] = {
    "displayModeBar": True,
    "displaylogo": False,
    "modeBarButtonsToRemove": ["select2d", "lasso2d"],
    "toImageButtonOptions": {"format": "png", "scale": 2},
}


def render_chart(fig: go.Figure, **kwargs: Any) -> None:
    """Render a plotly figure with the shared dashboard configuration.

    Args:
        fig: The plotly figure to display.
        **kwargs: Extra keyword arguments forwarded to ``st.plotly_chart``.
    """
    kwargs.setdefault("width", "stretch")
    st.plotly_chart(fig, config=CHART_CONFIG, **kwargs)
