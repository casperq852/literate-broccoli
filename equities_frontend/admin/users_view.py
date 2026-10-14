"""Admin — user account management (admin role only)."""

from __future__ import annotations

import streamlit as st

from auth.auth_controller import UsersController
from auth.helpers import AUTH_CONFIG_PATH

# ── guard ─────────────────────────────────────────────────────────────────────

if not st.session_state.get("is_admin"):
    st.error("You do not have permission to access this page.")
    st.stop()

if not AUTH_CONFIG_PATH.exists():
    st.error("auth_config.yaml not found — authentication is not configured.")
    st.stop()

controller = UsersController()

# ── page ──────────────────────────────────────────────────────────────────────

st.title("User Management")
st.caption("Add, remove, or update user accounts. Changes take effect on the next login.")

# ── current users ─────────────────────────────────────────────────────────────

st.subheader("Current users")

if not controller.users:
    st.info("No users configured yet.")
else:
    for username, info in list(controller.users.items()):
        col_name, col_role, col_del = st.columns([4, 2, 1])
        role = "admin" if info.get("role") == "admin" else "user"
        col_name.markdown(f"**{info.get('name', username)}** `{username}`")
        col_role.caption(role)
        if col_del.button("Remove", key=f"del_{username}", type="secondary"):
            if username == st.session_state.get("current_username"):
                st.warning("You cannot remove your own account.")
            else:
                controller.delete_user(username)
                st.success(f"Removed user **{username}**.")
                st.rerun()

st.divider()

# ── add new user ──────────────────────────────────────────────────────────────

st.subheader("Add user")

with st.form("add_user_form", clear_on_submit=True):
    new_username = st.text_input("Username", placeholder="jdoe")
    new_name = st.text_input("Full name", placeholder="Jane Doe")
    new_password = st.text_input("Password", type="password")
    new_is_admin = st.checkbox("Grant admin role")
    submitted = st.form_submit_button("Create user", type="primary")

if submitted:
    normalised_username = new_username.strip().lower()
    if not normalised_username or not new_name or not new_password:
        st.error("All fields are required.")
    elif normalised_username in controller.users:
        st.error(f"Username **{normalised_username}** already exists.")
    else:
        controller.create_user(new_username, new_name, new_password, is_admin=new_is_admin)
        st.success(f"Created user **{normalised_username}** ({'admin' if new_is_admin else 'user'}).")
        st.rerun()

st.divider()

# ── change password ───────────────────────────────────────────────────────────

st.subheader("Reset password")

with st.form("reset_pw_form", clear_on_submit=True):
    target = st.selectbox("User", options=list(controller.users.keys()))
    new_pw = st.text_input("New password", type="password")
    confirm_pw = st.text_input("Confirm password", type="password")
    reset_submitted = st.form_submit_button("Reset password")

if reset_submitted:
    if not new_pw:
        st.error("Password cannot be empty.")
    elif new_pw != confirm_pw:
        st.error("Passwords do not match.")
    elif target:
        controller.reset_password(target, new_pw)
        st.success(f"Password updated for **{target}**.")
