"""
Post-enrichment company-name canonicalization.

The native avb_search ``MetadataEnricher`` (avb_search.enrichment.metadata_controller)
extracts ``companies``/``tickers`` directly from each document's text -- a
per-document LLM call with no memory of how the same company was named in any
other document. Two documents about the same company can independently
produce "Nike, Inc." and "NIKE Incorporated" with no dedup between them.

This module closes that gap as a single pure function, not a revived
``CompanyResolver`` class: given a segment's raw (names, tickers) plus the set
of companies already known (from database/cache.py's companies table, kept
fresh by the hourly scheduler classification job), replace near-duplicate
spellings with the one canonical name/ticker pair already on record. There is
no separate LLM-fallback step here -- the native enricher's direct extraction
already *is* the LLM step; an unmatched name is left as the enricher produced
it, since there's nothing more authoritative to fall back to.
"""

from __future__ import annotations

import re

from rapidfuzz import fuzz, process, utils

DEFAULT_FUZZY_THRESHOLD = 80

# A shared corporate legal-entity suffix (jurisdiction marker, not part of
# what makes a company's name distinctive) is, on its own, enough for
# rapidfuzz's WRatio to score two *completely unrelated* companies as a
# match: "ING Groep N.V." vs "Just Eat Takeaway.com N.V." scores 85/100
# unstripped -- comfortably above DEFAULT_FUZZY_THRESHOLD -- purely because
# both end in "N.V.". Confirmed in production: a batch of ING Group research
# notes got silently canonicalized onto Just Eat Takeaway's ticker this way,
# and separately, a batch of Novonesis notes onto Netcompany Group's, both
# sharing a jurisdiction suffix (N.V./A/S) with an already-known company.
#
# The same failure mode isn't limited to legal-entity markers -- any generic,
# non-distinctive trailing word does it. Confirmed again in production: once
# "Yangzhou Yangjie Electronic Technology Co., Ltd." was a known company,
# every subsequent document about an unrelated company whose enricher-
# extracted name happened to also end in "Technology" ("Microchip
# Technology", "Marvell Technology", ...) silently got reassigned to its
# ticker, purely because both names end in "Technology" after suffix
# stripping. Same cause, same fix: strip the generic trailing word so only
# the distinctive part of each name is ever compared.
#
# Stripped before every fuzzy comparison below (name_similarity, and
# indirectly _fuzzy_processor) so a shared suffix or generic descriptor can
# no longer manufacture a false match; not exhaustive, just the
# jurisdictions/entity types/descriptors already seen causing false matches
# in this corpus -- extend as new ones turn up.
_LEGAL_SUFFIX_RE = re.compile(
    r"\b(N\.?V\.?|A/S|PLC|S\.?A\.?|AG|SE|Inc\.?|Incorporated|Corp\.?|Corporation|"
    r"Ltd\.?|Limited|GmbH|Oyj|ASA|AB|SpA|BV|Group|Co\.?|Company|Holdings?|"
    r"Technology|Technologies|Electronic|Electronics|International|Industries|"
    r"Systems|Solutions|Enterprises)\.?$",
    re.IGNORECASE,
)


def _strip_legal_suffix(name: str) -> str:
    """Repeatedly strip a trailing corporate legal-entity suffix, e.g. ``"ASML Holding N.V."`` -> ``"ASML"``."""
    previous = None
    while previous != name:
        previous = name
        name = _LEGAL_SUFFIX_RE.sub("", name).strip().rstrip(",.").strip()
    return name


def _fuzzy_processor(name: object, **_kwargs: object) -> str:
    """rapidfuzz ``processor``: strip a legal-entity suffix, then case/punctuation-normalize (rapidfuzz.utils.default_process)."""
    return utils.default_process(_strip_legal_suffix(str(name)))


def company_name_similarity(name_a: str, name_b: str) -> float:
    """
    Fuzzy-match score (0-100) between two company display names.

    Strips a common corporate legal-entity suffix and case/punctuation-
    normalizes both sides first (see ``_fuzzy_processor``) -- without that, a
    merely-shared, non-distinctive suffix like "N.V." or "A/S" is enough on
    its own to score two unrelated companies above the usual matching
    threshold (see the module-level comment above ``_LEGAL_SUFFIX_RE``).
    Public so ``companies.company_dedup_model`` scores candidate merges the
    same way ``resolve_companies`` does at ingestion time -- one definition
    of "similar enough," not two that can silently drift apart.

    Args:
        name_a: First company display name.
        name_b: Second company display name.

    Returns:
        float: rapidfuzz WRatio score, 0-100.
    """
    return fuzz.WRatio(name_a, name_b, processor=_fuzzy_processor)


