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
from helpers import datalake_account, download_from_datalake, hide_research_notes
from kpi_monitor import kpi_model as model
from kpi_monitor.kpi_controller import KpiController, UploadOutcome
from kpi_monitor.kpi_export_model import DOCX_MIME
from kpi_monitor.kpi_model import KpiAnalysis, SourceFile
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
    sources_key = f"kpi_new_sources_{ticker}"
    account = datalake_account()

    st.caption(
        "Add the latest Bloomberg model (named `MODL_*.xlsx`) together with earnings transcripts and other "
        "documents, then run the review. The model picks the KPIs that matter for this company right now, "
        "judges which moves are significant, and ties them to management's commentary."
    )
    _render_saved_reviews(ticker, controller, config, account)
    pending = _render_sources(ticker, controller, company_controller, config)

    st.divider()
    focus_notes = st.text_area(
        "Analyst focus (optional)",
        placeholder="e.g. Watch gross margin and the new segment disclosure; I'm concerned about inventory.",
        key=f"kpi_focus_{ticker}",
    )
    pending_notice = st.empty()
    if pending:
        pending_notice.info(
            f"{len(pending)} selected file(s) have not been ingested yet. Running the review will upload and "
            "ingest them first."
        )
    if st.button("Run KPI review", type="primary", key=f"kpi_run_{ticker}"):
        ingested_now = False
        if pending:
            outcomes = _ingest_files(ticker, pending, controller, company_controller, config)
            st.session_state[f"kpi_ingest_report_{ticker}"] = outcomes
            pending_notice.empty()
            _show_outcomes(outcomes)
            ingested_now = any(outcome.segments > 0 for outcome in outcomes)
        else:
            st.session_state.pop(f"kpi_ingest_report_{ticker}", None)
        document_types = [t for t in ALL_DOCUMENT_TYPES if t != RESEARCH_NOTE_DOC_TYPE] if hide_research_notes() else None
        try:
            with st.spinner("Reading the model and the documents..."):
                analysis = controller.analyze(
                    company=company["display_name"],
                    ticker=ticker,
                    config=config,
                    focus_notes=focus_notes,
                    document_types=document_types,
                    sources=tuple(st.session_state.get(sources_key, [])),
                )
        except Exception as error:
            logger.exception("KPI review failed for %s", ticker)
            st.error(f"The KPI review failed: {error}")
            return
        if analysis is None:
            if ingested_now:
                st.info(
                    "The files were just ingested, but the search index can take a minute to show a new model. "
                    "Try running the review again shortly."
                )
            else:
                st.warning(
                    f"No financial model records found in the index for {company['display_name']} ({ticker}). "
                    "Add its `MODL_*.xlsx` under **Add sources** (a file counts as a model only if its name "
                    "starts with `MODL_`). If you already did, the model may have been indexed under a "
                    "different company name or ticker; check the Companies page."
                )
            st.session_state.pop(result_key, None)
            return
        st.session_state[result_key] = analysis
        st.session_state[sources_key] = []
        try:
            controller.save_review(analysis, ticker, company["display_name"], config, account)
            st.toast("Review saved. Reopen it any time from Saved reviews.")
        except Exception:
            logger.exception("Could not save the KPI review for %s", ticker)
            st.warning("The review is shown below but could not be saved to the Data Lake.")
        if pending:
            st.rerun()  # redraw with the uploader cleared; the review and ingest report persist in session state

    analysis: KpiAnalysis | None = st.session_state.get(result_key)
    if analysis is not None:
        _render_analysis(ticker, company["display_name"], analysis, controller)


# ── saved reviews ────────────────────────────────────────────────────────────────


def _load_saved(ticker: str, controller: KpiController, config: dict[str, Any], account: str) -> None:
    """Button callback: reopen the selected saved review, restoring its focus note too."""
    review_id = st.session_state.get(f"kpi_saved_pick_{ticker}")
    if not review_id:
        return
    try:
        analysis = controller.load_review(ticker, review_id, config, account)
    except Exception as error:
        logger.exception("Could not load KPI review %s for %s", review_id, ticker)
        st.session_state[f"kpi_load_error_{ticker}"] = str(error)
        return
    st.session_state.pop(f"kpi_load_error_{ticker}", None)
    st.session_state[f"kpi_analysis_{ticker}"] = analysis
    st.session_state[f"kpi_focus_{ticker}"] = analysis.focus_notes


