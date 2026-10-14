"""Central application configuration loader.

This is the a.s.r. configuration entry-point: it reads the single
``app_config.yaml`` that holds every dataset- and user-specific value, so a data
or naming change only touches YAML, never Python (coding standard section 7,
"No hard-coded values"). The parsed document is cached so repeated lookups are
cheap and consistent across the app.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

# The YAML lives one level up, alongside the package, so a single edit there
# reconfigures the whole dashboard.
_CONFIG_PATH = Path(__file__).parent.parent / "app_config.yaml"


@lru_cache(maxsize=1)
def load_app_config() -> dict[str, Any]:
    """Load and cache the central application configuration.

    Returns:
        dict[str, Any]: The parsed ``app_config.yaml`` document.

    Raises:
        FileNotFoundError: When the configuration file is missing.
    """
    if not _CONFIG_PATH.exists():
        raise FileNotFoundError(f"Application config not found at {_CONFIG_PATH}.")
    with _CONFIG_PATH.open(encoding="utf-8") as config_file:
        return yaml.safe_load(config_file)
