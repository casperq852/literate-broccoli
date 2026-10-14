"""Round-trip serialisation between report objects and silver parquet bytes.

The silver layer stores every parsed Bloomberg report as a single parquet table so
the dashboard can rebuild the exact report dataclasses without re-parsing the raw
files. This module is the *only* place that knows the on-disk parquet shape:
:func:`report_to_parquet_bytes` flattens a report into one row per record (plus
repeated ``meta_*`` metadata columns) and :func:`parquet_bytes_to_report` rebuilds
the typed report. Keeping it pure (no I/O beyond in-memory buffers) makes the
contract easy to type-check and unit-test (a.s.r. Model layer).
"""

from __future__ import annotations

import dataclasses as dc
import json
import warnings
from io import BytesIO
from typing import Any

import numpy as np
import pandas as pd

from portfolio_dashboard.reports import reports_model as rm

# Report class name -> the record dataclass every one of its rows uses.
_RECORD_CLASS: dict[str, type] = {
    "AttributionReport": rm.AttributionRecord,
    "TrackingErrorReport": rm.TrackingErrorRecord,
    "SectorAttributionReport": rm.SectorAttributionRecord,
    "FactorAttributionReport": rm.FactorAttributionRecord,
    "TERiskFactorReport": rm.TERiskFactorRecord,
}

# Report class name -> ordered (attribute, role, is_list) layout. The role labels
# double as the parquet ``_role`` values so each list is restored independently.
_REPORT_LAYOUT: dict[str, tuple[tuple[str, str, bool], ...]] = {
    "AttributionReport": (
        ("total", "total", False),
        ("securities", "securities", True),
    ),
    "TrackingErrorReport": (
        ("total", "total", False),
        ("all_records", "all_records", True),
        ("sectors", "sectors", True),
        ("industry_groups", "industry_groups", True),
        ("securities", "securities", True),
    ),
    "SectorAttributionReport": (
        ("total", "total", False),
        ("sectors", "sectors", True),
        ("securities", "securities", True),
    ),
    "FactorAttributionReport": (
        ("total", "total", False),
        ("sectors", "sectors", True),
        ("securities", "securities", True),
    ),
    "TERiskFactorReport": (("factors", "factors", True),),
}

# Record fields that hold a mapping and must be JSON-encoded for parquet.
_DICT_FIELDS: frozenset[str] = frozenset({"factor_ctrs_d"})

_META_PREFIX = "meta_"
_ROLE_COLUMN = "_role"
_SEQ_COLUMN = "_seq"
_CLASS_COLUMN = "_report_class"
_META_ONLY_ROLE = "__meta_only__"
# Column added when stacking per-date silver frames into one aggregation parquet.
_AGG_DATE_COLUMN = "_agg_date"


def _encode_record(record: object, role: str, seq: int) -> dict[str, Any]:
    """Flatten one record dataclass into a JSON-safe parquet row.

    Args:
        record: A frozen record dataclass instance.
        role: The layout role the record belongs to.
        seq: The record's position within its role.

    Returns:
        dict[str, Any]: Column name to value, with dict fields JSON-encoded.
    """
    row: dict[str, Any] = {}
    for field in dc.fields(record):  # type: ignore[arg-type]
        value = getattr(record, field.name)
        row[field.name] = json.dumps(value) if field.name in _DICT_FIELDS else value
    row[_ROLE_COLUMN] = role
    row[_SEQ_COLUMN] = seq
    return row


def report_to_parquet_bytes(report: object) -> bytes:
    """Serialise a parsed report object into parquet bytes for the silver layer.

    Args:
        report: Any report dataclass produced by the parser.

    Returns:
        bytes: The parquet payload, ready to upload to the silver container.

    Raises:
        KeyError: When ``report`` is not a recognised report type.
    """
    class_name = type(report).__name__
    layout = _REPORT_LAYOUT[class_name]

    rows: list[dict[str, Any]] = []
    for attribute, role, is_list in layout:
        value = getattr(report, attribute, None)
        if is_list:
            for seq, record in enumerate(value or []):
                rows.append(_encode_record(record, role, seq))
        elif value is not None:
            rows.append(_encode_record(value, role, 0))

    if not rows:
        # Preserve metadata even for an empty report by keeping one marker row.
        rows.append({_ROLE_COLUMN: _META_ONLY_ROLE, _SEQ_COLUMN: 0})

    frame = pd.DataFrame(rows)
    frame[_CLASS_COLUMN] = class_name
    for field in dc.fields(report.metadata):  # type: ignore[attr-defined]
        frame[f"{_META_PREFIX}{field.name}"] = getattr(report.metadata, field.name)

    buffer = BytesIO()
    frame.to_parquet(buffer, engine="pyarrow", index=False)
    return buffer.getvalue()


