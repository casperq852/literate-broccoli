"""Logs page — view recent application logs without needing Azure Portal access."""

from __future__ import annotations

import os

import streamlit as st
from logs import logs_model as model
from logs.logs_controller import LogsController

import helpers

controller = LogsController()

# Not real access control -- this is a lightweight "type before you click"
# friction gate for a destructive, expensive action, not a secret. Anyone
# with repo access can read this constant.
_DANGER_ZONE_PASSWORD = "1234"  # noqa: S105 - deliberate friction gate, not a real credential

st.title("Logs")
st.caption(
    "Recent log output from this running process. Restarting the container resets both "
    "sources below. Container start-up crashes (before the app ever serves a page) can't "
    "appear here at all — see the note at the bottom for those."
)

st.subheader("Demo mode")
st.toggle(
    "Hide research notes",
    value=helpers.hide_research_notes(),
    key="hide_research_notes",
    help="Excludes research_note documents from the Index Browser and from Chat's retrieval, "
    "for this browser session only. Other document types (earnings transcripts, annual reports, "
    "investor presentations, financial models) are unaffected.",
)

st.subheader("Manual actions")
st.caption(
    "The Portfolio Dashboard's data sync already runs once on every container start, and "
    "otherwise once a day around 07:30 Amsterdam time. Use this to run it again right now "
    "instead of waiting — e.g. to pick up a fix without restarting the container."
)
if st.button("Run portfolio sync now", icon=":material/sync:"):
    with st.spinner(
        "Syncing portfolio data — this repeats the Data Lake read the daily "
        "schedule normally does, so it can take a while..."
    ):
        success, message = controller.run_portfolio_sync_now()
    if success:
        st.success(message)
    else:
        st.error(message)

st.caption(
    "The scheduler also merges duplicate companies (e.g. the same company under two Bloomberg "
    "exchange tickers) once an hour. Use this to run it now — for the local cache only; it "
    "doesn't touch already-indexed documents in Azure AI Search, which still need re-ingesting "
    "to pick up the merge permanently."
)
if st.button("Merge duplicate companies now", icon=":material/join:"):
    with st.spinner("Looking for duplicate companies in the local cache..."):
        success, message = controller.run_company_dedup_now()
    if success:
        st.success(message)
    else:
        st.error(message)

st.caption(
    "A daily LLM pass goes further than the rapidfuzz merge above: it refreshes an authoritative "
    "list of real company names from Portfolio Dashboard holdings, reviews the company list for "
    "duplicates only an LLM would catch, and backfills documents with no company tagged at all. "
    "A proposed merge only applies automatically when every company in it is independently "
    "confirmed against that authoritative list — anything else lands below for you to approve."
)
if st.button("Run company cleanup now", icon=":material/auto_fix_high:"):
    with st.spinner("Refreshing company reference, reviewing for duplicates, backfilling untagged documents..."):
        success, message = controller.run_company_cleanup_now()
    if success:
        st.success(message)
    else:
        st.error(message)

pending_suggestions = controller.list_pending_merge_suggestions()
if pending_suggestions:
    st.markdown(f"**{len(pending_suggestions)} company merge(s) awaiting review**")
    for suggestion in pending_suggestions:
        with st.container(border=True):
            st.write(
                f"**{', '.join(suggestion['tickers'])}** → {suggestion.get('canonical_name') or '(no name proposed)'}"
            )
            if suggestion.get("reason"):
                st.caption(suggestion["reason"])
            col_approve, col_reject = st.columns(2)
            if col_approve.button("Approve merge", key=f"approve_{suggestion['id']}", icon=":material/check:"):
                success, message = controller.approve_merge_suggestion(suggestion["id"], suggestion["tickers"])
                if success:
                    st.success(message)
                else:
                    st.error(message)
                st.rerun()
            if col_reject.button("Reject", key=f"reject_{suggestion['id']}", icon=":material/close:"):
                controller.reject_merge_suggestion(suggestion["id"])
                st.rerun()

