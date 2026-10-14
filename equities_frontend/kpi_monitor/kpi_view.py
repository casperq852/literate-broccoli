"""
KPI Monitor tab of the company detail screen.

The analyst adds the company's latest Bloomberg model (``MODL_*.xlsx``),
earnings transcripts and other documents, then asks for a review. An LLM picks
the metrics that matter for this company, judges which moves are significant or
concerning, and links them to what management said. Each finding is shown next
to the model's own figures so the numbers can be checked.

Rendered by ``companies/company_detail_view.py``; every widget key is
namespaced with the ticker like the other company tabs.
"""

from __future__ import annotations

import re
from logging import Logger
from typing import Any

import streamlit as st
from streamlit.logger import get_logger

from companies.company_controller import CompanyController
from helpers import hide_research_notes
from kpi_monitor import kpi_model as model
from kpi_monitor.kpi_controller import KpiAnalysis, KpiController
from search.search_model import ALL_DOCUMENT_TYPES, RESEARCH_NOTE_DOC_TYPE

logger: Logger = get_logger(name=__name__)

_BADGES: dict[str, str] = {
    "concerning": ":red-badge[Concerning]",
    "watch": ":orange-badge[Watch]",
    "positive": ":green-badge[Positive]",
    "neutral": ":gray-badge[Neutral]",
}


def _escape_currency(text: str) -> str:
    """Escape $ signs so Streamlit doesn't render them as LaTeX delimiters."""
    return re.sub(r"(?<!\\)\$", r"\\$", text)


def render(ticker: str, company: dict, company_controller: CompanyController, config: dict[str, Any]) -> None:
    """
    Render the KPI Monitor tab for one company.

    Args:
        ticker (str): The selected company's ticker.
        company (dict): The company's cache row (needs ``display_name``).
        company_controller (CompanyController): Used to refresh the company's cached metadata after uploads.
        config (dict[str, Any]): The equities app config block.
    """
    controller = KpiController()
    result_key = f"kpi_analysis_{ticker}"

    st.caption(
        "Add the latest Bloomberg model (named `MODL_*.xlsx`) together with earnings transcripts and other "
        "documents, then run the review. The model picks the KPIs that matter for this company right now, "
        "judges which moves are significant, and ties them to management's commentary."
    )
    _render_sources(ticker, controller, company_controller, config)

    st.divider()
    focus_notes = st.text_area(
        "Analyst focus (optional)",
        placeholder="e.g. Watch gross margin and the new segment disclosure; I'm concerned about inventory.",
        key=f"kpi_focus_{ticker}",
    )
    if st.button("Run KPI review", type="primary", key=f"kpi_run_{ticker}"):
        document_types = [t for t in ALL_DOCUMENT_TYPES if t != RESEARCH_NOTE_DOC_TYPE] if hide_research_notes() else None
        try:
            with st.spinner("Reading the model and the documents..."):
                analysis = controller.analyze(
                    company=company["display_name"],
                    ticker=ticker,
                    config=config,
                    focus_notes=focus_notes,
                    document_types=document_types,
                )
        except Exception as error:
            logger.exception("KPI review failed for %s", ticker)
            st.error(f"The KPI review failed: {error}")
            return
        if analysis is None:
            st.warning("No financial model data found for this company yet. Upload its `MODL_*.xlsx` above first.")
            st.session_state.pop(result_key, None)
            return
        st.session_state[result_key] = analysis

    analysis: KpiAnalysis | None = st.session_state.get(result_key)
    if analysis is not None:
        _render_analysis(ticker, company["display_name"], analysis, controller)


# ── sources ──────────────────────────────────────────────────────────────────────


