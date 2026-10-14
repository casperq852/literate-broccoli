"""Wiring test for the KPI review: model data + documents in, prompt out, review back."""

from __future__ import annotations

import json

from kpi_monitor import kpi_controller
from search import search_model
from search.search_model import Chunk

CONFIG = {
    "index_name": "idx",
    "chat_deployment": "gpt",
    "kpi_monitor": {
        "max_bulk_records": 100,
        "max_reported_periods": 12,
        "max_estimate_periods": 3,
        "max_series": 50,
        "queries_top_k": 5,
        "max_documents": 2,
    },
}


def _model_chunk(period: str, value: str, est: str = "reported") -> Chunk:
    return Chunk(
        content=f"ACME - Revenue - {period} (period ending {period}) (Reported): {value} EUR Millions",
        metric="Revenue", period_label=period, period_end_date=f"{period}T00:00:00Z", is_estimate=est,
        document_type="financial_model", document_date="2025-05-01", publisher=None, score=0.0,
        currency="EUR", unit="Millions",
    )  # fmt: skip


def _doc(text: str, date: str) -> Chunk:
    return Chunk(text, None, None, None, None, "earnings_transcript", date, None, 1.0)


class _FakeClient:
    def __init__(self) -> None:
        self.messages: list[dict] = []

    def chat(self, messages: list[dict]) -> str:
        self.messages = messages
        return json.dumps(
            {
                "summary": "ok",
                "findings": [{"metric": "Revenue", "assessment": "watch", "commentary": "c", "document_refs": [1]}],
            }
        )


def test_analyze_builds_prompt_from_model_and_documents(monkeypatch) -> None:
    client = _FakeClient()
    monkeypatch.setattr(
        search_model,
        "retrieve_financial_model_bulk",
        lambda *_: [_model_chunk("2024-12-31", "100.0"), _model_chunk("2025-03-31", "90.0")],
    )
    monkeypatch.setattr(
        search_model,
        "retrieve",
        lambda **_: [_doc("Margins were pressured by pricing.", "2025-04-30"), _doc("Older call.", "2024-10-30")],
    )
    monkeypatch.setattr(search_model, "get_chat_client", lambda *_: client)

    analysis = kpi_controller.KpiController().analyze("Acme NV", "ACME", CONFIG, focus_notes="pricing")

    assert analysis is not None
    assert analysis.as_of == "2025-03-31"
    assert len(analysis.documents) == 2  # duplicates across queries collapse, capped at max_documents
    assert analysis.review.findings[0].document_refs == (1,)
    user_prompt = client.messages[1]["content"]
    assert "2025-03-31=90.00" in user_prompt and "QoQ -10.0%" in user_prompt
    assert "Margins were pressured by pricing." in user_prompt and "pricing" in user_prompt


def test_analyze_returns_none_without_model_data(monkeypatch) -> None:
    monkeypatch.setattr(search_model, "retrieve_financial_model_bulk", lambda *_: [])
    assert kpi_controller.KpiController().analyze("Acme", "ACME", CONFIG) is None
