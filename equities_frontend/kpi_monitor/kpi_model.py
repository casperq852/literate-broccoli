"""
Pure functions for the KPI Monitor: turning a company's Bloomberg model records
into compact per-metric series, describing how each one moved, building the LLM
prompt text, and validating the LLM's review.

Nothing here makes a network call or touches Streamlit. The LLM is what judges
which metrics matter and how significant a move is; the numbers it is shown are
computed here, so every figure in a finding can be checked against the series.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

import yaml

from dcf.dcf_parsing_model import ParsedRecord
from search.search_model import Chunk
from vectorization.financial_model.financial_model_controller import RATIO_TOKENS

DEFAULT_CONTENT_PATH: Path = Path(__file__).resolve().parent.parent / "configs" / "kpi_monitor_content.yaml"

# Bloomberg model exports are recognised at ingestion by this filename prefix
# (config.yaml's financial_model_extraction.filename_prefix).
MODL_FILENAME_PREFIX = "MODL_"

ASSESSMENTS: tuple[str, ...] = ("concerning", "watch", "positive", "neutral")

_MIN_STREAK = 3
_YEAR_AGO_MIN_DAYS, _YEAR_AGO_MAX_DAYS = 350, 380
_LARGE_VALUE = 1000


def load_content(path: Path = DEFAULT_CONTENT_PATH) -> dict[str, Any]:
    """Load the KPI Monitor's prompts and retrieval queries from YAML."""
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


# ── series ──────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class KpiPoint:
    """One period of one metric, as of the newest model that covers it."""

    period_end: str  # YYYY-MM-DD
    value: float
    is_estimate: bool
    # For a reported period: what an older upload of the model estimated for it.
    prior_estimate: float | None = None


@dataclass(frozen=True)
class KpiSeries:
    """One metric's recent history (oldest-first) plus the next few estimated periods."""

    metric: str
    segment: str | None
    currency: str | None
    unit: str | None
    points: tuple[KpiPoint, ...]

    @property
    def reported(self) -> tuple[KpiPoint, ...]:
        """The reported (non-estimate) points, oldest-first."""
        return tuple(point for point in self.points if not point.is_estimate)

    @property
    def ratio_like(self) -> bool:
        """True for ratios/percentages, whose absolute change is quoted in points rather than relative %."""
        name = self.metric.lower()
        return any(token in name for token in RATIO_TOKENS)


def build_series(
    records: list[ParsedRecord], max_reported_periods: int = 12, max_estimate_periods: int = 3
) -> list[KpiSeries]:
    """
    Group parsed model records into one series per metric.

    When the same metric x period was ingested from several uploads of the
    model, the newest upload's value is used and, if that period has since
    been reported, the most recent earlier *estimate* for it is kept as
    ``prior_estimate`` -- that is what lets a reported figure be compared with
    what was expected of it. Metrics with no reported period are dropped.

    Args:
        records (list[ParsedRecord]): Parsed records for one company.
        max_reported_periods (int): Most recent reported periods to keep per metric.
        max_estimate_periods (int): Estimated periods after the last reported one to keep.

    Returns:
        list[KpiSeries]: One series per metric, sorted by metric breadcrumb.
    """
    grouped: dict[str, dict[str, list[ParsedRecord]]] = {}
    for record in records:
        grouped.setdefault(record.metric, {}).setdefault(record.period_end_date[:10], []).append(record)

    series_list: list[KpiSeries] = []
    for metric, by_period in sorted(grouped.items()):
        points: list[KpiPoint] = []
        for period_end, versions in sorted(by_period.items()):
            versions = sorted(versions, key=lambda record: record.document_date or "")
            current = versions[-1]
            prior_estimate = None
            if not current.is_estimate:
                prior_estimate = next((v.value for v in reversed(versions[:-1]) if v.is_estimate), None)
            points.append(KpiPoint(period_end, current.value, current.is_estimate, prior_estimate))

        reported = [point for point in points if not point.is_estimate]
        if not reported:
            continue
        upcoming = [point for point in points if point.is_estimate and point.period_end > reported[-1].period_end]
        first = next(record for versions in by_period.values() for record in versions)
        series_list.append(
            KpiSeries(
                metric=metric,
                segment=first.segment,
                currency=first.currency,
                unit=first.unit,
                points=tuple(reported[-max_reported_periods:] + upcoming[:max_estimate_periods]),
            )
        )
    return series_list