with st.expander("⚠️ Danger zone", expanded=False):
    st.caption(
        "Re-ingests **every currently-indexed document** through today's ingestion "
        "methodology (extraction, LLM enrichment, and company resolution) — fixes documents "
        "that were indexed before company resolution existed, so their Companies/tickers are "
        "currently blank or wrong. This re-runs the full pipeline, including LLM calls, for "
        "every document in the index: it can take a long time and cost real LLM tokens for a "
        'large index, and there is no undo. Run "Merge duplicate companies now" above '
        "afterwards to fold together anything this newly reveals is the same company under a "
        "different spelling than what was already cached."
    )
    danger_password = st.text_input("Password to unlock this action", type="password", key="danger_zone_password")
    unlocked = danger_password == _DANGER_ZONE_PASSWORD
    if danger_password and not unlocked:
        st.error("Incorrect password.")

    confirm_reindex = st.checkbox(
        "I understand this re-processes every indexed document and cannot be undone.",
        disabled=not unlocked,
    )
    if st.button(
        "Re-ingest ALL indexed documents",
        icon=":material/refresh:",
        disabled=not (unlocked and confirm_reindex),
    ):
        progress = st.progress(0.0, text="Syncing document list from Azure...")

        def _on_reindex_progress(path: str, completed: int, total: int) -> None:
            progress.progress(
                completed / total,
                text=f"Re-ingesting... {completed}/{total} ({os.path.basename(path)} done)",
            )

        try:
            results = controller.reindex_all_documents(on_progress=_on_reindex_progress)
        except Exception as exc:  # noqa: BLE001
            st.error(f"Re-ingestion failed: {exc}")
        else:
            progress.progress(1.0, text="Complete")
            if not results:
                st.info("No indexed documents found.")
            else:
                succeeded = sum(1 for segments in results.values() if segments > 0)
                st.success(f"Re-ingested {succeeded}/{len(results)} document(s).")
                empty = [path for path, segments in results.items() if segments == 0]
                st.session_state["reindex_empty_paths"] = empty
                if empty:
                    with st.expander(f"{len(empty)} document(s) yielded no content"):
                        for path in empty:
                            st.write(f"- {os.path.basename(path)}")

    empty_paths: list[str] = st.session_state.get("reindex_empty_paths") or []
    if empty_paths:
        st.caption(
            f"{len(empty_paths)} document(s) from the last re-ingest run yielded no content — some may be "
            "a deleted live blob with an older version still sitting in a sibling `archive/` folder "
            "(see `upload.archive_restore_model`). This checks each one and, where an archived version "
            "exists, uploads and re-ingests it fresh at today's canonical location. Requires the same "
            "password above."
        )
        if st.button(
            f"Restore {len(empty_paths)} missing document(s) from archive",
            icon=":material/restore:",
            disabled=not unlocked,
        ):
            progress = st.progress(0.0, text="Checking for a deleted live blob...")

            def _on_restore_progress(path: str, completed: int, total: int) -> None:
                progress.progress(
                    completed / total,
                    text=f"Checking archive... {completed}/{total} ({os.path.basename(path)} done)",
                )

            try:
                results = controller.restore_missing_from_archive(empty_paths, on_progress=_on_restore_progress)
            except Exception as exc:  # noqa: BLE001
                st.error(f"Archive restore failed: {exc}")
            else:
                progress.progress(1.0, text="Complete")
                if not results:
                    st.warning("None of these documents had a recoverable archived version.")
                else:
                    succeeded = sum(1 for segments in results.values() if segments > 0)
                    st.success(f"Restored and re-ingested {succeeded}/{len(results)} document(s) from archive.")
                    unrecovered = [path for path in empty_paths if path not in results]
                    st.session_state["reindex_empty_paths"] = [path for path in empty_paths if results.get(path, 0) == 0]
                    if unrecovered:
                        with st.expander(f"{len(unrecovered)} document(s) had no archived version available"):
                            for path in unrecovered:
                                st.write(f"- {os.path.basename(path)}")

        st.caption(
            "For whatever's left with no archived version: remove those rows from this app's own local "
            "cache so they stop cluttering the Index Browser/Companies page. This never touches Azure AI "
            "Search — existing content stays fully retrievable in Chat exactly as before, only the local "
            "list entry disappears. Not permanent: syncing the Index Browser from Azure again brings them "
            "straight back, since Azure AI Search itself still has the stale entry (deleting it there isn't "
            "implemented yet). Re-upload the real file through the normal Upload page whenever you track "
            "down a replacement."
        )
        if st.button(
            f"Remove {len(empty_paths)} ghost document(s) from local cache",
            icon=":material/delete_sweep:",
            disabled=not unlocked,
        ):
            progress = st.progress(0.0, text="Checking for a deleted live blob...")

            def _on_ghost_progress(path: str, completed: int, total: int) -> None:
                progress.progress(
                    completed / total,
                    text=f"Checking... {completed}/{total} ({os.path.basename(path)} done)",
                )

            try:
                ghosts = controller.remove_ghost_documents(empty_paths, on_progress=_on_ghost_progress)
            except Exception as exc:  # noqa: BLE001
                st.error(f"Ghost cleanup failed: {exc}")
            else:
                progress.progress(1.0, text="Complete")
                if not ghosts:
                    st.info("None of these were confirmed permanently gone (some may still have an archived version).")
                else:
                    st.success(f"Removed {len(ghosts)} ghost document(s) from the local cache.")
                    st.session_state["reindex_empty_paths"] = [path for path in empty_paths if path not in ghosts]
                    with st.expander(f"{len(ghosts)} document(s) removed — re-upload these manually when you can"):
                        for path in ghosts:
                            st.write(f"- {os.path.basename(path)}")

