"""Pure log-line parsing and filtering for the Logs page."""

from __future__ import annotations


def parse_lines(text: str) -> list[str]:
    """Split raw log file text into non-empty lines, oldest first."""
    return [line for line in text.splitlines() if line.strip()]


def filter_lines(lines: list[str], level: str, query: str) -> list[str]:
    """Keep only lines matching ``level`` (unless "ALL") and containing ``query``, case-insensitive."""
    result = lines
    if level and level != "ALL":
        result = [line for line in result if level in line]
    if query:
        needle = query.lower()
        result = [line for line in result if needle in line.lower()]
    return result


def tail(lines: list[str], max_lines: int) -> list[str]:
    """Return the most recent ``max_lines`` of ``lines``."""
    return lines[-max_lines:] if max_lines > 0 else lines
