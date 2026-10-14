"""Index browser — lists all indexed documents with metadata and PDF access."""

from __future__ import annotations

import base64
from logging import Logger

import pandas as pd
import streamlit as st
from streamlit.logger import get_logger

from helpers import datalake_account, download_from_datalake, equities_config, hide_research_notes
from index.index_controller import IndexController
from search.search_model import RESEARCH_NOTE_DOC_TYPE

logger: Logger = get_logger(name=__name__)

cfg = equities_config()
controller = IndexController(cfg["index_name"])

# ── page ──────────────────────────────────────────────────────────────────────

col_title, col_sync = st.columns([6, 1])
col_title.title("Index Browser")

if col_sync.button("Sync from Azure", icon=":material/sync:", help="Re-fetch all document metadata from Azure AI Search"):
    with st.spinner("Syncing from Azure AI Search..."):
        try:
            count = controller.sync_from_azure()
            st.success(f"Synced {count} documents from Azure.")
        except Exception as err:
            st.error(f"Sync failed: {err}")
            logger.exception("Azure sync failed")
    st.rerun()

df_all = controller.load_documents()

if hide_research_notes():
    df_all = df_all[df_all["Type"] != RESEARCH_NOTE_DOC_TYPE.replace("_", " ")]

if df_all.empty:
    st.info(
        "The local document cache is empty. Click **Sync from Azure** above to "
        "populate it from the search index, or upload a document to add the first entry."
    )
    st.stop()

# ── filters ───────────────────────────────────────────────────────────────────

f1, f2, f3, f4 = st.columns(4)
with f1:
    types = ["All"] + sorted(df_all["Type"].replace("", pd.NA).dropna().unique().tolist())
    sel_type = st.selectbox("Document type", types)
with f2:
    sources = ["All"] + sorted(df_all["Source"].replace("", pd.NA).dropna().unique().tolist())
    sel_source = st.selectbox("Source", sources)
with f3:
    publishers = ["All"] + sorted(df_all["Publisher"].replace("", pd.NA).dropna().unique().tolist())
    sel_pub = st.selectbox("Publisher", publishers)
with f4:
    ticker = st.text_input("Company ticker", placeholder="e.g. MU")

df = df_all.copy()
if sel_type != "All":
    df = df[df["Type"] == sel_type]
if sel_source != "All":
    df = df[df["Source"] == sel_source]
if sel_pub != "All":
    df = df[df["Publisher"] == sel_pub]
if ticker:
    df = df[df["Companies"].str.contains(ticker.upper(), case=False, na=False)]

df = df.reset_index(drop=True)
st.caption(f"Showing **{len(df)}** of **{len(df_all)}** documents — select a row to view")

# ── table ─────────────────────────────────────────────────────────────────────

event = st.dataframe(
    df.drop(columns=["_path"]),
    use_container_width=True,
    hide_index=True,
    on_select="rerun",
    selection_mode="single-row",
    column_config={
        "Date": st.column_config.DateColumn("Date", format="YYYY-MM-DD"),
    },
)

# ── detail panel ──────────────────────────────────────────────────────────────

selected = event.selection.rows
if not selected:
    st.stop()

row = df.iloc[selected[0]]
path = row["_path"]
filename = row["File"]

st.divider()
st.subheader(filename)

m1, m2, m3, m4 = st.columns(4)
m1.metric("Date", row["Date"] or "—")
m2.metric("Publisher", row["Publisher"] or "—")
m3.metric("Type", row["Type"] or "—")
m4.metric("Companies", row["Companies"] or "—")

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
    )

    if is_pdf and view_col.toggle("View PDF inline"):
        b64 = base64.b64encode(file_bytes).decode()
        st.components.v1.html(
            f'<iframe src="data:application/pdf;base64,{b64}" '
            f'width="100%" height="900px" style="border:none;"></iframe>',
            height=920,
        )

except Exception as err:
    st.error(f"Could not load file from Data Lake: {err}")
    logger.exception("Failed to download %s", path)
