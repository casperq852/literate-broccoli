"""Data shapes shared between the financial-model parsing model and controller."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class Period:
    """A single reporting period column in the model."""

    label: str
    end_date: datetime
    is_estimate: bool


@dataclass(frozen=True)
class ModelHeader:
    """Identifying metadata from the top of a model sheet."""

    company_name: str = ""
    ticker: str = ""
    periodicity: str = "Q"
    currency: str = "USD"


@dataclass(frozen=True)
class MetricRow:
    """A data row's metric identity, shared across all of its period values."""

    path: str
    leaf_name: str
    segment: str | None
    bloomberg_field: str
    indent: int
