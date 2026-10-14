"""
Pure functions for detecting and merging duplicate company entries in the
local cache.

``vectorization.company_resolution.resolve_companies`` only ever canonicalizes
a freshly-enriched document's raw (name, ticker) pairs against companies
already known *at that moment* -- documents ingested before a company was
first recorded (or concurrently, within the same upload batch, before that
batch's own in-flight fix could see them; see ``upload/ingestion_model.py``)
can still leave two or more ticker entries for the same real company sitting
side by side in the local cache, each with its own slice of documents. This
module is the after-the-fact cleanup pass that finds those and merges them,
working from the already-cached companies list rather than re-running
enrichment -- see ``companies.company_controller.CompanyController.deduplicate_companies``
for the side-effecting half (rewriting documents, deleting alias rows).
"""

from __future__ import annotations

from typing import Any

from vectorization.company_resolution import DEFAULT_FUZZY_THRESHOLD, company_name_similarity, normalize_ticker


def find_merge_groups(
    companies: list[dict[str, Any]],
    fuzzy_threshold: int = DEFAULT_FUZZY_THRESHOLD,
) -> list[dict[str, Any]]:
    """
    Group cached companies that refer to the same real company under different tickers.

    Two companies are grouped together if either:
      - normalizing away a trailing Bloomberg exchange-code suffix makes
        their tickers identical (e.g. "SRT" and "SRT GR"), or
      - their display names fuzzy-match at or above ``fuzzy_threshold`` --
        the same scorer and default threshold ``resolve_companies`` uses at
        ingestion time, so this cleanup pass agrees with it on what counts
        as "the same company" rather than applying a second, looser rule.

    Grouping is a single greedy pass (each remaining company is compared
    only against the group's seed, not pairwise against every other member
    already added), not full transitive closure -- deliberately, so one
    borderline match can't chain two otherwise-unrelated companies together
    through a third.

    Within each group, the canonical entry is the one with the most cached
    documents (ties broken by preferring a ticker with no exchange-code
    suffix, then alphabetically) -- the ticker/name readers have actually
    seen the most, not necessarily the one discovered first.

    Args:
        companies: Cached company rows, as returned by
            ``database.cache.get_all_companies()``.
        fuzzy_threshold: Minimum rapidfuzz score (0-100) for a name match.

    Returns:
        list[dict]: One entry per group that actually needs merging
        (single-company groups are omitted), each
        ``{"canonical": {...company row...}, "aliases": [...company rows...]}``.
    """
    remaining = list(companies)
    raw_groups: list[list[dict[str, Any]]] = []

    while remaining:
        seed = remaining.pop(0)
        seed_ticker_norm = normalize_ticker(seed["ticker"])
        group = [seed]
        still_remaining: list[dict[str, Any]] = []
        for other in remaining:
            same_ticker_family = normalize_ticker(other["ticker"]) == seed_ticker_norm
            name_score = company_name_similarity(seed["display_name"], other["display_name"])
            if same_ticker_family or name_score >= fuzzy_threshold:
                group.append(other)
            else:
                still_remaining.append(other)
        remaining = still_remaining
        raw_groups.append(group)

    return [_to_merge_group(group) for group in raw_groups if len(group) >= 2]


def _to_merge_group(group: list[dict[str, Any]]) -> dict[str, Any]:
    """Pick the canonical entry (most documents, ties broken by no exchange suffix then alphabetically) from a group."""
    canonical = sorted(
        group,
        key=lambda c: (-c["doc_count"], c["ticker"] != normalize_ticker(c["ticker"]), c["ticker"]),
    )[0]
    aliases = [c for c in group if c["ticker"] != canonical["ticker"]]
    return {"canonical": canonical, "aliases": aliases}


