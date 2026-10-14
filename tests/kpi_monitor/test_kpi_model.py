"""Tests for the pure KPI Monitor series maths, prompt text and review parsing."""

from __future__ import annotations

import json

from dcf.dcf_parsing_model import ParsedRecord
from kpi_monitor.kpi_model import (
    KpiFinding,
    KpiReview,
    build_series,
    classify_upload,
    compute_stats,
    format_series_line,
    latest_reported_period,
    parse_review,
    review_to_markdown,
    select_series,
    series_for_metric,
)


def _rec(metric: str, period: str, value: float, *, est: bool = False, doc: str | None = None) -> ParsedRecord:
    return ParsedRecord(metric, None, f"{period}T00:00:00Z", est, value, "EUR", "Millions", doc)


QUARTERS = ["2024-03-31", "2024-06-30", "2024-09-30", "2024-12-31", "2025-03-31"]


def _series(metric: str, values: list[float], **kwargs):
    return build_series([_rec(metric, p, v, **kwargs) for p, v in zip(QUARTERS, values, strict=False)])[0]


class TestBuildSeries:
    def test_newest_upload_wins_and_keeps_prior_estimate(self) -> None:
        records = [
            _rec("Revenue", "2025-03-31", 100.0, est=True, doc="2025-01-15"),
            _rec("Revenue", "2025-03-31", 90.0, doc="2025-04-30"),
            _rec("Revenue", "2024-12-31", 95.0, doc="2025-04-30"),
        ]
        (series,) = build_series(records)
        latest = series.points[-1]
        assert (latest.value, latest.is_estimate, latest.prior_estimate) == (90.0, False, 100.0)

    def test_keeps_only_recent_reported_and_next_estimates(self) -> None:
        records = [_rec("Rev", f"20{y}-12-31", float(y), est=y > 22) for y in range(15, 27)]
        (series,) = build_series(records, max_reported_periods=3, max_estimate_periods=2)
        assert [p.period_end[:4] for p in series.points] == ["2020", "2021", "2022", "2023", "2024"]
        assert [p.is_estimate for p in series.points] == [False, False, False, True, True]

    def test_metric_with_no_reported_period_is_dropped(self) -> None:
        assert build_series([_rec("Guidance", "2026-03-31", 1.0, est=True)]) == []


class TestStats:
    def test_changes_and_streak(self) -> None:
        stats = compute_stats(_series("Rev", [100, 110, 105, 100, 90]))
        assert stats is not None
        assert stats.qoq == -0.1
        assert stats.yoy == -0.1
        assert stats.streak == -3

    def test_three_decreases_in_a_row(self) -> None:
        stats = compute_stats(_series("Rev", [100, 99, 98, 97, 96]))
        assert stats is not None and stats.streak == -4

    def test_zero_base_gives_no_relative_change(self) -> None:
        stats = compute_stats(_series("Rev", [0, 0, 0, 0, 5]))
        assert stats is not None and stats.qoq is None and stats.yoy is None

    def test_surprise_against_prior_estimate(self) -> None:
        records = [
            _rec("Rev", "2025-03-31", 100.0, est=True, doc="2025-01-01"),
            _rec("Rev", "2025-03-31", 90.0, doc="2025-05-01"),
        ]
        stats = compute_stats(build_series(records)[0])
        assert stats is not None and stats.vs_prior_estimate == -0.1


