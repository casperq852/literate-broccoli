"""
Low-level Azure AI Search / Azure AI Foundry retrieval primitives.

Client construction, hybrid retrieval, and single-shot chat completions shared
by every topic controller (chat, investment case). Nothing here decides *when*
to retrieve, *what* system prompt a given feature should use, or how to
orchestrate multi-step generation -- that is Controller-owned per topic.

Client construction goes through ``avb_llm.clients`` (the org's internal LLM
toolkit) rather than constructing ``openai``/``azure-search-documents``
clients directly -- this is the org-mandated way to talk to Azure AI Foundry
and Azure AI Search, and it adds DefaultAzureCredential/managed-identity
fallback on top of the API-key auth this module already used.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Generator
from dataclasses import dataclass
from datetime import date
from typing import Any

import streamlit as st
from avb_llm.clients import AzureAIFoundryClient, AzureAISearchClient
from azure.search.documents.models import VectorizableTextQuery

from helpers import equities_config

logger = logging.getLogger(__name__)

RESEARCH_NOTE_DOC_TYPE = "research_note"

# The enricher's documentType enum (config.yaml's enrichment prompt: "One of
# exactly: research_note | financial_model | earnings_transcript |
# annual_report | investor_presentation | other"). Shared here so every
# document_types filter across topics (Chat's picker, Investment Case's
# demo-mode exclusion) stays in sync with one definition instead of drifting
# copies.
ALL_DOCUMENT_TYPES = [
    "research_note",
    "financial_model",
    "earnings_transcript",
    "annual_report",
    "investor_presentation",
    "other",
]


@dataclass(frozen=True)
class Chunk:
    """A single retrieved search result."""

    content: str
    metric: str | None
    period_label: str | None
    period_end_date: str | None
    is_estimate: str | None
    document_type: str | None
    document_date: str | None
    publisher: str | None
    score: float
    segment: str | None = None
    currency: str | None = None
    unit: str | None = None
    companies: tuple[str, ...] = ()


# Maps raw publisher values stored in the index to human-readable display names.
PUBLISHER_ALIASES: dict[str, str] = {
    "big data federation inc": "Financial Model",
    "big data federation": "Financial Model",
    "bigdata federation": "Financial Model",
}


def normalize_publisher(name: str | None) -> str | None:
    """
    Return a cleaned publisher display name, resolving known index artefacts.

    Args:
        name (str | None): The raw publisher value stored in the index.

    Returns:
        str | None: The display name, or None if ``name`` was None.
    """
    if name is None:
        return None
    return PUBLISHER_ALIASES.get(name.lower().strip(), name)


def format_source_label(
    publisher: str | None,
    document_type: str | None,
    document_date: str | None,
    *,
    companies: tuple[str, ...] | list[str] | None = None,
    humanize_type: bool = False,
) -> str:
    """
    Join company / publisher / document type / date into one citation label.

    Args:
        publisher (str | None): Raw publisher value from the index.
        document_type (str | None): Raw document type value from the index.
        document_date (str | None): ISO document date from the index.
        companies (tuple[str, ...] | list[str] | None): Company name(s) the
            source document is about, if known. Leading the label with this
            is what actually distinguishes two sources that share the same
            publisher/type/date (e.g. two same-day notes from one broker,
            each about a different company) -- without it they render as
            identical, indistinguishable lines.
        humanize_type (bool): Replace underscores with spaces in the document
            type for display (e.g. "research_note" -> "research note").

    Returns:
        str: A "company · publisher · type · date" label, or "Unknown source"
        if none of the parts are available.
    """
    doc_type = document_type.replace("_", " ") if (humanize_type and document_type) else document_type
    pub = normalize_publisher(publisher)
    company = ", ".join(companies) if companies else None
    parts = [part for part in (company, pub, doc_type, (document_date or "")[:10] or None) if part]
    return " · ".join(parts) if parts else "Unknown source"


@st.cache_resource
def get_chat_client(chat_deployment: str, _config: dict[str, Any] | None = None) -> AzureAIFoundryClient:
    """
    Return a cached Azure AI Foundry client for the given chat deployment.

    Args:
        chat_deployment (str): The chat model deployment name to bind this
            client to (avb_llm bakes the deployment into the client itself).
        _config (dict[str, Any] | None): Explicit equities config, bypassing
            ``equities_config()``/``st.session_state``. Only needed by callers
            with no live Streamlit session (e.g. the scheduler background
            thread in ``scheduler/``) -- the leading underscore is Streamlit's
            own convention for excluding an unhashable param from this
            function's cache key.

    Returns:
        AzureAIFoundryClient: The cached client, one per distinct deployment.
    """
    cfg = _config or equities_config()
    return AzureAIFoundryClient(
        endpoint=cfg["foundry_endpoint"],
        api_version=cfg["foundry_api_version"],
        chat_deployment=chat_deployment,
    )


@st.cache_resource
def get_search_client(index_name: str) -> AzureAISearchClient:
    """Return a cached Azure AI Search client for the given index."""
    logger.debug("creating AzureAISearchClient for index '%s'", index_name)
    t0 = time.perf_counter()
    client = AzureAISearchClient(
        endpoint=equities_config()["search_endpoint"],
        index_name=index_name,
    )
    logger.debug("AzureAISearchClient ready (%.2fs)", time.perf_counter() - t0)
    return client


@st.cache_data(ttl=300)
def get_facets(index_name: str) -> dict[str, list[str]]:
    """
    Return filterable facet values (publishers, document types) from the index.

    Cached for 5 minutes so the filter widgets populate without a live query on
    every render.

    Args:
        index_name (str): The Azure AI Search index to query.

    Returns:
        dict[str, list[str]]: ``{"publishers": [...], "document_types": [...]}``,
        each sorted and empty on failure.
    """
    t0 = time.perf_counter()
    try:
        client = get_search_client(index_name)
        results = client.search(
            search_text="*",
            facets=["publisher,count:50", "documentType,count:20"],
            top=0,
        )
        raw = results.get_facets() or {}
        out = {
            "publishers": sorted(f["value"] for f in raw.get("publisher", []) if f.get("value")),
            "document_types": sorted(f["value"] for f in raw.get("documentType", []) if f.get("value")),
        }
        logger.debug(
            "facets ready — %d publishers, %d doc types (%.2fs)",
            len(out["publishers"]),
            len(out["document_types"]),
            time.perf_counter() - t0,
        )
        return out
    except Exception:  # noqa: BLE001
        logger.warning("facet fetch failed (%.2fs)", time.perf_counter() - t0)
        return {"publishers": [], "document_types": []}


def build_odata_filter(
    source_type: str | None,
    publishers: list[str] | None = None,
    document_types: list[str] | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
) -> str | None:
    """
    Build an OData filter from optional search criteria.

    ``company`` is intentionally not a parameter here -- ticker formats are
    inconsistent across sources ("NVO", "NOVOB DC", "NOVO-B DC" all refer to
    the same company). Instead, the ticker is prepended to the search query in
    ``retrieve()`` so that BM25 keyword + vector similarity surface the right
    documents regardless of format.

    Args:
        source_type (str | None): Restrict to "financial_model" or "document".
        publishers (list[str] | None): Restrict to these publisher values.
        document_types (list[str] | None): Restrict to these document types.
        date_from (date | None): Only documents dated on or after this date.
        date_to (date | None): Only documents dated on or before this date.

    Returns:
        str | None: An OData filter expression, or None if no criteria were given.
    """
    clauses: list[str] = []
    if source_type == "document":
        # Nothing set sourceType for a regular document until
        # vectorization.extraction.extraction_controller.EquitiesExtractor.extract
        # started defaulting it, so it's null on every document ingested
        # before that fix -- and OData's "eq" never matches null, so a plain
        # "sourceType eq 'document'" would silently exclude all of them (see
        # database.cache.sync_from_azure for the same gap on the sync side).
        clauses.append("(sourceType eq 'document' or sourceType eq null)")
    elif source_type:
        clauses.append(f"sourceType eq '{source_type}'")
    if publishers:
        pub_parts = " or ".join(f"publisher eq '{publisher}'" for publisher in publishers)
        clauses.append(f"({pub_parts})")
    if document_types:
        type_parts = " or ".join(f"documentType eq '{document_type}'" for document_type in document_types)
        clauses.append(f"({type_parts})")
    if date_from:
        clauses.append(f"documentDate ge {date_from.isoformat()}T00:00:00Z")
    if date_to:
        clauses.append(f"documentDate le {date_to.isoformat()}T23:59:59Z")
    return " and ".join(clauses) if clauses else None


def retrieve(
    query: str,
    index_name: str,
    top_k: int,
    company: str | None = None,
    source_type: str | None = None,
    publishers: list[str] | None = None,
    document_types: list[str] | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
) -> list[Chunk]:
    """
    Hybrid search (vector + keyword) against the index, optionally filtered.

    The embedding is produced server-side by the index's vectorizer, so the
    raw text is passed as a vectorizable query rather than embedding it here.

    Args:
        query (str): The search text.
        index_name (str): The Azure AI Search index to query.
        top_k (int): Maximum number of chunks to return.
        company (str | None): Ticker or name prepended to the query to anchor
            retrieval to a specific company.
        source_type (str | None): Restrict to "financial_model" or "document".
        publishers (list[str] | None): Restrict to these publisher values.
        document_types (list[str] | None): Restrict to these document types.
        date_from (date | None): Only documents dated on or after this date.
        date_to (date | None): Only documents dated on or before this date.

    Returns:
        list[Chunk]: The retrieved chunks, ranked by relevance.
    """
    effective_query = f"{company} {query}" if company else query

    client = get_search_client(index_name)
    vector_query = VectorizableTextQuery(
        text=effective_query,
        k_nearest_neighbors=top_k,
        fields="contentVector",
    )
    results = client.search(
        search_text=effective_query,
        vector_queries=[vector_query],
        filter=build_odata_filter(
            source_type=source_type,
            publishers=publishers,
            document_types=document_types,
            date_from=date_from,
            date_to=date_to,
        ),
        select=[
            "content",
            "metric",
            "segment",
            "periodLabel",
            "periodEndDate",
            "isEstimate",
            "currency",
            "unit",
            "documentType",
            "documentDate",
            "publisher",
            "companies",
        ],
        top=top_k,
    )

    return [_chunk_from_result(result) for result in results]


def _chunk_from_result(result: dict) -> Chunk:
    """Build a ``Chunk`` from one raw Azure AI Search result document."""
    return Chunk(
        content=result.get("content", ""),
        metric=result.get("metric"),
        period_label=result.get("periodLabel"),
        period_end_date=result.get("periodEndDate"),
        is_estimate=result.get("isEstimate"),
        document_type=result.get("documentType"),
        companies=tuple(result.get("companies") or ()),
        document_date=result.get("documentDate"),
        publisher=result.get("publisher"),
        score=result.get("@search.score", 0.0),
        segment=result.get("segment"),
        currency=result.get("currency"),
        unit=result.get("unit"),
    )


_BULK_PAGE_SIZE = 1000  # Azure AI Search's maximum $top per request


def build_financial_model_filter(ticker: str, company_name: str | None = None) -> str:
    """
    Build the OData filter selecting one company's financial-model chunks.

    A model's chunks are tagged with the company's ticker in ``tickers`` and
    its (canonical) name in ``companies`` -- the index's ``companies`` field
    holds names, not tickers (see ``database.cache``'s sync mapping). The
    ticker is matched against ``tickers`` first; ``companies`` is also tried
    with the ticker (how this filter originally worked) and with the company
    name, so a model tagged any of those ways is found.

    Args:
        ticker (str): The company ticker as stored in this app's company cache.
        company_name (str | None): The company's display name, if known.

    Returns:
        str: An OData filter expression.
    """

    def escape(value: str) -> str:
        return value.replace("'", "''")

    matches = [
        f"tickers/any(t: t eq '{escape(ticker)}')",
        f"companies/any(c: c eq '{escape(ticker)}')",
    ]
    if company_name:
        matches.append(f"companies/any(c: c eq '{escape(company_name)}')")
    return f"sourceType eq 'financial_model' and ({' or '.join(matches)})"


def retrieve_financial_model_bulk(
    index_name: str, ticker: str, max_records: int, company_name: str | None = None
) -> list[Chunk]:
    """
    Pull every financial-model chunk for one company via a filter-only query.

    Unlike ``retrieve()``, this issues no vector query and is not capped to a
    small ``top_k`` -- it is a structured, complete pull of one company's
    ingested Bloomberg model (every metric x period record), ordered
    oldest-to-newest by period end date so downstream historical-trend
    calculations don't need to re-sort. ``max_records`` is a hard safety cap;
    if the company's model has more records than that, results are truncated
    to the oldest ``max_records`` -- this should not happen in practice for a
    normal quarterly/annual model.

    Pages explicitly via ``skip`` rather than passing ``top=max_records`` to a
    single call and trusting plain iteration to auto-continue past Azure AI
    Search's actual per-request cap (1000) -- confirmed by hand that plain
    iteration silently stops short of the true result count well before that
    cap for a query returning more than one page, which would have made this
    function return an incomplete (and non-obviously incomplete) financial
    history for any company whose model exceeds one page. See
    ``database.cache._paginate_search_paths`` for the same fix applied there.

    Args:
        index_name (str): The Azure AI Search index to query.
        ticker (str): The company ticker as stored in this app's company cache
            (the canonical ticker stored in the index's ``tickers`` field at ingest time).
        max_records (int): Hard cap on records returned.
        company_name (str | None): The company's display name; also matched
            against the index's ``companies`` field. See ``build_financial_model_filter``.

    Returns:
        list[Chunk]: Every financial_model chunk for this company, oldest-first.
    """
    odata_filter = build_financial_model_filter(ticker, company_name)

    client = get_search_client(index_name)
    select_fields = [
        "content",
        "metric",
        "segment",
        "periodLabel",
        "periodEndDate",
        "isEstimate",
        "currency",
        "unit",
        "documentType",
        "documentDate",
        "publisher",
    ]

    chunks: list[Chunk] = []
    skip = 0
    while len(chunks) < max_records:
        page = list(
            client.search(
                search_text="*",
                filter=odata_filter,
                order_by=["periodEndDate asc"],
                select=select_fields,
                top=min(_BULK_PAGE_SIZE, max_records - len(chunks)),
                skip=skip,
            )
        )
        if not page:
            break
        chunks.extend(_chunk_from_result(result) for result in page)
        if len(page) < _BULK_PAGE_SIZE:
            break
        skip += _BULK_PAGE_SIZE

    if len(chunks) >= max_records:
        logger.warning(
            "retrieve_financial_model_bulk hit max_records=%d for ticker=%s; results may be truncated",
            max_records,
            ticker,
        )
    return chunks[:max_records]


_CONDENSE_PROMPT = (
    "Given the conversation so far and a follow-up question, rewrite the follow-up "
    "as a standalone search query that captures the full intent, resolving any "
    "pronouns or references to earlier turns (e.g. 'it', 'that company', 'those "
    "margins'). Return ONLY the rewritten query text, nothing else. If the question "
    "is already standalone, return it unchanged."
)


def condense_query(
    question: str,
    history: list[dict[str, str]],
    chat_deployment: str,
) -> str:
    """
    Rewrite a follow-up question into a standalone search query using history.

    ``history`` is a list of ``{"role": "user"|"assistant", "content": str}``
    dicts. When there is no prior history the question is returned unchanged
    (no LLM call). Pronoun/reference resolution lets follow-ups like "what about
    its margins?" retrieve the right chunks.

    Args:
        question (str): The follow-up question as typed by the user.
        history (list[dict[str, str]]): Prior conversation turns.
        chat_deployment (str): Chat model deployment name.

    Returns:
        str: A standalone search query, or the original question on failure.
    """
    if not history:
        return question

    transcript = "\n".join(f"{turn['role']}: {turn['content']}" for turn in history[-6:])
    user_prompt = f"Conversation so far:\n{transcript}\n\nFollow-up question: {question}\n\nStandalone query:"

    try:
        rewritten = (
            get_chat_client(chat_deployment)
            .chat(
                messages=[
                    {"role": "system", "content": _CONDENSE_PROMPT},
                    {"role": "user", "content": user_prompt},
                ],
            )
            .strip()
        )
        return rewritten or question
    except Exception:  # noqa: BLE001 - fall back to the raw question on any failure
        return question


def format_context(chunks: list[Chunk]) -> str:
    """
    Render retrieved chunks into a numbered context block for the prompt.

    Args:
        chunks (list[Chunk]): Retrieved chunks, in the order to present them.

    Returns:
        str: A "[1] ... [2] ..." formatted context block.
    """
    lines: list[str] = []
    for index, chunk in enumerate(chunks, start=1):
        tags: list[str] = []
        if chunk.document_type:
            tags.append(chunk.document_type)
        if chunk.publisher:
            tags.append(chunk.publisher)
        if chunk.document_date:
            tags.append(f"as of {chunk.document_date[:10]}")
        suffix = f"  [{', '.join(tags)}]" if tags else ""
        lines.append(f"[{index}] {chunk.content}{suffix}")
    return "\n".join(lines)


def format_investment_context(chunks: list[Chunk]) -> str:
    """
    Format chunks for investment case generation: metadata header first, then content.

    Args:
        chunks (list[Chunk]): Retrieved chunks, in the order to present them.

    Returns:
        str: A "[1] publisher | type | date\\ncontent" formatted context block.
    """
    parts: list[str] = []
    for index, chunk in enumerate(chunks, start=1):
        pub = normalize_publisher(chunk.publisher)
        tags = [
            tag for tag in (pub, chunk.document_type, chunk.document_date[:10] if chunk.document_date else None) if tag
        ]
        header = f"[{index}] {' | '.join(tags)}" if tags else f"[{index}]"
        parts.append(f"{header}\n{chunk.content}")
    return "\n\n".join(parts)


_SYSTEM_PROMPT = (
    "You are an equity research assistant for a portfolio manager. Ground your "
    "answer in the provided context, which comes from the documents (broker notes, "
    "earnings transcripts, annual reports, investor presentations, and more) and "
    "Bloomberg financial models indexed for the companies you cover -- be precise "
    "with figures, always state the period and whether a number is reported or an "
    "estimate, and cite the publisher and document date where relevant. You may "
    "also draw on your own general financial and sector knowledge to add context, explain concepts, or "
    "cover points the provided context doesn't address -- but clearly distinguish "
    "what comes from the retrieved documents versus your own background "
    "knowledge, and never state general knowledge as if it were a sourced fact. "
    "If neither the context nor your general knowledge can answer the question, "
    "say so plainly rather than guessing."
)

_REFINE_PROMPT = (
    "You are a quality editor for equity research answers. Rewrite the draft to:\n"
    "1. Remove repetition — state each point exactly once.\n"
    "2. Cut filler — every sentence must add information; remove hedging preambles.\n"
    "3. Reduce broker/analyst name-dropping — replace repeated firm names with 'analysts', "
    "'brokers', or 'consensus'; a firm name may appear at most once if directly material.\n"
    "4. Lead with the conclusion — key finding first, supporting evidence after.\n"
    "5. Stay faithful — do not introduce any figures or facts not present in the draft.\n"
    "Return ONLY the rewritten answer, no preamble or commentary."
)


def answer(
    question: str,
    chunks: list[Chunk],
    chat_deployment: str,
    max_tokens: int,
    history: list[dict[str, str]] | None = None,
    stream: bool = False,
) -> str | Generator[str, None, None]:
    """
    Ask the chat model to answer the question from the retrieved chunks.

    When ``stream=True`` returns a generator that yields text chunks as they
    arrive from the model; the caller is responsible for collecting them.
    When ``stream=False`` (default) blocks until the full response is ready
    and returns a plain string.

    Prior conversation turns in ``history`` are included before the current
    question so the model can resolve references and stay consistent.

    Args:
        question (str): The user's question.
        chunks (list[Chunk]): Retrieved chunks to ground the answer in.
        chat_deployment (str): Chat model deployment name.
        max_tokens (int): Max completion tokens.
        history (list[dict[str, str]] | None): Prior conversation turns.
        stream (bool): Whether to stream the response.

    Returns:
        str | Generator[str, None, None]: The answer text, or a token generator.
    """
    if not chunks:
        no_answer = (
            "I couldn't find anything relevant in the indexed documents for that "
            "question. Try rephrasing, or check whether the relevant document has "
            "been uploaded."
        )
        return (chunk for chunk in [no_answer]) if stream else no_answer  # type: ignore[return-value]

    context = format_context(chunks)
    messages: list[dict[str, str]] = [{"role": "system", "content": _SYSTEM_PROMPT}]
    for turn in (history or [])[-6:]:
        messages.append({"role": turn["role"], "content": turn["content"]})
    messages.append({"role": "user", "content": f"Context:\n{context}\n\nQuestion: {question}"})

    foundry_client = get_chat_client(chat_deployment)

    if stream:
        # AzureAIFoundryClient.chat() only returns a finished string, so streaming
        # goes through the raw client it wraps -- still avb_llm-issued and
        # avb_llm-authenticated, just without the non-streaming convenience wrapper.
        response = foundry_client.client.chat.completions.create(
            model=chat_deployment,
            messages=messages,
            stream=True,
        )

        def _gen() -> Generator[str, None, None]:
            for event in response:
                if event.choices and event.choices[0].delta.content:
                    yield event.choices[0].delta.content

        return _gen()

    return foundry_client.chat(messages=messages)


def validate_answer(text: str, chat_deployment: str) -> str:
    """
    Quality-editing pass: remove repetition, cut filler, reduce broker name-dropping.

    Args:
        text (str): The draft answer to refine.
        chat_deployment (str): Chat model deployment name.

    Returns:
        str: The refined answer, or the original ``text`` on failure.
    """
    try:
        refined = get_chat_client(chat_deployment).chat(
            messages=[
                {"role": "system", "content": _REFINE_PROMPT},
                {"role": "user", "content": f"Draft answer:\n\n{text}"},
            ],
        )
        return (refined or text).strip()
    except Exception:  # noqa: BLE001
        return text