def groups_from_tickers(ticker_groups: list[list[str]], companies: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Convert externally-proposed groups of tickers (e.g. from an LLM merge review) into merge groups.

    Reuses the exact same canonical-selection rule ``find_merge_groups`` uses
    (most cached documents, ties broken by preferring no exchange-code
    suffix then alphabetically), so a merge proposed by any source executes
    identically to one this module found on its own -- one merge-application
    code path, not two.

    Args:
        ticker_groups: Each entry a list of >=2 tickers proposed to be the
            same real company.
        companies: The full current company list, as returned by
            ``database.cache.get_all_companies()``, used to look up each
            ticker's full row.

    Returns:
        list[dict]: Same shape as ``find_merge_groups``'s return value.
        A ticker not found in ``companies`` is dropped from its group
        (defensive against a stale/unknown ticker); a group left with fewer
        than 2 resolvable companies is omitted entirely.
    """
    by_ticker = {c["ticker"]: c for c in companies}
    merge_groups: list[dict[str, Any]] = []
    for tickers in ticker_groups:
        group = [by_ticker[t] for t in tickers if t in by_ticker]
        if len(group) >= 2:
            merge_groups.append(_to_merge_group(group))
    return merge_groups


def build_alias_map(merge_groups: list[dict[str, Any]]) -> dict[str, dict[str, str]]:
    """
    Flatten merge groups into a single alias_ticker -> canonical lookup.

    Args:
        merge_groups: Output of ``find_merge_groups``.

    Returns:
        dict[str, dict[str, str]]: ``alias_ticker -> {"ticker": canonical_ticker,
        "name": canonical_display_name}``.
    """
    return {
        alias["ticker"]: {"ticker": group["canonical"]["ticker"], "name": group["canonical"]["display_name"]}
        for group in merge_groups
        for alias in group["aliases"]
    }


def rewrite_document_tickers(
    documents: list[dict[str, Any]], alias_map: dict[str, dict[str, str]]
) -> list[dict[str, Any]]:
    """
    Rewrite documents' companies/company_names lists to fold alias tickers onto their canonical ticker.

    A document that already names both an alias and its canonical (e.g. one
    file mentioning a company by two exchange tickers) collapses to a single
    entry rather than a repeated one.

    Args:
        documents: Cached document records, as returned by
            ``database.cache.get_all_documents()``.
        alias_map: Output of ``build_alias_map``.

    Returns:
        list[dict]: Only the documents whose companies/company_names actually
        changed, each a full record ready to pass straight to
        ``database.cache.upsert_documents``.
    """
    changed: list[dict[str, Any]] = []
    for doc in documents:
        tickers = doc.get("companies") or []
        names = doc.get("company_names") or []
        if not any(ticker in alias_map for ticker in tickers):
            continue

        new_tickers: list[str] = []
        new_names: list[str] = []
        seen: set[str] = set()
        for index, ticker in enumerate(tickers):
            name = names[index] if index < len(names) else ticker
            mapped = alias_map.get(ticker)
            resolved_ticker = mapped["ticker"] if mapped else ticker
            resolved_name = mapped["name"] if mapped else name
            if resolved_ticker in seen:
                continue
            seen.add(resolved_ticker)
            new_tickers.append(resolved_ticker)
            new_names.append(resolved_name)

        changed.append({**doc, "companies": new_tickers, "company_names": new_names})
    return changed


def best_classification(group: dict[str, Any]) -> dict[str, Any]:
    """
    Pick the region/GICS sector/classified_at to keep for a merged group.

    Prefers the canonical entry's own classification; falls back to the
    first already-classified alias's, so a merge never regresses an
    already-classified company back to "Unclassified" just because its
    doc-count-leading ticker happened to be the one still awaiting the
    scheduler's classification pass.

    Args:
        group: One entry from ``find_merge_groups``'s return value.

    Returns:
        dict[str, Any]: ``{"region": ..., "gics_sector": ..., "classified_at": ...}``.
    """
    candidates = [group["canonical"], *group["aliases"]]
    source = next((c for c in candidates if c.get("classified_at")), group["canonical"])
    return {
        "region": source.get("region") or "Unclassified",
        "gics_sector": source.get("gics_sector") or "Unclassified",
        "classified_at": source.get("classified_at"),
    }
