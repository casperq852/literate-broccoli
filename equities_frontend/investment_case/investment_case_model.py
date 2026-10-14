"""
Pure functions for the Investment Case Builder.

Content loading (sections, personas, dialogue templates), prompt templating,
markdown/HTML rendering, and source-citation formatting. Every function here
takes plain strings/dicts/lists -- no Streamlit calls, no LLM calls -- so it
stays independent of how the Controller orchestrates generation or how the
View renders the result.
"""

from __future__ import annotations

import html as html_lib
import re
from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from search.search_model import Chunk, format_source_label

DEFAULT_CONTENT_PATH: Path = Path(__file__).resolve().parent.parent / "configs" / "investment_case_content.yaml"


@lru_cache(maxsize=1)
def load_content(path: str = str(DEFAULT_CONTENT_PATH)) -> dict[str, Any]:
    """
    Load the Investment Case Builder's sections, personas, and dialogue content.

    Args:
        path (str): Path to the YAML content file.

    Returns:
        dict[str, Any]: A dict with keys "moat_options", "sections",
        "speaker_styles", "nl_months", "characters" ({"nl": ..., "en": ...}),
        and "fallback_dialogue" ({"nl": ..., "en": ...}).
    """
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def section_title(section: dict, language: str) -> str:
    """Return the section's title in the requested language."""
    return section["title_en"] if language == "English" else section["title_nl"]


def section_prompt(section: dict, language: str) -> str:
    """Return the section's prompt template in the requested language."""
    return section["prompt_en"] if language == "English" else section["prompt_nl"]


def build_system_context(company: str, sections_text: dict[str, str], language: str) -> str:
    """
    Build the short, punchy system prompt used for per-turn dialogue generation.

    Args:
        company (str): The company under discussion.
        sections_text (dict[str, str]): Generated section id -> section text,
            used to ground the committee's discussion in the case's key facts.
        language (str): "English" or "Nederlands".

    Returns:
        str: A system prompt summarising the case and the dialogue rules.
    """
    # Balanced set of snippets: bull-relevant (summary, moat, expected changes/catalysts)
    # and bear-relevant (risks) sections, so the shared context isn't skewed toward risk
    # framing before the debate even starts.
    snippets = [
        sections_text.get("samenvatting", "") or sections_text.get("track_record", ""),
        sections_text.get("moat", ""),
        sections_text.get("veranderingen", ""),
        sections_text.get("risicos", ""),
    ]
    case_text = "\n\n".join(snippet for snippet in snippets if snippet)
    words = case_text.split()
    if len(words) > 500:  # noqa: PLR2004
        case_text = " ".join(words[:500]) + "..."

    if language == "English":
        return (
            f"You are a member of an investment committee discussing {company}.\n\n"
            f"Key facts from the investment case:\n{case_text}\n\n"
            "RULES — follow these strictly:\n"
            "- Plain, conversational English only. No buzzwords or heavy financial jargon.\n"
            "- React directly to what the PREVIOUS speaker said — you may agree, partially agree, or "
            "disagree, but your reaction must be genuine, not a reflexive pile-on. If you agree, add a "
            "point they didn't make; if you disagree, say specifically why.\n"
            "- Do not simply restate a critique (or a defence) another speaker already made in different words.\n"
            "- Stay true to your own read of the evidence, even if it differs from the rest of the room — "
            "the goal is an honest debate, not forced consensus.\n"
            "- Maximum 3 sentences per turn. Short and to the point.\n"
            "- No bullet points. This is spoken dialogue.\n"
            "- Do NOT start your reply with your own name."
        )
    return (
        f"Je bent lid van een beleggingscomité dat {company} bespreekt.\n\n"
        f"Kernfeiten uit de investment case:\n{case_text}\n\n"
        "REGELS — houd je hier strikt aan:\n"
        "- Eenvoudige spreektaal. Geen zwaar jargon of academisch taalgebruik.\n"
        "- Reageer DIRECT op wat de VORIGE spreker zei — je mag het eens zijn, deels eens zijn, of "
        "oneens zijn, maar je reactie moet oprecht zijn, geen reflexmatige bijval. Als je het eens bent, "
        "voeg dan een punt toe dat nog niet genoemd is; als je het oneens bent, zeg specifiek waarom.\n"
        "- Herhaal niet gewoon een kritiekpunt (of verdediging) dat een andere spreker al met andere "
        "woorden heeft gemaakt.\n"
        "- Blijf trouw aan je eigen lezing van het bewijs, ook als die afwijkt van de rest van de kamer — "
        "het doel is een eerlijk debat, geen geforceerde consensus.\n"
        "- Maximaal 3 zinnen per spreekbeurt. Kort en krachtig.\n"
        "- Geen opsommingen. Dit is gesproken dialoog.\n"
        "- Begin je antwoord NIET met je eigen naam."
    )


