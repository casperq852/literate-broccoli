"""
Controller for the Investment Case Builder topic.

Owns the LLM orchestration for generating RAG-grounded sections and running
the investment-committee dialogue, plus loading the section/persona/dialogue
content that both the generation prompts and the view need.
"""

from __future__ import annotations

import contextlib
import json
import logging
import re
from datetime import date, datetime
from typing import Any

from portfolio_dashboard.chat_context_model import find_company_position, format_company_portfolio_context

from database import cache
from investment_case import investment_case_model as model
from search import search_model

logger = logging.getLogger(__name__)

_INVESTMENT_SYSTEM_PROMPT = (
    "Je bent een ervaren aandelenanalist bij een Nederlandse vermogensbeheerder. "
    "Genereer professionele, kritisch-analytische beleggingsanalyse. "
    "Gebruik aangeleverde onderzoeksdata als primaire bron en vul aan met brede sectorkennis voor context. "
    "Schrijf direct en analytisch — geen filler, geen herhaling, geen clichés.\n\n"
    "BRONCITATIE — verplicht:\n"
    "- Voeg inline citatienummers [N] toe bij elke specifieke bewering, cijfer, koersdoel of prognose "
    "die afkomstig is uit een bron. Gebruik het bronnummer zoals aangegeven in de context (bijv. [1], [2]).\n"
    "- Presenteer analistenmeningen en prognoses NIET als feiten. Gebruik formuleringen als "
    "'Volgens [uitgever] [N]...' of 'Analisten verwachten [N]...' bij toekomstgerichte uitspraken.\n"
    "- Als bronnen onderling tegenstrijdig zijn, benoem de spreiding van meningen in plaats van één conclusie te trekken.\n\n"
    "BRONNENKWALITEIT — wees kritisch:\n"
    "- Brokerresearch bevat mogelijk directionale bias. Wees sceptisch bij eenzijdig positieve of negatieve toon.\n"
    "- Koersdoelen, prognoses en managementguidance uit documenten ouder dan 18 maanden zijn verouderd — "
    "presenteer ze als historisch analistenstandpunt, niet als actuele verwachting.\n"
    "- Als een BRONWAARSCHUWING is opgenomen in de context, houd daar rekening mee bij tijdgevoelige beweringen."
)

_INVESTMENT_REFINE_PROMPT = (
    "You are a quality editor for investment case sections. Rewrite the draft to:\n"
    "1. Remove repetition — state each point exactly once; merge sentences that repeat the same claim.\n"
    "2. Cut filler — remove hedging preambles, transitional clichés, and closing summaries that restate the intro.\n"
    "3. Stay faithful — do not add, change, or remove any specific figures, dates, or factual claims.\n"
    "4. PRESERVE all inline citation markers [N] exactly as they appear — never drop or renumber them.\n"
    "5. Return ONLY the rewritten text, no preamble or commentary."
)