# ── how a series moved ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class SeriesStats:
    """How the latest reported period compares with earlier ones (relative, plus absolute for ratios)."""

    latest_period: str
    qoq: float | None = None
    yoy: float | None = None
    vs_prior_estimate: float | None = None
    qoq_abs: float | None = None
    yoy_abs: float | None = None
    vs_prior_estimate_abs: float | None = None
    # Signed run length of consecutive increases (+) or decreases (-) ending at the latest period.
    streak: int = 0


def _relative_change(new: float, old: float | None) -> float | None:
    if old is None or old == 0:
        return None
    return (new - old) / abs(old)


def compute_stats(series: KpiSeries) -> SeriesStats | None:
    """Compare a series' latest reported period with the prior period, a year ago, and its prior estimate."""
    reported = series.reported
    if not reported:
        return None
    latest = reported[-1]
    previous = reported[-2] if len(reported) > 1 else None

    year_ago = None
    latest_date = date.fromisoformat(latest.period_end)
    for point in reversed(reported[:-1]):
        days = (latest_date - date.fromisoformat(point.period_end)).days
        if _YEAR_AGO_MIN_DAYS <= days <= _YEAR_AGO_MAX_DAYS:
            year_ago = point
            break

    streak = 0
    for newer, older in zip(reversed(reported[1:]), reversed(reported[:-1]), strict=False):
        step = (newer.value > older.value) - (newer.value < older.value)
        if step == 0 or (streak != 0 and (step > 0) != (streak > 0)):
            break
        streak += step

    def _abs(old: float | None) -> float | None:
        return None if old is None else latest.value - old

    return SeriesStats(
        latest_period=latest.period_end,
        qoq=_relative_change(latest.value, previous.value if previous else None),
        yoy=_relative_change(latest.value, year_ago.value if year_ago else None),
        vs_prior_estimate=_relative_change(latest.value, latest.prior_estimate),
        qoq_abs=_abs(previous.value if previous else None),
        yoy_abs=_abs(year_ago.value if year_ago else None),
        vs_prior_estimate_abs=_abs(latest.prior_estimate),
        streak=streak,
    )


def _interest_score(stats: SeriesStats | None) -> float:
    """How much a series moved, for trimming only when there are more metrics than fit in the prompt."""
    if stats is None:
        return 0.0
    moves = [abs(change) for change in (stats.qoq, stats.yoy, stats.vs_prior_estimate) if change is not None]
    return max(moves, default=0.0) + (0.1 * abs(stats.streak) if abs(stats.streak) >= _MIN_STREAK else 0.0)


def select_series(series_list: list[KpiSeries], max_series: int) -> tuple[list[KpiSeries], int]:
    """
    Drop all-zero metrics, then keep the ``max_series`` that moved the most if there are more than that.

    The LLM still decides what matters; this only keeps the prompt within a
    budget for very large models. Original (breadcrumb) order is preserved.

    Returns:
        tuple[list[KpiSeries], int]: The kept series and how many informative series were left out.
    """
    informative = [s for s in series_list if any(point.value != 0 for point in s.reported)]
    if len(informative) <= max_series:
        return informative, 0
    ranked = sorted(informative, key=lambda s: _interest_score(compute_stats(s)), reverse=True)
    keep = {s.metric for s in ranked[:max_series]}
    return [s for s in informative if s.metric in keep], len(informative) - max_series


# ── prompt text ─────────────────────────────────────────────────────────────────


def format_value(value: float, decimals: int = 0) -> str:
    """Format a metric value; large values (1,000 and up) never show decimals."""
    return f"{value:,.{0 if abs(value) >= _LARGE_VALUE else decimals}f}"


def series_decimals(series: KpiSeries) -> int:
    """Decimals to show for every value of a series: 2 if any value is fractional (and below 1,000), else 0."""
    values = [point.value for point in series.points]
    values += [point.prior_estimate for point in series.points if point.prior_estimate is not None]
    return 2 if any(v != round(v) and abs(v) < _LARGE_VALUE for v in values) else 0


