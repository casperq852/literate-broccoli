"""Pure query-templating functions for the chat quick-questions feature."""

from __future__ import annotations


def resolve_quick_question_parts(quick_question: dict, company: str) -> list[dict]:
    """
    Resolve ``{company}`` placeholders and build a retrieval query for each part.

    Args:
        quick_question (dict): A quick-question template with a "parts" list,
            each part having "label", "prompt", and optional "search_hint".
            May also carry a "word_limit" applied to every part's prompt.
        company (str): The company ticker/name to substitute into placeholders,
            or an empty string to leave prompts unscoped.

    Returns:
        list[dict]: One ``{"label", "prompt", "retrieval_query"}`` dict per part.
    """
    word_limit = quick_question.get("word_limit")
    resolved: list[dict] = []
    for part in quick_question["parts"]:
        prompt_text = part["prompt"].replace("{company}", company) if company else part["prompt"]
        if word_limit:
            prompt_text = prompt_text.strip() + f"\n\nAnswer in at most {word_limit} words."
        hint = part.get("search_hint", "")
        retrieval_query = f"{company} {hint}".strip() if company else hint
        resolved.append(
            {
                "label": part["label"],
                "prompt": prompt_text.strip(),
                "retrieval_query": retrieval_query or None,
            }
        )
    return resolved
