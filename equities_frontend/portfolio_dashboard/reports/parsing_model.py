"""
Bloomberg report parsers.

Every Bloomberg CSV export shares the same outer structure:
  1. Key: Value metadata lines (report name, portfolio, benchmark, dates …)
  2. A CSV table that begins with a "Level,Name,…" header row
  3. A trailing "Disclaimer: …" line that must be stripped

Column mapping is done by name, not position, so future Bloomberg template
changes that reorder columns do not break parsing.

Public API
----------
load_attribution_report(source)   → AttributionReport
load_te_report(source)            → TrackingErrorReport
load_sector_attribution(source)   → SectorAttributionReport
detect_and_load(source)           → one of the above (auto-detects type)

``source`` may be a file path (str / Path) or raw bytes from
``st.file_uploader(...).read()``.
"""

from __future__ import annotations

import csv
import io
import logging
import unicodedata
from collections.abc import Callable
from pathlib import Path

from portfolio_dashboard.reports.reports_model import (
    ALL_FACTOR_NAMES,
    AttributionRecord,
    AttributionReport,
    FactorAttributionRecord,
    FactorAttributionReport,
    Report,
    ReportMetadata,
    SectorAttributionRecord,
    SectorAttributionReport,
    TERiskFactorRecord,
    TERiskFactorReport,
    TrackingErrorRecord,
    TrackingErrorReport,
)

logger = logging.getLogger(__name__)

_PathOrBytes = str | Path | bytes


# ── internal helpers ───────────────────────────────────────────────────────────


def _opt_float(cell: str) -> float | None:
    """Return None for an empty Bloomberg cell, float otherwise."""
    v = cell.strip()
    return float(v) if v else None


def _read_lines(source: _PathOrBytes) -> list[str]:
    """Decode bytes (Streamlit upload) or read a local file."""
    if isinstance(source, bytes):
        return source.decode("utf-8-sig").splitlines()
    return Path(source).read_text(encoding="utf-8-sig").splitlines()


def _parse_header(lines: list[str]) -> tuple[dict[str, str], int]:
    """
    Scan the Key: Value metadata block at the top of the file.

    Returns (metadata_dict, index_of_data_header_row).
    Recognises two data-header styles:
      "Level,"       — standard Bloomberg PORT attribution / TE reports
      "Factor Name," — Bloomberg TE factor risk decomposition report
    """
    meta: dict[str, str] = {}
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("Level,") or stripped.startswith("Factor Name,"):
            return meta, i
        if ":" in stripped:
            key, _, val = stripped.partition(":")
            meta[key.strip()] = val.strip()
    raise ValueError("Bloomberg CSV: no 'Level,' or 'Factor Name,' header row found — " "is this a valid export?")


def _data_rows(lines: list[str], data_start: int) -> list[list[str]]:
    """
    Parse CSV rows from data_start up to (not including) the Disclaimer line.
    The first returned row is the column-name header.
    """
    relevant: list[str] = []
    for line in lines[data_start:]:
        if line.strip().startswith("Disclaimer"):
            break
        relevant.append(line)
    return list(csv.reader(relevant))


def _build_metadata(meta: dict[str, str]) -> ReportMetadata:
    """Build a :class:`ReportMetadata` from parsed key/value header lines.

    Args:
        meta: Raw header fields keyed by their Bloomberg label.

    Returns:
        ReportMetadata: The populated, immutable report header.
    """
    period = meta.get("Period", "")
    period_start = period_end = None
    if " - " in period:
        start, _, end = period.partition(" - ")
        period_start, period_end = start.strip(), end.strip()
    return ReportMetadata(
        report_name=meta.get("Report Name", ""),
        portfolio=meta.get("Portfolio", ""),
        benchmark=meta.get("Benchmark", ""),
        run_date=meta.get("Run Date", ""),
        base_currency=meta.get("Base Currency", ""),
        period_start=period_start,
        period_end=period_end,
        as_of=meta.get("As of"),
        classification=meta.get("Classification") or None,
    )


def _col_getter(header_row: list[str]) -> Callable[[list[str], str], float | None]:
    """Return a callable f(row, col_name) → Optional[float] using name-based lookup."""
    col = {name.strip(): i for i, name in enumerate(header_row)}

    def f(row: list[str], name: str) -> float | None:
        """Return the float value of column ``name`` in ``row``, else None."""
        idx = col.get(name)
        return _opt_float(row[idx]) if idx is not None and idx < len(row) else None

    return f


def _is_data_row(row: list[str]) -> bool:
    """True when row[0] looks like a Bloomberg level integer (0, 1, 2)."""
    if not row or not row[0].strip():
        return False
    try:
        int(row[0].strip())
        return True
    except ValueError:
        return False


# ── public loaders ─────────────────────────────────────────────────────────────


