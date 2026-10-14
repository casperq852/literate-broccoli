"""Persist and retrieve chat sessions on the Azure Data Lake.

Sessions are stored under ``LLM/sessions/<tab>/<session_id>.json`` in the
configured filesystem, where ``<tab>`` is the page key with underscores
replaced by hyphens (e.g. ``equities_chat`` -> ``equities-chat``). This mirrors
the session storage used by the avb_llm_frontend app.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
from datetime import datetime, timezone
from typing import Any

import streamlit as st

import helpers

logger = logging.getLogger(__name__)

SESSIONS_ROOT = "LLM/sessions"

# Keep at most this many conversations per tab; older ones are pruned on save.
MAX_SESSIONS = 10


def now_iso() -> str:
    """Return the current UTC time as an ISO-8601 string."""
    return datetime.now(timezone.utc).isoformat()


def tab_from_page_key(page_key: str) -> str:
    """Map a page key to its session folder name (underscores -> hyphens)."""
    return page_key.replace("_", "-")


def _session_dir(page_key: str) -> str:
    return f"{SESSIONS_ROOT}/{tab_from_page_key(page_key)}"


def _session_path(page_key: str, session_id: str) -> str:
    return f"{_session_dir(page_key)}/{session_id}.json"


def save_session(
    *,
    account_name: str,
    filesystem_name: str,
    page_key: str,
    session_id: str,
    title: str,
    messages: list[dict[str, Any]],
    created_at: str | None = None,
) -> None:
    """Write (or overwrite) a session to the data lake and refresh the listing."""
    payload = {
        "session_id": session_id,
        "page_key": page_key,
        "tab": tab_from_page_key(page_key),
        "title": title,
        "created_at": created_at or now_iso(),
        "updated_at": now_iso(),
        "message_count": len(messages),
        "messages": messages,
    }
    data = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")

    async def _save() -> None:
        manager = await helpers.get_async_datalake_manager(account_name=account_name)
        try:
            await manager.upload_file(
                filesystem_name=filesystem_name,
                file_path=_session_path(page_key, session_id),
                file_content=io.BytesIO(data),
                archive=False,
                overwrite=True,
            )
            await _prune_old(manager, filesystem_name, page_key, keep=MAX_SESSIONS)
        finally:
            await manager.close()

    asyncio.run(_save())
    list_sessions.clear()


async def _prune_old(manager: Any, filesystem_name: str, page_key: str, *, keep: int) -> None:
    """Delete the oldest sessions of a tab so at most ``keep`` remain."""
    directory = _session_dir(page_key)
    entries: list[tuple[str, str]] = []
    try:
        async for path_props in manager.list_paths(filesystem_name=filesystem_name, path=directory, recursive=False):
            if getattr(path_props, "is_directory", False):
                continue
            name = path_props.name
            if not name.endswith(".json"):
                continue
            modified = getattr(path_props, "last_modified", None)
            entries.append((str(modified) if modified is not None else "", name))
    except Exception as exc:  # noqa: BLE001
        logger.info("Could not list sessions for pruning in %s: %s", directory, exc)
        return
    if len(entries) <= keep:
        return
    # Newest first, then delete everything beyond the keep limit.
    entries.sort(key=lambda e: e[0], reverse=True)
    for _modified, name in entries[keep:]:
        try:
            await manager.delete_file(filesystem_name, name)
        except Exception as exc:  # noqa: BLE001
            logger.info("Could not prune old session %s: %s", name, exc)


@st.cache_data(ttl=30, show_spinner=False)
def list_sessions(account_name: str, filesystem_name: str, page_key: str) -> list[dict[str, Any]]:
    """Return metadata for stored sessions of a tab, newest first."""
    directory = _session_dir(page_key)

    async def _read_meta(manager: Any, full_path: str) -> dict[str, Any] | None:
        name = full_path.rsplit("/", 1)[-1]
        try:
            raw = await manager.download_file(filesystem_name=filesystem_name, file_path=full_path)
            meta = json.loads(raw)
        except Exception as exc:  # noqa: BLE001
            logger.info("Skipping unreadable session %s: %s", name, exc)
            return None
        return {
            "session_id": meta.get("session_id") or name[:-5],
            "title": meta.get("title") or "(untitled)",
            "created_at": meta.get("created_at"),
            "updated_at": meta.get("updated_at"),
            "message_count": meta.get("message_count") or len(meta.get("messages", [])),
        }

    async def _list() -> list[dict[str, Any]]:
        manager = await helpers.get_async_datalake_manager(account_name=account_name)
        try:
            paths: list[str] = []
            async for path_props in manager.list_paths(filesystem_name=filesystem_name, path=directory, recursive=False):
                if getattr(path_props, "is_directory", False):
                    continue
                if not path_props.name.endswith(".json"):
                    continue
                paths.append(path_props.name)
            # Fetch session metadata concurrently; sequential downloads dominate
            # the first-load latency of a page's chat sidebar.
            metas = await asyncio.gather(*(_read_meta(manager, p) for p in paths))
        except Exception as exc:  # noqa: BLE001
            # Folder may not exist yet, or listing is not permitted.
            logger.info("No sessions listed for %s: %s", directory, exc)
            return []
        finally:
            await manager.close()
        out = [meta for meta in metas if meta is not None]
        out.sort(key=lambda s: s.get("updated_at") or "", reverse=True)
        return out

    return asyncio.run(_list())


def load_session(account_name: str, filesystem_name: str, page_key: str, session_id: str) -> dict[str, Any]:
    """Load a full session (including messages) from the data lake."""

    async def _load() -> dict[str, Any]:
        manager = await helpers.get_async_datalake_manager(account_name=account_name)
        try:
            raw = await manager.download_file(
                filesystem_name=filesystem_name, file_path=_session_path(page_key, session_id)
            )
        finally:
            await manager.close()
        return json.loads(raw)

    return asyncio.run(_load())


def delete_session(account_name: str, filesystem_name: str, page_key: str, session_id: str) -> None:
    """Delete a stored session and refresh the listing."""

    async def _delete() -> None:
        manager = await helpers.get_async_datalake_manager(account_name=account_name)
        try:
            await manager.delete_file(filesystem_name, _session_path(page_key, session_id))
        finally:
            await manager.close()

    asyncio.run(_delete())
    list_sessions.clear()
