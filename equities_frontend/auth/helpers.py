"""Shared access to ``auth_config.yaml`` — path, load/save, and password hashing.

Used by ``version_selector.py`` (login gate), ``auth/auth_controller.py`` (user
management), and ``create_user.py`` (CLI user creation) so all three agree on
where the config lives and how passwords are hashed.
"""

from __future__ import annotations

from pathlib import Path

import bcrypt
import yaml

AUTH_CONFIG_PATH: Path = Path(__file__).parent.parent / "configs" / "auth_config.yaml"


def load_config() -> dict:
    """Read and parse ``auth_config.yaml``."""
    with open(AUTH_CONFIG_PATH) as f:
        return yaml.safe_load(f)


def save_config(cfg: dict) -> None:
    """Write ``cfg`` back to ``auth_config.yaml``."""
    AUTH_CONFIG_PATH.write_text(yaml.dump(cfg, default_flow_style=False, allow_unicode=True))


def hash_password(plain: str) -> str:
    """Hash a plain-text password with bcrypt."""
    return bcrypt.hashpw(plain.encode(), bcrypt.gensalt()).decode()
