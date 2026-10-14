"""Pure functions for the upload / bulk-upload topic: archive filtering and progress math."""

from __future__ import annotations

import os


def supported_archive_members(namelist: list[str], allowed_extensions: list[str]) -> list[str]:
    """
    Filter a zip archive's member names down to supported, non-hidden files.

    Args:
        namelist (list[str]): Every entry name in the archive (files and dirs).
        allowed_extensions (list[str]): Accepted file extensions, e.g. [".pdf", ".xlsx"].

    Returns:
        list[str]: Member names that are files, not hidden/junk entries, and
        have a supported extension.
    """
    members: list[str] = []
    for name in namelist:
        if name.endswith("/"):
            continue
        if os.path.basename(name).startswith("."):  # skip hidden / __MACOSX junk
            continue
        if os.path.splitext(name)[-1].lower() in allowed_extensions:
            members.append(name)
    return members


def build_destination_path(index_path: str, filename: str) -> str:
    """
    Build the Data Lake destination path for an uploaded file.

    Args:
        index_path (str): The deployment's configured Data Lake index path.
        filename (str): The uploaded file's name.

    Returns:
        str: The full destination path, e.g. "LLM/indices/Equities/MODL_NVDA.xlsx".
    """
    return f"{index_path}/{filename}"


def upload_progress_fraction(completed: int, total: int) -> float:
    """Progress fraction for the upload half of a two-phase bulk upload (0.0-0.5)."""
    return completed / total / 2


def ingest_progress_fraction(completed: int, total: int) -> float:
    """Progress fraction for the ingest half of a two-phase bulk upload (0.5-1.0)."""
    return 0.5 + completed / total / 2
