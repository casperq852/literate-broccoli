"""
Single source of truth for which app_config.yaml deployment block is active.

Read by both version_selector.py (the Streamlit entry point) and
scheduler/scheduler_model.py (the standalone background scheduler, which has
no Streamlit session to read this from) -- kept in its own module so the two
loaders can't silently drift onto different deployment blocks.
"""

from __future__ import annotations

# Fixed deployment environment; must match a top-level block in configs/app_config.yaml.
DEPLOYMENT_ENVIRONMENT: str = "production"