class TestFormatting:
    def test_ratio_metric_quotes_percentage_points(self) -> None:
        line = format_series_line(_series("Gross Margin %", [40.0, 41.0, 40.5, 40.0, 38.0]))
        assert "QoQ -2.00pp" in line
        assert "[" not in line.split(":")[0]

    def test_absolute_metric_quotes_relative_change_and_marks_estimates(self) -> None:
        records = [_rec("Revenue", p, v) for p, v in zip(QUARTERS, [100, 110, 120, 130, 143], strict=False)]
        records.append(_rec("Revenue", "2025-06-30", 150.0, est=True))
        line = format_series_line(build_series(records)[0])
        assert line.startswith("- Revenue [EUR Millions]: 2024-03-31=100.00")
        assert "2025-06-30e=150.00" in line
        assert "QoQ +10.0%" in line and "YoY +43.0%" in line
        assert "4 consecutive increases" in line

    def test_latest_reported_period_ignores_estimates(self) -> None:
        records = [_rec("Rev", "2025-03-31", 1.0), _rec("Rev", "2025-06-30", 2.0, est=True)]
        assert latest_reported_period(build_series(records)) == "2025-03-31"


class TestSelectSeries:
    def test_drops_all_zero_metrics(self) -> None:
        kept, left_out = select_series([_series("Zero", [0, 0, 0, 0, 0]), _series("Rev", [1, 2, 3, 4, 5])], 10)
        assert [s.metric for s in kept] == ["Rev"] and left_out == 0

    def test_keeps_biggest_movers_when_over_budget(self) -> None:
        flat, jumpy, mid = (
            _series("A flat", [100, 100, 100, 100, 101]),
            _series("B jumpy", [100, 100, 100, 100, 200]),
            _series("C mid", [100, 100, 100, 100, 120]),
        )
        kept, left_out = select_series([flat, jumpy, mid], 2)
        assert [s.metric for s in kept] == ["B jumpy", "C mid"] and left_out == 1


class TestParseReview:
    def _raw(self, **finding) -> str:
        base = {"metric": "Revenue", "assessment": "watch", "commentary": "Slowing.", "document_refs": [1, 9]}
        return json.dumps({"summary": "S", "findings": [{**base, **finding}]})

    def test_valid_review_drops_out_of_range_refs(self) -> None:
        review = parse_review(self._raw(), n_documents=3)
        assert review is not None and review.findings[0].document_refs == (1,)

    def test_fences_and_surrounding_prose_are_tolerated(self) -> None:
        assert parse_review("Here you go:\n```json\n" + self._raw() + "\n```", 2) is not None

    def test_findings_are_ordered_by_severity_and_unknown_assessment_is_neutral(self) -> None:
        raw = json.dumps(
            {
                "summary": "",
                "findings": [
                    {"metric": "A", "assessment": "positive", "commentary": "x"},
                    {"metric": "B", "assessment": "bogus", "commentary": "x"},
                    {"metric": "C", "assessment": "concerning", "commentary": "x"},
                    {"metric": "D", "commentary": ""},
                ],
            }
        )
        review = parse_review(raw, 0)
        assert review is not None
        assert [(f.metric, f.assessment) for f in review.findings] == [
            ("C", "concerning"),
            ("A", "positive"),
            ("B", "neutral"),
        ]

    def test_unusable_responses_return_none(self) -> None:
        assert parse_review("not json", 1) is None
        assert parse_review('{"summary": "x"}', 1) is None


class TestHelpers:
    def test_series_for_metric_matches_exactly_then_loosely(self) -> None:
        series = [_series("Income Statement > Revenue", [1, 2, 3, 4, 5])]
        assert series_for_metric(series, "Income Statement > Revenue") is series[0]
        assert series_for_metric(series, "revenue") is series[0]
        assert series_for_metric(series, "Nonexistent") is None

    def test_classify_upload(self) -> None:
        assert classify_upload("MODL_ASML.xlsx") == "financial_model"
        assert classify_upload("Q3 transcript.pdf") == "document"
        assert classify_upload("notes.xlsx") == "document"

    def test_markdown_lists_sources(self) -> None:
        finding = KpiFinding("Revenue", "concerning", "Slowing", "Text.", "100 -> 90", (1,), "Ask why")
        text = review_to_markdown("ASML", "2025-03-31", KpiReview("Summary", (finding,)), {1: "Q1 call"})
        assert "[CONCERNING] Slowing" in text and "[1] Q1 call" in text
