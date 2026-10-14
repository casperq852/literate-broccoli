"""
Shared, topic-agnostic helpers for the equities RAG frontend.

Small cross-cutting utilities used across pages: Data Lake manager construction,
config/session accessors, document upload/download, custom CSS, and in-memory log
capture so ``avb_core``/``avb_search``/``vectorization`` records can be surfaced in
the UI. Heavier, topic-specific logic (e.g. search-index ingestion) lives in its
own topic package rather than here.

Everything here talks only to Azure services (Data Lake), the only outbound access
permitted in the a.s.r. environment.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import logging
import os
import sys
from collections import deque
from pathlib import Path
from typing import Any

import streamlit as st
from streamlit.logger import get_logger

from avb_core.storage.aio import AsyncDataLakeManager
from avb_core.storage.datalakemanager import DataLakeManager

logger = get_logger(__name__)

_REPO_ROOT: Path = Path(__file__).resolve().parents[1]  # helpers.py -> equities_frontend -> repo root
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

CLIENT_ID: str | None = os.getenv("DEFAULT_IDENTITY_CLIENT_ID")

# Single source of truth for the rotating file log's location, so
# version_selector.py (which writes it), the Logs page (which reads it), and
# the scheduler (which backs it up to the Data Lake) can never drift onto
# different paths.
LOG_FILE_PATH: Path = Path(__file__).resolve().parent / "logging" / "equities_frontend.log"


# ── log capture (surfaces avb_core / avb_search / vectorization logs in the UI) ──

_LOG_CAPTURE_MAXLEN: int = 2000
_captured_log_records: deque[str] = deque(maxlen=_LOG_CAPTURE_MAXLEN)
_LOG_CAPTURE_INSTALLED_FLAG: str = "_equities_frontend_log_capture_installed"


class _DequeLogHandler(logging.Handler):
    """A logging handler that buffers formatted log records in memory."""

    def emit(self, record: logging.LogRecord) -> None:
        """Append the formatted record to the in-memory buffer, never raising."""
        with contextlib.suppress(Exception):
            _captured_log_records.append(self.format(record))


def install_log_capture(level: int = logging.INFO) -> None:
    """
    Attach an in-memory capture handler to the root logger (idempotent).

    Records emitted by ``avb_core``/``avb_search``/``avb_llm`` and the
    ``vectorization`` pipeline propagate to the root logger, so a single handler
    here captures ingestion and retrieval activity that can be surfaced in the UI.

    Args:
        level (int): Minimum level of records to buffer.
    """
    root: logging.Logger = logging.getLogger()
    if getattr(root, _LOG_CAPTURE_INSTALLED_FLAG, False):
        return
    handler: _DequeLogHandler = _DequeLogHandler()
    handler.setLevel(level)
    handler.setFormatter(
        logging.Formatter(fmt="%(asctime)s | %(levelname)s | %(name)s | %(message)s", datefmt="%H:%M:%S")
    )
    root.addHandler(hdlr=handler)
    setattr(root, _LOG_CAPTURE_INSTALLED_FLAG, True)


def get_captured_logs() -> list[str]:
    """Return the buffered log lines, oldest first."""
    return list(_captured_log_records)


def clear_captured_logs() -> None:
    """Empty the in-memory log buffer."""
    _captured_log_records.clear()


# ── styling (from original framework) ───────────────────────────────────────────


def load_css() -> None:
    """Inject custom CSS to widen the multiselect widget."""
    st.markdown(
        body="""
        <style>
            .stMultiSelect [data-baseweb=select] span{
                max-width: 1000px;
                font-size: 0.9rem;
            }
        </style>
        """,
        unsafe_allow_html=True,
    )


# ── Data Lake managers (from original framework) ────────────────────────────────


@st.cache_resource
def get_datalake_manager(account_name: str) -> DataLakeManager:
    """Return a cached synchronous DataLakeManager for the given account."""
    return DataLakeManager(account_name=account_name, client_id=CLIENT_ID)


async def get_async_datalake_manager(account_name: str) -> AsyncDataLakeManager:
    """Return an initialised asynchronous DataLakeManager for the given account."""
    manager = AsyncDataLakeManager(account_name=account_name, client_id=CLIENT_ID)
    await manager.async_init()
    return manager


# ── config access ───────────────────────────────────────────────────────────────


def equities_config() -> dict[str, Any]:
    """Return the 'equities' block from the loaded app config."""
    return st.session_state["app_config"]["equities"]


def datalake_account() -> str:
    """Return the Data Lake account name for the selected deployment."""
    return st.session_state["app_config"]["deployment"]["datalake_name"]


# ── demo mode ──────────────────────────────────────────────────────────────────
# A presenter-facing toggle (Logs page) that hides research_note documents from
# the Index Browser and excludes them from Chat's and Investment Case's
# retrieval, for demos where showing that broker research is included in
# storage isn't wanted. Scoped to st.session_state, so it applies only to the
# toggling browser session for as long as it's open -- no new persistence
# layer, and it never affects other users or survives a container restart.
# See search.search_model.RESEARCH_NOTE_DOC_TYPE/ALL_DOCUMENT_TYPES for the
# plain (Streamlit-free) document-type constants Controllers filter with.

_HIDE_RESEARCH_NOTES_KEY = "hide_research_notes"


def hide_research_notes() -> bool:
    """True if the Logs page's "Hide research notes" demo-mode toggle is on for this session."""
    return bool(st.session_state.get(_HIDE_RESEARCH_NOTES_KEY, False))


# ── uploads ──────────────────────────────────────────────────────────────────────


def upload_to_datalake(account_name: str, file_path: str, data: bytes, *, _filesystem_name: str | None = None) -> None:
    """
    Write a file's bytes to the Data Lake at ``file_path``.

    Overwrites any existing file at that path, so re-uploading the same document
    refreshes it rather than erroring. ``upload_file`` expects a binary stream,
    so the raw bytes are wrapped in a BytesIO.

    Args:
        _filesystem_name: Explicit Data Lake filesystem name, bypassing
            ``equities_config()``/``st.session_state``. Only needed by callers
            with no live Streamlit session (e.g. the scheduler background
            thread in ``scheduler/``).
    """
    manager = get_datalake_manager(account_name=account_name)
    filesystem_name = _filesystem_name or equities_config()["data_layer"]
    manager.upload_file(
        filesystem_name=filesystem_name,
        file_path=file_path,
        file_content=io.BytesIO(data),
        overwrite=True,
    )


# ── downloads ────────────────────────────────────────────────────────────────────


def download_from_datalake(account_name: str, file_path: str, *, _filesystem_name: str | None = None) -> bytes:
    """Download a file from the Data Lake by its full path. See ``upload_to_datalake`` for ``_filesystem_name``."""
    return asyncio.run(_download_async(account_name, file_path, _filesystem_name))


async def _download_async(account_name: str, file_path: str, _filesystem_name: str | None = None) -> bytes:
    filesystem_name = _filesystem_name or equities_config()["data_layer"]
    manager = AsyncDataLakeManager(account_name=account_name, client_id=CLIENT_ID)
    await manager.async_init()
    try:
        return await manager.download_file(filesystem_name=filesystem_name, file_path=file_path)
    finally:
        await manager.close()
