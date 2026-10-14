"""
Headless Streamlit tests for the KPI Monitor tab's source handling.

Regression guard for: files selected in the uploader but never ingested made
"Run KPI review" report "no financial model data", with no hint why. The
controller is faked, so these need neither Azure nor an index.
"""

# ruff: noqa: PLC0415

from __future__ import annotations

import sys
import types

import pytest
from streamlit.testing.v1 import AppTest

_STATE_MODULE = "kpi_view_test_state"
_UPLOADER = "kpi_uploader_ASML_0"


def _app() -> None:
    # AppTest runs only this function's body, so everything it uses must be imported here.
    import sys

    import streamlit as st

    from dcf.dcf_parsing_model import ParsedRecord
    from kpi_monitor import kpi_model as m
    from kpi_monitor import kpi_view
    from kpi_monitor.kpi_controller import KpiController, UploadOutcome

    state = sys.modules["kpi_view_test_state"].state
    st.session_state.setdefault("app_config", {"deployment": {"datalake_name": "acct"}})
    records = [
        ParsedRecord("Gross Margin %", None, f"{p}T00:00:00Z", False, v, "EUR", "Millions", None)
        for p, v in [("2024-09-30", 41.0), ("2024-12-31", 40.0), ("2025-03-31", 38.0)]
    ]
    review = m.parse_review('{"summary":"S","findings":[{"metric":"Gross Margin %","commentary":"c"}]}', 0)

    def upload_sources(self, files, config, on_progress=None):
        state["uploaded"] = [name for name, _ in files]
        state["ingested"] = True
        return [UploadOutcome(name, m.classify_upload(name), segments=10, path=f"p/{name}") for name, _ in files]

    def analyze(self, **kwargs):
        if not state["ingested"] or state["index_never_ready"]:
            return None
        state["sources_on_review"] = [source.filename for source in kwargs["sources"]]
        return m.KpiAnalysis(
            review,
            "2025-03-31",
            m.build_series(records),
            [],
            sources=kwargs["sources"],
            created_at="2026-10-14T08:55:01+00:00",
        )

    KpiController.upload_sources = upload_sources
    KpiController.analyze = analyze
    KpiController.save_review = lambda *args, **kwargs: "id"
    KpiController.list_reviews = lambda *args, **kwargs: []

    class _Companies:
        def sync_companies_incremental(self, *args) -> None:
            state["synced"] = True

    config = {"allowed_extensions": [".pdf", ".xlsx"], "data_layer": "silver"}
    kpi_view.render("ASML", {"display_name": "ASML Holding"}, _Companies(), config)


@pytest.fixture
def state(monkeypatch) -> dict:
    holder = types.ModuleType(_STATE_MODULE)
    holder.state = {"ingested": False, "index_never_ready": False}
    monkeypatch.setitem(sys.modules, _STATE_MODULE, holder)
    return holder.state


def _open() -> AppTest:
    app = AppTest.from_function(_app, default_timeout=30).run()
    assert not app.exception
    return app


def test_run_ingests_selected_files_first_and_records_them_as_sources(state) -> None:
    app = _open()
    app.file_uploader(key=_UPLOADER).upload("MODL_ASML_NA.xlsx", b"x").run()
    assert any("have not been ingested yet" in info.value for info in app.info)

    app.button(key="kpi_run_ASML").click().run()

    assert not app.exception
    assert state["uploaded"] == ["MODL_ASML_NA.xlsx"] and state["synced"]
    assert state["sources_on_review"] == ["MODL_ASML_NA.xlsx"]
    assert not [w for w in app.warning if "No financial model data" in w.value]
    assert [m for m in app.markdown if m.value == "S"], "the review is shown"
    assert not any("have not been ingested yet" in info.value for info in app.info), "selection cleared"
    assert any("Ingested" in success.value for success in app.success), "ingest report kept"


def test_ingested_but_not_yet_searchable_says_to_try_again(state) -> None:
    state["index_never_ready"] = True
    app = _open()
    app.file_uploader(key=_UPLOADER).upload("MODL_ASML_NA.xlsx", b"x").run()
    app.button(key="kpi_run_ASML").click().run()
    assert any("just ingested" in info.value for info in app.info)
    assert not [w for w in app.warning if "No financial model data" in w.value]


def test_no_model_anywhere_explains_the_modl_naming_rule(state) -> None:
    app = _open()
    app.button(key="kpi_run_ASML").click().run()
    assert any("starts with `MODL_`" in warning.value for warning in app.warning)
    assert state.get("uploaded") is None
