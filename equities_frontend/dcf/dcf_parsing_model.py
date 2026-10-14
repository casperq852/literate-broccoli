"""
Pure functions for the DCF Model Builder: content-string parsing, metric
classification, and historical-trend calculations.

The search index does not store a raw numeric value for financial-model
chunks -- only a rendered display string (``content``) plus structured
``metric``/``segment``/``periodEndDate``/``isEstimate``/``currency``/``unit``
fields. Every function here works from those fields alone; none of them make
network calls or know about Streamlit/LLM orchestration.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from search.search_model import Chunk

DEFAULT_CONTENT_PATH: Path = Path(__file__).resolve().parent.parent / "configs" / "dcf_content.yaml"

# The ingestion-side format string (financial_model_controller._build_record)
# always ends with "): <value><suffix>" -- this is the one fixed anchor every
# financial-model chunk's content shares, regardless of company or metric.
_TAIL_RE = re.compile(r"\):\s*(.+)$")


@lru_cache(maxsize=1)
def load_content(path: str = str(DEFAULT_CONTENT_PATH)) -> dict[str, Any]:
    """
    Load the DCF Model Builder's canonical line-item rules and LLM prompt template.

    Args:
        path (str): Path to the YAML content file.

    Returns:
        dict[str, Any]: A dict with keys "canonical_line_items" (id -> rule
        dict with "label"/"required"/"keywords"/"exclude_keywords") and
        "llm_suggestion" ("system_prompt"/"user_prompt_template").
    """
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


@dataclass(frozen=True)
class ParsedRecord:
    """One metric x period record with its numeric value recovered from ``content``."""

    metric: str
    segment: str | None
    period_end_date: str
    is_estimate: bool
    value: float
    currency: str | None
    unit: str | None
    # Date of the source MODL file (the chunk's documentDate), so callers that
    # see several uploads of the same model can tell which version a value came from.
    document_date: str | None = None


def parse_value_from_content(content: str, currency: str | None, unit: str | None) -> float | None:
    """
    Recover the raw numeric value from a financial-model chunk's rendered content.

    The ingestion-side format string always ends with "): <value><suffix>"
    where <suffix> is either empty (ratio/percentage metrics) or
    " {currency} {unit}" (absolute metrics). This strips the known suffix
    built from the chunk's own structured ``currency``/``unit`` fields when
    present, rather than re-guessing ratio-vs-not from the metric name a
    second time.

    Args:
        content (str): The chunk's rendered display string.
        currency (str | None): The chunk's structured currency field.
        unit (str | None): The chunk's structured unit field.

    Returns:
        float | None: The parsed numeric value, or None if the tail could not
        be parsed.
    """
    match = _TAIL_RE.search(content)
    if not match:
        return None
    tail = match.group(1).strip()
    suffix = f" {currency} {unit}" if currency and unit else None
    numeric_part = tail[: -len(suffix)] if suffix and tail.endswith(suffix) else tail
    numeric_part = numeric_part.replace(",", "").strip()
    try:
        return float(numeric_part)
    except ValueError:
        return None


def parse_chunks(chunks: list[Chunk]) -> list[ParsedRecord]:
    """
    Parse a company's bulk-retrieved financial-model chunks into numeric records.

    Chunks whose value cannot be parsed, or that lack a metric breadcrumb or
    period end date, are silently dropped (they cannot contribute to any
    canonical line item).

    Args:
        chunks (list[Chunk]): Chunks from ``search_model.retrieve_financial_model_bulk``.

    Returns:
        list[ParsedRecord]: Successfully parsed records.
    """
    records: list[ParsedRecord] = []
    for chunk in chunks:
        if not chunk.metric or not chunk.period_end_date:
            continue
        value = parse_value_from_content(chunk.content, chunk.currency, chunk.unit)
        if value is None:
            continue
        records.append(
            ParsedRecord(
                metric=chunk.metric,
                segment=chunk.segment,
                period_end_date=chunk.period_end_date,
                is_estimate=chunk.is_estimate == "estimate",
                value=value,
                currency=chunk.currency,
                unit=chunk.unit,
                document_date=chunk.document_date,
            )
        )
    return records


def distinct_metrics(records: list[ParsedRecord]) -> list[str]:
    """Return every distinct metric breadcrumb present in ``records``, in first-seen order."""
    seen: list[str] = []
    seen_set: set[str] = set()
    for record in records:
        if record.metric not in seen_set:
            seen_set.add(record.metric)
            seen.append(record.metric)
    return seen


def classify_metric(metric_path: str, canonical_item: dict) -> bool:
    """
    Check whether a metric breadcrumb matches a canonical line item's keyword rule.

    Args:
        metric_path (str): The full metric breadcrumb, e.g. "Segment Revenue > Data Center".
        canonical_item (dict): A canonical_line_items entry with "keywords"/"exclude_keywords".

    Returns:
        bool: True if any keyword matches and no exclude_keyword matches.
    """
    path_lower = metric_path.lower()
    if any(exclude.lower() in path_lower for exclude in canonical_item.get("exclude_keywords", [])):
        return False
    return any(keyword.lower() in path_lower for keyword in canonical_item.get("keywords", []))


def rank_candidates(metric_paths: list[str], canonical_item: dict) -> list[str]:
    """
    Rank distinct metric breadcrumbs matching one canonical line item.

    Shallower breadcrumbs (fewer " > " segments) rank first, since a
    consolidated total is more likely to sit near the top of the metric
    hierarchy than a segment-level cut (e.g. "Total Revenue" over "Segment
    Revenue > Data Center"). ``indent_depth`` isn't available from the index
    at read time (dropped at ingestion), so the breadcrumb's own segment count
    is the substitute depth signal here. Breadcrumbs containing "total" are
    boosted further; alphabetical order is the final, stable tiebreak.

    Args:
        metric_paths (list[str]): Distinct metric breadcrumbs to rank.
        canonical_item (dict): The canonical_line_items entry to match against.

    Returns:
        list[str]: Matching breadcrumbs, most-likely-correct first.
    """
    matches = [path for path in metric_paths if classify_metric(path, canonical_item)]

    def _sort_key(path: str) -> tuple[int, int, str]:
        depth = path.count(" > ")
        has_total = 0 if "total" in path.lower() else 1
        return (depth, has_total, path)

    return sorted(matches, key=_sort_key)


def time_series_for_metric(records: list[ParsedRecord], metric_path: str) -> list[ParsedRecord]:
    """Return records for one metric breadcrumb, sorted oldest-to-newest by period end date."""
    matching = [record for record in records if record.metric == metric_path]
    return sorted(matching, key=lambda record: record.period_end_date)


def has_consistent_currency(series: list[ParsedRecord]) -> bool:
    """Return True if every record in ``series`` shares the same currency and unit."""
    if not series:
        return True
    currency, unit = series[0].currency, series[0].unit
    return all(record.currency == currency and record.unit == unit for record in series)


def derive_difference_series(
    minuend_series: list[ParsedRecord], subtrahend_series: list[ParsedRecord]
) -> list[ParsedRecord]:
    """
    Derive a synthetic time series as ``minuend - subtrahend`` over matching periods.

    Used for line items not directly present in the model but derivable from
    two that are (e.g. EBIT ~= EBITDA - D&A, or Net Debt = Total Debt - Cash).
    The derived records inherit ``metric``/``segment`` from the minuend
    series (with a "derived" marker) and are only produced for periods
    present in both inputs.

    Args:
        minuend_series (list[ParsedRecord]): The series to subtract from.
        subtrahend_series (list[ParsedRecord]): The series being subtracted.

    Returns:
        list[ParsedRecord]: One derived record per matching period, oldest-first.
    """
    subtrahend_by_period = {record.period_end_date: record for record in subtrahend_series}
    derived: list[ParsedRecord] = []
    for record in minuend_series:
        other = subtrahend_by_period.get(record.period_end_date)
        if other is None:
            continue
        derived.append(
            ParsedRecord(
                metric=f"{record.metric} (derived)",
                segment=record.segment,
                period_end_date=record.period_end_date,
                is_estimate=record.is_estimate or other.is_estimate,
                value=record.value - other.value,
                currency=record.currency,
                unit=record.unit,
            )
        )
    return sorted(derived, key=lambda r: r.period_end_date)


def latest_value(series: list[ParsedRecord], *, reported_only: bool = True) -> float | None:
    """
    Return the most recent value in a time series.

    Args:
        series (list[ParsedRecord]): A metric's time series, oldest-first.
        reported_only (bool): If True, only consider reported (non-estimate) periods.

    Returns:
        float | None: The most recent matching value, or None if the series is empty.
    """
    candidates = [record for record in series if not reported_only or not record.is_estimate]
    return candidates[-1].value if candidates else None


def average_growth_rate(series: list[ParsedRecord], *, reported_only: bool = True) -> float | None:
    """
    Average period-over-period growth rate across a time series.

    Args:
        series (list[ParsedRecord]): A metric's time series, oldest-first.
        reported_only (bool): If True, only consider reported (non-estimate) periods.

    Returns:
        float | None: The average growth rate as a decimal fraction, or None
        if fewer than two usable periods are available.
    """
    candidates = [record for record in series if not reported_only or not record.is_estimate]
    if len(candidates) < 2:  # noqa: PLR2004
        return None
    growth_rates = [
        (candidates[i].value - candidates[i - 1].value) / candidates[i - 1].value
        for i in range(1, len(candidates))
        if candidates[i - 1].value != 0
    ]
    if not growth_rates:
        return None
    return sum(growth_rates) / len(growth_rates)


def average_margin(
    numerator_series: list[ParsedRecord],
    denominator_series: list[ParsedRecord],
    *,
    reported_only: bool = True,
) -> float | None:
    """
    Average ratio of two time series across matching periods (e.g. EBIT margin).

    Args:
        numerator_series (list[ParsedRecord]): E.g. EBIT values, oldest-first.
        denominator_series (list[ParsedRecord]): E.g. Revenue values, oldest-first.
        reported_only (bool): If True, only consider reported (non-estimate) periods.

    Returns:
        float | None: The average ratio, or None if no matching periods exist.
    """
    denominator_by_period = {
        record.period_end_date: record.value
        for record in denominator_series
        if not reported_only or not record.is_estimate
    }
    ratios = [
        record.value / denominator_by_period[record.period_end_date]
        for record in numerator_series
        if (not reported_only or not record.is_estimate)
        and record.period_end_date in denominator_by_period
        and denominator_by_period[record.period_end_date] != 0
    ]
    if not ratios:
        return None
    return sum(ratios) / len(ratios)
