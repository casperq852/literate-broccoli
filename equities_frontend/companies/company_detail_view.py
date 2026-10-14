"""
Company detail screen: research documents, financial summary, scoped chat,
and upload for one company.

Rendered by ``company_view.py`` when a ``ticker`` query param is present,
rather than being its own registered page -- see that module's docstring for
why. Every widget key below is namespaced with the ticker so switching
companies within the same browser session never collides with stale widget
state from a previously viewed company.
"""

from __future__ import annotations

import base64
import re
from logging import Logger
from typing import Any

import pandas as pd
import streamlit as st
from streamlit.logger import get_logger

from chat.chat_controller import ChatController
from companies.company_controller import CompanyController
from helpers import datalake_account, download_from_datalake, hide_research_notes
from kpi_monitor import kpi_view
from search.search_model import ALL_DOCUMENT_TYPES, RESEARCH_NOTE_DOC_TYPE
from upload.upload_controller import UploadController

logger: Logger = get_logger(name=__name__)


def _escape_currency(text: str) -> str:
    """Escape $ signs so Streamlit doesn't render them as LaTeX delimiters."""
    return re.sub(r"(?<!\\)\$", r"\\$", text)


def render(ticker: str, controller: CompanyController, config: dict[str, Any]) -> None:
    """
    Render the full-page detail screen for one company.

    Args:
        ticker (str): The selected company's ticker.
        controller (CompanyController): The Companies topic controller.
        config (dict[str, Any]): The equities app config block.
    """
    if st.button("← Back to Companies"):
        st.query_params.clear()
        st.rerun()

    company = controller.get_company(ticker)
    if company is None:
        st.error(f"'{ticker}' is not in the local company cache. Go back and click Sync.")
        return

    st.title(company["display_name"])
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Ticker", company["ticker"])
    m2.metric("Region", company["region"] or "Unclassified")
    m3.metric("Sector", company["gics_sector"] or "Unclassified")
    m4.metric("Documents", company["doc_count"])

    tab_docs, tab_financials, tab_kpi, tab_positioning, tab_chat, tab_upload = st.tabs(
        ["Documents", "Financials", "KPI Monitor", "Positioning", "Chat", "Upload"]
    )
    with tab_docs:
        _render_documents_tab(ticker, controller)
    with tab_financials:
        _render_financials_tab(ticker, controller, config, company)
    with tab_kpi:
        kpi_view.render(ticker, company, controller, config)
    with tab_positioning:
        _render_positioning_tab(controller, company)
    with tab_chat:
        _render_chat_tab(ticker, config)
    with tab_upload:
        _render_upload_tab(ticker, controller, config)


# ── documents ────────────────────────────────────────────────────────────────


def _render_documents_tab(ticker: str, controller: CompanyController) -> None:
    documents = controller.documents_for_company(ticker)
    if documents.empty:
        st.info("No documents cached for this company yet.")
        return

    st.caption(f"**{len(documents)}** document(s), newest first.")
    event = st.dataframe(
        documents.drop(columns=["_path"]),
        use_container_width=True,
        hide_index=True,
        on_select="rerun",
        selection_mode="single-row",
        column_config={"Date": st.column_config.DateColumn("Date", format="YYYY-MM-DD")},
        key=f"company_docs_table_{ticker}",
    )

    selected = event.selection.rows
    if not selected:
        return

    row = documents.iloc[selected[0]]
    path = row["_path"]
    filename = row["File"]
    st.divider()
    st.subheader(filename)

    try:
        with st.spinner("Loading file from Data Lake..."):
            file_bytes = download_from_datalake(account_name=datalake_account(), file_path=path)

        is_pdf = filename.lower().endswith(".pdf")
        mime = "application/pdf" if is_pdf else "application/octet-stream"

        dl_col, view_col = st.columns([1, 5])
        dl_col.download_button(
            "Download",
            data=file_bytes,
            file_name=filename,
            mime=mime,
            icon=":material/download:",
            key=f"company_dl_{ticker}_{filename}",
        )
        if is_pdf and view_col.toggle("View PDF inline", key=f"company_view_pdf_{ticker}_{filename}"):
            b64 = base64.b64encode(file_bytes).decode()
            st.components.v1.html(
                f'<iframe src="data:application/pdf;base64,{b64}" '
                f'width="100%" height="900px" style="border:none;"></iframe>',
                height=920,
            )
    except Exception as err:
        st.error(f"Could not load file from Data Lake: {err}")
        logger.exception("Failed to download %s", path)