def trim_to_complete_sentence(text: str) -> str:
    """
    Trim trailing partial-sentence text, for responses cut off by a token limit.

    Finds the last sentence-ending punctuation (. ! ?) and drops everything
    after it, so a mid-word/mid-clause cutoff never reaches the user. Returns
    the text unchanged if no sentence boundary is found (e.g. a very short
    truncated response), rather than dropping it entirely.

    Args:
        text (str): The raw (possibly truncated) generated text.

    Returns:
        str: The text trimmed to its last complete sentence.
    """
    last_boundary = max(text.rfind("."), text.rfind("!"), text.rfind("?"))
    if last_boundary == -1:
        return text
    return text[: last_boundary + 1].strip()


def md_to_html(text: str) -> str:
    """
    Convert a subset of markdown to HTML (for LLM-generated section text).

    Args:
        text (str): Markdown text using headers, bold/italic, and lists.

    Returns:
        str: The equivalent HTML, with plain paragraphs wrapped in ``<p>``.
    """
    # Escape HTML entities before any other processing
    text = html_lib.escape(text)

    # Headers
    text = re.sub(r"^#### (.+)$", r"<h4>\1</h4>", text, flags=re.MULTILINE)
    text = re.sub(r"^### (.+)$", r"<h3>\1</h3>", text, flags=re.MULTILINE)
    text = re.sub(r"^## (.+)$", r"<h3>\1</h3>", text, flags=re.MULTILINE)
    text = re.sub(r"^# (.+)$", r"<h2>\1</h2>", text, flags=re.MULTILINE)

    # Bold / italic
    text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"\*([^*\n]+?)\*", r"<em>\1</em>", text)

    # Lists — group consecutive bullet / numbered lines
    lines = text.split("\n")
    out: list[str] = []
    in_ul = in_ol = False
    for line in lines:
        ul = re.match(r"^[-*]\s+(.+)$", line)
        ol = re.match(r"^\d+\.\s+(.+)$", line)
        if ul:
            if in_ol:
                out.append("</ol>")
                in_ol = False
            if not in_ul:
                out.append("<ul>")
                in_ul = True
            out.append(f"<li>{ul.group(1)}</li>")
        elif ol:
            if in_ul:
                out.append("</ul>")
                in_ul = False
            if not in_ol:
                out.append("<ol>")
                in_ol = True
            out.append(f"<li>{ol.group(1)}</li>")
        else:
            if in_ul:
                out.append("</ul>")
                in_ul = False
            if in_ol:
                out.append("</ol>")
                in_ol = False
            out.append(line)
    if in_ul:
        out.append("</ul>")
    if in_ol:
        out.append("</ol>")
    text = "\n".join(out)

    # Paragraphs: split on blank lines
    blocks = re.split(r"\n\n+", text)
    parts: list[str] = []
    for block in blocks:
        block = block.strip()
        if not block:
            continue
        if block.startswith(("<h", "<ul", "<ol")):
            parts.append(block)
        else:
            parts.append(f"<p>{block.replace(chr(10), '<br>')}</p>")
    return "\n".join(parts)


def chunk_label(chunk: Chunk) -> str:
    """Return the citation label for one retrieved chunk."""
    return format_source_label(chunk.publisher, chunk.document_type, chunk.document_date)


def grouped_sources(chunks: list[Chunk]) -> list[tuple[str, list[int]]]:
    """
    Group a section's chunks by source label, preserving first-seen order.

    Several retrieved chunks often come from the same document (same publisher/
    type/date), which would otherwise repeat one label under multiple citation
    numbers. This collapses those into a single entry carrying all of its
    citation numbers, e.g. ``[1][4] UBS · Broker Research · 2026-04-22``.

    Args:
        chunks (list[Chunk]): A section's retrieved chunks, in citation order.

    Returns:
        list[tuple[str, list[int]]]: ``(label, citation_numbers)`` pairs, in
        first-seen order.
    """
    order: list[str] = []
    numbers: dict[str, list[int]] = {}
    for i, chunk in enumerate(chunks, start=1):
        label = chunk_label(chunk)
        if label not in numbers:
            numbers[label] = []
            order.append(label)
        numbers[label].append(i)
    return [(label, numbers[label]) for label in order]


