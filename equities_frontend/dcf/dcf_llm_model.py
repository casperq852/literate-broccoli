"""
Pure prompt-templating and response-validation functions for LLM-assisted DCF
assumption suggestions.

Nothing here makes the LLM call itself (that is Controller-owned orchestration,
in ``dcf_controller.py``) -- these functions only build the prompt text and
validate/parse the model's response.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import TypedDict

from search.search_model import Chunk


class ParsedSuggestion(TypedDict):
    """A validated, range-checked LLM assumption suggestion, keyed and typed per field."""

    revenue_growth_rate: float
    ebit_margin: float
    discount_rate: float
    rationale: str
    cited_indices: list[int]


# Sane financial ranges: a suggestion outside these bounds is treated as
# unusable rather than silently passed through to the UI.
_MIN_GROWTH_RATE, _MAX_GROWTH_RATE = -0.5, 1.0
_MIN_EBIT_MARGIN, _MAX_EBIT_MARGIN = 0.0, 1.0
_MIN_DISCOUNT_RATE, _MAX_DISCOUNT_RATE = 0.02, 0.25


@dataclass(frozen=True)
class LlmAssumptionSuggestion:
    """The LLM's suggested starting DCF assumptions, or an unavailable result."""

    available: bool
    revenue_growth_rate: float | None = None
    ebit_margin: float | None = None
    discount_rate: float | None = None
    rationale: str = ""
    sources: list[Chunk] = field(default_factory=list)


def build_suggestion_prompt(user_prompt_template: str, company: str, context: str) -> str:
    """
    Fill the LLM-suggestion user-prompt template with this company's research context.

    Args:
        user_prompt_template (str): The prompt template (from dcf_content.yaml),
            with ``{company}`` and ``{context}`` placeholders.
        company (str): The company name.
        context (str): The formatted, numbered research context.

    Returns:
        str: The filled-in prompt, ready to send as the user message.
    """
    return user_prompt_template.format(company=company, context=context)


def parse_llm_suggestion(raw_text: str) -> ParsedSuggestion | None:
    """
    Strip markdown fences, parse JSON, and range-validate an LLM assumption suggestion.

    Args:
        raw_text (str): The raw LLM response text.

    Returns:
        ParsedSuggestion | None: The validated suggestion, or None if
        parsing/validation failed at any step -- callers must treat None as
        "no usable suggestion" rather than propagate a partially-valid or
        out-of-range value.
    """
    cleaned = re.sub(r"^```(?:json)?\s*\n?", "", raw_text.strip())
    cleaned = re.sub(r"\n?```\s*$", "", cleaned)

    try:
        parsed = json.loads(cleaned.strip())
    except json.JSONDecodeError:
        return None

    if not isinstance(parsed, dict):
        return None

    try:
        growth_rate = float(parsed["revenue_growth_rate"])
        ebit_margin = float(parsed["ebit_margin"])
        discount_rate = float(parsed["discount_rate"])
        rationale = str(parsed.get("rationale", ""))
        cited_indices = [int(index) for index in parsed.get("cited_indices", [])]
    except (KeyError, TypeError, ValueError):
        return None

    in_range = (
        _MIN_GROWTH_RATE <= growth_rate <= _MAX_GROWTH_RATE
        and _MIN_EBIT_MARGIN <= ebit_margin <= _MAX_EBIT_MARGIN
        and _MIN_DISCOUNT_RATE <= discount_rate <= _MAX_DISCOUNT_RATE
    )
    if not in_range:
        return None

    return {
        "revenue_growth_rate": growth_rate,
        "ebit_margin": ebit_margin,
        "discount_rate": discount_rate,
        "rationale": rationale,
        "cited_indices": cited_indices,
    }
