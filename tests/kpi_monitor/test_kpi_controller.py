"""Wiring test for the KPI review: model data + documents in, prompt out, review back."""

from __future__ import annotations

import dataclasses
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
    assert "2025-03-31=90" in user_prompt and "QoQ -10.0%" in user_prompt
    assert "Margins were pressured by pricing." in user_prompt and "pricing" in user_prompt


def test_analyze_returns_none_without_model_data(monkeypatch) -> None:
    monkeypatch.setattr(search_model, "retrieve_financial_model_bulk", lambda *_: [])
    assert kpi_controller.KpiController().analyze("Acme", "ACME", CONFIG) is None


def test_saved_reviews_can_be_listed_loaded_and_are_pruned_by_config(monkeypatch) -> None:
    from kpi_monitor import review_store as store  # noqa: PLC0415

    files: dict[tuple[str, str], dict] = {}

    def fake_save(*, folder, review_id, payload, keep, **_):
        files[(folder, review_id)] = payload
        for old in kpi_controller.model.ids_to_prune([r for f, r in files if f == folder], keep):
            del files[(folder, old)]

    monkeypatch.setattr(store, "save_review", fake_save)
    monkeypatch.setattr(
        store, "list_reviews", lambda _a, _fs, folder: sorted((r for f, r in files if f == folder), reverse=True)
    )
    monkeypatch.setattr(store, "load_review", lambda _a, _fs, folder, rid: files[(folder, rid)])
    monkeypatch.setattr(search_model, "retrieve_financial_model_bulk", lambda *_: [_model_chunk("2025-03-31", "90.0")])
    monkeypatch.setattr(search_model, "retrieve", lambda **_: [_doc("Call.", "2025-04-30")])
    monkeypatch.setattr(search_model, "get_chat_client", lambda *_: _FakeClient())

    cfg = {**CONFIG, "data_layer": "silver", "kpi_monitor": {**CONFIG["kpi_monitor"], "max_saved_reviews": 2}}
    controller = kpi_controller.KpiController()
    sources = (kpi_controller.model.SourceFile("MODL_ACME.xlsx", "financial_model", "p/MODL_ACME.xlsx", 5),)
    saved_ids = []
    for minute in range(3):
        analysis = controller.analyze("Acme NV", "BRK/B", cfg, focus_notes=f"run {minute}", sources=sources)
        analysis = dataclasses.replace(analysis, created_at=f"2026-10-14T08:0{minute}:00+00:00")
        saved_ids.append(controller.save_review(analysis, "BRK/B", "Acme NV", cfg, "acct"))

    listed = controller.list_reviews("BRK/B", cfg, "acct")
    assert listed == [saved_ids[2], saved_ids[1]]  # oldest pruned, newest first
    reopened = controller.load_review("BRK/B", listed[0], cfg, "acct")
    assert reopened.focus_notes == "run 2"
    assert reopened.sources == sources
    assert reopened.documents[0].content == "Call."
    assert ("brk-b", listed[0]) in files  # stored under a filesystem-safe company folder


def test_review_looks_the_model_up_by_ticker_and_company_name(monkeypatch) -> None:
    seen: list[tuple] = []
    monkeypatch.setattr(search_model, "retrieve_financial_model_bulk", lambda *args: seen.append(args) or [])
    kpi_controller.KpiController().analyze("ASML Holding N.V.", "ASML", CONFIG)
    assert seen == [("idx", "ASML", 100, "ASML Holding N.V.")]
