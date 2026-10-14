"""Chat page: ask questions answered from the indexed documents and models."""

from __future__ import annotations

import os
import re
from dataclasses import asdict
from datetime import datetime
from logging import Logger

import streamlit as st
from streamlit.logger import get_logger

from chat import session_store
from chat.chat_controller import ChatController
from helpers import datalake_account, equities_config, hide_research_notes
from search import search_model
from search.search_model import ALL_DOCUMENT_TYPES, RESEARCH_NOTE_DOC_TYPE

logger: Logger = get_logger(name=__name__)

config = equities_config()
controller = ChatController()

_SOURCE_TYPE_MAP = {
    "All sources": None,
    "Financial models only": "financial_model",
    "Documents only": "document",
}


# ── rendering helpers ────────────────────────────────────────────────────────


def _escape_currency(text: str) -> str:
    """Escape $ signs so Streamlit doesn't render them as LaTeX delimiters."""
    return re.sub(r"(?<!\\)\$", r"\\$", text)


def _md(text: str) -> None:
    st.markdown(_escape_currency(text))


def _collect_stream(gen, buf: list[str]):
    """Yield escaped tokens from gen while appending each to buf."""
    for chunk in gen:
        tok = _escape_currency(chunk)
        buf.append(tok)
        yield tok


def _render_answer(result, validate: bool, chat_deployment: str, refine_label: str) -> str:
    """Render ``result`` (a full string or a stream, depending on ``validate``) and return the final text."""
    if validate:
        assert isinstance(result, str)
        with st.spinner(refine_label):
            text = search_model.validate_answer(result, chat_deployment)
        _md(text)
    else:
        buf: list[str] = []
        st.write_stream(_collect_stream(result, buf))
        text = "".join(buf)
    return text


def _render_sources(chunks: list[dict]) -> None:
    """Collapsible expander showing which company, publisher, type, and date each source is from."""
    if not chunks:
        return
    with st.expander(f"Sources ({len(chunks)})", expanded=False):
        for i, c in enumerate(chunks, start=1):
            label = search_model.format_source_label(
                c.get("publisher"),
                c.get("document_type"),
                c.get("document_date"),
                companies=c.get("companies"),
                humanize_type=True,
            )
            st.markdown(f"**[{i}]** {label}")


# ── page header ───────────────────────────────────────────────────────────────

st.title("Equities Chat")
st.caption(
    "Ask about companies in the coverage universe. Answers are grounded in the "
    "documents and financial models indexed in Azure AI Search."
)
validate = st.toggle(
    "Validate & refine answers",
    value=False,
    help="Runs a second LLM pass to remove repetition, cut filler, and reduce broker name-dropping.",
)

# ── filters ───────────────────────────────────────────────────────────────────

with st.expander("Filters (optional)", expanded=False):
    col1, col2 = st.columns(2)
    with col1:
        company = (
            st.text_input(
                "Company ticker",
                value="",
                help="Scope to a single ticker or name, e.g. NVDA. Leave blank to search all.",
            )
            .strip()
            .upper()
        )
    with col2:
        source_label = st.selectbox("Source type", options=list(_SOURCE_TYPE_MAP))

    facets = search_model.get_facets(config["index_name"])
    col3, col4 = st.columns(2)
    with col3:
        selected_publishers: list[str] = st.multiselect(
            "Publisher",
            options=facets.get("publishers", []),
            help="Filter to specific brokers or data providers.",
        )
    with col4:
        doc_type_options = facets.get("document_types") or ALL_DOCUMENT_TYPES
        if hide_research_notes():
            doc_type_options = [t for t in doc_type_options if t != RESEARCH_NOTE_DOC_TYPE]
        selected_doc_types: list[str] = st.multiselect(
            "Document type",
            options=doc_type_options,
            help="Filter to specific document categories.",
        )

    col5, col6 = st.columns(2)
    with col5:
        date_from = st.date_input("Documents from", value=None, help="Only use documents dated on or after this date.")
    with col6:
        date_to = st.date_input("Documents to", value=None, help="Only use documents dated on or before this date.")

# research_note is force-excluded below regardless of what's selected above,
# so a stale multiselect selection from before the toggle was switched on
# can't leak it back in.
effective_doc_types: list[str] | None
if hide_research_notes():
    effective_doc_types = [t for t in (selected_doc_types or ALL_DOCUMENT_TYPES) if t != RESEARCH_NOTE_DOC_TYPE]
else:
    effective_doc_types = selected_doc_types or None

source_type = _SOURCE_TYPE_MAP[source_label]

# ── quick starters ────────────────────────────────────────────────────────────