st.divider()

col_source, col_level, col_query, col_lines = st.columns([1.3, 1, 2, 1])
source = col_source.radio("Source", ["In-memory (live)", "Log file (on disk)"])
level = col_level.selectbox("Level", ["ALL", "DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"])
query = col_query.text_input("Filter (substring)", placeholder="e.g. scheduler, DefaultAzureCredential")
max_lines = col_lines.number_input("Max lines shown", min_value=50, max_value=5000, value=500, step=50)

all_lines = controller.read_captured_lines() if source == "In-memory (live)" else controller.read_file_lines()
matching = model.filter_lines(all_lines, level, query)
shown = model.tail(matching, int(max_lines))

caption = f"Showing {len(shown)} of {len(matching)} matching line(s), {len(all_lines)} total"
if source == "Log file (on disk)":
    caption += f" — `{controller.log_file_path}`"
st.caption(caption)

if not shown:
    st.info("No matching log lines.")
else:
    st.code("\n".join(shown), language="log")

st.divider()

if controller.log_file_exists:
    st.download_button(
        "Download full log file",
        data=controller.file_bytes(),
        file_name="equities_frontend.log",
        mime="text/plain",
    )
else:
    st.caption("No on-disk log file yet.")

with st.expander("Can't find what you're looking for?"):
    st.markdown(
        "- Both sources here only cover the **currently running process** — a restart "
        "clears them.\n"
        "- **Container start-up crashes** (the app never gets far enough to serve a page) "
        "can't be captured here at all. Use Azure's own container log stream instead:\n"
        "  - Portal: your App Service → **Log stream** (under Monitoring), or "
        "**Diagnose and solve problems**.\n"
        "  - CLI: `az webapp log tail --name <app-name> --resource-group <resource-group>`\n"
        "- A copy of the on-disk log file is also uploaded to the Data Lake once an hour "
        "by the background scheduler, alongside the cache backup — useful if you don't "
        "have portal or CLI access."
    )
