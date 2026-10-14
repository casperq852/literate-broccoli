"""Controller for the chat topic: quick-questions wiring and RAG orchestration."""

from __future__ import annotations

from collections.abc import Generator
from datetime import date
from pathlib import Path
from typing import Any

import streamlit as st
import yaml
from portfolio_dashboard.chat_context_model import detect_portfolio_question, format_portfolio_context

from chat import chat_model
from database import cache
from search import search_model

_DEFAULT_QUICK_QUESTIONS_PATH: Path = Path(__file__).resolve().parent.parent / "configs" / "quick_questions.yaml"


@st.cache_data
def _load_quick_questions_file(path: str) -> list[dict]:
    """Load quick-question templates from ``path``, or an empty list if absent."""
    qq_path = Path(path)
    if not qq_path.exists():
        return []
    with open(qq_path) as f:
        return yaml.safe_load(f) or []


class ChatController:
    """Wires chat-topic quick questions and end-to-end RAG answering."""

    def __init__(self, quick_questions_path: Path = _DEFAULT_QUICK_QUESTIONS_PATH) -> None:
        self._quick_questions_path = quick_questions_path

    def load_quick_questions(self) -> list[dict]:
        """Return the configured quick-question templates, or an empty list if absent."""
        return _load_quick_questions_file(str(self._quick_questions_path))

    def resolve_parts(self, quick_question: dict, company: str) -> list[dict]:
        """Resolve a multi-part quick question's placeholders into concrete prompts."""
        return chat_model.resolve_quick_question_parts(quick_question, company)

    def _portfolio_context_chunk(self, question: str) -> search_model.Chunk | None:
        """
        Return a synthetic ``Chunk`` grounding ``question`` in cached portfolio
        metrics, or ``None`` if the question doesn't name a known portfolio.

        A direct structured lookup (database.cache.get_portfolio_metrics(),
        defaulting to the latest date), not vector/keyword search -- portfolio
        numbers need exact, deterministic answers, which retrieval over
        embedded text can't reliably guarantee. Returned as a Chunk so it
        flows through the existing citation numbering/rendering in
        search_model.format_context() unchanged.
        """
        portfolio = detect_portfolio_question(question, cache.get_portfolio_names())
        if not portfolio:
            return None
        record = cache.get_portfolio_metrics(portfolio)
        if not record:
            return None
        return search_model.Chunk(
            content=format_portfolio_context(record),
            metric=None,
            period_label=None,
            period_end_date=None,
            is_estimate=None,
            document_type="portfolio_data",
            document_date=record["as_of_date"],
            publisher="Portfolio Dashboard",
            score=1.0,
            currency=record.get("base_currency"),
        )

    def ask(
        self,
        question: str,
        config: dict[str, Any],
        company: str | None = None,
        source_type: str | None = None,
        history: list[dict[str, str]] | None = None,
        publishers: list[str] | None = None,
        document_types: list[str] | None = None,
        date_from: date | None = None,
        date_to: date | None = None,
        stream: bool = False,
        retrieval_query: str | None = None,
    ) -> tuple[str | Generator[str, None, None], list[search_model.Chunk], str]:
        """
        End-to-end history-aware RAG: condense -> retrieve -> answer.

        If ``question`` names a portfolio with cached metrics (see
        ``_portfolio_context_chunk``), a synthetic chunk grounding it in the
        latest structured portfolio data is prepended to the retrieved chunks
        before the answer step.

        ``retrieval_query``, if provided, is used for Azure Search instead of
        condensing ``question``. Use this when the question is a long roleplay
        prompt whose keywords would confuse BM25 -- pass a short, topic-focused
        query for retrieval and let the full ``question`` go only to the LLM
        answer step.

        Args:
            question (str): The user's question, sent to the LLM answer step.
            config (dict[str, Any]): The equities app config block (index_name,
                top_k, chat_deployment, max_answer_tokens).
            company (str | None): Ticker/name to scope retrieval to.
            source_type (str | None): Restrict to "financial_model" or "document".
            history (list[dict[str, str]] | None): Prior conversation turns.
            publishers (list[str] | None): Restrict to these publisher values.
            document_types (list[str] | None): Restrict to these document types.
            date_from (date | None): Only documents dated on or after this date.
            date_to (date | None): Only documents dated on or before this date.
            stream (bool): Whether to stream the answer.
            retrieval_query (str | None): Override query used for retrieval only.

        Returns:
            tuple[str | Generator[str, None, None], list[search_model.Chunk], str]:
            The answer (text or stream), the retrieved chunks, and the
            standalone query used for retrieval, so the UI can show sources
            and the condensed query.
        """
        history = history or []
        standalone = retrieval_query or search_model.condense_query(
            question=question,
            history=history,
            chat_deployment=config["chat_deployment"],
        )
        chunks = search_model.retrieve(
            query=standalone,
            index_name=config["index_name"],
            top_k=config["top_k"],
            company=company,
            source_type=source_type,
            publishers=publishers,
            document_types=document_types,
            date_from=date_from,
            date_to=date_to,
        )
        portfolio_chunk = self._portfolio_context_chunk(question)
        if portfolio_chunk:
            chunks = [portfolio_chunk, *chunks]
        text = search_model.answer(
            question=question,
            chunks=chunks,
            chat_deployment=config["chat_deployment"],
            max_tokens=config["max_answer_tokens"],
            history=history,
            stream=stream,
        )
        return text, chunks, standalone