# ── financials ───────────────────────────────────────────────────────────────


def _render_financials_tab(ticker: str, controller: CompanyController, config: dict[str, Any], company: dict) -> None:
    if not company["has_financial_model"]:
        st.info("No ingested financial model (MODL) for this company yet.")
        return

    with st.spinner("Loading financial data..."):
        rows = controller.financial_summary(ticker, config)

    if not rows:
        st.warning("A financial model is indexed for this company, but no line items could be parsed from it.")
        return

    summary = pd.DataFrame(rows)
    pivoted = summary.pivot_table(index="Metric", columns="Period", values="Value", aggfunc="last")
    pivoted = pivoted.reindex(sorted(pivoted.columns), axis=1)
    st.dataframe(pivoted, use_container_width=True)
    st.caption(
        "Most recent periods, auto-mapped from the ingested model. Open the full "
        "DCF Model Builder to confirm the mapping and run a valuation."
    )

    if st.button("Open in DCF Model Builder →", key=f"company_open_dcf_{ticker}"):
        st.session_state["dcf_preselect_ticker"] = ticker
        st.switch_page("dcf/dcf_view.py")


# ── positioning & crowding ───────────────────────────────────────────────────

_FACTOR_METRICS: list[tuple[str, str]] = [
    ("overall_factor_pct", "Overall"),
    ("momentum_factor_pct", "Momentum"),
    ("quality_factor_pct", "Quality"),
    ("low_vol_factor_pct", "Low Volatility"),
    ("growth_factor_pct", "Growth"),
    ("valuation_factor_pct", "Valuation"),
]


def _render_positioning_tab(controller: CompanyController, company: dict) -> None:
    """
    Show this company's real portfolio positioning and its latest uploaded
    factor/crowding screen data side by side.

    Inspired by the "Network Intelligence" idea of judging whether a thesis
    is already priced: this pairs data this app already has (real portfolio
    holdings, via ``CompanyController.portfolio_positions``) with a new,
    periodically-uploaded factor screen (``CompanyController.factor_screen_for_company``)
    -- see the Companies page for the upload widget.
    """
    st.caption(
        "Real portfolio positioning and the latest uploaded factor/crowding screen for "
        f"{company['display_name']} -- see the Companies page to upload a new screen."
    )

    st.subheader("Portfolio positioning")
    positions = controller.portfolio_positions(company["display_name"])
    if not positions:
        st.info("Not currently held in any covered portfolio.")
    else:
        rows = [
            {
                "Portfolio": position["portfolio"],
                "As of": position["as_of_date"],
                "Active weight %": position["active_weight_pct"],
                "Weight %": position["weight_pct"],
                "Risk contribution %": position["contribution_pct"],
            }
            for position in positions
        ]
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

    st.divider()
    st.subheader("Factor / crowding screen")
    screen_row = controller.factor_screen_for_company(company["display_name"])
    if screen_row is None:
        st.info("No factor screen data for this company yet -- upload one on the Companies page.")
        return

    st.caption(
        f"As of {screen_row['as_of_date']} -- matched to **{screen_row['name']}** ({screen_row.get('symbol') or 'n/a'})"
    )
    metric_cols = st.columns(len(_FACTOR_METRICS))
    for col, (field, label) in zip(metric_cols, _FACTOR_METRICS, strict=False):
        value = screen_row.get(field)
        col.metric(label, f"{value:.0f}" if value is not None else "n/a")

    if screen_row.get("crowding_factor") is not None:
        st.metric("Crowding Factor", f"{screen_row['crowding_factor']:.2f}")