quick_questions = controller.load_quick_questions()
if quick_questions:
    with st.expander("Quick starters", expanded=False):
        cols = st.columns(min(len(quick_questions), 4))
        for idx, qq in enumerate(quick_questions):
            if cols[idx % 4].button(qq["label"], key=f"qq_{idx}", use_container_width=True):
                if qq.get("requires_company") and not company:
                    st.warning(f"Set a company ticker in Filters before running **{qq['label']}**.")
                elif qq.get("mode") == "sequential_parts":
                    st.session_state["pending_parts"] = {
                        "label": qq["label"],
                        "company": company,
                        "parts": controller.resolve_parts(qq, company),
                    }
                    st.rerun()
                else:
                    pt = qq["prompt"]
                    if company:
                        pt = pt.replace("{company}", company)
                    st.session_state["pending_prompt"] = pt.strip()
                    st.rerun()

# ── conversation state & sidebar ─────────────────────────────────────────────

_PAGE_KEY = "equities_chat"
_SESSION_ACCOUNT = datalake_account()
_SESSION_FILESYSTEM = config.get("data_layer", "silver")


def _timestamp_title(created_at_iso: str | None) -> str:
    """Derive a session title from its creation timestamp."""
    if not created_at_iso:
        return "New chat"
    try:
        return datetime.fromisoformat(created_at_iso).strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return created_at_iso


def _start_new_session() -> None:
    """Reset the chat to an empty, freshly-identified session.

    Point the history dropdown at the new session so it shows the fresh chat.
    Must run before the dropdown widget is instantiated on the current run.
    """
    new_id = os.urandom(8).hex()
    st.session_state["messages"] = []
    st.session_state["current_session_id"] = new_id
    st.session_state["session_created_at"] = None
    st.session_state[f"histsel_{_PAGE_KEY}"] = new_id


def _load_session(session_id: str) -> None:
    """Load a stored session's messages into the chat."""
    data = session_store.load_session(_SESSION_ACCOUNT, _SESSION_FILESYSTEM, _PAGE_KEY, session_id)
    st.session_state["messages"] = data.get("messages", [])
    st.session_state["current_session_id"] = session_id
    st.session_state["session_created_at"] = data.get("created_at")


