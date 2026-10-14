"""
Standalone (non-Streamlit) config loading for the background scheduler.

app.py's config bootstrap reads ``st.session_state``-free values out of
``configs/app_config.yaml`` but stores the result in ``st.session_state``,
which only exists inside a live Streamlit session. The scheduler runs in a
plain background thread with no session, so these functions duplicate app.py's
three-line bootstrap (same file, same 'deployment' key shape) without touching
Streamlit at all.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

import yaml
from deployment_env import DEPLOYMENT_ENVIRONMENT

logger = logging.getLogger(__name__)

APP_CONFIG_PATH = Path("./equities_frontend/configs/app_config.yaml")

# One hourly tick: classify newly-seen companies, then back up the cache.
TICK_INTERVAL_SECONDS = 3600


def load_app_config() -> dict[str, Any]:
    """
    Load ``configs/app_config.yaml`` and resolve the active deployment block.

    Mirrors app.py's bootstrap exactly (same file, same 'deployment' key
    shape) so callers can treat the result identically to
    ``st.session_state["app_config"]`` inside a real Streamlit session.

    Returns:
        dict[str, Any]: The full parsed config, with a 'deployment' key set
        to the resolved ``DEPLOYMENT_ENVIRONMENT`` block.

    Raises:
        KeyError: If ``DEPLOYMENT_ENVIRONMENT`` has no matching top-level
            block in the config file.
    """
    with open(APP_CONFIG_PATH) as config_file:
        app_config: dict[str, Any] = yaml.safe_load(config_file)
    if DEPLOYMENT_ENVIRONMENT not in app_config:
        raise KeyError(f"No '{DEPLOYMENT_ENVIRONMENT}' block found in {APP_CONFIG_PATH}.")
    app_config["deployment"] = app_config[DEPLOYMENT_ENVIRONMENT]
    app_config["deployment"]["deployment_name"] = DEPLOYMENT_ENVIRONMENT
    return app_config


def load_equities_config() -> dict[str, Any]:
    """Return the 'equities' block -- standalone equivalent of helpers.equities_config()."""
    return load_app_config()["equities"]


def resolve_datalake_account(app_config: dict[str, Any]) -> str:
    """
    Return the Data Lake account name for the selected deployment.

    Prefers the ``ACCOUNT_NAME`` env var (already how main.py resolves it,
    already set as a Docker ARG/ENV) over the config file, falling back to
    ``app_config["deployment"]["datalake_name"]`` when unset.
    """
    return os.getenv("ACCOUNT_NAME") or app_config["deployment"]["datalake_name"]
