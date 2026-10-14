"""
Pure functions for recovering documents whose live blob was deleted but
whose upload/overwrite history survives in a sibling ``archive/`` folder.

``avb_core``'s ``DataLakeManager.upload_file(overwrite=True)`` never truly
overwrites a live file in place -- it renames the *previous* live blob into
a sibling ``archive/`` folder as ``"<stem>_v<NNN><ext>"`` (an incrementing,
zero-padded version number) before writing the new content to the live
path (see ``scheduler.scheduler_controller._prune_backup_archives``, which
already relies on this same archiving behaviour for backup pruning). If a
live blob is later deleted outright -- not merely superseded by another
overwrite -- only these versioned copies survive. This module finds, for a
given live path, the highest-numbered archived version of it: that file's
last-known-good content, and the natural thing to re-upload and re-ingest
in its place.
"""

from __future__ import annotations

import re
from pathlib import PurePosixPath

_VERSION_RE = re.compile(r"^(?P<stem>.+)_v(?P<version>\d{3,})(?P<ext>\.[^./]+)$")


def parse_archived_name(filename: str) -> tuple[str, int] | None:
    """
    Parse one archive filename into the live basename it's a version of, and its version number.

    Args:
        filename: An archive folder entry's bare filename, e.g. "MODL_NVDA_US1_v006.xlsx".

    Returns:
        tuple[str, int] | None: ``(live_basename, version)``, e.g.
        ``("MODL_NVDA_US1.xlsx", 6)``, or ``None`` if ``filename`` doesn't
        match the ``"<stem>_v<NNN><ext>"`` versioning pattern.
    """
    match = _VERSION_RE.match(filename)
    if not match:
        return None
    return f"{match['stem']}{match['ext']}", int(match["version"])


def latest_versions_by_basename(archive_filenames: list[str]) -> dict[str, str]:
    """
    Group archive filenames by the live basename they're a version of, keeping only the highest version of each.

    Args:
        archive_filenames: Every entry's bare filename in one ``archive/`` folder.

    Returns:
        dict[str, str]: ``live_basename -> archive filename of its highest
        version`` (e.g. ``"MODL_NVDA_US1.xlsx" -> "MODL_NVDA_US1_v006.xlsx"``).
        Filenames that don't match the versioning pattern are skipped.
    """
    best: dict[str, tuple[int, str]] = {}
    for name in archive_filenames:
        parsed = parse_archived_name(name)
        if not parsed:
            continue
        basename, version = parsed
        if basename not in best or version > best[basename][0]:
            best[basename] = (version, name)
    return {basename: name for basename, (_version, name) in best.items()}


def archive_dir_for(path: str) -> str:
    """
    Return the sibling ``archive/`` directory for a live Data Lake path.

    Mirrors ``scheduler_controller._prune_backup_archives``'s own
    ``PurePosixPath(path).parent / "archive"`` derivation, so both agree on
    where ``avb_core``'s overwrite-archiving puts things.

    Args:
        path: A live Data Lake file path.

    Returns:
        str: The sibling archive directory's path.
    """
    return str(PurePosixPath(path).parent / "archive")


def build_restore_plan(missing_paths: list[str], archive_listing_by_dir: dict[str, list[str]]) -> dict[str, str]:
    """
    Map each missing live path to the archive path of its recoverable latest version.

    Args:
        missing_paths: Live Data Lake paths whose blob is confirmed gone
            (e.g. a 404 on download).
        archive_listing_by_dir: ``archive_dir -> every bare filename found
            there``, one entry per distinct archive directory among
            ``missing_paths`` -- callers only need to list each archive
            folder once, however many missing paths share it.

    Returns:
        dict[str, str]: ``live_path -> full archive path`` of the version to
        restore from. A live path is omitted if its archive folder has no
        matching versioned entry.
    """
    plan: dict[str, str] = {}
    for path in missing_paths:
        archive_dir = archive_dir_for(path)
        listing = archive_listing_by_dir.get(archive_dir)
        if not listing:
            continue
        latest = latest_versions_by_basename(listing)
        match = latest.get(PurePosixPath(path).name)
        if match:
            plan[path] = str(PurePosixPath(archive_dir) / match)
    return plan
