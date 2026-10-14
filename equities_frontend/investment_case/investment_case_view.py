"""Investment Case Builder — structured investment case with per-turn committee critique."""

from __future__ import annotations

from collections import defaultdict

import streamlit as st

from helpers import equities_config, hide_research_notes
from investment_case import investment_case_model as model
from investment_case.investment_case_controller import InvestmentCaseController
from search.search_model import RESEARCH_NOTE_DOC_TYPE, Chunk

config = equities_config()
controller = InvestmentCaseController()


# ── rendering helpers ─────────────────────────────────────────────────────────


def _render_turn(speaker: str, text: str) -> None:
    style = controller.speaker_styles.get(speaker, {"color": "#555", "bg": "#f9f9f9"})
    st.markdown(
        f'<div style="background:{style["bg"]};border-left:4px solid {style["color"]};'
        f'padding:10px 14px;margin:6px 0;border-radius:4px">'
        f'<strong style="color:{style["color"]}">{speaker}</strong><br/>{text}</div>',
        unsafe_allow_html=True,
    )


def _render_dialogue(turns: list[dict[str, str]]) -> None:
    for turn in turns:
        _render_turn(turn["speaker"], turn["text"])


# ── page ──────────────────────────────────────────────────────────────────────

st.title("Investment Case Builder")
st.caption(
    "Generates a structured investment case from indexed research, "
    "followed by a 5-person committee discussion with a clear verdict."
)

# Language toggle
language = st.radio(
    "Language / Taal",
    ["Nederlands", "English"],
    horizontal=True,
    index=0,
)

st.divider()

# Inputs
col_a, col_b = st.columns([2, 1])
with col_a:
    company = st.text_input(
        "Bedrijf / Company",
        value=st.session_state.get("ic_company", ""),
        placeholder="e.g. NOVO NORDISK or NVO",
    ).strip()
with col_b:
    max_words = st.slider("Max words per section", min_value=75, max_value=400, value=150, step=25)

direction = st.text_area(
    "Investment angle (optional)",
    value="",
    placeholder="e.g. 'Focus on the moat in the context of GLP-1 competition and biosimilar risk'",
    height=70,
)

moat_types: list[str] = st.multiselect(
    "MOAT classification (select one or more)",
    options=controller.moat_options,
    default=[],
)

generate_clicked = st.button(
    "Generate Investment Case" if language == "English" else "Genereer Investment Case",
    type="primary",
    disabled=not company,
)

# ── case generation ───────────────────────────────────────────────────────────

if generate_clicked and company:
    st.session_state["ic_company"] = company
    st.session_state["ic_moat_types"] = moat_types
    st.session_state["ic_language"] = language
    st.session_state["ic_sections"] = {}
    st.session_state["ic_sources"] = {}
    st.session_state.pop("ic_dialogue", None)

    progress = st.progress(0, text="Generating..." if language == "English" else "Genereren...")

    moat_str = ", ".join(moat_types)
    for i, section in enumerate(controller.sections):
        n = len(controller.sections)
        progress.progress(i / n, text=f"⏳ {model.section_title(section, language)}...")

        moat_context = ""
        if section["id"] == "moat" and moat_str:
            moat_context = (
                f"The selected MOAT type(s): **{moat_str}**. Build the analysis around these moat types."
                if language == "English"
                else f"De geselecteerde MOAT-type(s): **{moat_str}**. Bouw de analyse rond deze moat types."
            )

        fmt: dict[str, str] = defaultdict(
            str,
            {
                "company": company,
                "max_words": str(max_words),
                "direction": direction.strip(),
                "moat_context": moat_context,
            },
        )
        prompt = model.section_prompt(section, language).format_map(fmt)

        text, chunks = controller.generate_section(
            section_prompt=prompt,
            company=company,
            retrieval_query=f"{company} {section['search_hint']}",
            config=config,
            section_id=section["id"],
            exclude_research_notes=hide_research_notes(),
        )
        st.session_state["ic_sections"][section["id"]] = text
        st.session_state["ic_sources"][section["id"]] = chunks

    progress.progress(1.0, text="Done!" if language == "English" else "Klaar!")
    st.rerun()

# ── display ───────────────────────────────────────────────────────────────────

ic_sections: dict[str, str] = st.session_state.get("ic_sections", {})
ic_sources: dict[str, list[Chunk]] = st.session_state.get("ic_sources", {})
ic_company: str = st.session_state.get("ic_company", "")
ic_moat_types: list[str] = st.session_state.get("ic_moat_types", [])
ic_language: str = st.session_state.get("ic_language", language)
ic_dialogue: list[dict[str, str]] = st.session_state.get("ic_dialogue", [])

