"""Companies page: browse every covered company, or view one company's detail screen."""

from __future__ import annotations

from logging import Logger

import streamlit as st
from streamlit.logger import get_logger

from companies import company_detail_view
from companies.company_controller import CompanyController
from helpers import equities_config
from index.index_controller import IndexController

logger: Logger = get_logger(name=__name__)

config = equities_config()
controller = CompanyController(config["index_name"])

# ── detail screen (query-param driven, no separate nav entry) ──────────────────

selected_ticker = st.query_params.get("ticker")
if selected_ticker:
    company_detail_view.render(selected_ticker, controller, config)
    st.stop()

# ── browser ──────────────────────────────────────────────────────────────────

col_title, col_sync = st.columns([6, 1])
col_title.title("Companies")
st.caption(
    "Every company with indexed research or a financial model, deduplicated and "
    "grouped by region and sector. Loads instantly from the local cache -- click "
    "Sync to pull in anything ingested elsewhere."
)

if col_sync.button("Sync", icon=":material/sync:", help="Refresh documents from Azure and classify new companies"):
    with st.spinner("Syncing documents and classifying companies..."):
        try:
            doc_count = IndexController(config["index_name"]).sync_from_azure()
            newly_classified = controller.sync_companies(config)
            st.success(f"Synced {doc_count} documents. Classified {newly_classified} new company(s).")
        except Exception as err:
            st.error(f"Sync failed: {err}")
            logger.exception("Company sync failed")
    st.rerun()

with st.expander("Upload factor/crowding screen", icon=":material/upload_file:"):
    st.caption(
        'Upload a periodic Bloomberg factor/crowding screen export (e.g. "Momentum '
        "Screen_2026_Aug_24.xlsx\") -- each company's factor percentiles and crowding "
        "signal then show up on its own Positioning tab, alongside its real portfolio "
        "positions."
    )
    screen_file = st.file_uploader("Choose a file", type=["xlsx"], key="factor_screen_uploader")
    if screen_file is not None and st.button("Upload screen", icon=":material/upload:"):
        try:
            with st.spinner("Parsing and storing the screen..."):
                as_of_date, row_count = controller.upload_factor_screen(screen_file.name, screen_file.getvalue())
            st.success(f"Uploaded {row_count:,} companies for {as_of_date}.")
        except ValueError as err:
            st.error(str(err))
        except Exception as err:
            st.error(f"Upload failed: {err}")
            logger.exception("Factor screen upload failed")

df_all = controller.load_companies()

if df_all.empty:
    st.info(
        "No companies yet. Click **Sync** above, or upload a document on the "
        "Upload Document page, to populate the Companies page."
    )
    st.stop()

# ── filters ───────────────────────────────────────────────────────────────────

f1, f2, f3 = st.columns([2, 1, 1])
with f1:
    query = st.text_input("Search", placeholder="Ticker or company name").strip().lower()
with f2:
    regions = ["All regions"] + sorted(df_all["Region"].unique().tolist())
    selected_region = st.selectbox("Region", regions)
with f3:
    sectors = ["All sectors"] + sorted(df_all["Sector"].unique().tolist())
    selected_sector = st.selectbox("Sector", sectors)

df = df_all.copy()
if query:
    df = df[
        df["Ticker"].str.lower().str.contains(query, na=False) | df["Company"].str.lower().str.contains(query, na=False)
    ]
if selected_region != "All regions":
    df = df[df["Region"] == selected_region]
if selected_sector != "All sectors":
    df = df[df["Sector"] == selected_sector]

df = df.reset_index(drop=True)
st.caption(f"Showing **{len(df)}** of **{len(df_all)}** companies -- select a row to view")

# ── table ─────────────────────────────────────────────────────────────────────

event = st.dataframe(
    df,
    use_container_width=True,
    hide_index=True,
    on_select="rerun",
    selection_mode="single-row",
    column_config={
        "Latest Date": st.column_config.DateColumn("Latest Date", format="YYYY-MM-DD"),
        "Documents": st.column_config.NumberColumn("Documents"),
    },
)

selected_rows = event.selection.rows
if selected_rows:
    st.query_params["ticker"] = df.iloc[selected_rows[0]]["Ticker"]
    st.rerun()
