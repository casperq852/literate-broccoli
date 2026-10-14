"""
Helper script to add a named user to auth_config.yaml.

Usage:
    python equities_frontend/create_user.py <username> <full_name> <password> [--admin]

Examples:
    python equities_frontend/create_user.py admin "Admin User" mysecretpassword --admin
    python equities_frontend/create_user.py jdoe "Jane Doe" mysecretpassword

The script hashes the password with bcrypt and appends the user entry to
equities_frontend/auth_config.yaml, creating the file (with a random cookie key)
if it doesn't exist yet.

Admin users see the User Management page in the app and can create/delete
accounts from the browser without needing server access.
"""

from __future__ import annotations

import sys

from auth.auth_controller import UsersController
from auth.helpers import AUTH_CONFIG_PATH


def main() -> None:
    """Parse CLI args and create or update the requested user account."""
    args = sys.argv[1:]
    is_admin = "--admin" in args
    args = [a for a in args if a != "--admin"]

    if len(args) != 3:  # noqa: PLR2004
        print(__doc__)
        sys.exit(1)

    username, full_name, password = args[0], args[1], args[2]

    controller = UsersController()
    normalised_username = username.strip().lower()
    if normalised_username in controller.users:
        print(f"User '{normalised_username}' already exists — updating.")

    controller.create_user(username, full_name, password, is_admin=is_admin)

    role_label = "admin" if is_admin else "user"
    print(f"User '{normalised_username}' ({full_name}, {role_label}) saved to {AUTH_CONFIG_PATH}.")
    print("Restart the Streamlit app to apply the change.")


if __name__ == "__main__":
    main()