if ic_sections and ic_company:
    st.divider()
    st.subheader(f"Investment Case — {ic_company}")
    if ic_moat_types:
        st.caption(f"MOAT: {', '.join(ic_moat_types)}")

    case_parts: list[str] = []
    for section in controller.sections:
        text = ic_sections.get(section["id"], "")
        if not text:
            continue
        with st.expander(model.section_title(section, ic_language), expanded=True):
            st.markdown(text)
            section_chunks = ic_sources.get(section["id"], [])
            if hide_research_notes():
                # Belt and suspenders: exclude_research_notes at generation time already
                # keeps new sections from citing research_note documents at all, but a
                # section generated earlier (toggle off, or an older session) still has
                # them baked into ic_sources -- filtered here too so the displayed
                # Bronnen list is never stale relative to the current toggle state, even
                # though the already-generated text may still be grounded in them.
                section_chunks = [c for c in section_chunks if c.document_type != RESEARCH_NOTE_DOC_TYPE]
            if section_chunks:
                st.divider()
                src_header = "Sources" if ic_language == "English" else "Bronnen"
                st.caption(f"**{src_header}**")
                for label, nums in model.grouped_sources(section_chunks):
                    tag = "".join(f"[{n}]" for n in nums)
                    st.caption(f"{tag} {label}")
        case_parts.append(f"### {model.section_title(section, ic_language)}\n\n{text}")

    full_case = "\n\n".join(case_parts)

    # ── committee ─────────────────────────────────────────────────────────────

    st.divider()
    committee_label = "Investment Committee Discussion" if ic_language == "English" else "Commissievergadering"
    st.subheader(committee_label)
    st.caption(
        "Buffett (moat/quality), Dalio (macro/cycles), Asness (quant/factors), "
        "Arnott (valuation/contrarian), Marks (risk/second-level), Nico (verdict)"
    )

    start_label = "Start Committee Meeting" if ic_language == "English" else "Start Commissievergadering"
    if st.button(start_label, type="secondary"):
        st.session_state.pop("ic_dialogue", None)
        characters = controller.characters(ic_language)
        system_ctx = model.build_system_context(ic_company, ic_sections, ic_language)

        planning_msg = (
            "Identifying key discussion points..."
            if ic_language == "English"
            else "Zwakke punten identificeren en debat plannen..."
        )
        with st.spinner(planning_msg):
            plan = controller.plan_dialogue(
                company=ic_company,
                investment_case=full_case,
                language=ic_language,
                config=config,
            )

        if not plan:
            fallback = controller.fallback_dialogue(ic_language)
            plan = [{"speaker": s, "focus": f} for s, f in fallback]

        conversation: list[dict[str, str]] = []
        for turn in plan:
            speaker = turn["speaker"]
            focus = turn["focus"]
            max_tok = 480 if speaker == "Nico" else 320
            with st.spinner(f"{speaker}..."):
                text = controller.generate_turn(
                    speaker=speaker,
                    character=characters[speaker],
                    focus=focus,
                    conversation_history=conversation,
                    system_context=system_ctx,
                    config=config,
                    max_tokens=max_tok,
                )
            if text:
                conversation.append({"speaker": speaker, "text": text})
                _render_turn(speaker, text)

        st.session_state["ic_dialogue"] = conversation
        ic_dialogue = conversation

    elif ic_dialogue:
        _render_dialogue(ic_dialogue)

    # ── download ──────────────────────────────────────────────────────────────

    st.divider()
    html_sources = ic_sources
    if hide_research_notes():
        # Same belt-and-suspenders reasoning as the on-page Bronnen list above:
        # a case generated before the toggle was on still has research_note
        # chunks in ic_sources, so the HTML export's own source footnotes and
        # appendix (investment_case_model.build_html) need the same filter,
        # not just the Streamlit rendering.
        html_sources = {
            section_id: [c for c in chunks if c.document_type != RESEARCH_NOTE_DOC_TYPE]
            for section_id, chunks in ic_sources.items()
        }
    html_bytes = model.build_html(
        ic_company,
        ic_moat_types,
        ic_language,
        controller.sections,
        ic_sections,
        ic_dialogue,
        controller.nl_months,
        controller.speaker_styles,
        html_sources,
    ).encode("utf-8")
    safe_name = ic_company.lower().replace(" ", "_")
    st.download_button(
        label="\U0001f4e5 Download HTML (open in browser → Print / Save as PDF)",
        data=html_bytes,
        file_name=f"investment_case_{safe_name}.html",
        mime="text/html",
    )