def _delete_saved(ticker: str, controller: KpiController, config: dict[str, Any], account: str) -> None:
    """Button callback: delete the selected saved review."""
    review_id = st.session_state.get(f"kpi_saved_pick_{ticker}")
    if not review_id:
        return
    try:
        controller.delete_review(ticker, review_id, config, account)
    except Exception as error:
        logger.exception("Could not delete KPI review %s for %s", review_id, ticker)
        st.session_state[f"kpi_load_error_{ticker}"] = f"Could not delete: {error}"


def _render_saved_reviews(ticker: str, controller: KpiController, config: dict[str, Any], account: str) -> None:
    review_ids = controller.list_reviews(ticker, config, account)
    if not review_ids:
        return
    with st.expander(f"Saved reviews ({len(review_ids)})", expanded=False):
        st.selectbox(
            "Reopen a previous review for this company",
            review_ids,
            format_func=model.review_label,
            key=f"kpi_saved_pick_{ticker}",
        )
        open_col, delete_col, _ = st.columns([1, 1, 4])
        open_col.button(
            "Open",
            key=f"kpi_open_{ticker}",
            on_click=_load_saved,
            args=(ticker, controller, config, account),
        )
        with delete_col.popover("Delete"):
            st.write("Delete this saved review? The uploaded files stay in the index.")
            st.button(
                "Confirm delete",
                key=f"kpi_delete_{ticker}",
                on_click=_delete_saved,
                args=(ticker, controller, config, account),
            )
        if error := st.session_state.get(f"kpi_load_error_{ticker}"):
            st.error(error)


# ── sources ──────────────────────────────────────────────────────────────────────


def _uploader_key(ticker: str) -> str:
    """Uploader widget key; bumping the generation clears the selected files after an ingest."""
    return f"kpi_uploader_{ticker}_{st.session_state.get(f'kpi_upload_gen_{ticker}', 0)}"


def _show_outcomes(outcomes: list[UploadOutcome]) -> None:
    for outcome in outcomes:
        if outcome.error:
            st.error(f"**{outcome.filename}** failed: {outcome.error}")
        elif outcome.segments > 0:
            st.success(f"Ingested **{outcome.filename}** as {outcome.segments:,} searchable chunks.")
        else:
            st.warning(f"**{outcome.filename}** was uploaded but produced no searchable content.")
        for warning in outcome.warnings:
            st.warning(warning)


def _ingest_files(
    ticker: str,
    files: list[Any],
    controller: KpiController,
    company_controller: CompanyController,
    config: dict[str, Any],
) -> list[UploadOutcome]:
    """
    Upload and ingest the selected files, record them as this review's sources, and clear the uploader.

    Returns:
        list[UploadOutcome]: One outcome per file.
    """
    status = st.empty()
    outcomes = controller.upload_sources(
        [(file.name, file.getvalue()) for file in files],
        config,
        on_progress=lambda name: status.info(f"Ingesting {name}..."),
    )
    status.empty()
    for outcome in outcomes:
        if outcome.segments > 0 and not outcome.error:
            st.session_state.setdefault(f"kpi_new_sources_{ticker}", []).append(
                SourceFile(outcome.filename, outcome.kind, outcome.path, outcome.segments)
            )
    if any(outcome.segments > 0 for outcome in outcomes):
        company_controller.sync_companies_incremental([ticker], config)
    gen_key = f"kpi_upload_gen_{ticker}"
    st.session_state[gen_key] = st.session_state.get(gen_key, 0) + 1
    return outcomes


def _render_sources(
    ticker: str, controller: KpiController, company_controller: CompanyController, config: dict[str, Any]
) -> list[Any]:
    """
    Render the source uploader.

    Returns:
        list[Any]: The files currently selected but not yet ingested.
    """
    allowed = [ext.lstrip(".") for ext in config["allowed_extensions"]]
    report_key = f"kpi_ingest_report_{ticker}"
    with st.expander("Add sources: model, transcripts, other documents", expanded=False):
        if previous := st.session_state.get(report_key):
            _show_outcomes(previous)
        uploaded = st.file_uploader(
            "Choose files",
            type=allowed,
            accept_multiple_files=True,
            key=_uploader_key(ticker),
            help="Selecting files does not upload them. Click Upload and ingest, or just run the review.",
        )
        if not uploaded:
            return []
        for file in uploaded:
            kind = "financial model" if model.classify_upload(file.name) == "financial_model" else "document"
            st.write(f"**{file.name}**  ·  {file.size / 1024:.1f} KB  ·  {kind}")
        if not any(model.classify_upload(file.name) == "financial_model" for file in uploaded):
            st.caption("No `MODL_*.xlsx` selected: the review needs the Bloomberg model to be ingested at some point.")

        if st.button("Upload and ingest", key=f"kpi_upload_btn_{ticker}"):
            st.session_state[report_key] = _ingest_files(ticker, uploaded, controller, company_controller, config)
            st.rerun()
    return list(uploaded)