def _fmt_change(label: str, relative: float | None, absolute: float | None, *, ratio_like: bool) -> str | None:
    if relative is None:
        return None
    if ratio_like and absolute is not None:
        return f"{label} {absolute:+.2f}pp"
    return f"{label} {relative:+.1%}"


def format_series_line(series: KpiSeries) -> str:
    """Render one series as a single prompt line: values by period end date, then its computed moves."""
    decimals = series_decimals(series)
    values = ", ".join(
        f"{p.period_end}{'e' if p.is_estimate else ''}={format_value(p.value, decimals)}" for p in series.points
    )
    unit = " ".join(part for part in (series.currency, series.unit) if part)
    header = f"- {series.metric}" + (f" [{unit}]" if unit and not series.ratio_like else "")

    stats = compute_stats(series)
    notes: list[str] = []
    if stats:
        for label, rel, abs_ in (
            ("QoQ", stats.qoq, stats.qoq_abs),
            ("YoY", stats.yoy, stats.yoy_abs),
            ("vs prior estimate", stats.vs_prior_estimate, stats.vs_prior_estimate_abs),
        ):
            if note := _fmt_change(label, rel, abs_, ratio_like=series.ratio_like):
                notes.append(note)
        if abs(stats.streak) >= _MIN_STREAK:
            notes.append(f"{abs(stats.streak)} consecutive {'increases' if stats.streak > 0 else 'decreases'}")
    return f"{header}: {values}" + (f"  | {'; '.join(notes)}" if notes else "")


def format_series_table(series_list: list[KpiSeries]) -> str:
    """Render all series for the prompt, one line each ('e' after a date marks an estimate)."""
    return "\n".join(format_series_line(series) for series in series_list)


def latest_reported_period(series_list: list[KpiSeries]) -> str | None:
    """The newest reported period end date across all series, or None if there are none."""
    dates = [s.reported[-1].period_end for s in series_list if s.reported]
    return max(dates, default=None)


def build_user_prompt(
    template: str,
    *,
    company: str,
    ticker: str,
    as_of: str,
    series_table: str,
    omitted_note: str,
    documents: str,
    focus_notes: str,
) -> str:
    """Fill the user-prompt template (placeholders: company, ticker, as_of, series_table, omitted_note, documents, focus_notes)."""
    return template.format(
        company=company,
        ticker=ticker,
        as_of=as_of,
        series_table=series_table,
        omitted_note=omitted_note,
        documents=documents or "(no earnings transcripts or other documents are indexed for this company)",
        focus_notes=focus_notes.strip() or "(none)",
    )


# ── the LLM's review ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class KpiFinding:
    """One KPI the LLM flagged, with its judgement and the sources it relied on."""

    metric: str
    assessment: str  # one of ASSESSMENTS
    headline: str
    commentary: str
    evidence: str
    document_refs: tuple[int, ...]
    follow_up: str


@dataclass(frozen=True)
class KpiReview:
    """The LLM's overall read of the quarter plus its findings, most severe first."""

    summary: str
    findings: tuple[KpiFinding, ...]


def _extract_json(raw_text: str) -> Any | None:
    cleaned = re.sub(r"^```(?:json)?\s*\n?", "", raw_text.strip())
    cleaned = re.sub(r"\n?```\s*$", "", cleaned).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        return json.loads(cleaned[start : end + 1])
    except json.JSONDecodeError:
        return None


def _valid_refs(raw: Any, n_documents: int) -> tuple[int, ...]:
    """Keep only integer references that point at one of the ``n_documents`` provided sources."""
    if not isinstance(raw, list):
        return ()
    refs = {int(ref) for ref in raw if isinstance(ref, (int, str)) and str(ref).strip().isdigit()}
    return tuple(sorted(ref for ref in refs if 1 <= ref <= n_documents))