class InvestmentCaseController:
    """Wires the Investment Case Builder's content, generation, and committee dialogue."""

    def __init__(self) -> None:
        self._content: dict[str, Any] = model.load_content()

    # ── content access ─────────────────────────────────────────────────────────

    @property
    def sections(self) -> list[dict]:
        """Section definitions, in display order."""
        return self._content["sections"]

    @property
    def moat_options(self) -> list[str]:
        """Selectable MOAT classification labels."""
        return self._content["moat_options"]

    @property
    def speaker_styles(self) -> dict[str, dict[str, str]]:
        """Per-speaker "color"/"bg" styling for dialogue turns."""
        return self._content["speaker_styles"]

    @property
    def nl_months(self) -> list[str]:
        """Dutch month names, for the HTML export's cover-page date."""
        return self._content["nl_months"]

    def characters(self, language: str) -> dict[str, str]:
        """Return the committee member persona prompts for the given language."""
        return self._content["characters"]["en" if language == "English" else "nl"]

    def fallback_dialogue(self, language: str) -> list[tuple[str, str]]:
        """Return the static dialogue plan used when committee planning fails."""
        pairs = self._content["fallback_dialogue"]["en" if language == "English" else "nl"]
        return [(speaker, focus) for speaker, focus in pairs]

    # ── generation ────────────────────────────────────────────────────────────

    def _portfolio_positioning_chunk(self, company: str) -> search_model.Chunk | None:
        """
        Return a synthetic ``Chunk`` grounding the "Role in the Portfolio"
        section in ``company``'s real positions across our portfolios, or
        ``None`` if it isn't held anywhere with cached metrics.

        The reverse of ``chat_controller.ChatController._portfolio_context_chunk``:
        that one looks up a named portfolio's data; this looks up, across every
        known portfolio's latest cached holdings, whether ``company`` is held
        and at what weight/risk contribution (see
        ``portfolio_dashboard.chat_context_model.find_company_position``).
        """
        if not company:
            return None
        positions = []
        for portfolio in cache.get_portfolio_names():
            record = cache.get_portfolio_metrics(portfolio)
            if not record:
                continue
            position = find_company_position(company, record)
            if position:
                positions.append({"portfolio": portfolio, "as_of_date": record["as_of_date"], **position})
        if not positions:
            return None
        return search_model.Chunk(
            content=format_company_portfolio_context(company, positions),
            metric=None,
            period_label=None,
            period_end_date=None,
            is_estimate=None,
            document_type="portfolio_data",
            document_date=max(p["as_of_date"] for p in positions),
            publisher="Portfolio Dashboard",
            companies=(company,),
            score=1.0,
        )

    def generate_section(
        self,
        section_prompt: str,
        company: str,
        retrieval_query: str,
        config: dict[str, Any],
        section_id: str | None = None,
        exclude_research_notes: bool = False,
    ) -> tuple[str, list[search_model.Chunk]]:
        """
        Generate one investment case section, RAG-grounded where docs exist.

        Returns ``(text, chunks)`` so callers can render inline citations and
        source footnotes. Chunks are sorted newest-first; a freshness warning
        is injected into the context when the oldest source is over 12 months
        old.

        When ``section_id == "portefeuille"`` (Role in the Portfolio), a
        synthetic chunk grounding the section in ``company``'s real positions
        across our portfolios (see ``_portfolio_positioning_chunk``) is
        prepended to the retrieved chunks, the same synthetic-chunk pattern
        ``chat_controller.py`` uses for portfolio questions.

        Args:
            section_prompt (str): The fully-templated prompt for this section.
            company (str): The company under discussion, used to scope retrieval.
            retrieval_query (str): Search query for this section's retrieval.
            config (dict[str, Any]): The equities app config block (index_name,
                top_k, chat_deployment).
            section_id (str | None): This section's id, e.g. "portefeuille" —
                used only to decide whether to add portfolio-positioning context.
            exclude_research_notes (bool): When True, this section is neither
                grounded in nor cites research_note documents at all -- the
                Logs page's demo-mode toggle (helpers.hide_research_notes()),
                read by the view and passed in here as a plain bool so this
                controller stays Streamlit-free.

        Returns:
            tuple[str, list[search_model.Chunk]]: The refined section text (or
            "" on failure) and the chunks it was grounded in.
        """
        document_types = (
            [t for t in search_model.ALL_DOCUMENT_TYPES if t != search_model.RESEARCH_NOTE_DOC_TYPE]
            if exclude_research_notes
            else None
        )
        chunks = search_model.retrieve(
            query=retrieval_query,
            index_name=config["index_name"],
            top_k=config["top_k"],
            company=company or None,
            document_types=document_types,
        )

        if section_id == "portefeuille":
            portfolio_chunk = self._portfolio_positioning_chunk(company)
            if portfolio_chunk:
                chunks = [portfolio_chunk, *chunks]

        # Newest sources first — model attends more to earlier context positions
        chunks = sorted(chunks, key=lambda chunk: chunk.document_date or "", reverse=True)

        messages: list[dict[str, str]] = [{"role": "system", "content": _INVESTMENT_SYSTEM_PROMPT}]
        if chunks:
            # Compute source date range and emit a warning for stale data
            dates: list[date] = []
            for chunk in chunks:
                if chunk.document_date:
                    with contextlib.suppress(ValueError):
                        dates.append(datetime.fromisoformat(chunk.document_date[:10]).date())
            freshness = ""
            if dates:
                oldest, newest = min(dates), max(dates)
                oldest_months = (date.today() - oldest).days // 30
                freshness = f"\nBRONWAARSCHUWING: Bronnen dateren van {oldest} tot {newest}."
                if oldest_months > 12:  # noqa: PLR2004
                    freshness += (
                        f" Oudste bron is {oldest_months} maanden oud — "
                        "behandel koersdoelen, prognoses en guidance met gepast scepticisme "
                        "en presenteer ze als historisch standpunt."
                    )

            context = search_model.format_investment_context(chunks)
            user_content = f"Beschikbare research en data:{freshness}\n\n{context}\n\n{section_prompt}"
        else:
            user_content = section_prompt

        messages.append({"role": "user", "content": user_content})
        try:
            raw_text = search_model.get_chat_client(config["chat_deployment"]).chat(messages=messages).strip()
        except Exception:  # noqa: BLE001
            logger.exception("Investment section generation failed")
            return "", []

        # Citation-preserving refine pass: strip repetition without touching [N] markers
        try:
            refined_response = search_model.get_chat_client(config["chat_deployment"]).chat(
                messages=[
                    {"role": "system", "content": _INVESTMENT_REFINE_PROMPT},
                    {"role": "user", "content": f"Draft:\n\n{raw_text}"},
                ],
            )
            refined = (refined_response or raw_text).strip()
        except Exception:  # noqa: BLE001
            refined = raw_text

        return refined, chunks

    def plan_dialogue(
        self,
        company: str,
        investment_case: str,
        language: str,
        config: dict[str, Any],
        n_turns: int = 13,
    ) -> list[dict[str, str]]:
        """
        Ask the LLM to plan the committee debate before it starts.

        Reads the full investment case, identifies the weakest points, then
        decides who speaks first and in what order -- returning a list of
        ``{"speaker": name, "focus": specific_instruction}`` dicts.

        Returns an empty list on failure so the caller can fall back to
        ``fallback_dialogue``. The last entry is always validated to be Nico
        (the chairman/verdict).

        Args:
            company (str): The company under discussion.
            investment_case (str): The full generated investment case text.
            language (str): "English" or "Nederlands".
            config (dict[str, Any]): The equities app config block (chat_deployment).
            n_turns (int): Number of dialogue turns to plan.

        Returns:
            list[dict[str, str]]: The planned turns, or an empty list on failure.
        """
        valid_speakers = set(self.characters(language))
        if language == "English":
            members = (
                "- Buffett: thinks like an owner — moat durability, pricing power, 20-year competitive advantage, "
                "sceptical of leverage and complexity; speaks in plain folksy language\n"
                "- Dalio: macro-first — debt cycles, currency risk, geopolitical shifts; asks how this stock behaves "
                "across all economic regimes (growth, inflation, deflation, recession)\n"
                "- Asness: quantitative sceptic — demands data over narrative, calls out factor mispricings, "
                "focuses on value vs momentum exposures and historical evidence\n"
                "- Arnott: contrarian on valuation — obsesses over what is already priced in, mean reversion, "
                "long-horizon return expectations; sceptical of permanently-high-multiple growth stories\n"
                "- Marks: risk and second-level thinking — what does the consensus believe and where could they be wrong? "
                "asymmetric risk/reward, downside scenarios, margin of safety\n"
                "- Nico: chairman, speaks LAST only — one-sentence summary per person then a clear YES or NO"
            )
            task = (
                f"Read the investment case carefully. Identify both (a) the 2-3 strongest supporting points — the "
                f"claims best backed by evidence in the case — and (b) the 2-3 most vulnerable points — the claims "
                f"least supported or most dependent on optimistic assumptions. Plan a {n_turns}-turn debate that "
                f"examines the case from both sides: some turns build on and pressure-test the strongest points, "
                f"others challenge the weakest ones. The debate must reflect genuine disagreement between members, "
                f"not a one-sided pile-on — the panel's job is to reach an honest verdict based on how the evidence "
                f"actually balances out, which may be YES, NO, or a clearly qualified stance. Do not pre-decide the "
                f"outcome before the debate plays out."
            )
            rules = (
                "- Assign the OPENING turn to whoever is best positioned to raise the single most decision-relevant "
                "point, bullish or bearish.\n"
                "- DISAGREEMENT PAIRS (mandatory, at least 3 pairs): in some turns, one investor must directly "
                "challenge or push back on a SPECIFIC claim another investor made in an earlier turn — not a neutral "
                "question, a real counter-argument. The very next turn must be that investor responding to the "
                "challenge. Format the challenger's focus as: '... then push back on [Name]'s point about [topic]: "
                "[specific counter-argument]'. Format the responder's focus as: 'Defend or refine your point about "
                "[topic] against [Challenger]'s pushback. Then [own point].'.\n"
                "- BALANCE (mandatory): at least 2 turns must argue FOR a specific strength of the case, and at "
                "least 2 turns must argue AGAINST a specific weakness — do not let every member converge on the "
                "same conclusion.\n"
                "- Rotate perspectives — no investor should appear in two consecutive turns unless one is "
                "challenging and the next is responding.\n"
                f"- Nico MUST be turn {n_turns} (the last speaker) and must reach an HONEST verdict based on how "
                "the debate actually played out — YES, NO, or a clearly qualified stance are all acceptable.\n"
                "- Every member except Nico speaks at least twice.\n"
                "- Each 'focus' instruction must reference SPECIFIC claims, numbers, or risks from this case — "
                "not generic investing platitudes."
            )
            lang_note = "Write every focus instruction in English."
            json_note = (
                f"Return ONLY a valid JSON array with exactly {n_turns} objects:\n"
                '[{"speaker": "Name", "focus": "specific instruction referencing this case"}, ...]\n'
                f"Allowed speaker names: {', '.join(sorted(valid_speakers))}.\n"
                "No markdown fences, no explanation — raw JSON only."
            )
        else:
            members = (
                "- Buffett: denkt als eigenaar — moat-duurzaamheid, prijsmacht, 20-jaar concurrentievoordeel, "
                "sceptisch over leverage en complexiteit; spreekt eenvoudig en direct\n"
                "- Dalio: macro-first — schuldcycli, valutarisico, geopolitiek; vraagt hoe dit aandeel zich gedraagt "
                "in alle economische regimes (groei, inflatie, deflatie, recessie)\n"
                "- Asness: kwantitatief scepticus — eist data boven verhalen, wijst op factor-mispricings, "
                "focust op waarde- vs momentumfactoren en historisch bewijs\n"
                "- Arnott: contrarian op waardering — geobsedeerd door wat er al is ingeprijsd, mean reversion, "
                "langetermijnrendementsverwachtingen; sceptisch over groeiverhalen met hoge multiples\n"
                "- Marks: risico en tweede-orde-denken — wat gelooft de consensus en waar kunnen ze ongelijk hebben? "
                "asymmetrisch risico/rendement, neerwaartse scenario's, veiligheidsmarge\n"
                "- Nico: voorzitter, spreekt ALLEEN als LAATSTE — één zin per persoon dan helder JA of NEE"
            )
            task = (
                f"Lees de investment case zorgvuldig. Identificeer zowel (a) de 2-3 sterkste onderbouwde punten — de "
                f"beweringen die het beste worden gestaafd door bewijs in de case — als (b) de 2-3 kwetsbaarste "
                f"punten — de beweringen die het minst onderbouwd zijn of het meest afhankelijk van optimistische "
                f"aannames. Plan een debat van {n_turns} beurten dat de case vanuit beide kanten bekijkt: sommige "
                f"beurten bouwen voort op en toetsen de sterkste punten, andere dagen de zwakste punten uit. Het "
                f"debat moet echte onenigheid tussen de leden laten zien, geen eenzijdige stapeling van kritiek — "
                f"de taak van het comité is een eerlijk oordeel te vellen op basis van hoe het bewijs daadwerkelijk "
                f"uitpakt, wat JA, NEE, of een duidelijk genuanceerd standpunt kan zijn. Beslis de uitkomst niet "
                f"vooraf, voordat het debat heeft plaatsgevonden."
            )
            rules = (
                "- Wijs de OPENINGSBEURT toe aan degene die het beste gepositioneerd is om het meest "
                "beslissingsrelevante punt aan te kaarten, positief of negatief.\n"
                "- ONENIGHEIDSPAREN (verplicht, minimaal 3 paren): in sommige beurten moet één belegger een "
                "SPECIFIEKE bewering van een andere belegger uit een eerdere beurt direct uitdagen of weerleggen — "
                "geen neutrale vraag, maar een echt tegenargument. De eerstvolgende beurt moet die belegger zijn die "
                "op de uitdaging reageert. Formuleer de focus van de uitdager als: '... weerleg dan het punt van "
                "[Naam] over [onderwerp]: [specifiek tegenargument]'. Formuleer de focus van de reagerende partij "
                "als: 'Verdedig of nuanceer je punt over [onderwerp] tegen de weerlegging van [Uitdager]. Dan [eigen "
                "punt].'.\n"
                "- BALANS (verplicht): minstens 2 beurten moeten VÓÓR een specifieke sterkte van de case pleiten, en "
                "minstens 2 beurten moeten TEGEN een specifieke zwakte pleiten — laat niet elk lid naar dezelfde "
                "conclusie toewerken.\n"
                "- Wissel van perspectief — geen belegger in twee opeenvolgende beurten, tenzij één uitdaagt en de "
                "ander reageert.\n"
                f"- Nico MOET beurt {n_turns} zijn (de laatste spreker) en moet een EERLIJK oordeel vellen op basis "
                "van hoe het debat daadwerkelijk verliep — JA, NEE, of een duidelijk genuanceerd standpunt zijn "
                "allemaal acceptabele uitkomsten.\n"
                "- Elke deelnemer behalve Nico spreekt minstens twee keer.\n"
                "- Elke 'focus'-instructie moet verwijzen naar SPECIFIEKE beweringen, cijfers of risico's uit deze case — "
                "geen generieke beleggingsclichés."
            )
            lang_note = "Schrijf elke focus-instructie in het Nederlands."
            json_note = (
                f"Geef ALLEEN een geldige JSON array terug met exact {n_turns} objecten:\n"
                '[{"speaker": "Naam", "focus": "specifieke instructie die verwijst naar deze case"}, ...]\n'
                f"Toegestane namen: {', '.join(sorted(valid_speakers))}.\n"
                "Geen markdown, geen uitleg — alleen ruwe JSON."
            )

        prompt = (
            f"Commissieleden:\n{members}\n\n"
            f"{task}\n\n"
            f"Regels:\n{rules}\n\n"
            f"Investment case voor {company}:\n{investment_case}\n\n"
            f"{lang_note}\n\n{json_note}"
        )

        try:
            raw = (
                search_model.get_chat_client(config["chat_deployment"])
                .chat(
                    messages=[{"role": "user", "content": prompt}],
                    reasoning_effort="none",
                )
                .strip()
            )
            raw = re.sub(r"^```(?:json)?\s*\n?", "", raw)
            raw = re.sub(r"\n?```\s*$", "", raw)
            plan: list[dict[str, str]] = json.loads(raw.strip())
            non_nico_speakers = valid_speakers - {"Nico"}
            plan_speakers: set[str] = (
                {turn["speaker"] for turn in plan if "speaker" in turn} if isinstance(plan, list) else set()
            )
            if (
                isinstance(plan, list)
                # A severely truncated plan (e.g. 4 of 13 turns) defeats the balance/disagreement
                # rules entirely and starves Nico of most of the panel's input -- require close to
                # the full requested length, not just "enough to pass a minimum".
                and len(plan) >= n_turns - 1
                and all("speaker" in turn and "focus" in turn for turn in plan)
                and all(turn["speaker"] in valid_speakers for turn in plan)
                and non_nico_speakers <= plan_speakers
                and plan[-1]["speaker"] == "Nico"
            ):
                return plan
            logger.warning(
                "Committee plan had unexpected structure (got %d/%d turns, speakers=%s); using fallback",
                len(plan) if isinstance(plan, list) else -1,
                n_turns,
                sorted(plan_speakers),
            )
        except Exception:  # noqa: BLE001
            logger.exception("Committee planning failed; using fallback")
        return []

    def generate_turn(
        self,
        speaker: str,
        character: str,
        focus: str,
        conversation_history: list[dict[str, str]],
        system_context: str,
        config: dict[str, Any],
        max_tokens: int = 320,
    ) -> str:
        """
        Generate one committee member's contribution to the investment-committee dialogue.

        The most recent turn is surfaced separately and prominently so the
        model is forced to engage with it before making its own point. Earlier
        turns supply broader context. ``max_tokens`` is kept modest (default
        320) to enforce short, punchy turns while leaving headroom for a
        complete 3-sentence reaction with a specific counter-argument; Nico's
        closing summary should use a higher value (~480). ``reasoning_effort``
        is explicitly set to "none" so a reasoning-capable chat_deployment
        doesn't silently spend the token budget on hidden reasoning and leave
        too little for the visible reply -- the same failure mode documented
        in ``metadata_enrichment_controller.py``. If the response is still cut
        off mid-sentence despite this, it's trimmed to the last complete
        sentence rather than shown truncated.

        Args:
            speaker (str): The committee member speaking this turn.
            character (str): That member's persona prompt.
            focus (str): The specific instruction for this turn (from the plan).
            conversation_history (list[dict[str, str]]): Prior turns, each with
                "speaker" and "text".
            system_context (str): The shared system prompt for this dialogue.
            config (dict[str, Any]): The equities app config block (chat_deployment).
            max_tokens (int): Max completion tokens for this turn.

        Returns:
            str: The generated turn text, or "" on failure.
        """
        # Split history: everything before the last turn is context; the last turn
        # is foregrounded with an explicit reaction requirement.
        prior = conversation_history[:-1]
        last = conversation_history[-1] if conversation_history else None

        prior_lines = "\n".join(f'  {turn["speaker"]}: "{turn["text"]}"' for turn in prior[-8:])
        context_block = f"Earlier in the conversation:\n{prior_lines}\n\n" if prior_lines else ""

        if last:
            last_block = (
                f"MOST RECENT — {last['speaker']} just said:\n"
                f'  "{last["text"]}"\n\n'
                f"Your FIRST sentence MUST directly engage with what {last['speaker']} said "
                f"(agree, push back, answer their question, or build on their point). "
                f"Do not ignore it.\n\n"
            )
        else:
            last_block = ""

        user_content = f"{context_block}{last_block}You are {speaker}. {character}\n\nYour task this turn: {focus}"
        try:
            chat_deployment = config["chat_deployment"]
            response = search_model.get_chat_client(chat_deployment).client.chat.completions.create(
                model=chat_deployment,
                messages=[
                    {"role": "system", "content": system_context},
                    {"role": "user", "content": user_content},
                ],
                max_completion_tokens=max_tokens,
                reasoning_effort="none",
            )
            choice = response.choices[0]
            text = (choice.message.content or "").strip().strip('"').strip("'")
            if choice.finish_reason == "length" and text:
                text = model.trim_to_complete_sentence(text)
            return text
        except Exception:  # noqa: BLE001
            logger.exception("Dialogue turn failed for %s", speaker)
            return ""