def load_attribution_report(source: _PathOrBytes) -> AttributionReport:
    """
    Parse a Bloomberg portfolio attribution CSV.

    Expected columns (Bloomberg Port Attribution report):
      Level, Name, Avg % Wgt[P/B/D], Tot Rtn[P/B/D],
      Factor Rtn (Contr)[P/B/D], Selection Rtn (Contr)[P/B/D],
      Time Rtn (Contr)[P/B/D], CTR[P/B/D]
    """
    lines = _read_lines(source)
    meta_dict, data_start = _parse_header(lines)
    rows = _data_rows(lines, data_start)

    metadata = _build_metadata(meta_dict)
    f = _col_getter(rows[0])

    records: list[AttributionRecord] = []
    for row in rows[1:]:
        if not _is_data_row(row):
            continue
        records.append(
            AttributionRecord(
                level=int(row[0].strip()),
                name=row[1].strip(),
                avg_wgt_p=f(row, "Avg % Wgt[P]"),
                avg_wgt_b=f(row, "Avg % Wgt[B]"),
                avg_wgt_d=f(row, "Avg % Wgt[D]"),
                tot_rtn_p=f(row, "Tot Rtn[P]"),
                tot_rtn_b=f(row, "Tot Rtn[B]"),
                tot_rtn_d=f(row, "Tot Rtn[D]"),
                factor_p=f(row, "Factor Rtn (Contr)[P]"),
                factor_b=f(row, "Factor Rtn (Contr)[B]"),
                factor_d=f(row, "Factor Rtn (Contr)[D]"),
                selection_p=f(row, "Selection Rtn (Contr)[P]"),
                selection_b=f(row, "Selection Rtn (Contr)[B]"),
                selection_d=f(row, "Selection Rtn (Contr)[D]"),
                timing_p=f(row, "Time Rtn (Contr)[P]"),
                timing_b=f(row, "Time Rtn (Contr)[B]"),
                timing_d=f(row, "Time Rtn (Contr)[D]"),
                ctr_p=f(row, "CTR[P]"),
                ctr_b=f(row, "CTR[B]"),
                ctr_d=f(row, "CTR[D]"),
            )
        )

    total = next((r for r in records if r.level == 0), None)
    if total is None:
        raise ValueError("Attribution report: level-0 total row not found")

    # Port Attribution CSVs with BREAKDOWN BY:GICS Sector are 3-level:
    # level 0 = total, level 1 = GICS sector, level 2 = security.
    # Without a sector breakdown the format is 2-level: level 1 = security.
    max_level = max((r.level for r in records), default=0)
    sec_min_level = 2 if max_level >= 2 else 1
    securities = [r for r in records if r.level >= sec_min_level]
    logger.debug("Loaded attribution report: %d securities", len(securities))
    return AttributionReport(metadata=metadata, total=total, securities=securities)


def load_te_report(source: _PathOrBytes) -> TrackingErrorReport:
    """
    Parse a Bloomberg tracking-error / ex-ante risk report CSV.

    Expected columns:
      Level, Name, % Wgt[P], % Wgt[D], Gross Active Weight[D],
      Total Risk[D], Contribution (%)[P], Beta (ex-ante)[P]
    """
    lines = _read_lines(source)
    meta_dict, data_start = _parse_header(lines)
    rows = _data_rows(lines, data_start)

    metadata = _build_metadata(meta_dict)
    f = _col_getter(rows[0])

    records: list[TrackingErrorRecord] = []
    for row in rows[1:]:
        if not _is_data_row(row):
            continue
        records.append(
            TrackingErrorRecord(
                level=int(row[0].strip()),
                name=row[1].strip(),
                wgt_p=f(row, "% Wgt[P]"),
                wgt_d=f(row, "% Wgt[D]"),
                gross_active_wgt=f(row, "Gross Active Weight[D]"),
                total_risk_d=f(row, "Total Risk[D]"),
                contribution_p=f(row, "Contribution (%)[P]"),
                beta_p=f(row, "Beta (ex-ante)[P]"),
                non_factor_contrib=f(row, "Non-Factor Contrib (%)"),
            )
        )

    total = next((r for r in records if r.level == 0), None)
    if total is None:
        raise ValueError("TE report: level-0 total row not found")

    max_level = max((r.level for r in records), default=0)
    if max_level >= 3:
        # 4-level GICS report: L0=total, L1=sector, L2=industry group, L3=security
        sectors = [r for r in records if r.level == 1]
        industry_groups = [r for r in records if r.level == 2]
        securities = [r for r in records if r.level == 3]
    else:
        # Legacy 2-level report: L0=total, L1=security
        sectors = []
        industry_groups = []
        securities = [r for r in records if r.level == 1]

    logger.debug(
        "Loaded TE report: %d sectors, %d industry groups, %d securities",
        len(sectors),
        len(industry_groups),
        len(securities),
    )
    return TrackingErrorReport(
        metadata=metadata,
        total=total,
        all_records=records,
        sectors=sectors,
        industry_groups=industry_groups,
        securities=securities,
    )


def load_sector_attribution(source: _PathOrBytes) -> SectorAttributionReport:
    """
    Parse a Bloomberg GICS sector attribution CSV.

    Expected columns:
      Level, Name, Avg % Wgt[P/B/D], CTR[P/B/D], Tot Rtn[P/B/D],
      Alloc, Selec, Inter, Tot Attr  (plus start/end weights and currency CTR
      columns that are read but not stored in the model).

    Level 1 = GICS sector aggregate; Level 2 = individual security.
    """
    lines = _read_lines(source)
    meta_dict, data_start = _parse_header(lines)
    rows = _data_rows(lines, data_start)

    metadata = _build_metadata(meta_dict)
    f = _col_getter(rows[0])

    total: SectorAttributionRecord | None = None
    sectors: list[SectorAttributionRecord] = []
    securities: list[SectorAttributionRecord] = []

    for row in rows[1:]:
        if not _is_data_row(row):
            continue
        level = int(row[0].strip())
        record = SectorAttributionRecord(
            level=level,
            name=row[1].strip(),
            avg_wgt_p=f(row, "Avg % Wgt[P]"),
            avg_wgt_b=f(row, "Avg % Wgt[B]"),
            avg_wgt_d=f(row, "Avg % Wgt[D]"),
            ctr_p=f(row, "CTR[P]"),
            ctr_b=f(row, "CTR[B]"),
            ctr_d=f(row, "CTR[D]"),
            tot_rtn_p=f(row, "Tot Rtn[P]"),
            tot_rtn_b=f(row, "Tot Rtn[B]"),
            tot_rtn_d=f(row, "Tot Rtn[D]"),
            allocation=f(row, "Alloc"),
            selection=f(row, "Selec"),
            interaction=f(row, "Inter"),
            total_attribution=f(row, "Tot Attr"),
        )
        if level == 0:
            total = record
        elif level == 1:
            sectors.append(record)
        else:
            securities.append(record)

    if total is None:
        raise ValueError("Sector attribution report: level-0 total row not found")

    logger.debug(
        "Loaded sector attribution: %d sectors, %d securities",
        len(sectors),
        len(securities),
    )
    return SectorAttributionReport(metadata=metadata, total=total, sectors=sectors, securities=securities)


