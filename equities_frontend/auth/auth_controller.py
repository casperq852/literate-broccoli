"""
Controller for user-account management.

Wraps the plain YAML load/save and password-hashing utilities in
``auth.helpers`` with the account-management business rules: usernames are
case-insensitive and stored lowercase, and admin role is stored as an
explicit ``"role": "admin"`` field. Used by both ``admin/users_view.py``
(browser UI) and ``create_user.py`` (CLI bootstrap) so account creation
logic exists in exactly one place.
"""

from __future__ import annotations

import secrets

from auth import helpers


def _default_config() -> dict:
    """A fresh ``auth_config.yaml`` skeleton with a new random cookie key."""
    return {
        "credentials": {"usernames": {}},
        "cookie": {
            "expiry_days": 7,
            "key": secrets.token_hex(32),
            "name": "avb_equities_auth",
        },
    }


class UsersController:
    """
    Manages user accounts stored in ``auth_config.yaml``.

    Bootstraps a fresh config (with a new random cookie key) if the file does
    not exist yet, so this also serves as the entry point for creating the
    very first admin user.
    """

    def __init__(self) -> None:
        self._config: dict = helpers.load_config() if helpers.AUTH_CONFIG_PATH.exists() else _default_config()

    @property
    def users(self) -> dict:
        """All configured users, keyed by username."""
        return self._config.setdefault("credentials", {}).setdefault("usernames", {})

    def create_user(self, username: str, name: str, password: str, is_admin: bool = False) -> None:
        """
        Create or update a user account.

        Args:
            username (str): Login name; normalised to lowercase and stripped.
            name (str): Full display name.
            password (str): Plain-text password, hashed before storage.
            is_admin (bool): Whether to grant the admin role.
        """
        username = username.strip().lower()
        entry: dict = {"name": name, "password": helpers.hash_password(password)}
        if is_admin:
            entry["role"] = "admin"
        self.users[username] = entry
        helpers.save_config(self._config)

    def delete_user(self, username: str) -> None:
        """
        Remove a user account.

        Args:
            username (str): Login name to remove.

        Raises:
            KeyError: If ``username`` does not exist.
        """
        del self.users[username]
        helpers.save_config(self._config)

    def reset_password(self, username: str, new_password: str) -> None:
        """
        Reset a user's password.

        Args:
            username (str): Login name whose password is being reset.
            new_password (str): Plain-text new password, hashed before storage.

        Raises:
            KeyError: If ``username`` does not exist.
        """
        self.users[username]["password"] = helpers.hash_password(new_password)
        helpers.save_config(self._config)
