"""
Pure functions for resolving Portfolio Dashboard holdings names to
ticker/canonical-name pairs.

Bloomberg holdings data only ever gives a security's display name (e.g.
"ASML HOLDING NV"), never a ticker (see
``portfolio_dashboard.chat_context_model.distinct_security_names``) -- an LLM
call resolves that name to its stock ticker and canonical legal name, using
the exact same call shape, batching, and defensive JSON-parsing pattern
``company_model.py`` already uses for region/GICS sector classification, so
this is a second instance of one already-trusted pattern, not a new one.
"""

from __future__ import annotations

import json
import re


def build_reference_prompt(names: list[str]) -> str:
    """
    Build the prompt asking the LLM to resolve Bloomberg holdings names to ticker + canonical legal name.

    Args:
        names: Bloomberg security display names, e.g. "ASML HOLDING NV".

    Returns:
        str: The user-turn prompt text for a single chat completion call.
    """
    name_lines = "\n".join(f"- {name}" for name in names)
    return (
        "For each listed company (given as a Bloomberg security display name), identify its "
        'primary stock ticker (the short code used on its main listing, e.g. "ASML") and its '
        'full canonical legal name (e.g. "ASML Holding N.V."), using your general knowledge.\n\n'
        f"Companies:\n{name_lines}\n\n"
        "Return ONLY a valid JSON array, one object per company, in this exact form:\n"
        '[{"name": "the exact Bloomberg name as given", "ticker": "TICKER", '
        '"canonical_name": "Full Legal Name"}, ...]\n'
        "No markdown fences, no explanation -- raw JSON only. If you are not fully certain, still "
        "provide your best answer rather than omitting the entry."
    )


def parse_reference_response(raw: str, names: list[str]) -> dict[str, dict[str, str | None]]:
    """
    Parse the LLM's ticker/canonical-name response into a validated name -> {ticker, canonical_name} map.

    Every requested name is guaranteed to be present in the result -- one
    missing from the response, or malformed, falls back to
    ``{"ticker": None, "canonical_name": None}`` rather than being dropped.

    Args:
        raw: The raw LLM response text.
        names: Every Bloomberg display name that was requested, so the
            result always has full coverage regardless of what the LLM returned.

    Returns:
        dict[str, dict[str, str | None]]: ``name -> {"ticker": ..., "canonical_name": ...}``.
    """
    result: dict[str, dict[str, str | None]] = {name: {"ticker": None, "canonical_name": None} for name in names}

    cleaned = re.sub(r"^```(?:json)?\s*\n?", "", raw.strip())
    cleaned = re.sub(r"\n?```\s*$", "", cleaned)

    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError:
        return result
    if not isinstance(parsed, list):
        return result

    for entry in parsed:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name", "")).strip()
        if name not in result:
            continue
        ticker = str(entry.get("ticker", "")).strip().upper()
        canonical_name = str(entry.get("canonical_name", "")).strip()
        result[name] = {"ticker": ticker or None, "canonical_name": canonical_name or None}

    return result