def load_factor_attribution(source: _PathOrBytes) -> FactorAttributionReport:
    """
    Parse a Bloomberg multi-factor attribution CSV.

    Expected columns: Level, Name, Avg % Wgt[P/B/D], Tot Rtn[P/B/D],
      Factor Rtn (Contr)[P/B/D], Selection Rtn (Contr)[P/B/D],
      Time Rtn (Contr)[P/B/D], CTR[P/B/D],
      Currency[P/B/D], Beta[P/B/D], Momentum[P/B/D], … (all EU factors)

    Level 0 = portfolio total, Level 1 = GICS sector, Level 2 = security.
    """
    lines = _read_lines(source)
    meta_dict, data_start = _parse_header(lines)
    rows = _data_rows(lines, data_start)

    metadata = _build_metadata(meta_dict)
    f = _col_getter(rows[0])

    total: FactorAttributionRecord | None = None
    sectors: list[FactorAttributionRecord] = []
    securities: list[FactorAttributionRecord] = []

    for row in rows[1:]:
        if not _is_data_row(row):
            continue
        level = int(row[0].strip())
        # Collect active contribution for every known factor column
        factor_ctrs: dict[str, float] = {}
        for fname in ALL_FACTOR_NAMES:
            val = f(row, f"{fname}[D]")
            if val is not None:
                factor_ctrs[fname] = val

        record = FactorAttributionRecord(
            level=level,
            name=row[1].strip(),
            avg_wgt_p=f(row, "Avg % Wgt[P]"),
            avg_wgt_b=f(row, "Avg % Wgt[B]"),
            avg_wgt_d=f(row, "Avg % Wgt[D]"),
            tot_rtn_p=f(row, "Tot Rtn[P]"),
            tot_rtn_b=f(row, "Tot Rtn[B]"),
            tot_rtn_d=f(row, "Tot Rtn[D]"),
            ctr_p=f(row, "CTR[P]"),
            ctr_b=f(row, "CTR[B]"),
            ctr_d=f(row, "CTR[D]"),
            factor_d=f(row, "Factor Rtn (Contr)[D]"),
            selection_d=f(row, "Selection Rtn (Contr)[D]"),
            timing_d=f(row, "Time Rtn (Contr)[D]"),
            end_mkt_val=f(row, "End Mkt Val"),
            factor_ctrs_d=factor_ctrs,
        )
        if level == 0:
            total = record
        elif level == 1:
            sectors.append(record)
        else:
            securities.append(record)

    if total is None:
        raise ValueError("Factor attribution: level-0 total row not found")

    logger.debug(
        "Loaded factor attribution: %d sectors, %d securities",
        len(sectors),
        len(securities),
    )
    return FactorAttributionReport(metadata=metadata, total=total, sectors=sectors, securities=securities)


def load_te_factors(source: _PathOrBytes) -> TERiskFactorReport:
    """
    Parse a Bloomberg TE factor risk decomposition CSV.

    Expected columns:
      Factor Name, Portfolio Exposure, Benchmark Exposure, Active Exposure,
      Factor Vol (Std %), Tot. Active (Std), Marginal (X100), Contribution %

    Factor names use a "Category:Label" format, e.g. "Beta:EU Beta",
    "Industrials:EU InduEquip", "Currency:GBP".
    """
    lines = _read_lines(source)
    meta_dict, data_start = _parse_header(lines)
    rows = _data_rows(lines, data_start)

    metadata = _build_metadata(meta_dict)
    f = _col_getter(rows[0])

    factors: list[TERiskFactorRecord] = []
    for row in rows[1:]:
        if not row or not row[0].strip():
            continue
        factor_name = row[0].strip()
        if ":" in factor_name:
            category, _, factor_label = factor_name.partition(":")
        else:
            category = factor_name
            factor_label = factor_name

        factors.append(
            TERiskFactorRecord(
                factor_name=factor_name,
                category=category.strip(),
                factor_label=factor_label.strip(),
                portfolio_exposure=f(row, "Portfolio Exposure"),
                benchmark_exposure=f(row, "Benchmark Exposure"),
                active_exposure=f(row, "Active Exposure"),
                factor_vol=f(row, "Factor Vol (Std %)"),
                tot_active_std=f(row, "Tot. Active (Std)"),
                marginal=f(row, "Marginal (X100)"),
                contribution_pct=f(row, "Contribution %"),
            )
        )

    logger.debug("Loaded TE factor report: %d factors", len(factors))
    return TERiskFactorReport(metadata=metadata, factors=factors)


