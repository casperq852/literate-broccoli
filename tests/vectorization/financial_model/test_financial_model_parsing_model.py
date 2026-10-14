"""Tests for the pure Bloomberg financial-model sheet parsing/formatting functions."""

from __future__ import annotations

from datetime import datetime

import openpyxl
import pytest

from vectorization.financial_model.financial_model_parsing_model import (
    build_metric_path,
    format_metric_value,
    indent_depth,
    is_row_section_header,
    parse_header,
    parse_periods,
    parse_unit,
    stamp_latest_reported_date,
)
from vectorization.financial_model.types import Period


def _sheet_from_rows(rows: list[tuple]):
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    for row in rows:
        sheet.append(row)
    return sheet


@pytest.fixture
def bloomberg_sheet():
    return _sheet_from_rows(
        [
            ("ASML Holding N.V. - Company Financial Summary", None, None, None, None, None, None),
            ("ASML NA Equity Periodicity:Q Currency:EUR", None, None, None, None, None, None),
            (None, None, None, None, "2025 Q1 (Rep)", "2025 Q2 (Fwd)", "Junk Label"),
            (None, None, None, None, datetime(2025, 3, 31), datetime(2025, 6, 30), "not a date"),
        ]
    )


class TestParseHeader:
    def test_extracts_company_name_ticker_periodicity_currency(self, bloomberg_sheet) -> None:
        header = parse_header(bloomberg_sheet)
        assert header.company_name == "ASML Holding N.V."
        assert header.ticker == "ASML"
        assert header.periodicity == "Q"
        assert header.currency == "EUR"

    def test_defaults_when_metadata_row_absent(self) -> None:
        sheet = _sheet_from_rows([("Some Company",)])
        header = parse_header(sheet)
        assert header.company_name == "Some Company"
        assert header.ticker == ""
        assert header.periodicity == "Q"
        assert header.currency == "USD"

    def test_defaults_on_empty_sheet(self) -> None:
        header = parse_header(_sheet_from_rows([]))
        assert header.company_name == ""
        assert header.ticker == ""


class TestParsePeriods:
    def test_maps_valid_period_columns_only(self, bloomberg_sheet) -> None:
        periods = parse_periods(bloomberg_sheet, data_start_col=4)
        assert set(periods) == {4, 5}
        assert periods[4] == Period(label="2025 Q1 (Rep)", end_date=datetime(2025, 3, 31), is_estimate=False)
        assert periods[5] == Period(label="2025 Q2 (Fwd)", end_date=datetime(2025, 6, 30), is_estimate=True)

    def test_semi_annual_label_is_recognised(self) -> None:
        sheet = _sheet_from_rows(
            [
                (None,),
                (None,),
                (None, None, None, None, "2023 S1 (Rep)"),
                (None, None, None, None, datetime(2023, 6, 30)),
            ]
        )
        periods = parse_periods(sheet, data_start_col=4)
        assert periods[4].label == "2023 S1 (Rep)"
        assert periods[4].is_estimate is False

    def test_returns_empty_when_header_rows_missing(self) -> None:
        assert parse_periods(_sheet_from_rows([(None,)]), data_start_col=4) == {}


class TestParseUnit:
    @pytest.mark.parametrize(
        ("cell_value", "expected"),
        [
            ("In Millions of USD", "Millions"),
            ("In Billions of EUR", "Billions"),
            ("In Thousand of GBP", "Thousands"),
            (None, "Units"),
            ("no unit token here", "Units"),
        ],
    )
    def test_detects_unit_or_falls_back(self, cell_value: str | None, expected: str) -> None:
        sheet = _sheet_from_rows([(None,), (None,), (cell_value,)])
        assert parse_unit(sheet) == expected


class TestIndentDepth:
    def test_counts_leading_spaces(self) -> None:
        assert indent_depth("    Revenue") == 4
        assert indent_depth("Revenue") == 0


class TestBuildMetricPath:
    def test_joins_parents_and_leaf(self) -> None:
        assert build_metric_path(["Income Statement", "Revenue"], "Product Revenue") == (
            "Income Statement > Revenue > Product Revenue"
        )

    def test_drops_consecutive_repeats(self) -> None:
        assert build_metric_path(["Segment Revenue", "Segment Revenue"], "Data Center") == (
            "Segment Revenue > Data Center"
        )

    def test_no_parents(self) -> None:
        assert build_metric_path([], "Revenue") == "Revenue"


class TestFormatMetricValue:
    def test_ratio_renders_as_bare_two_decimal_number(self) -> None:
        value, suffix = format_metric_value(15.678, "Ratios > Operating Margin", "USD", "Millions", ("margin", "eps"))
        assert value == "15.68"
        assert suffix == ""

    def test_whole_float_renders_without_decimals(self) -> None:
        value, suffix = format_metric_value(1_500_000.0, "Revenue", "USD", "Millions", ("margin", "eps"))
        assert value == "1,500,000"
        assert suffix == " USD Millions"

    def test_fractional_float_renders_with_two_decimals(self) -> None:
        value, suffix = format_metric_value(1_500_000.5, "Revenue", "USD", "Millions", ("margin", "eps"))
        assert value == "1,500,000.50"
        assert suffix == " USD Millions"

    def test_int_value_renders_with_thousands_separator(self) -> None:
        value, suffix = format_metric_value(1_500_000, "Revenue", "USD", "Millions", ("margin", "eps"))
        assert value == "1,500,000"
        assert suffix == " USD Millions"


class TestIsRowSectionHeader:
    def test_true_when_no_field_and_no_numeric_value(self) -> None:
        period_columns = {4: Period("2025 Q1 (Rep)", datetime(2025, 3, 31), False)}
        row = ("Income Statement", None, None, None, None)
        assert is_row_section_header(row, period_columns) is True

    def test_false_when_bloomberg_field_present(self) -> None:
        period_columns = {4: Period("2025 Q1 (Rep)", datetime(2025, 3, 31), False)}
        row = ("Revenue", "SALES", None, None, 1234.5)
        assert is_row_section_header(row, period_columns) is False

    def test_false_when_numeric_value_in_period_column(self) -> None:
        period_columns = {4: Period("2025 Q1 (Rep)", datetime(2025, 3, 31), False)}
        row = ("Revenue", None, None, None, 1234.5)
        assert is_row_section_header(row, period_columns) is False


class TestStampLatestReportedDate:
    def test_stamps_max_reported_date_on_every_record(self) -> None:
        older, newer = datetime(2024, 12, 31), datetime(2025, 3, 31)
        records = [
            {"period_end_date": older, "is_estimate": "reported"},
            {"period_end_date": datetime(2025, 6, 30), "is_estimate": "estimate"},
            {"period_end_date": newer, "is_estimate": "reported"},
        ]
        stamp_latest_reported_date(records)
        assert all(record["latest_reported_date"] == newer for record in records)

    def test_none_when_no_reported_records(self) -> None:
        records = [{"period_end_date": datetime(2025, 6, 30), "is_estimate": "estimate"}]
        stamp_latest_reported_date(records)
        assert records[0]["latest_reported_date"] is None