def _coerce_scalar(field_name: str, value: Any) -> Any:
    """Convert a parquet cell back into a native Python value.

    Args:
        field_name: The target dataclass field name.
        value: The raw value read from the parquet frame.

    Returns:
        Any: A JSON-decoded dict for mapping fields, ``None`` for missing values,
        or a plain Python scalar otherwise.
    """
    if field_name in _DICT_FIELDS:
        if isinstance(value, dict):
            return value
        if value is None or (isinstance(value, float) and pd.isna(value)):
            return {}
        return json.loads(value)
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(value, np.integer):
        return int(value)
    if isinstance(value, np.floating):
        return float(value)
    if isinstance(value, np.bool_):
        return bool(value)
    return value


def _build_record(record_class: type, row: pd.Series) -> object:
    """Rebuild a single record dataclass from a parquet row.

    Args:
        record_class: The target record dataclass.
        row: The parquet row holding the record's fields.

    Returns:
        object: A reconstructed record instance.
    """
    kwargs = {field.name: _coerce_scalar(field.name, row.get(field.name)) for field in dc.fields(record_class)}
    return record_class(**kwargs)


def _build_metadata(row: pd.Series) -> rm.ReportMetadata:
    """Rebuild the report metadata from the repeated ``meta_*`` columns.

    Args:
        row: Any row of the parquet frame (metadata is identical across rows).

    Returns:
        rm.ReportMetadata: The reconstructed metadata.
    """
    kwargs = {
        field.name: _coerce_scalar(field.name, row.get(f"{_META_PREFIX}{field.name}"))
        for field in dc.fields(rm.ReportMetadata)
    }
    return rm.ReportMetadata(**kwargs)


def _frame_to_report(frame: pd.DataFrame) -> object:
    """Rebuild a typed report object from one report's parquet frame.

    Args:
        frame: The rows for a single report (one date's silver table, or one
            date-slice of an aggregation table with the date column removed).

    Returns:
        object: The reconstructed report dataclass instance.

    Raises:
        KeyError: When the stored report class is not recognised.
    """
    class_name = str(frame[_CLASS_COLUMN].iloc[0])
    record_class = _RECORD_CLASS[class_name]
    metadata = _build_metadata(frame.iloc[0])

    records_by_role: dict[str, list[object]] = {}
    real_rows = frame[frame[_ROLE_COLUMN] != _META_ONLY_ROLE]
    for role, group in real_rows.groupby(_ROLE_COLUMN, sort=False):
        ordered = group.sort_values(_SEQ_COLUMN)
        records_by_role[str(role)] = [_build_record(record_class, row) for _, row in ordered.iterrows()]

    return _assemble_report(class_name, metadata, records_by_role)


def parquet_bytes_to_report(data: bytes) -> object:
    """Rebuild a typed report object from silver parquet bytes.

    Args:
        data: The parquet payload produced by :func:`report_to_parquet_bytes`.

    Returns:
        object: The reconstructed report dataclass instance.

    Raises:
        KeyError: When the stored report class is not recognised.
    """
    return _frame_to_report(pd.read_parquet(BytesIO(data), engine="pyarrow"))


def _empty_parquet_bytes() -> bytes:
    """Return the parquet bytes for an empty aggregation table."""
    buffer = BytesIO()
    pd.DataFrame().to_parquet(buffer, engine="pyarrow", index=False)
    return buffer.getvalue()