def detect_and_load(source: _PathOrBytes) -> Report:
    """
    Auto-detect the Bloomberg report type by inspecting the data column headers.

    Detection is content-based — file name and Bloomberg "Report Name" are not
    used — so any naming convention works.

    Priority (checked in order):
    1. Data header starts with "Factor Name,"   → TERiskFactorReport
    2. Data columns contain "Total Risk[D]"     → TrackingErrorReport
    3. Data columns contain individual factor columns (e.g. "EU Beta[D]") → FactorAttributionReport
    4. Data columns contain Brinson columns ("Alloc", "Selec", "Inter") → SectorAttributionReport
    5. Otherwise                                → AttributionReport
    """
    lines = _read_lines(source)
    meta_dict, data_start = _parse_header(lines)

    data_header = lines[data_start].strip() if data_start < len(lines) else ""

    # 1. TE Factors: unique header format — no "Level" column
    if data_header.startswith("Factor Name,"):
        return load_te_factors(source)

    # Parse the column names from the data header row
    cols = {c.strip() for c in data_header.split(",")}

    # 2. TE Report: always has a "Total Risk[D]" column
    if "Total Risk[D]" in cols:
        return load_te_report(source)

    # 3. Factor Attribution: has individual factor columns such as "EU Beta[D]".
    #    "Currency" is excluded: Port Attribution also exports Currency[D] as a
    #    currency-effect column, so it cannot serve as a reliable detection signal.
    #    All other names in ALL_FACTOR_NAMES (EU Beta, EU Momentum, …) are unique
    #    to Factor Attribution exports.
    _fa_names_csv = [n for n in ALL_FACTOR_NAMES if n != "Currency"]
    _factor_cols_csv = {f"{n}[D]" for n in _fa_names_csv}
    if _factor_cols_csv & cols:
        return load_factor_attribution(source)

    # 4. Sector Attribution: classic Brinson report — identified by the "Inter"
    #    (interaction effect) column. Port Attribution uses a different decomposition
    #    (Curr, Transact, Lev, Px Diff) and never has an "Inter" column.
    classification = meta_dict.get("Classification", "").strip()
    if "Inter" in cols and classification:
        return load_sector_attribution(source)

    # 5. Port Attribution (incl. Alpha Portf Attribution): fallback
    return load_attribution_report(source)


# ── Excel support ──────────────────────────────────────────────────────────────
# Bloomberg PORT Excel exports differ significantly from CSV:
#   • Metadata: cells like "PORT:EQUS CORE_ADAAF", "BMRK:…", "RUN DATE:…"
#   • Columns: two header rows — merged group name + "Port" / "Bench" / "+/-"
#   • Hierarchy: Excel row-outline depth, NOT a "Level" column
#   • Dates: written as "July 13, 2026" (full month name)


def _xl_parse_date(s: str) -> str:
    """Parse any Bloomberg date format and return MM/DD/YYYY."""
    import datetime as _dt

    for fmt in ("%B %d, %Y", "%b %d, %Y", "%m/%d/%Y", "%Y-%m-%d", "%d/%m/%Y"):
        try:
            return _dt.datetime.strptime(s.strip(), fmt).strftime("%m/%d/%Y")
        except ValueError:
            continue
    return s.strip()


def _xl_num(v) -> float | None:
    """Excel cell value → float, or None for empty / non-numeric."""
    if v is None:
        return None
    try:
        return float(str(v).strip().replace(",", "").replace(" ", ""))
    except (ValueError, TypeError):
        return None


def _load_bloomberg_excel_sheet(file_bytes: bytes, sheet_name: str) -> Report:
    """
    Parse one Bloomberg PORT Excel worksheet into a report object.

    Bloomberg Excel layout (observed):
      Row 1    : "Bloomberg"
      Row 3    : Report type string ("PORTFOLIO_ATTRIBUTION", "FACTOR_ATTRIBUTION", …)
      Rows 4-5 : Metadata — "PORT:…", "BMRK:…", "RUN DATE:…", "CURR:…", "BREAKDOWN BY:…"
      Row 7    : Column group headers (merged across 3 cols each)
      Row 8    : Sub-headers — "Port", "Bench", "+/-" under each group
      Row 9+   : Data; hierarchy encoded as Excel row-outline depth
    """
    try:
        import openpyxl
    except ImportError as exc:
        raise ImportError("openpyxl is required for Excel support: pip install openpyxl") from exc

    # Full load (NOT read_only) — needed to read row-outline levels and merged-cell ranges
    wb = openpyxl.load_workbook(io.BytesIO(file_bytes), data_only=True)
    if sheet_name not in wb.sheetnames:
        wb.close()
        raise ValueError(f"Tabblad '{sheet_name}' niet gevonden. " f"Beschikbare tabs: {', '.join(wb.sheetnames)}")
    ws = wb[sheet_name]
    try:
        return _parse_bloomberg_xl_ws(ws, sheet_name)
    finally:
        wb.close()