# ── results ──────────────────────────────────────────────────────────────────────


def _render_analysis(ticker: str, company_name: str, analysis: KpiAnalysis, controller: KpiController) -> None:
    review = analysis.review
    source_labels = controller.source_labels(analysis.documents)

    st.subheader("Review")
    if analysis.created_at:
        st.caption(
            f"Made {analysis.created_at[:16].replace('T', ' ')} UTC"
            + (f" · focus: {analysis.focus_notes}" if analysis.focus_notes else "")
        )
    st.caption(
        f"Latest reported period: **{analysis.as_of}** · {len(analysis.series)} metrics reviewed · "
        f"{len(analysis.documents)} document passages"
        + (f" · {analysis.omitted_series} smaller-moving metrics left out" if analysis.omitted_series else "")
    )
    if not analysis.documents:
        st.info("No transcripts or other documents are indexed for this company, so the review rests on the model alone.")
    _render_export(ticker, company_name, analysis, controller, source_labels)
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

    if analysis.sources:
        _render_source_files(ticker, analysis)

    if analysis.documents:
        with st.expander("Document passages used"):
            for index, chunk in enumerate(analysis.documents, start=1):
                st.markdown(f"**[{index}] {source_labels[index]}**")
                st.caption(_escape_currency(chunk.content[:600]) + ("…" if len(chunk.content) > 600 else ""))


def _render_export(
    ticker: str, company_name: str, analysis: KpiAnalysis, controller: KpiController, source_labels: dict[int, str]
) -> None:
    """Export the review: a Word report (built on request, since it draws charts) or plain Markdown."""
    docx_key = f"kpi_docx_{ticker}_{analysis.created_at}"
    word_col, markdown_col, _ = st.columns([2, 2, 4])
    if docx_key in st.session_state:
        word_col.download_button(
            "Download Word report",
            data=st.session_state[docx_key],
            file_name=model.report_filename(ticker, analysis.as_of, "docx"),
            mime=DOCX_MIME,
            icon=":material/description:",
            type="primary",
            key=f"{docx_key}_dl",
        )
    elif word_col.button("Prepare Word report", icon=":material/description:", key=f"{docx_key}_build"):
        try:
            with st.spinner("Building the report..."):
                st.session_state[docx_key] = controller.build_report(analysis, ticker, company_name)
        except Exception as error:
            logger.exception("Could not build the KPI report for %s", ticker)
            st.error(f"Could not build the report: {error}")
        else:
            st.rerun()
    markdown_col.download_button(
        "Markdown",
        data=model.review_to_markdown(company_name, analysis.as_of, analysis.review, source_labels),
        file_name=model.report_filename(ticker, analysis.as_of, "md"),
        mime="text/markdown",
        icon=":material/download:",
        key=f"kpi_md_{ticker}_{analysis.created_at}",
    )


def _render_source_files(ticker: str, analysis: KpiAnalysis) -> None:
    """List the files added for this review, each fetchable from the Data Lake on demand."""
    with st.expander(f"Files added for this review ({len(analysis.sources)})"):
        for source in analysis.sources:
            name_col, action_col = st.columns([4, 1])
            kind = "model" if source.kind == "financial_model" else "document"
            name_col.write(f"**{source.filename}** · {kind} · {source.segments:,} chunks")
            cache_key = f"kpi_file_{ticker}_{source.path}"
            if cache_key not in st.session_state:
                if action_col.button("Fetch", key=f"{cache_key}_fetch"):
                    try:
                        st.session_state[cache_key] = download_from_datalake(datalake_account(), source.path)
                        st.rerun()
                    except Exception as error:
                        logger.exception("Could not fetch %s", source.path)
                        action_col.error(f"Not available: {error}")
                continue
            action_col.download_button(
                "Download", st.session_state[cache_key], file_name=source.filename, key=f"{cache_key}_dl"
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
