"""
Streamlit entry point for the Equities RAG frontend.

Configures file logging, renders the sidebar branding, runs the optional login
gate, and then loads the application defined in ``app.py``. The deployment
environment is fixed in code (``DEPLOYMENT_ENVIRONMENT``); ``configs/app_config.yaml``
still carries every environment block so switching only requires changing this
one constant.
"""

from __future__ import annotations

import logging
import os
from logging import FileHandler, Formatter, Logger
from pathlib import Path

import streamlit as st
from deployment_env import DEPLOYMENT_ENVIRONMENT
from dotenv import load_dotenv
from streamlit.logger import get_logger

from auth.helpers import AUTH_CONFIG_PATH, load_config
from helpers import LOG_FILE_PATH, install_log_capture

try:
    import streamlit_authenticator as stauth
except ImportError:  # optional dependency; the login gate degrades gracefully
    stauth = None

BASE_DIR: Path = Path("equities_frontend")
APP_DIR: Path = BASE_DIR
ASSETS_PATH: Path = BASE_DIR / "assets" / "asr_vermogensbeheer_logo.png"
STREAMLIT_LOG_FILE: Path = LOG_FILE_PATH
AZUREML_RUN_ID: str = os.getenv("AZUREML_RUN_ID") or "local"

streamlit_logger: Logger = get_logger(name=__name__)
load_dotenv()


def configure_streamlit_logger_to_file_with_rotation(
    log_file_path: Path, level: int = logging.INFO, max_lines: int = 20_000
) -> None:
    """
    Configure Streamlit's logger to write to a file with simple line rotation.

    If the log file already exceeds ``max_lines`` it is truncated to its most
    recent ``max_lines`` lines before a file handler is attached. The handler is
    only added once, so repeated Streamlit reruns do not stack duplicate handlers.

    Args:
        log_file_path (Path): Destination log file.
        level (int): Minimum logging level for the file handler.
        max_lines (int): Maximum retained lines before truncation.
    """
    if os.path.exists(path=log_file_path):
        with open(file=log_file_path, mode="r+") as log_file:
            lines: list[str] = log_file.readlines()
            if len(lines) > max_lines:
                log_file.seek(0)
                log_file.truncate()
                log_file.writelines(lines[-max_lines:])

    for handler in streamlit_logger.handlers:
        if isinstance(handler, logging.FileHandler) and handler.baseFilename == str(log_file_path):
            return

    file_handler: FileHandler = logging.FileHandler(filename=log_file_path)
    file_handler.setLevel(level)
    formatter: Formatter = logging.Formatter(fmt="%(asctime)s - %(levelname)s - %(message)s")
    file_handler.setFormatter(fmt=formatter)
    streamlit_logger.addHandler(hdlr=file_handler)
    streamlit_logger.setLevel(level)


configure_streamlit_logger_to_file_with_rotation(log_file_path=STREAMLIT_LOG_FILE, level=logging.DEBUG)
# Capture avb_core / avb_search / vectorization logs (propagated to root) for the UI log panels.
install_log_capture(level=logging.DEBUG)

if "azure_version" not in st.session_state:
    st.session_state["azure_version"] = DEPLOYMENT_ENVIRONMENT


def run_auth() -> bool:
    """
    Run the login gate when ``auth_config.yaml`` is present.

    Returns True when the user is authenticated, or when authentication is
    disabled because no config file exists (developer mode). On success the
    username and admin flag are stored in the session for the rest of the app.

    Returns:
        bool: Whether the request may proceed to the application.
    """
    if not AUTH_CONFIG_PATH.exists():
        return True  # dev mode -- no auth file, skip the gate

    if stauth is None:
        st.error("streamlit-authenticator is not installed. Run `uv add streamlit-authenticator~=0.3` and restart.")
        return False

    try:
        auth_config: dict = load_config()
    except Exception as error:  # noqa: BLE001
        st.error(f"Could not read auth_config.yaml: {error}")
        return False

    cookie_config: dict = auth_config.get("cookie", {})
    authenticator = stauth.Authenticate(
        credentials=auth_config["credentials"],
        cookie_name=cookie_config.get("name", "avb_equities_auth"),
        cookie_key=cookie_config.get("key", "change-me-in-auth-config"),
        cookie_expiry_days=cookie_config.get("expiry_days", 7),
    )

    name, auth_status, username = authenticator.login("Login", "main")

    if auth_status is False:
        st.error("Incorrect username or password.")
        return False
    if auth_status is None:
        st.info("Please enter your credentials.")
        return False

    user_info: dict = auth_config["credentials"]["usernames"].get(username, {})
    st.session_state["current_username"] = username
    st.session_state["is_admin"] = user_info.get("role") == "admin"
    st.sidebar.success(f"Logged in as **{name}**")
    if st.session_state["is_admin"]:
        st.sidebar.caption("admin")
    authenticator.logout("Logout", "sidebar")
    return True


def load_app() -> None:
    """Load and execute ``app.py``, injecting the fixed deployment environment."""
    app_path: Path = APP_DIR / "app.py"
    if app_path.exists():
        with open(file=app_path) as app_file:
            exec_globals: dict = {"azure_version": st.session_state["azure_version"]}
            exec(app_file.read(), exec_globals)  # noqa: S102 - trusted first-party app module
    else:
        streamlit_logger.error(msg=f"App file not found at: {app_path}")
        st.error(body=f"App file not found at {app_path}")


def render_environment_badge() -> None:
    """Render the deployment-environment badge as the last sidebar element."""
    st.sidebar.markdown(
        body=f"""
        <div style="background-color: #4CAF50; color: white; padding: 10px;
        border-radius: 5px; text-align: center; font-weight: bold; margin: 10px 0 20px 0;
        border: 1px solid #388E3C">
            {DEPLOYMENT_ENVIRONMENT.upper()}
        </div>
        """,
        unsafe_allow_html=True,
    )


def render_frontend() -> None:
    """Render the sidebar branding, run the auth gate, and load the app."""
    if ASSETS_PATH.exists():
        st.sidebar.image(image=str(ASSETS_PATH), width=200)

    st.markdown(
        body="""
        <style>
            [data-testid="stSidebar"] {
                background-color: #fbfbe3 !important;
            }
        </style>
        """,
        unsafe_allow_html=True,
    )

    st.sidebar.title(body="Equities RAG")
    st.sidebar.markdown(
        body="Search and chat with the Equities desk's indexed documents (broker notes, "
        "earnings transcripts, annual reports, investor presentations, and more) and "
        "Bloomberg financial models. Upload new documents to index them on the fly. Powered "
        "by Azure AI Search on top of the avb_search ingestion pipeline."
    )

    render_environment_badge()

    if not run_auth():
        st.stop()

    load_app()


st.set_page_config(page_title="Equities RAG", layout="wide")

render_frontend()