def _parse_bloomberg_xl_ws(ws, sheet_name: str) -> Report:  # noqa: C901 (complexity)
    """Core parsing logic for a Bloomberg PORT Excel worksheet."""

    # ── 1. Extract metadata ────────────────────────────────────────────────────
    _META = {
        "PORT:": "portfolio",
        "BMRK:": "benchmark",
        "RUN DATE:": "run_date",
        "CURR:": "base_currency",
        "BREAKDOWN BY:": "classification",
        "AS OF:": "as_of",
        "START DATE:": "period_start",
        "END DATE:": "period_end",
    }
    meta_raw: dict[str, str] = {}
    report_type_hint = ""

    for row in ws.iter_rows(min_row=1, max_row=12, values_only=True):
        for v in row:
            if not v:
                continue
            s = str(v).strip()
            if not s:
                continue
            if (
                not report_type_hint
                and not any(s.upper().startswith(p) for p in _META)
                and any(k in s.upper() for k in ("ATTRIBUTION", "RISK", "FACTOR"))
            ):
                report_type_hint = s
            for prefix, key in _META.items():
                if s.upper().startswith(prefix):
                    val = s[len(prefix) :].strip()
                    if key == "run_date":
                        val = _xl_parse_date(val)
                    meta_raw.setdefault(key, val)

    metadata = ReportMetadata(
        report_name=report_type_hint,
        portfolio=meta_raw.get("portfolio", ""),
        benchmark=meta_raw.get("benchmark", ""),
        run_date=meta_raw.get("run_date", ""),
        base_currency=meta_raw.get("base_currency", ""),
        period_start=meta_raw.get("period_start"),
        period_end=meta_raw.get("period_end"),
        as_of=meta_raw.get("as_of"),
        classification=meta_raw.get("classification") or None,
    )

    # ── 2. Propagate merged-cell values into every cell of the range ───────────
    merge_fill: dict[tuple[int, int], object] = {}
    for rng in ws.merged_cells.ranges:
        top_val = ws.cell(rng.min_row, rng.min_col).value
        for r in range(rng.min_row, rng.max_row + 1):
            for c in range(rng.min_col, rng.max_col + 1):
                merge_fill[(r, c)] = top_val

    def cv(row_i: int, col_i: int) -> object:
        """Return the merged-cell fill value, else the raw worksheet cell value."""
        return merge_fill.get((row_i, col_i), ws.cell(row_i, col_i).value)

    # ── 3. Find the "Name" / "Factor Name" header row ─────────────────────────
    name_row: int | None = None
    is_factor_risk = False
    for ri in range(1, 25):
        v = cv(ri, 1)
        if v is None:
            continue
        s = str(v).strip()
        if s == "Name":
            name_row = ri
            break
        if s == "Factor Name":
            name_row = ri
            is_factor_risk = True
            break

    if name_row is None:
        sample = [ws.cell(r, 1).value for r in range(1, 6)]
        raise ValueError(
            f"Tabblad '{sheet_name}': geen 'Name' kolomkop gevonden in de eerste 24 rijen. "
            f"Eerste cellen in kolom A: {sample}"
        )

    sub_row = name_row + 1
    data_start = sub_row + 1

    # ── 4. Build column map from the two header rows ───────────────────────────

    def _nrm(s: str) -> str:
        """NFKC-normalise: converts en-dashes, special quotes, etc. to ASCII."""
        return unicodedata.normalize("NFKC", s).strip()

    # Sub-header label → CSV [suffix]
    _SUB = {
        "Port": "P",
        "Portfolio": "P",
        "P": "P",
        "Bench": "B",
        "Benchmark": "B",
        "B": "B",
        "+/-": "D",
        "Active": "D",
        "D": "D",
    }
    # Verbose group label → canonical base name used by CSV parsers
    # All keys are NFKC-normalised so Bloomberg's en-dash / special chars match.
    _GRP = {
        "Avg % Wgt": "Avg % Wgt",
        "Avg % Weight": "Avg % Wgt",
        "Tot Rtn": "Tot Rtn",
        "Total Return": "Tot Rtn",
        "Factor Rtn (Contr)": "Factor Rtn (Contr)",
        "Factor Return (Contribution)": "Factor Rtn (Contr)",
        "Factor Rtn": "Factor Rtn (Contr)",
        "Selection Rtn (Contr)": "Selection Rtn (Contr)",
        "Selection Return (Contribution)": "Selection Rtn (Contr)",
        "Selection Rtn": "Selection Rtn (Contr)",
        "Time Rtn (Contr)": "Time Rtn (Contr)",
        "Timing Return (Contribution)": "Time Rtn (Contr)",
        "Time Rtn": "Time Rtn (Contr)",
        "CTR": "CTR",
        "Contribution": "CTR",
        "% Wgt": "% Wgt",
        "% Weight": "% Wgt",
        "Gross Active Weight": "Gross Active Weight",
        "Total Risk": "Total Risk",
        "Contribution (%)": "Contribution (%)",
        "Non-Factor Contrib (%)": "Non-Factor Contrib (%)",
        "Non-Factor Contribution (%)": "Non-Factor Contrib (%)",
        # Beta variants — Bloomberg sometimes uses dashes, parens, or casing variations
        "Beta (ex-ante)": "Beta (ex-ante)",
        "Beta (Ex-Ante)": "Beta (ex-ante)",
        "Beta ex-ante": "Beta (ex-ante)",
        "Beta Ex-Ante": "Beta (ex-ante)",
        "Beta (ex ante)": "Beta (ex-ante)",
        "Beta": "Beta (ex-ante)",
        "Alloc": "Alloc",
        "Allocation": "Alloc",
        "Allocation Effect": "Alloc",
        "Selec": "Selec",
        "Selection": "Selec",
        "Selection Effect": "Selec",
        "Inter": "Inter",
        "Interaction": "Inter",
        "Interaction Effect": "Inter",
        "Tot Attr": "Tot Attr",
        "Total Attribution": "Tot Attr",
        "Portfolio Exposure": "Portfolio Exposure",
        "Benchmark Exposure": "Benchmark Exposure",
        "Active Exposure": "Active Exposure",
        "Factor Vol (Std %)": "Factor Vol (Std %)",
        "Tot. Active (Std)": "Tot. Active (Std)",
        "Marginal (X100)": "Marginal (X100)",
        "Contribution %": "Contribution %",
        "End Mkt Val": "End Mkt Val",
    }
    # Also recognise individual factor names as column group headers
    _GRP.update({fn: fn for fn in ALL_FACTOR_NAMES})

    col_map: dict[int, str] = {}  # 1-based col → CSV-style column name
    current_group = ""
    # Scan up to ws.max_column + 3 as a buffer (Bloomberg occasionally declares
    # fewer columns than are physically present in the header row).
    _col_scan_end = ws.max_column + 3
    for col in range(2, _col_scan_end + 1):
        g_raw = cv(name_row, col)
        has_new_hdr = bool(g_raw and str(g_raw).strip())
        if has_new_hdr:
            current_group = _nrm(str(g_raw))
        # Don't add columns beyond the declared range unless they have an explicit header
        if col > ws.max_column and not has_new_hdr:
            break
        if not current_group:
            continue

        grp = _GRP.get(current_group, current_group)
        s_raw = cv(sub_row, col)
        sub = _nrm(str(s_raw)) if s_raw is not None and str(s_raw).strip() else ""

        if sub in _SUB:
            col_map[col] = f"{grp}[{_SUB[sub]}]"
        else:
            col_map[col] = grp  # single column, no sub-header

    logger.debug(
        "Excel '%s' column map (all %d cols): %s",
        sheet_name,
        len(col_map),
        col_map,
    )

    # ── 5. Read data rows ──────────────────────────────────────────────────────
    all_data: list[tuple[int, str, dict]] = []
    is_first = True

    for ri in range(data_start, ws.max_row + 1):
        name_cell = ws.cell(ri, 1)
        raw_name = name_cell.value
        if raw_name is None:
            continue
        name_str = str(raw_name).strip()
        if not name_str:
            continue
        if name_str.lower().startswith("disclaimer"):
            break

        if is_factor_risk:
            level = 0  # flat list for TE factor risk
        elif is_first:
            level = 0  # first data row is always the portfolio total
            is_first = False
        else:
            # Primary: Excel row-outline depth.
            # Bloomberg places sector rows at outlineLevel 1 (inside the portfolio
            # group), industry groups at 2, securities at 3.  So level = outline
            # maps directly to our model: outline 1 → L1, 2 → L2, 3 → L3.
            rd = ws.row_dimensions.get(ri)
            outline = int(rd.outlineLevel or 0) if rd else 0
            if outline > 0:
                level = outline
            else:
                # Fallback when no outline info: cell indent or bold styling
                try:
                    align = name_cell.alignment
                    indent = int(align.indent or 0) if align else 0
                except Exception:
                    indent = 0
                if indent > 0:
                    level = indent
                else:
                    try:
                        bold = bool(name_cell.font and name_cell.font.bold)
                    except Exception:
                        bold = False
                    level = 1 if bold else 2

        # Bloomberg sometimes merges a column header across several physical
        # columns (e.g. "Beta (ex-ante)" spans G:H).  A dict comprehension
        # would write the good value from col G, then immediately overwrite
        # it with None from the empty col H.  Iterate in column order and
        # keep the first non-None value for each name.
        vals: dict[str, float | None] = {}
        for col, col_name in col_map.items():
            v = _xl_num(ws.cell(ri, col).value)
            if col_name not in vals or (vals[col_name] is None and v is not None):
                vals[col_name] = v
        all_data.append((level, name_str, vals))

    if not all_data:
        raise ValueError(
            f"Tabblad '{sheet_name}': geen gegevensrijen gevonden na rij {data_start}. "
            f"Controleer of het tabblad Bloomberg PORT-data bevat."
        )

    return _xl_build_report(metadata, all_data, is_factor_risk)