def _render_sources(
    ticker: str, controller: KpiController, company_controller: CompanyController, config: dict[str, Any]
) -> None:
    allowed = [ext.lstrip(".") for ext in config["allowed_extensions"]]
    with st.expander("Add sources: model, transcripts, other documents", expanded=False):
        uploaded = st.file_uploader(
            "Choose files",
            type=allowed,
            accept_multiple_files=True,
            key=f"kpi_uploader_{ticker}",
        )
        if not uploaded:
            return
        for file in uploaded:
            kind = "financial model" if model.classify_upload(file.name) == "financial_model" else "document"
            st.write(f"**{file.name}**  ·  {file.size / 1024:.1f} KB  ·  {kind}")

        if not st.button("Upload and ingest", key=f"kpi_upload_btn_{ticker}"):
            return
        status = st.empty()
        outcomes = controller.upload_sources(
            [(file.name, file.getvalue()) for file in uploaded],
            config,
            on_progress=lambda name: status.info(f"Ingesting {name}..."),
        )
        status.empty()
        for outcome in outcomes:
            if outcome.error:
                st.error(f"**{outcome.filename}** failed: {outcome.error}")
            elif outcome.segments > 0:
                st.success(f"Ingested **{outcome.filename}** as {outcome.segments:,} searchable chunks.")
            else:
                st.warning(f"**{outcome.filename}** was uploaded but produced no searchable content.")
            for warning in outcome.warnings:
                st.warning(warning)
        if any(o.segments > 0 for o in outcomes):
            company_controller.sync_companies_incremental([ticker], config)


# ── results ──────────────────────────────────────────────────────────────────────


def _render_analysis(ticker: str, company_name: str, analysis: KpiAnalysis, controller: KpiController) -> None:
    review = analysis.review
    source_labels = controller.source_labels(analysis.documents)

    st.subheader("Review")
    st.caption(
        f"Latest reported period: **{analysis.as_of}** · {len(analysis.series)} metrics reviewed · "
        f"{len(analysis.documents)} document passages"
        + (f" · {analysis.omitted_series} smaller-moving metrics left out" if analysis.omitted_series else "")
    )
    if not analysis.documents:
        st.info("No transcripts or other documents are indexed for this company, so the review rests on the model alone.")
    if review.summary:
        st.markdown(_escape_currency(review.summary))
    if not review.findings:
        st.info("The review did not flag any metrics.")

    for finding in review.findings:
        title = finding.headline or finding.metric
        with st.expander(f"{finding.metric}: {title}", expanded=finding.assessment == "concerning"):
            st.markdown(_BADGES[finding.assessment])
            st.markdown(_escape_currency(finding.commentary))
            if finding.evidence:
                st.markdown(f"**Evidence:** {_escape_currency(finding.evidence)}")
            if finding.follow_up:
                st.markdown(f"**Follow-up:** {_escape_currency(finding.follow_up)}")
            _render_series(finding.metric, analysis)
            if finding.document_refs:
                st.caption("Sources: " + " · ".join(f"[{ref}] {source_labels[ref]}" for ref in finding.document_refs))

    if analysis.documents:
        with st.expander("Document passages used"):
            for index, chunk in enumerate(analysis.documents, start=1):
                st.markdown(f"**[{index}] {source_labels[index]}**")
                st.caption(_escape_currency(chunk.content[:600]) + ("…" if len(chunk.content) > 600 else ""))

    st.download_button(
        "Download review (Markdown)",
        data=model.review_to_markdown(company_name, analysis.as_of, review, source_labels),
        file_name=f"kpi_review_{ticker}_{analysis.as_of}.md",
        mime="text/markdown",
        icon=":material/download:",
        key=f"kpi_dl_{ticker}",
    )


def _render_series(metric: str, analysis: KpiAnalysis) -> None:
    """Show the model's own figures for a metric, so the commentary can be checked against them."""
    series = model.series_for_metric(analysis.series, metric)
    if series is None:
        st.caption("This metric name was not found in the model table; check the commentary against the model.")
        return
    st.dataframe(
        [
            {
                "Period end": point.period_end,
                "Value": point.value,
                "Status": "Estimate" if point.is_estimate else "Reported",
                "Prior estimate": point.prior_estimate,
            }
            for point in series.points
        ],
        hide_index=True,
        width="stretch",
    )
    st.caption(" ".join(part for part in (series.currency, series.unit) if part))