def _autosave_session() -> None:
    """Persist the current conversation to the Data Lake (best-effort)."""
    if not _SESSION_ACCOUNT:
        return
    messages = st.session_state.get("messages", [])
    if not messages:
        return
    if st.session_state.get("session_created_at") is None:
        st.session_state["session_created_at"] = session_store.now_iso()
    try:
        session_store.save_session(
            account_name=_SESSION_ACCOUNT,
            filesystem_name=_SESSION_FILESYSTEM,
            page_key=_PAGE_KEY,
            session_id=st.session_state["current_session_id"],
            title=_timestamp_title(st.session_state["session_created_at"]),
            messages=messages,
            created_at=st.session_state["session_created_at"],
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to save session: %s", exc)


def _render_session_sidebar() -> None:
    """Sidebar UI for creating, loading, and deleting Data Lake chat sessions."""
    with st.sidebar:
        st.divider()
        head = st.columns([4, 1], vertical_alignment="center")
        head[0].subheader("Conversations")
        # A new-chat request from a previous run must be applied here, before the
        # history dropdown widget is created (its value can't change once built).
        if st.session_state.pop("_start_new_chat", False):
            _start_new_session()
        if head[1].button("➕", key=f"newchat_{_PAGE_KEY}", help="New chat"):
            st.session_state["_start_new_chat"] = True
            st.rerun()

        if not _SESSION_ACCOUNT:
            st.caption("Session storage unavailable.")
            return

        try:
            sessions = session_store.list_sessions(_SESSION_ACCOUNT, _SESSION_FILESYSTEM, _PAGE_KEY)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not list sessions: %s", exc)
            st.caption("Couldn't load earlier conversations.")
            return

        if not sessions:
            st.caption("No saved conversations yet.")
            return

        current = st.session_state.get("current_session_id")
        stored_titles = {sess["session_id"]: (sess.get("title") or "(untitled)") for sess in sessions}
        # Keep the active chat selectable even before it has been saved.
        id_to_title = stored_titles if current in stored_titles else {current: "New chat", **stored_titles}
        options = list(id_to_title)

        histsel_key = f"histsel_{_PAGE_KEY}"
        # Default to the active chat, and recover if a stale id lingers here.
        if st.session_state.get(histsel_key) not in options:
            st.session_state[histsel_key] = current

        selected = st.selectbox(
            "History",
            options=options,
            format_func=lambda sid: id_to_title.get(sid, sid),
            key=histsel_key,
            label_visibility="collapsed",
        )
        if selected != current:
            _load_session(selected)
            st.rerun()

        if selected in stored_titles and st.button(
            "🗑 Delete this conversation", use_container_width=True, key=f"del_{_PAGE_KEY}"
        ):
            session_store.delete_session(_SESSION_ACCOUNT, _SESSION_FILESYSTEM, _PAGE_KEY, selected)
            st.session_state["_start_new_chat"] = True
            st.rerun()


if "messages" not in st.session_state:
    st.session_state["messages"] = []
if "current_session_id" not in st.session_state:
    st.session_state["current_session_id"] = os.urandom(8).hex()
if "session_created_at" not in st.session_state:
    st.session_state["session_created_at"] = None

_render_session_sidebar()

# ── replay history ────────────────────────────────────────────────────────────

for msg in st.session_state["messages"]:
    with st.chat_message(msg["role"]):
        _md(msg["content"])
        if msg["role"] == "assistant":
            _render_sources(msg.get("chunks") or [])
            if msg.get("condensed_query"):
                st.caption(f"Search query: `{msg['condensed_query']}`")

# ── sequential multi-part execution ──────────────────────────────────────────

pending_parts = st.session_state.pop("pending_parts", None)
if pending_parts:
    parts = pending_parts["parts"]
    total = len(parts)
    user_text = pending_parts["label"]
    if pending_parts["company"]:
        user_text += f" — {pending_parts['company']}"

    st.session_state["messages"].append({"role": "user", "content": user_text})
    with st.chat_message("user"):
        _md(user_text)

    all_text: list[str] = []
    all_chunks: list[dict] = []

    with st.chat_message("assistant"):
        for i, part in enumerate(parts, start=1):
            st.markdown(f"#### Lens {i} of {total}: {part['label']}")
            with st.spinner(f"Retrieving for lens {i} of {total}..."):
                try:
                    result, chunks, _ = controller.ask(
                        question=part["prompt"],
                        config=config,
                        company=company or None,
                        source_type=source_type,
                        publishers=selected_publishers or None,
                        document_types=effective_doc_types,
                        date_from=date_from or None,
                        date_to=date_to or None,
                        history=[],
                        stream=not validate,
                        retrieval_query=part.get("retrieval_query"),
                    )
                except Exception as exc:
                    logger.exception("Lens %d failed: %s", i, exc)
                    result, chunks = f"Error: {exc}", []

            lens_text = _render_answer(result, validate, config["chat_deployment"], "Refining...")

            all_text.append(f"#### Lens {i} of {total}: {part['label']}\n\n{lens_text}")
            all_chunks.extend([asdict(c) for c in chunks])
            if i < total:
                st.divider()

    st.session_state["messages"].append(
        {
            "role": "assistant",
            "content": "\n\n---\n\n".join(all_text),
            "chunks": all_chunks,
            "condensed_query": None,
        }
    )
    _autosave_session()

# ── single-turn input & response ─────────────────────────────────────────────

pending = st.session_state.pop("pending_prompt", None)
prompt = st.chat_input("Ask about a company's revenue, margins, guidance, estimates...") or pending

if prompt:
    st.session_state["messages"].append({"role": "user", "content": prompt})
    with st.chat_message("user"):
        _md(prompt)

    with st.chat_message("assistant"):
        history = [{"role": m["role"], "content": m["content"]} for m in st.session_state["messages"][:-1]]
        with st.spinner("Searching the index..."):
            try:
                result, raw_chunks, condensed_query = controller.ask(
                    question=prompt,
                    config=config,
                    company=company or None,
                    source_type=source_type,
                    publishers=selected_publishers or None,
                    document_types=effective_doc_types,
                    date_from=date_from or None,
                    date_to=date_to or None,
                    history=history,
                    stream=not validate,
                )
                chunks = [asdict(c) for c in raw_chunks]
            except Exception as error:
                logger.exception("Chat query failed: %s", error)
                result, chunks, condensed_query = f"Something went wrong: {error}", [], prompt

        answer_text = _render_answer(result, validate, config["chat_deployment"], "Refining answer...")

        _render_sources(chunks)
        if condensed_query and condensed_query != prompt:
            st.caption(f"Search query: `{condensed_query}`")

    st.session_state["messages"].append(
        {
            "role": "assistant",
            "content": answer_text,
            "chunks": chunks,
            "condensed_query": condensed_query if condensed_query != prompt else None,
        }
    )
    _autosave_session()