def _xl_build_report(
    metadata: ReportMetadata,
    all_data: list[tuple[int, str, dict]],
    is_factor_risk: bool,
) -> Report:
    """Create the correct report object from parsed Excel rows."""
    all_cols: set[str] = set()
    for _, _, v in all_data:
        all_cols.update(k for k, val in v.items() if val is not None)

    def get(vals: dict, *names: str) -> float | None:
        """Return the first non-None value among the given column ``names``."""
        for n in names:
            result = vals.get(n)
            if result is not None:
                return result
        return None

    # Bloomberg labels BOTH Port Attribution AND Factor Attribution as
    # "PORTFOLIO_ATTRIBUTION" in row 3, so the type string is not a reliable
    # discriminant between the two.  Column presence is the ground truth:
    #
    #   Port Attribution   : "Curr", "Transact", "Lev", "Px Diff" (Bloomberg-specific
    #                         currency/cost/leverage decomposition)
    #   Factor Attribution : "Factor Rtn (Contr)[D]" or individual EU/global factor
    #                         columns — never has Curr/Transact/Lev/Px Diff
    #   Sector Attribution : "Alloc", "Selec", "Inter" — no Curr/Transact
    #
    # Use the report_type_hint only as a secondary/confirmatory signal.
    _report_type = (metadata.report_name or "").upper()
    _is_factor_attr = "FACTOR" in _report_type and "ATTRIBUTION" in _report_type

    # Column-level base names (strip [P]/[B]/[D] suffix)
    _col_bases = {c.split("[")[0] for c in all_cols}

    # Port Attribution-specific columns: currency effect, transaction costs,
    # leverage, price-difference — never appear in Factor or Sector Attribution.
    _port_specific = bool({"Curr", "Transact", "Lev", "Px Diff"} & _col_bases)

    logger.debug(
        "Excel build — type_hint=%s  is_factor_hint=%s  port_specific=%s  cols=%s  rows=%d",
        _report_type or ("te_factor" if is_factor_risk else "auto"),
        _is_factor_attr,
        _port_specific,
        sorted(all_cols)[:10],
        len(all_data),
    )

    # ── TE Factor Risk Decomposition ───────────────────────────────────────────
    if is_factor_risk:
        factors = []
        for _, name, vals in all_data:
            cat, _, label = name.partition(":")
            factors.append(
                TERiskFactorRecord(
                    factor_name=name,
                    category=cat.strip(),
                    factor_label=(label.strip() if label else name),
                    portfolio_exposure=get(vals, "Portfolio Exposure"),
                    benchmark_exposure=get(vals, "Benchmark Exposure"),
                    active_exposure=get(vals, "Active Exposure"),
                    factor_vol=get(vals, "Factor Vol (Std %)"),
                    tot_active_std=get(vals, "Tot. Active (Std)"),
                    marginal=get(vals, "Marginal (X100)"),
                    contribution_pct=get(vals, "Contribution %"),
                )
            )
        return TERiskFactorReport(metadata=metadata, factors=factors)

    # ── Tracking-Error Report ──────────────────────────────────────────────────
    # Bloomberg Excel uses "Total Risk" (single column, no Port/Bench sub-header),
    # unlike the CSV which has "Total Risk[D]".
    if {"Total Risk[D]", "Total Risk"} & all_cols:
        te_recs: list[TrackingErrorRecord] = []
        te_total: TrackingErrorRecord | None = None
        for level, name, vals in all_data:
            rec = TrackingErrorRecord(
                level=level,
                name=name,
                wgt_p=get(vals, "% Wgt[P]"),
                wgt_d=get(vals, "% Wgt[D]"),
                gross_active_wgt=get(vals, "Gross Active Weight[D]", "Gross Active Weight"),
                total_risk_d=get(vals, "Total Risk[D]", "Total Risk"),
                contribution_p=get(vals, "Contribution (%)[P]", "Contribution (%)"),
                beta_p=get(vals, "Beta (ex-ante)[P]", "Beta (ex-ante)"),
                non_factor_contrib=get(vals, "Non-Factor Contrib (%)"),
            )
            te_recs.append(rec)
            if level == 0:
                te_total = rec
        if te_total is None:
            te_total = (
                te_recs[0]
                if te_recs
                else TrackingErrorRecord(
                    level=0,
                    name=metadata.portfolio or "Total",
                    wgt_p=None,
                    wgt_d=None,
                    gross_active_wgt=None,
                    total_risk_d=None,
                    contribution_p=None,
                    beta_p=None,
                )
            )
        if te_total.beta_p is None:
            # Log the raw column names so the cause can be diagnosed.
            beta_cols = [c for c in (all_cols or set()) if "beta" in c.lower()]
            logger.warning(
                "TE report '%s': beta_p is None on total row. " "Beta-related columns found: %s. All columns: %s",
                metadata.portfolio,
                beta_cols,
                sorted(all_cols),
            )
        max_lv = max((r.level for r in te_recs), default=0)
        if max_lv >= 3:
            sects = [r for r in te_recs if r.level == 1]
            ig = [r for r in te_recs if r.level == 2]
            secs = [r for r in te_recs if r.level == 3]
        elif max_lv >= 2:
            # 3-level: L1 = sector, L2 = security (no industry group)
            sects = [r for r in te_recs if r.level == 1]
            ig = []
            secs = [r for r in te_recs if r.level == 2]
        else:
            # 2-level or no outline info: all non-total rows are securities
            sects, ig = [], []
            secs = [r for r in te_recs if r.level >= 1]
        return TrackingErrorReport(
            metadata=metadata,
            total=te_total,
            all_records=te_recs,
            sectors=sects,
            industry_groups=ig,
            securities=secs,
        )

    # ── Factor Attribution ─────────────────────────────────────────────────────
    # Detected by EITHER:
    #   (a) Individual EU/global factor contribution columns (EU Beta[D] etc.)
    #   (b) "Factor Rtn (Contr)[D]" aggregate — present in Factor Attribution
    #       Excels that don't export individual factor columns (sub-portfolios)
    # AND the absence of Port Attribution-specific columns (Curr, Transact…).
    _fa_names = [fn for fn in ALL_FACTOR_NAMES if fn != "Currency"]
    _factor_col_check = ({f"{fn}[D]" for fn in _fa_names} | set(_fa_names)) & all_cols
    _factor_rtn_col = "Factor Rtn (Contr)[D]" in all_cols or "Factor Rtn (Contr)" in all_cols
    _is_factor_by_cols = (bool(_factor_col_check) or _factor_rtn_col) and not _port_specific

    if _is_factor_attr or _is_factor_by_cols:
        fa_total: FactorAttributionRecord | None = None
        fa_sects: list[FactorAttributionRecord] = []
        fa_secs: list[FactorAttributionRecord] = []
        for level, name, vals in all_data:
            factor_ctrs: dict[str, float] = {
                fn: v for fn in ALL_FACTOR_NAMES if (v := get(vals, f"{fn}[D]", fn)) is not None
            }
            rec = FactorAttributionRecord(
                level=level,
                name=name,
                avg_wgt_p=get(vals, "Avg % Wgt[P]"),
                avg_wgt_b=get(vals, "Avg % Wgt[B]"),
                avg_wgt_d=get(vals, "Avg % Wgt[D]"),
                tot_rtn_p=get(vals, "Tot Rtn[P]"),
                tot_rtn_b=get(vals, "Tot Rtn[B]"),
                tot_rtn_d=get(vals, "Tot Rtn[D]"),
                ctr_p=get(vals, "CTR[P]"),
                ctr_b=get(vals, "CTR[B]"),
                ctr_d=get(vals, "CTR[D]"),
                factor_d=get(vals, "Factor Rtn (Contr)[D]"),
                selection_d=get(vals, "Selection Rtn (Contr)[D]"),
                timing_d=get(vals, "Time Rtn (Contr)[D]"),
                end_mkt_val=get(vals, "End Mkt Val"),
                factor_ctrs_d=factor_ctrs,
            )
            if level == 0:
                fa_total = rec
            elif level == 1:
                fa_sects.append(rec)
            else:
                fa_secs.append(rec)
        if fa_total is None:
            raise ValueError("Factor Attribution (Excel): geen level-0 totaalrij gevonden.")
        return FactorAttributionReport(
            metadata=metadata,
            total=fa_total,
            sectors=fa_sects,
            securities=fa_secs,
        )

    # ── Sector Attribution (Brinson) ───────────────────────────────────────────
    # Identified by classic Brinson columns (Alloc/Selec/Inter).
    # Port Attribution also has Alloc/Selec but is excluded by _port_specific.
    if {"Alloc", "Selec", "Inter", "Tot Attr"} & _col_bases and not _port_specific:
        sa_total: SectorAttributionRecord | None = None
        sa_sects: list[SectorAttributionRecord] = []
        sa_secs: list[SectorAttributionRecord] = []
        for level, name, vals in all_data:
            rec = SectorAttributionRecord(
                level=level,
                name=name,
                avg_wgt_p=get(vals, "Avg % Wgt[P]"),
                avg_wgt_b=get(vals, "Avg % Wgt[B]"),
                avg_wgt_d=get(vals, "Avg % Wgt[D]"),
                ctr_p=get(vals, "CTR[P]"),
                ctr_b=get(vals, "CTR[B]"),
                ctr_d=get(vals, "CTR[D]"),
                tot_rtn_p=get(vals, "Tot Rtn[P]"),
                tot_rtn_b=get(vals, "Tot Rtn[B]"),
                tot_rtn_d=get(vals, "Tot Rtn[D]"),
                allocation=get(vals, "Alloc", "Alloc[D]"),
                selection=get(vals, "Selec", "Selec[D]"),
                interaction=get(vals, "Inter", "Inter[D]"),
                total_attribution=get(vals, "Tot Attr", "Tot Attr[D]"),
            )
            if level == 0:
                sa_total = rec
            elif level == 1:
                sa_sects.append(rec)
            else:
                sa_secs.append(rec)
        if sa_total is None:
            raise ValueError("Sector Attribution (Excel): geen level-0 totaalrij gevonden.")
        return SectorAttributionReport(
            metadata=metadata,
            total=sa_total,
            sectors=sa_sects,
            securities=sa_secs,
        )

    # ── Portfolio / Alpha Attribution (fallback) ───────────────────────────────
    # Bloomberg PORT Excel with BREAKDOWN BY:GICS Sector is 3-level:
    #   level 0 = portfolio total, level 1 = GICS sector, level 2 = security.
    # Without a sector breakdown it is 2-level: level 0 = total, level 1 = security.
    # Determine the max level first so we can filter correctly.
    _pa_max_level = max((lvl for lvl, _, _ in all_data), default=0)
    _pa_sec_min_level = 2 if _pa_max_level >= 2 else 1

    pa_total: AttributionRecord | None = None
    pa_secs: list[AttributionRecord] = []
    for level, name, vals in all_data:
        rec = AttributionRecord(
            level=level,
            name=name,
            avg_wgt_p=get(vals, "Avg % Wgt[P]"),
            avg_wgt_b=get(vals, "Avg % Wgt[B]"),
            avg_wgt_d=get(vals, "Avg % Wgt[D]"),
            tot_rtn_p=get(vals, "Tot Rtn[P]"),
            tot_rtn_b=get(vals, "Tot Rtn[B]"),
            tot_rtn_d=get(vals, "Tot Rtn[D]"),
            factor_p=get(vals, "Factor Rtn (Contr)[P]"),
            factor_b=get(vals, "Factor Rtn (Contr)[B]"),
            factor_d=get(vals, "Factor Rtn (Contr)[D]"),
            selection_p=get(vals, "Selection Rtn (Contr)[P]"),
            selection_b=get(vals, "Selection Rtn (Contr)[B]"),
            selection_d=get(vals, "Selection Rtn (Contr)[D]"),
            timing_p=get(vals, "Time Rtn (Contr)[P]"),
            timing_b=get(vals, "Time Rtn (Contr)[B]"),
            timing_d=get(vals, "Time Rtn (Contr)[D]"),
            ctr_p=get(vals, "CTR[P]"),
            ctr_b=get(vals, "CTR[B]"),
            ctr_d=get(vals, "CTR[D]"),
        )
        if level == 0:
            pa_total = rec
        elif level >= _pa_sec_min_level:
            pa_secs.append(rec)
    if pa_total is None:
        raise ValueError("Portfolio Attribution (Excel): geen level-0 totaalrij gevonden.")
    return AttributionReport(metadata=metadata, total=pa_total, securities=pa_secs)


def detect_and_load_excel(file_bytes: bytes) -> list:
    """
    Parse Detail_1, Detail_2, and Detail_3 tabs from a Bloomberg Excel export.

    Tab mapping varies by fund:
      ADAAF / ADWAF  : Detail_1 = Alpha,  Detail_2 = Core   (or Cash/Fonds in slot 1)
      AEIIAEF        : Detail_1 = Cash,   Detail_2 = Alpha,  Detail_3 = Core

    Returns a list of report objects (one per successfully parsed tab).
    Raises ValueError with a clear diagnostic if no tab could be loaded.
    """
    reports = []
    errors: list[str] = []
    for sheet_name in ("Detail_1", "Detail_2", "Detail_3"):
        try:
            report = _load_bloomberg_excel_sheet(file_bytes, sheet_name)
            reports.append(report)
            logger.debug(
                "Excel %s → %s (%s)",
                sheet_name,
                type(report).__name__,
                report.metadata.portfolio,
            )
        except Exception as exc:
            errors.append(f"  • {sheet_name}: {exc}")
            logger.debug("Excel tab '%s' not loaded: %s", sheet_name, exc)

    if not reports:
        raise ValueError("Geen enkel tabblad kon worden geladen:\n" + "\n".join(errors))
    return reports