def parse_review(raw_text: str, n_documents: int) -> KpiReview | None:
    """
    Parse and validate the LLM's JSON review.

    Findings missing a metric or commentary are dropped; an unknown assessment
    becomes ``neutral``; document references outside ``1..n_documents`` are
    removed so a citation can never point at a source that was not provided.

    Args:
        raw_text (str): The raw LLM response.
        n_documents (int): How many numbered documents the prompt contained.

    Returns:
        KpiReview | None: The review (findings ordered by severity), or None if the response was not usable.
    """
    parsed = _extract_json(raw_text)
    if not isinstance(parsed, dict) or not isinstance(parsed.get("findings"), list):
        return None

    findings: list[KpiFinding] = []
    for item in parsed["findings"]:
        if not isinstance(item, dict):
            continue
        metric, commentary = str(item.get("metric") or "").strip(), str(item.get("commentary") or "").strip()
        if not metric or not commentary:
            continue
        assessment = str(item.get("assessment") or "").strip().lower()
        findings.append(
            KpiFinding(
                metric=metric,
                assessment=assessment if assessment in ASSESSMENTS else "neutral",
                headline=str(item.get("headline") or "").strip(),
                commentary=commentary,
                evidence=str(item.get("evidence") or "").strip(),
                document_refs=_valid_refs(item.get("document_refs"), n_documents),
                follow_up=str(item.get("follow_up") or "").strip(),
            )
        )
    findings.sort(key=lambda finding: ASSESSMENTS.index(finding.assessment))
    return KpiReview(summary=str(parsed.get("summary") or "").strip(), findings=tuple(findings))


def series_for_metric(series_list: list[KpiSeries], metric: str) -> KpiSeries | None:
    """Find a series by the metric name the LLM quoted (exact, then case-insensitive, then breadcrumb suffix)."""
    wanted = metric.strip().lower()
    for series in series_list:
        if series.metric.lower() == wanted:
            return series
    return next((s for s in series_list if s.metric.lower().endswith(f"> {wanted}")), None)


def classify_upload(filename: str) -> str:
    """Return 'financial_model' for a MODL workbook, else 'document' (by filename, as at ingestion)."""
    name = Path(filename).name
    if name.startswith(MODL_FILENAME_PREFIX) and name.lower().endswith(".xlsx"):
        return "financial_model"
    return "document"


def report_filename(ticker: str, as_of: str, extension: str) -> str:
    """File name for an exported review, e.g. ``KPI_review_asml_2025-09-30.docx``."""
    return f"KPI_review_{storage_folder(ticker)}_{as_of}.{extension}"


def review_to_markdown(company: str, as_of: str, review: KpiReview, source_labels: dict[int, str]) -> str:
    """Render a review as Markdown for download, with its sources listed."""
    lines = [f"# KPI review: {company}", f"Latest reported period: {as_of}", "", review.summary, ""]
    for finding in review.findings:
        title = finding.headline or finding.metric
        lines.append(f"## [{finding.assessment.upper()}] {title}")
        lines.append(f"*{finding.metric}*")
        lines += ["", finding.commentary]
        if finding.evidence:
            lines += ["", f"Evidence: {finding.evidence}"]
        if finding.follow_up:
            lines += ["", f"Follow-up: {finding.follow_up}"]
        if finding.document_refs:
            lines += ["", "Sources: " + ", ".join(f"[{ref}]" for ref in finding.document_refs)]
        lines.append("")
    if source_labels:
        lines.append("## Sources")
        lines += [f"[{index}] {label}" for index, label in sorted(source_labels.items())]
    return "\n".join(lines)


# ── saved reviews ───────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class SourceFile:
    """A file the analyst added (uploaded and ingested) while preparing a review."""

    filename: str
    kind: str  # 'financial_model' | 'document'
    path: str  # Data Lake path, so the file can be fetched again
    segments: int = 0


@dataclass(frozen=True)
class KpiAnalysis:
    """
    A finished review plus everything it was based on.

    Holds the model's own figures and the document passages the LLM saw, so
    the view can show the evidence next to each claim -- and so a saved review
    can be reopened later exactly as it was, without the live index.
    """

    review: KpiReview
    as_of: str
    series: list[KpiSeries]
    documents: list[Chunk]
    omitted_series: int = 0
    focus_notes: str = ""
    sources: tuple[SourceFile, ...] = field(default_factory=tuple)
    created_at: str = ""  # ISO-8601 UTC; empty until the review is made


