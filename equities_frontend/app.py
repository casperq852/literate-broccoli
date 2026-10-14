"""
Streamlit application: config bootstrap and page navigation.

Executed by ``version_selector.py`` with the selected deployment environment
injected as the ``azure_version`` global. Loads ``configs/app_config.yaml`` once into
session state, resolves the active deployment block, and registers the pages.
"""

from logging import Logger

import streamlit as st
import yaml
from streamlit.logger import get_logger
from streamlit.navigation.page import StreamlitPage

from helpers import load_css

logger: Logger = get_logger(name=__name__)

load_css()

# Deployment environment injected by version_selector.py via exec globals.
azure_version: str = globals().get("azure_version", "")

if not azure_version:
    st.warning(body="Deployment environment not loaded properly.")
    logger.warning(msg="Deployment environment not loaded properly.")

# Load the application config once into session state and resolve the active
# deployment block (e.g. 'development' or 'acceptance') from app_config.yaml.
try:
    with open(file="./equities_frontend/configs/app_config.yaml") as config_file:
        app_config: dict = yaml.safe_load(stream=config_file)
        if azure_version not in app_config:
            logger.error(msg=f"No '{azure_version}' block found in configs/app_config.yaml.")
            st.error(body=f"No '{azure_version}' configuration found.")
            st.stop()
        app_config["deployment"] = app_config[azure_version]
        app_config["deployment"]["deployment_name"] = azure_version
        st.session_state["app_config"] = app_config
except Exception as error:  # noqa: BLE001
    st.error(body=f"Error loading configuration due to {error}.")
    logger.error(msg=f"Error loading configuration: {error}")
    st.stop()

# Page navigation.
pages: list[StreamlitPage] = [
    st.Page(page="chat/chat_view.py", title="Chat", icon=":material/chat:"),
    st.Page(page="companies/company_view.py", title="Companies", icon=":material/apartment:"),
    st.Page(
        page="investment_case/investment_case_view.py", title="Investment Case Builder", icon=":material/business_center:"
    ),
    st.Page(page="portfolio_dashboard_view.py", title="Portfolio Dashboard", icon=":material/pie_chart:"),
    st.Page(page="index/index_view.py", title="Index Browser", icon=":material/database:"),
    st.Page(page="upload/upload_view.py", title="Upload Document", icon=":material/upload_file:"),
    st.Page(page="upload/bulk_upload_view.py", title="Bulk Upload (zip)", icon=":material/folder_zip:"),
    st.Page(page="logs/logs_view.py", title="Logs", icon=":material/terminal:"),
]

if st.session_state.get("is_admin"):
    pages.append(st.Page(page="admin/users_view.py", title="User Management", icon=":material/manage_accounts:"))

pg: StreamlitPage = st.navigation(pages, position="top")
pg.run()
