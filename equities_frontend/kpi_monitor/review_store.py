"""
Data Lake storage for saved KPI reviews.

One JSON file per review under ``LLM/kpi-monitor/<company folder>/<review id>.json``
in the configured filesystem. The review id carries the creation time and latest
period (see ``kpi_model.make_review_id``), so listing a company's reviews is a
directory listing with no file downloads. Mirrors ``chat/session_store.py``.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
from typing import Any

import streamlit as st

import helpers
from kpi_monitor.kpi_model import ids_to_prune

logger = logging.getLogger(__name__)

REVIEWS_ROOT = "LLM/kpi-monitor"
_SUFFIX = ".json"


def _directory(folder: str) -> str:
    return f"{REVIEWS_ROOT}/{folder}"


def _path(folder: str, review_id: str) -> str:
    return f"{_directory(folder)}/{review_id}{_SUFFIX}"


async def _list_ids(manager: Any, filesystem_name: str, folder: str) -> list[str]:
    ids: list[str] = []
    async for path_props in manager.list_paths(filesystem_name=filesystem_name, path=_directory(folder), recursive=False):
        name = path_props.name.rsplit("/", 1)[-1]
        if not getattr(path_props, "is_directory", False) and name.endswith(_SUFFIX):
            ids.append(name[: -len(_SUFFIX)])
    return sorted(ids, reverse=True)


def save_review(
    *,
    account_name: str,
    filesystem_name: str,
    folder: str,
    review_id: str,
    payload: dict[str, Any],
    keep: int,
) -> None:
    """Write a review, then delete the oldest ones beyond ``keep``."""
    data = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")

    async def _save() -> None:
        manager = await helpers.get_async_datalake_manager(account_name=account_name)
        try:
            await manager.upload_file(
                filesystem_name=filesystem_name,
                file_path=_path(folder, review_id),
                file_content=io.BytesIO(data),
                archive=False,
                overwrite=True,
            )
            try:
                for old_id in ids_to_prune(await _list_ids(manager, filesystem_name, folder), keep):
                    await manager.delete_file(filesystem_name, _path(folder, old_id))
            except Exception as exc:  # noqa: BLE001 - pruning is housekeeping; the save itself succeeded
                logger.info("Could not prune old KPI reviews in %s: %s", folder, exc)
        finally:
            await manager.close()

    asyncio.run(_save())
    list_reviews.clear()


@st.cache_data(ttl=30, show_spinner=False)
def list_reviews(account_name: str, filesystem_name: str, folder: str) -> list[str]:
    """Return a company's saved review ids, newest first (empty if the folder does not exist yet)."""

    async def _list() -> list[str]:
        manager = await helpers.get_async_datalake_manager(account_name=account_name)
        try:
            return await _list_ids(manager, filesystem_name, folder)
        except Exception as exc:  # noqa: BLE001 - the folder doesn't exist until the first save
            logger.info("No saved KPI reviews listed for %s: %s", folder, exc)
            return []
        finally:
            await manager.close()

    return asyncio.run(_list())


def load_review(account_name: str, filesystem_name: str, folder: str, review_id: str) -> dict[str, Any]:
    """Load one saved review's JSON."""

    async def _load() -> dict[str, Any]:
        manager = await helpers.get_async_datalake_manager(account_name=account_name)
        try:
            raw = await manager.download_file(filesystem_name=filesystem_name, file_path=_path(folder, review_id))
        finally:
            await manager.close()
        return json.loads(raw)

    return asyncio.run(_load())


def delete_review(account_name: str, filesystem_name: str, folder: str, review_id: str) -> None:
    """Delete one saved review and refresh the listing."""

    async def _delete() -> None:
        manager = await helpers.get_async_datalake_manager(account_name=account_name)
        try:
            await manager.delete_file(filesystem_name, _path(folder, review_id))
        finally:
            await manager.close()

    asyncio.run(_delete())
    list_reviews.clear()