# Bloomberg tickers are sometimes reported with a trailing exchange code
# ("SRT GR" for Xetra vs. plain "SRT") depending on which of a company's
# listings the source document mentions. Left unnormalized, that alone is
# enough for the same company to end up as two separate cached entries --
# stripping it before matching means "SRT" and "SRT GR" resolve identically.
# Not exhaustive -- a genuinely new exchange code just means that document's
# ticker stays as-is (same as any other unmatched entry), not a hard failure;
# extend this set as new codes turn up in practice.
_BLOOMBERG_EXCHANGE_SUFFIXES = frozenset(
    {
        "US", "LN", "GR", "FP", "NA", "SM", "IM", "SW", "HK", "JP", "JT",
        "AU", "CN", "BB", "SS", "NO", "DC", "FH", "ID", "PL", "AT", "SJ", "IN",
    }
)  # fmt: skip


def normalize_ticker(ticker: str) -> str:
    """
    Strip a trailing Bloomberg exchange-code suffix, e.g. ``"SRT GR"`` -> ``"SRT"``.

    Public (not ingestion-internal) because ``companies.company_dedup_model``
    also uses it to detect already-cached tickers that only differ by this
    suffix, when cleaning up companies that slipped past this module's own
    at-ingestion-time resolution (e.g. two exchange variants uploaded before
    either was a known company).
    """
    prefix, _, suffix = ticker.rpartition(" ")
    return prefix if prefix and suffix in _BLOOMBERG_EXCHANGE_SUFFIXES else ticker


def resolve_companies(
    raw_names: list[str],
    raw_tickers: list[str],
    known_companies: dict[str, str],
    fuzzy_threshold: int = DEFAULT_FUZZY_THRESHOLD,
) -> tuple[list[str], list[str]]:
    """
    Canonicalize a segment's raw (names, tickers) pairs against known companies.

    Entry *i* of ``raw_names`` corresponds to entry *i* of ``raw_tickers`` --
    the same convention the native enricher's parallel-list output already
    uses. Every ticker is first normalized (trailing Bloomberg exchange-code
    suffixes stripped, see ``normalize_ticker``) so e.g. "SRT" and "SRT GR"
    are treated as the same ticker throughout. For each entry: if the
    (normalized) ticker is already in ``known_companies``, its canonical
    display name replaces whatever the enricher produced. Otherwise the raw
    name is fuzzy matched against every known display name; on a match at or
    above ``fuzzy_threshold``, both the name and ticker are replaced with the
    known pair. No match on either path leaves that entry's name as the
    enricher produced it, with only the ticker normalized.

    Args:
        raw_names: The enricher's raw ``companies`` (full name) values for one segment.
        raw_tickers: The enricher's raw ``tickers`` values for the same segment,
            parallel to ``raw_names``.
        known_companies: Mapping of ticker -> canonical display name, sourced
            from ``database.cache.get_all_companies()``.
        fuzzy_threshold: Minimum rapidfuzz score (0-100) for a name-only match.

    Returns:
        tuple[list[str], list[str]]: (resolved_names, resolved_tickers), same
        length and order as the input.
    """
    known_by_name = {name: ticker for ticker, name in known_companies.items()}
    resolved_names: list[str] = []
    resolved_tickers: list[str] = []

    for index, raw_name in enumerate(raw_names):
        raw_ticker = raw_tickers[index] if index < len(raw_tickers) else ""
        if raw_ticker:
            raw_ticker = normalize_ticker(raw_ticker)

        if raw_ticker and raw_ticker in known_companies:
            resolved_names.append(known_companies[raw_ticker])
            resolved_tickers.append(raw_ticker)
            continue

        match = (
            process.extractOne(
                raw_name,
                known_by_name.keys(),
                scorer=fuzz.WRatio,
                processor=_fuzzy_processor,
                score_cutoff=fuzzy_threshold,
            )
            if known_by_name
            else None
        )
        if match:
            matched_name = match[0]
            resolved_names.append(matched_name)
            resolved_tickers.append(known_by_name[matched_name])
            continue

        resolved_names.append(raw_name)
        resolved_tickers.append(raw_ticker)

    return resolved_names, resolved_tickers