# ── chat ─────────────────────────────────────────────────────────────────────


def _render_chat_tab(ticker: str, config: dict[str, Any]) -> None:
    chat_controller = ChatController()
    history_key = f"company_chat_{ticker}"
    if history_key not in st.session_state:
        st.session_state[history_key] = []

    for message in st.session_state[history_key]:
        with st.chat_message(message["role"]):
            st.markdown(_escape_currency(message["content"]))

    prompt = st.chat_input(f"Ask about {ticker}...", key=f"company_chat_input_{ticker}")
    if prompt:
        st.session_state[history_key].append({"role": "user", "content": prompt})
        with st.chat_message("user"):
            st.markdown(_escape_currency(prompt))

        with st.chat_message("assistant"):
            history = [{"role": m["role"], "content": m["content"]} for m in st.session_state[history_key][:-1]]
            with st.spinner("Searching the index..."):
                try:
                    document_types = (
                        [t for t in ALL_DOCUMENT_TYPES if t != RESEARCH_NOTE_DOC_TYPE] if hide_research_notes() else None
                    )
                    result, _chunks, _query = chat_controller.ask(
                        question=prompt,
                        config=config,
                        company=ticker,
                        history=history,
                        stream=False,
                        document_types=document_types,
                    )
                    answer_text = result if isinstance(result, str) else "".join(result)
                except Exception as error:
                    logger.exception("Company chat failed for %s: %s", ticker, error)
                    answer_text = f"Something went wrong: {error}"
            st.markdown(_escape_currency(answer_text))
        st.session_state[history_key].append({"role": "assistant", "content": answer_text})

    if st.session_state[history_key] and st.button("Clear chat", key=f"company_clear_chat_{ticker}"):
        st.session_state[history_key] = []
        st.rerun()


# ── upload ───────────────────────────────────────────────────────────────────


def _render_upload_tab(ticker: str, controller: CompanyController, config: dict[str, Any]) -> None:
    upload_controller = UploadController(config)
    st.caption(
        f"Add a research note or financial model for {ticker}. The company is detected "
        "automatically from the document's content during ingestion."
    )
    uploaded = st.file_uploader(
        "Choose a file",
        type=[ext.lstrip(".") for ext in upload_controller.allowed_extensions],
        accept_multiple_files=False,
        key=f"company_uploader_{ticker}",
    )
    if uploaded is None:
        return

    ext = "." + uploaded.name.rsplit(".", 1)[-1].lower() if "." in uploaded.name else ""
    if ext not in upload_controller.allowed_extensions:
        st.error(f"Unsupported file type {ext}. Allowed: {', '.join(upload_controller.allowed_extensions)}")
        return

    st.write(f"**{uploaded.name}**  ·  {uploaded.size / 1024:.1f} KB")

    if st.button("Upload and ingest", type="primary", key=f"company_upload_btn_{ticker}"):
        try:
            with st.spinner("Uploading to the Data Lake..."):
                dest_path = upload_controller.upload_document(uploaded.name, uploaded.getvalue())
        except Exception as error:
            logger.exception("Upload failed for %s: %s", uploaded.name, error)
            st.error(f"Upload failed: {error}")
            return

        try:
            with st.spinner("Ingesting into the search index..."):
                results = upload_controller.ingest_documents([dest_path])
            segments = results.get(dest_path, 0)
        except Exception as error:
            logger.exception("Ingestion failed for %s: %s", dest_path, error)
            st.error(f"The file was uploaded, but ingestion failed: {error}. It can be re-ingested later.")
            return

        if segments > 0:
            st.success(f"Ingested **{uploaded.name}** as {segments:,} searchable chunks.")
            controller.sync_companies_incremental([ticker], config)
            st.rerun()
        else:
            st.warning(f"**{uploaded.name}** was uploaded but produced no searchable content.")
