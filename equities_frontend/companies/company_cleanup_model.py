"""
Pure functions for the periodic LLM-driven company cleanup pass.

``company_dedup_model``'s rapidfuzz-only merge detection can't tell two
unrelated companies apart when they merely share a corporate legal-entity
suffix (confirmed in production: "ING Groep N.V." merged onto "Just Eat
Takeaway.com N.V." this way) -- an LLM has the world knowledge to actually
know these are different companies. This module builds that review prompt
and, critically, decides which of the LLM's proposed merges are safe to
apply automatically versus need a human to approve: only a merge confirmed
against the authoritative company reference (real portfolio holdings data,
not the LLM's own say-so) auto-applies; everything else queues for review.
See ``companies.company_controller.CompanyController.run_llm_cleanup`` for
the side-effecting half.
"""

from __future__ import annotations

import json
import re
from typing import Any

from vectorization.company_resolution import DEFAULT_FUZZY_THRESHOLD, company_name_similarity


def build_merge_review_prompt(companies: list[dict[str, Any]]) -> str:
    """
    Build the prompt asking the LLM to find company entries that refer to the same real company.

    Sends the full current company list in one call, not batched -- the
    Companies page currently shows only a couple dozen companies, well
    within one call's context; revisit with batching only if the corpus
    grows large enough for this to become a real limit.

    Args:
        companies: Cached company rows, as returned by
            ``database.cache.get_all_companies()``.

    Returns:
        str: The user-turn prompt text for a single chat completion call.
    """
    company_lines = "\n".join(f"- {c['ticker']}: {c['display_name']}" for c in companies)
    return (
        "Below is a list of companies (ticker: display name) currently tracked in an equities "
        "research database. Some entries may actually refer to the SAME real-world company under "
        "a different ticker or a slightly different name spelling -- find any such groups.\n\n"
        "Do NOT group two different companies together just because their names share a common "
        'corporate suffix (e.g. "N.V.", "A/S", "PLC", "Inc.") or a generic word (e.g. "Group", '
        '"Holding") -- only group them if they are genuinely the same real-world company.\n\n'
        f"Companies:\n{company_lines}\n\n"
        "Return ONLY a valid JSON array, one object per group that needs merging (omit companies "
        "that have no duplicate), in this exact form:\n"
        '[{"tickers": ["TICKER1", "TICKER2"], "canonical_name": "Full Legal Name", '
        '"reason": "short explanation"}, ...]\n'
        "No markdown fences, no explanation outside the JSON -- raw JSON only. If nothing needs "
        "merging, return an empty array []."
    )


def parse_merge_review_response(raw: str, valid_tickers: set[str]) -> list[dict[str, Any]]:
    """
    Parse the LLM's merge-review response into a validated list of proposed merge groups.

    Args:
        raw: The raw LLM response text.
        valid_tickers: Every ticker actually present in the company list sent
            to the LLM -- a proposed ticker outside this set is dropped
            rather than trusted (a hallucinated or malformed ticker), and a
            group left with fewer than 2 valid tickers isn't a merge at all.

    Returns:
        list[dict[str, Any]]: Each ``{"tickers": [...], "canonical_name": str | None, "reason": str | None}``.
    """
    cleaned = re.sub(r"^```(?:json)?\s*\n?", "", raw.strip())
    cleaned = re.sub(r"\n?```\s*$", "", cleaned)

    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError:
        return []
    if not isinstance(parsed, list):
        return []

    groups: list[dict[str, Any]] = []
    for entry in parsed:
        if not isinstance(entry, dict):
            continue
        tickers = [str(t).strip().upper() for t in entry.get("tickers") or [] if str(t).strip()]
        tickers = [t for t in tickers if t in valid_tickers]
        if len(tickers) < 2:
            continue
        groups.append(
            {
                "tickers": tickers,
                "canonical_name": str(entry.get("canonical_name", "")).strip() or None,
                "reason": str(entry.get("reason", "")).strip() or None,
            }
        )
    return groups


def gate_merge_groups(
    groups: list[dict[str, Any]],
    companies: list[dict[str, Any]],
    reference: list[dict[str, Any]],
    fuzzy_threshold: int = DEFAULT_FUZZY_THRESHOLD,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """
    Split LLM-proposed merge groups into auto-apply and needs-review, per the hybrid auto-apply policy.

    A group auto-applies only when EVERY ticker in it is independently
    confirmed against the SAME single authoritative reference entry --
    each member's own *current* display name (not the LLM's proposed
    ``canonical_name`` label) fuzzy-matches (via the legal-suffix-aware,
    now-fixed ``vectorization.company_resolution.company_name_similarity``)
    one resolved ``reference`` row, and they all match that one row, not
    different ones each.

    Checking only the proposed ``canonical_name`` against the reference (an
    earlier version of this function did exactly that) is NOT safe: it would
    auto-apply a group that wrongly folds an unrelated company into a real
    one, as long as the *label* the LLM chose happened to be a real
    company's name -- e.g. merging JET (real) with INGA (unrelated) under
    the canonical name "Just Eat Takeaway.com N.V." would pass a
    canonical-name-only check, since that name alone is genuinely a real,
    reference-confirmed company. That is the exact class of incident (ING
    Groep silently merged onto Just Eat Takeaway, Novonesis onto Netcompany
    Group) this whole mechanism exists to stop from happening again -- so
    every member, not just the label, must be independently confirmed.

    Args:
        groups: Output of ``parse_merge_review_response``.
        companies: The full current company list, as returned by
            ``database.cache.get_all_companies()``, used to look up each
            ticker's current display name.
        reference: Resolved reference rows, as returned by
            ``database.cache.get_all_company_reference()`` (unresolved rows,
            with no ``canonical_name`` yet, are ignored).
        fuzzy_threshold: Minimum rapidfuzz score (0-100) for a confirming match.

    Returns:
        tuple[list[dict], list[dict]]: ``(auto_apply, needs_review)``. A
        group referencing a ticker missing from ``companies`` (stale/unknown)
        always needs review.
    """
    by_ticker = {c["ticker"]: c["display_name"] for c in companies}
    resolved_names = [r["canonical_name"] for r in reference if r.get("canonical_name")]

    auto_apply: list[dict[str, Any]] = []
    needs_review: list[dict[str, Any]] = []
    for group in groups:
        member_names = [by_ticker[t] for t in group["tickers"] if t in by_ticker]
        confirmed = len(member_names) == len(group["tickers"]) and _all_confirm_same_reference(
            member_names, resolved_names, fuzzy_threshold
        )
        (auto_apply if confirmed else needs_review).append(group)
    return auto_apply, needs_review


def _all_confirm_same_reference(member_names: list[str], resolved_names: list[str], fuzzy_threshold: int) -> bool:
    """True if every member name independently fuzzy-matches the SAME single reference name."""
    return any(
        all(company_name_similarity(name, ref_name) >= fuzzy_threshold for name in member_names)
        for ref_name in resolved_names
    )