_HTML_CSS = """
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
body {
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, sans-serif;
  font-size: 14px; line-height: 1.65; color: #2c2c2c; background: #fff;
}
a { color: #1a5276; }

/* print button */
.no-print {
  position: fixed; top: 18px; right: 24px; z-index: 999;
  background: #1a5276; color: #fff; border: none; border-radius: 6px;
  padding: 10px 20px; font-size: 14px; cursor: pointer; box-shadow: 0 2px 8px rgba(0,0,0,.2);
}
.no-print:hover { background: #154360; }

/* cover */
.cover {
  display: flex; flex-direction: column; align-items: center;
  justify-content: center; min-height: 100vh; padding: 60px 48px;
  text-align: center; border-bottom: 3px solid #d0e4f0;
}
.cover h1 { font-size: 2.6em; color: #1a5276; letter-spacing: .04em; margin-bottom: 14px; }
.cover .subtitle { font-size: 1.3em; color: #555; margin-bottom: 10px; }
.cover .date { font-size: 1em; color: #999; margin-bottom: 22px; }
.cover .moat-list { display: flex; flex-wrap: wrap; gap: 8px; justify-content: center; margin-top: 8px; }
.cover .moat-badge {
  background: #eaf4fb; color: #1a5276; border: 1px solid #bdd6e8;
  border-radius: 20px; padding: 5px 14px; font-size: 0.88em; font-style: italic;
}

/* sections */
.section { max-width: 820px; margin: 0 auto; padding: 36px 48px 8px; }
.section-title {
  font-size: 1.2em; font-weight: 700; color: #1a5276;
  border-bottom: 2px solid #d0e4f0; padding-bottom: 7px; margin-bottom: 18px;
}
.section-body { color: #333; }
.section-body p  { margin-bottom: 12px; }
.section-body h2, .section-body h3, .section-body h4 {
  color: #1a5276; margin: 16px 0 8px; font-size: 1em;
}
.section-body ul, .section-body ol { margin: 8px 0 14px 24px; }
.section-body li { margin-bottom: 5px; }
.section-body strong { color: #1a3a4a; }

/* divider between sections */
.section-divider { max-width: 820px; margin: 0 auto 0; border: none; border-top: 1px solid #e8f0f5; }

/* source footnotes */
.sources-block { margin-top: 18px; padding-top: 12px; border-top: 1px dashed #d0e4f0; }
.sources-title { font-size: 0.74em; font-weight: 700; color: #7a9ab5; text-transform: uppercase;
                  letter-spacing: .07em; margin-bottom: 6px; }
.sources-list { list-style: none; padding: 0; margin: 0; }
.sources-list li { font-size: 0.76em; color: #888; margin-bottom: 3px; line-height: 1.4; }
.sources-list li span.src-num { font-weight: 700; color: #7a9ab5; margin-right: 4px; }

/* sources appendix */
.appendix { max-width: 820px; margin: 0 auto; padding: 28px 48px 40px; }
.appendix h2 { font-size: 1.1em; font-weight: 700; color: #7a9ab5;
                border-bottom: 1px solid #d0e4f0; padding-bottom: 6px; margin-bottom: 16px; }
.appendix table { width: 100%; border-collapse: collapse; font-size: 0.8em; }
.appendix th { text-align: left; color: #7a9ab5; font-weight: 600;
                border-bottom: 2px solid #d0e4f0; padding: 5px 8px; }
.appendix td { padding: 5px 8px; color: #555; border-bottom: 1px solid #f0f5f8; vertical-align: top; }
.appendix tr:last-child td { border-bottom: none; }

/* committee */
.committee-header { max-width: 820px; margin: 0 auto; padding: 36px 48px 18px; }
.committee-header h2 { font-size: 1.2em; font-weight: 700; color: #1a5276;
  border-bottom: 2px solid #d0e4f0; padding-bottom: 7px; }
.committee-body { max-width: 820px; margin: 0 auto; padding: 0 48px 36px; }
.turn {
  border-radius: 5px; padding: 12px 16px; margin: 8px 0;
  border-left-width: 4px; border-left-style: solid;
}
.turn .speaker { font-weight: 700; display: block; margin-bottom: 4px; font-size: .95em; }
.turn p { margin: 0; }

@media print {
  .no-print { display: none !important; }
  .cover { page-break-after: always; min-height: auto; padding: 80px 60px; }
  .section, .committee-header, .committee-body { padding-left: 0; padding-right: 0; }
  .section { page-break-inside: avoid; }
  .turn { page-break-inside: avoid; }
  @page { margin: 2.2cm 2.5cm; }
}
"""