def build_aggregation_parquet_bytes(dated_payloads: list[tuple[str, bytes]]) -> bytes:
    """Stack per-date silver parquet payloads into one aggregation parquet.

    Each payload is the exact silver table for one date of a single portfolio
    component; the rows are concatenated and tagged with an ``_agg_date`` column
    holding the eight-digit ``YYYYMMDD`` date folder they came from, so the
    original per-date reports can be rebuilt with
    :func:`aggregation_bytes_to_reports_by_date`.

    Args:
        dated_payloads: ``(date, payload)`` pairs, where ``date`` is the
            eight-digit date folder and ``payload`` is that date's silver parquet.

    Returns:
        bytes: The aggregation parquet payload, ready to upload to silver.
    """
    frames: list[pd.DataFrame] = []
    for date, payload in dated_payloads:
        frame = pd.read_parquet(BytesIO(payload), engine="pyarrow")
        frame[_AGG_DATE_COLUMN] = date
        frames.append(frame)
    if not frames:
        return _empty_parquet_bytes()
    # pandas warns that concatenating frames with all-NA columns (a date whose
    # report lacks a given record role) will change dtype handling; the retained-
    # column result is exactly what the round-trip needs, so adopt it explicitly.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", FutureWarning)
        combined = pd.concat(frames, ignore_index=True)
    buffer = BytesIO()
    combined.to_parquet(buffer, engine="pyarrow", index=False)
    return buffer.getvalue()


def aggregation_bytes_to_reports_by_date(data: bytes) -> dict[str, object]:
    """Rebuild the per-date report objects from an aggregation parquet payload.

    Args:
        data: The payload produced by :func:`build_aggregation_parquet_bytes`.

    Returns:
        dict[str, object]: Eight-digit ``YYYYMMDD`` date mapped to the
        reconstructed report dataclass for that date.
    """
    frame = pd.read_parquet(BytesIO(data), engine="pyarrow")
    if frame.empty or _AGG_DATE_COLUMN not in frame.columns:
        return {}
    reports_by_date: dict[str, object] = {}
    for date, group in frame.groupby(_AGG_DATE_COLUMN, sort=True):
        slice_frame = group.drop(columns=[_AGG_DATE_COLUMN]).reset_index(drop=True)
        reports_by_date[str(date)] = _frame_to_report(slice_frame)
    return reports_by_date


def aggregation_dates(data: bytes) -> list[str]:
    """Return the sorted distinct dates in an aggregation parquet payload.

    Only the ``_agg_date`` column is read, so this stays cheap even for large
    aggregation files: it never reconstructs any report objects.

    Args:
        data: The payload produced by :func:`build_aggregation_parquet_bytes`.

    Returns:
        list[str]: The eight-digit ``YYYYMMDD`` dates present, ascending.
    """
    frame = pd.read_parquet(BytesIO(data), engine="pyarrow", columns=[_AGG_DATE_COLUMN])
    if frame.empty or _AGG_DATE_COLUMN not in frame.columns:
        return []
    return sorted({str(value) for value in frame[_AGG_DATE_COLUMN].tolist()})


def aggregation_report_for_date(data: bytes, date: str) -> object | None:
    """Rebuild only one date's report from an aggregation parquet payload.

    The ``_agg_date`` filter is pushed down to pyarrow, so only the matching
    rows are materialised and a single report is reconstructed instead of the
    whole history. Use this for the current-period overview, which needs just
    the latest date, and :func:`aggregation_bytes_to_reports_by_date` for the
    trend views that need every date.

    Args:
        data: The payload produced by :func:`build_aggregation_parquet_bytes`.
        date: The eight-digit ``YYYYMMDD`` date to reconstruct.

    Returns:
        object | None: The reconstructed report for ``date``, or ``None`` when
        the date is absent.
    """
    frame = pd.read_parquet(BytesIO(data), engine="pyarrow", filters=[(_AGG_DATE_COLUMN, "==", date)])
    if frame.empty:
        return None
    slice_frame = frame.drop(columns=[_AGG_DATE_COLUMN]).reset_index(drop=True)
    return _frame_to_report(slice_frame)


def _assemble_report(class_name: str, metadata: rm.ReportMetadata, records_by_role: dict[str, list[object]]) -> object:
    """Assemble a report object from its metadata and role-grouped records.

    Args:
        class_name: The report class name.
        metadata: The reconstructed metadata.
        records_by_role: Records grouped by their layout role.

    Returns:
        object: The reconstructed report dataclass instance.
    """
    report_class = getattr(rm, class_name)

    if class_name == "TERiskFactorReport":
        return report_class(metadata=metadata, factors=records_by_role.get("factors", []))

    total_records = records_by_role.get("total", [])
    total = total_records[0] if total_records else None
    kwargs: dict[str, Any] = {"metadata": metadata, "total": total}
    for _, role, is_list in _REPORT_LAYOUT[class_name]:
        if is_list:
            kwargs[role] = records_by_role.get(role, [])
    return report_class(**kwargs)