_SCHEMA_VERSION = 1
_ID_TIME_FORMAT = "%Y%m%dT%H%M%SZ"
_ID_SEPARATOR = "__"


def storage_folder(ticker: str) -> str:
    """A filesystem-safe folder name for a ticker (``NOVO-B DC`` -> ``novo-b-dc``)."""
    return re.sub(r"[^a-z0-9]+", "-", ticker.lower()).strip("-") or "unknown"


def make_review_id(created_at: str, as_of: str) -> str:
    """Build a sortable id carrying the creation time and latest period, so listings need no file reads."""
    stamp = datetime.fromisoformat(created_at).strftime(_ID_TIME_FORMAT)
    return f"{stamp}{_ID_SEPARATOR}{as_of}"


def review_label(review_id: str) -> str:
    """Human-readable label for a saved review id, e.g. ``2026-10-14 08:55 UTC · period 2025-03-31``."""
    stamp, _, as_of = review_id.partition(_ID_SEPARATOR)
    try:
        when = datetime.strptime(stamp, _ID_TIME_FORMAT).strftime("%Y-%m-%d %H:%M UTC")  # noqa: DTZ007
    except ValueError:
        return review_id
    return f"{when} · period {as_of}" if as_of else when


def ids_to_prune(review_ids: list[str], keep: int) -> list[str]:
    """The oldest ids beyond the ``keep`` newest (ids sort chronologically)."""
    return sorted(review_ids, reverse=True)[keep:]


def analysis_to_dict(analysis: KpiAnalysis, ticker: str, company: str) -> dict[str, Any]:
    """Flatten an analysis to JSON-safe data for storage."""
    return {
        "schema": _SCHEMA_VERSION,
        "ticker": ticker,
        "company": company,
        "created_at": analysis.created_at,
        "as_of": analysis.as_of,
        "focus_notes": analysis.focus_notes,
        "omitted_series": analysis.omitted_series,
        "review": asdict(analysis.review),
        "series": [asdict(series) for series in analysis.series],
        "documents": [asdict(chunk) for chunk in analysis.documents],
        "sources": [asdict(source) for source in analysis.sources],
    }


def analysis_from_dict(data: dict[str, Any]) -> KpiAnalysis:
    """Rebuild an analysis from ``analysis_to_dict`` output."""
    review = data["review"]
    findings = tuple(
        KpiFinding(
            metric=finding["metric"],
            assessment=finding.get("assessment", "neutral"),
            headline=finding.get("headline", ""),
            commentary=finding.get("commentary", ""),
            evidence=finding.get("evidence", ""),
            document_refs=tuple(finding.get("document_refs", ())),
            follow_up=finding.get("follow_up", ""),
        )
        for finding in review.get("findings", [])
    )
    series = [
        KpiSeries(
            metric=item["metric"],
            segment=item.get("segment"),
            currency=item.get("currency"),
            unit=item.get("unit"),
            points=tuple(KpiPoint(**point) for point in item.get("points", [])),
        )
        for item in data.get("series", [])
    ]
    documents = [
        Chunk(
            content=chunk["content"],
            metric=chunk.get("metric"),
            period_label=chunk.get("period_label"),
            period_end_date=chunk.get("period_end_date"),
            is_estimate=chunk.get("is_estimate"),
            document_type=chunk.get("document_type"),
            document_date=chunk.get("document_date"),
            publisher=chunk.get("publisher"),
            score=chunk.get("score", 0.0),
            segment=chunk.get("segment"),
            currency=chunk.get("currency"),
            unit=chunk.get("unit"),
            companies=tuple(chunk.get("companies", ())),
        )
        for chunk in data.get("documents", [])
    ]
    return KpiAnalysis(
        review=KpiReview(summary=review.get("summary", ""), findings=findings),
        as_of=data.get("as_of", "unknown"),
        series=series,
        documents=documents,
        omitted_series=data.get("omitted_series", 0),
        focus_notes=data.get("focus_notes", ""),
        sources=tuple(SourceFile(**source) for source in data.get("sources", [])),
        created_at=data.get("created_at", ""),
    )