def build_html(
    company: str,
    moat_types: list[str],
    language: str,
    sections: list[dict],
    sections_text: dict[str, str],
    dialogue: list[dict[str, str]],
    nl_months: list[str],
    speaker_styles: dict[str, dict[str, str]],
    sources: dict[str, list[Chunk]] | None = None,
) -> str:
    """
    Render the full investment case (sections + committee dialogue) as a
    standalone, printable HTML document.

    Args:
        company (str): The company name, used in the title and cover page.
        moat_types (list[str]): Selected MOAT classification badges.
        language (str): "English" or "Nederlands".
        sections (list[dict]): The section definitions, in display order.
        sections_text (dict[str, str]): Generated section id -> section text.
        dialogue (list[dict[str, str]]): Committee turns, each with "speaker"
            and "text".
        nl_months (list[str]): Dutch month names, for the cover page date.
        speaker_styles (dict[str, dict[str, str]]): Per-speaker "color"/"bg"
            used to style dialogue turns.
        sources (dict[str, list[Chunk]] | None): Section id -> retrieved chunks,
            used to render per-section and appendix source footnotes.

    Returns:
        str: A complete, self-contained HTML document.
    """
    today = date.today()
    date_str = f"{today.day} {nl_months[today.month - 1]} {today.year}"
    lang_attr = "en" if language == "English" else "nl"

    # Cover moat badges
    moat_html = ""
    if moat_types:
        badges = "".join(f'<span class="moat-badge">{html_lib.escape(m)}</span>' for m in moat_types)
        moat_html = f'<div class="moat-list">{badges}</div>'

    # Sections (with per-section source footnotes)
    src = sources or {}
    sections_html = ""
    # Track all sources across sections for the appendix (deduped by label)
    all_sources_ordered: list[tuple[str, str]] = []  # (section_title, chunk_label)
    seen_labels: set[str] = set()

    for i, section in enumerate(sections):
        text = sections_text.get(section["id"], "")
        if not text:
            continue
        title = html_lib.escape(section_title(section, language))
        if i > 0:
            sections_html += '<hr class="section-divider">'

        # Build source footnote block for this section (one entry per source,
        # even if it backs several citation numbers)
        section_chunks = src.get(section["id"], [])
        sources_block = ""
        if section_chunks:
            items = ""
            for label, nums in grouped_sources(section_chunks):
                tag = "".join(f'<span class="src-num">[{n}]</span>' for n in nums)
                items += f"<li>{tag}{html_lib.escape(label)}</li>"
                if label not in seen_labels:
                    seen_labels.add(label)
                    all_sources_ordered.append((section_title(section, language), label))
            src_title = "Sources" if language == "English" else "Bronnen"
            sources_block = (
                f'<div class="sources-block">'
                f'<div class="sources-title">{src_title}</div>'
                f'<ul class="sources-list">{items}</ul>'
                f"</div>"
            )

        sections_html += (
            f'<div class="section">'
            f'<div class="section-title">{title}</div>'
            f'<div class="section-body">{md_to_html(text)}</div>'
            f"{sources_block}"
            f"</div>"
        )

    # Committee dialogue
    dialogue_html = ""
    if dialogue:
        committee_title = "Investment Committee Discussion" if language == "English" else "Commissievergadering"
        turns_html = ""
        for turn in dialogue:
            name = turn["speaker"]
            style = speaker_styles.get(name, {"color": "#555", "bg": "#f9f9f9"})
            turns_html += (
                f'<div class="turn" style="background:{style["bg"]};border-left-color:{style["color"]}">'
                f'<span class="speaker" style="color:{style["color"]}">{html_lib.escape(name)}</span>'
                f"<p>{html_lib.escape(turn['text'])}</p>"
                f"</div>"
            )
        dialogue_html = (
            f'<hr class="section-divider">'
            f'<div class="committee-header"><h2>{committee_title}</h2></div>'
            f'<div class="committee-body">{turns_html}</div>'
        )

    # Sources appendix (deduplicated across all sections)
    appendix_html = ""
    if all_sources_ordered:
        appendix_title = "Sources" if language == "English" else "Bronnen"
        sec_col = "Section" if language == "English" else "Sectie"
        src_col = "Source" if language == "English" else "Bron"
        rows = "".join(
            f"<tr><td>{html_lib.escape(sec)}</td><td>{html_lib.escape(lbl)}</td></tr>" for sec, lbl in all_sources_ordered
        )
        appendix_html = (
            f'<hr class="section-divider">'
            f'<div class="appendix">'
            f"<h2>{appendix_title}</h2>"
            f"<table><thead><tr><th>{sec_col}</th><th>{src_col}</th></tr></thead>"
            f"<tbody>{rows}</tbody></table>"
            f"</div>"
        )

    print_label = "🖨️ Print / Save as PDF"
    return f"""<!DOCTYPE html>
<html lang="{lang_attr}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html_lib.escape(company)} — Investment Case</title>
<style>{_HTML_CSS}</style>
</head>
<body>
<button class="no-print" onclick="window.print()">{print_label}</button>
<div class="cover">
  <h1>{html_lib.escape(company.upper())}</h1>
  <div class="subtitle">Investment Case</div>
  <div class="date">{date_str}</div>
  {moat_html}
</div>
{sections_html}
{dialogue_html}
{appendix_html}
</body>
</html>"""
