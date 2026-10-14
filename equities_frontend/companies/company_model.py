"""
Pure functions for the Companies page.

Aggregates the local document cache into one row per canonical company,
builds/parses the LLM region + GICS sector classification prompt, and formats
financial-model time series for the company detail screen's read-only summary
table. No Streamlit calls, no LLM calls, no Azure calls -- those are
Controller-owned.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from typing import Any

from dcf.dcf_parsing_model import ParsedRecord

_UNCLASSIFIED = "Unclassified"


def aggregate_companies(documents: list[dict]) -> list[dict]:
    """
    Group cached documents into one summary row per canonical ticker.

    Each document carries parallel ``companies`` (tickers) and
    ``company_names`` lists produced by ingestion-side metadata enrichment
    (the native avb_search ``MetadataEnricher``, canonicalized against
    already-known companies by ``vectorization.company_resolution.resolve_companies``)
    -- entry *i* of one list corresponds to entry *i* of the other. A ticker
    can still appear under slightly different name spellings across documents
    (e.g. before a name was first recorded as a known company); the most
    frequently seen spelling is kept as the display name so the page never
    shows near-duplicate entries for the same company.

    Args:
        documents (list[dict]): Cached document records, as returned by
            ``database.cache.get_all_documents()``.

    Returns:
        list[dict]: One dict per ticker with keys "ticker", "display_name",
        "doc_count", "has_financial_model", "first_document_date",
        "last_document_date".
    """
    by_ticker: dict[str, dict[str, Any]] = {}

    for doc in documents:
        tickers = doc.get("companies") or []
        names = doc.get("company_names") or []
        source_type = doc.get("source_type") or ""
        doc_date = (doc.get("document_date") or "")[:10] or None

        for index, raw_ticker in enumerate(tickers):
            ticker = (raw_ticker or "").strip().upper()
            if not ticker:
                continue
            name = names[index].strip() if index < len(names) and names[index] else ticker

            entry = by_ticker.setdefault(
                ticker,
                {"name_counts": Counter(), "doc_count": 0, "has_financial_model": False, "dates": []},
            )
            entry["name_counts"][name] += 1
            entry["doc_count"] += 1
            if source_type == "financial_model":
                entry["has_financial_model"] = True
            if doc_date:
                entry["dates"].append(doc_date)

    aggregated: list[dict] = []
    for ticker, entry in by_ticker.items():
        dates: list[str] = entry["dates"]
        name_counts: Counter[str] = entry["name_counts"]
        aggregated.append(
            {
                "ticker": ticker,
                "display_name": name_counts.most_common(1)[0][0] if name_counts else ticker,
                "doc_count": entry["doc_count"],
                "has_financial_model": entry["has_financial_model"],
                "first_document_date": min(dates) if dates else None,
                "last_document_date": max(dates) if dates else None,
            }
        )
    return aggregated


def companies_needing_classification(aggregated: list[dict], classified_tickers: set[str]) -> list[dict]:
    """
    Filter aggregated companies down to those without a cached classification.

    Args:
        aggregated (list[dict]): Output of ``aggregate_companies``.
        classified_tickers (set[str]): Tickers already classified, from
            ``database.cache.get_classified_tickers()``.

    Returns:
        list[dict]: The subset of ``aggregated`` whose ticker is not in
        ``classified_tickers``.
    """
    return [company for company in aggregated if company["ticker"] not in classified_tickers]


def build_classification_prompt(
    companies: list[dict],
    allowed_regions: list[str],
    allowed_gics_sectors: list[str],
) -> str:
    """
    Build the batched prompt asking the LLM to classify companies by region and GICS sector.

    Classification is constrained to the two fixed lists so the Companies
    page's filters never accumulate free-text near-duplicate categories.

    Args:
        companies (list[dict]): Companies to classify, each with "ticker" and
            "display_name".
        allowed_regions (list[str]): The fixed set of region labels to choose from.
        allowed_gics_sectors (list[str]): The fixed set of GICS sector labels to choose from.

    Returns:
        str: The user-turn prompt text for a single chat completion call.
    """
    company_lines = "\n".join(f"- {company['ticker']}: {company['display_name']}" for company in companies)
    return (
        "For each listed company, classify it by primary listing region and GICS sector, "
        "using your general knowledge of the company.\n\n"
        f"Allowed regions (choose exactly one per company): {', '.join(allowed_regions)}\n"
        f"Allowed GICS sectors (choose exactly one per company): {', '.join(allowed_gics_sectors)}\n\n"
        f"Companies:\n{company_lines}\n\n"
        "Return ONLY a valid JSON array, one object per company, in this exact form:\n"
        '[{"ticker": "TICKER", "region": "one of the allowed regions", '
        '"gics_sector": "one of the allowed sectors"}, ...]\n'
        "No markdown fences, no explanation -- raw JSON only. If you are not confident about a "
        "company, still pick the closest allowed region and sector rather than omitting it."
    )


def parse_classification_response(
    raw: str,
    tickers: list[str],
    allowed_regions: list[str],
    allowed_gics_sectors: list[str],
) -> dict[str, dict[str, str]]:
    """
    Parse the LLM's classification response into a validated ticker -> {region, gics_sector} map.

    Every requested ticker is guaranteed to be present in the result -- a
    ticker missing from the response, or classified with a value outside the
    allowed lists, falls back to "Unclassified" rather than being dropped or
    left with an unmoderated free-text label.

    Args:
        raw (str): The raw LLM response text.
        tickers (list[str]): Every ticker that was requested, so the result
            always has full coverage regardless of what the LLM returned.
        allowed_regions (list[str]): The fixed set of valid region labels.
        allowed_gics_sectors (list[str]): The fixed set of valid GICS sector labels.

    Returns:
        dict[str, dict[str, str]]: ticker -> {"region": ..., "gics_sector": ...}.
    """
    result: dict[str, dict[str, str]] = {
        ticker: {"region": _UNCLASSIFIED, "gics_sector": _UNCLASSIFIED} for ticker in tickers
    }

    cleaned = re.sub(r"^```(?:json)?\s*\n?", "", raw.strip())
    cleaned = re.sub(r"\n?```\s*$", "", cleaned)

    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError:
        return result
    if not isinstance(parsed, list):
        return result

    regions = set(allowed_regions)
    sectors = set(allowed_gics_sectors)
    for entry in parsed:
        if not isinstance(entry, dict):
            continue
        ticker = str(entry.get("ticker", "")).strip().upper()
        if ticker not in result:
            continue
        region = str(entry.get("region", "")).strip()
        sector = str(entry.get("gics_sector", "")).strip()
        result[ticker] = {
            "region": region if region in regions else _UNCLASSIFIED,
            "gics_sector": sector if sector in sectors else _UNCLASSIFIED,
        }

    return result


def batches(items: list[Any], batch_size: int) -> list[list[Any]]:
    """
    Split a list into fixed-size batches, preserving order.

    Args:
        items (list[Any]): The items to split.
        batch_size (int): Maximum number of items per batch.

    Returns:
        list[list[Any]]: Consecutive chunks of at most ``batch_size`` items.
    """
    return [items[i : i + batch_size] for i in range(0, len(items), batch_size)]


def financial_summary_rows(
    historicals: dict[str, list[ParsedRecord]],
    item_labels: dict[str, str],
    max_periods: int = 6,
) -> list[dict]:
    """
    Flatten mapped historical line items into long-format rows for a summary table.

    Only the most recent ``max_periods`` periods per line item are kept, so
    the company detail screen shows a compact snapshot rather than the full
    model (the full Bloomberg model remains available via the DCF Model
    Builder).

    Args:
        historicals (dict[str, list[ParsedRecord]]): canonical_item_id ->
            oldest-to-newest time series, as returned by
            ``DcfController.historical_series``.
        item_labels (dict[str, str]): canonical_item_id -> display label.
        max_periods (int): Maximum number of most-recent periods per line item.

    Returns:
        list[dict]: Rows with keys "Metric", "Period", "Value", "Estimate",
        "Currency", "Unit", ordered by line item then period.
    """
    rows: list[dict] = []
    for item_id, series in historicals.items():
        if not series:
            continue
        label = item_labels.get(item_id, item_id)
        for record in series[-max_periods:]:
            rows.append(
                {
                    "Metric": label,
                    "Period": record.period_end_date[:10],
                    "Value": record.value,
                    "Estimate": record.is_estimate,
                    "Currency": record.currency or "",
                    "Unit": record.unit or "",
                }
            )
    return rows
