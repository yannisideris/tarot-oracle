# -*- coding: utf-8 -*-
"""
Tarot LLM Oracle v3 (web edition) - deck, prompts and parsing.

Ported from the desktop tarot_llm_oracle_v3.py. The deck, spread variants,
system prompts, history selection and output parsing are unchanged; only the
tkinter UI, LM Studio calls and local file writing were removed.
"""

from __future__ import annotations

import io
import json
import os
import random
import re
from dataclasses import dataclass, asdict
from typing import Dict, List, Optional, Tuple

MAX_RELEVANT_HISTORY = 8
MAX_RECENT_HISTORY = 4

RNG = random.SystemRandom()

@dataclass
class TarotCard:
    name: str
    arcana: str
    suit: str
    keywords: str


MAJOR = [
    ("The Fool", "beginning, leap, holy naivety, zero-point, risk"),
    ("The Magician", "agency, articulation, technique, translation into action"),
    ("The High Priestess", "hidden knowledge, silence, threshold, veiled pattern"),
    ("The Empress", "growth, embodiment, fertility, material abundance"),
    ("The Emperor", "structure, sovereignty, law, boundary, architecture"),
    ("The Hierophant", "tradition, doctrine, initiation, shared symbolic order"),
    ("The Lovers", "choice, relation, alignment, value, erotic/ethical bond"),
    ("The Chariot", "direction, will, vehicle, disciplined conquest"),
    ("Strength", "gentle mastery, courage, inward force, taming without crushing"),
    ("The Hermit", "solitude, lamp, search, withdrawal for insight"),
    ("Wheel of Fortune", "cycle, turn, fate, timing, pattern beyond control"),
    ("Justice", "measure, consequence, truth, balance, right proportion"),
    ("The Hanged Man", "suspension, reversal, sacrifice, seeing from elsewhere"),
    ("Death", "ending, transformation, pruning, irreversible passage"),
    ("Temperance", "mixture, mediation, alchemy, proportion, healing blend"),
    ("The Devil", "bondage, appetite, glamour, fixation, material spell"),
    ("The Tower", "rupture, revelation, collapse of false structure"),
    ("The Star", "hope, blessing, distance, guidance, lucid renewal"),
    ("The Moon", "dream, ambiguity, fear, image, nocturnal distortion"),
    ("The Sun", "clarity, vitality, manifestation, innocence, disclosure"),
    ("Judgement", "call, awakening, reckoning, return, vocation"),
    ("The World", "completion, cosmos, integration, dance of totality"),
]

MINOR_SUITS = {
    "Wands": {
        "domain": "fire, drive, spirit, invention, will, charisma",
        "cards": [
            ("Ace", "spark, beginning of force, living impulse"),
            ("Two", "planning, horizon, divided dominion"),
            ("Three", "expansion, ships leaving harbor, active expectation"),
            ("Four", "celebration, threshold of stability, communal rite"),
            ("Five", "contest, friction, many wills colliding"),
            ("Six", "recognition, victory, public sign"),
            ("Seven", "defense, courage under pressure, high ground"),
            ("Eight", "swiftness, messages, sudden movement"),
            ("Nine", "wounded persistence, guarded endurance"),
            ("Ten", "burden, overextension, force becoming weight"),
            ("Page", "young fire, experiment, message of enthusiasm"),
            ("Knight", "questing fire, haste, adventure, volatility"),
            ("Queen", "radiant confidence, magnetism, warm command"),
            ("King", "visionary authority, creative leadership, solar will"),
        ],
    },
    "Cups": {
        "domain": "water, feeling, image, relation, imagination, soul-weather",
        "cards": [
            ("Ace", "gift of feeling, grace, emotional source"),
            ("Two", "reciprocity, union, mirror, pact"),
            ("Three", "friendship, feast, shared joy"),
            ("Four", "apathy, refusal, inward saturation"),
            ("Five", "loss, regret, mourning, partial blindness"),
            ("Six", "memory, childhood, gift from the past"),
            ("Seven", "fantasy, many images, temptation of possibilities"),
            ("Eight", "departure, spiritual dissatisfaction, leaving fullness"),
            ("Nine", "satisfaction, wish, emotional plenitude"),
            ("Ten", "shared happiness, household of feeling, rainbow"),
            ("Page", "tender message, poetic receptivity, strange cup"),
            ("Knight", "romantic quest, invitation, aesthetic movement"),
            ("Queen", "deep empathy, dream-water, inner listening"),
            ("King", "emotional governance, depth under control"),
        ],
    },
    "Swords": {
        "domain": "air, thought, language, conflict, analysis, cutting truth",
        "cards": [
            ("Ace", "clear idea, incision, truth-force"),
            ("Two", "stalemate, blocked decision, blind balance"),
            ("Three", "sorrow, wound of thought, painful clarity"),
            ("Four", "rest, recovery, truce, chapel-silence"),
            ("Five", "defeat, empty victory, corrosive argument"),
            ("Six", "passage, exile, mental transition"),
            ("Seven", "strategy, stealth, partial truth, escape"),
            ("Eight", "restriction, mental prison, imagined limit"),
            ("Nine", "anxiety, nightmare, thought turning against itself"),
            ("Ten", "ending, collapse, maximum negativity before dawn"),
            ("Page", "watchful mind, message, curiosity, sharp youth"),
            ("Knight", "argument, charge, speed of thought"),
            ("Queen", "discernment, sorrow refined into clarity"),
            ("King", "judgement, intellectual law, strategic command"),
        ],
    },
    "Pentacles": {
        "domain": "earth, matter, work, body, economy, craft, time",
        "cards": [
            ("Ace", "seed of matter, opportunity, material blessing"),
            ("Two", "juggling, adaptation, rhythm amid change"),
            ("Three", "craft, collaboration, apprenticeship, building"),
            ("Four", "possession, enclosure, conservation, fear of loss"),
            ("Five", "exclusion, hardship, poverty, outside the lit window"),
            ("Six", "gift, patronage, imbalance of giving"),
            ("Seven", "patience, cultivation, delayed harvest"),
            ("Eight", "practice, repetition, craft discipline"),
            ("Nine", "self-sufficiency, cultivated luxury, earned grace"),
            ("Ten", "inheritance, institution, family/system wealth"),
            ("Page", "study of matter, beginner's craft, practical message"),
            ("Knight", "steadfast labor, slow reliability, field-work"),
            ("Queen", "nourishing matter, domestic abundance, body wisdom"),
            ("King", "material mastery, estate, pragmatic authority"),
        ],
    },
}

DECK: List[TarotCard] = []
for name, kw in MAJOR:
    DECK.append(TarotCard(name=name, arcana="Major Arcana", suit="Major", keywords=kw))
for suit, spec in MINOR_SUITS.items():
    for rank, kw in spec["cards"]:
        DECK.append(TarotCard(name=f"{rank} of {suit}", arcana="Minor Arcana", suit=suit, keywords=f"{kw}; suit domain: {spec['domain']}"))

SPREAD_VARIANTS = [
    (
        "Core theoretical triad",
        [
            "Main answer / the strongest symbolic claim",
            "Complement, correction, or necessary second force",
            "What this is not / the wrong way to see it / false path",
        ],
    ),
    (
        "Dialectical triad",
        [
            "Thesis shown by the cards",
            "Counter-pressure or hidden complication",
            "Rejected synthesis / seductive but misleading resolution",
        ],
    ),
    (
        "Metaphysical lens",
        [
            "Ontological core: what kind of thing this is",
            "Epistemic access: how it can be known or misread",
            "Idol or distortion: the image that must not be worshipped",
        ],
    ),
    (
        "Practical-philosophical lens",
        [
            "True orientation",
            "Condition, limit, or price",
            "Misapplication / vulgar version / dead end",
        ],
    ),
]

SYSTEM_PROMPT = """You are a Tarot-based theoretical oracle.
You interpret questions about philosophy, metaphysics, art, psychology, culture, symbolic systems, and difficult life-patterns.

Rules:
- Always answer in English, even if the user asks in another language, Greek, Greeklish, or mixed-language text.
- Treat the supplied Root research anchor as the permanent subject of the whole sequence.
- Treat the Current question/facet as a temporary lens on that root anchor, never as a replacement anchor.
- If a follow-up question has become narrow, symbolic, or technical, translate it back into the root inquiry before interpreting it.
- The Python program, not you, performs the Tarot draw. Treat the cards as binding symbolic evidence.
- The Python program may supply any number of cards. Follow the supplied card count and roles exactly.
- Do not dismiss a card because it seems counterintuitive. Give priority to the cards within the limits of Tarot symbolism.
- Also use your knowledge, judgement, and conceptual precision. Do not become vague New Age fog.
- Preserve tension. If the spread contradicts itself, interpret the contradiction rather than smoothing it away.
- If a supplied card role is negative, apophatic, or corrective, treat it that way; otherwise do not force the old three-card pattern.
- You may open the question laterally, then close it again, but every detour must clarify a facet, contrast, mechanism, threshold, implication, or counterpressure of the root subject.
- Do not dilute the answer into generic moral, ethical, spiritual, or metaphysical generalities unless that dimension is part of the root question or directly forced by the cards.
- When previous relevant readings are supplied, synthesize with them, but use them to sharpen the present question rather than replacing it with the older theme.
- Related questions must serve the root anchor. Prefer: one deeper question about the current facet, one adjacent facet of the root anchor, and one zoom-out question reconnecting the current finding to the root trajectory.
- Avoid turning Tarot card names, metaphors, or prior phrasing into the next anchor unless the user explicitly asks for that symbolic mode.
- For any subject, choose the level of detail that best serves the root inquiry, ranging from broad synthesis to method-level, material, quantitative, or formal detail when that level changes the interpretation.
- Use detail as leverage, not ornament: enter depth when it clarifies mechanism, evidence, constraints, thresholds, alternatives, or consequences, then reconnect the detail to the wider pattern and root trajectory.
- For technical, mathematical, scientific, craft, strategic, artistic, psychological, historical, or other domain-specific topics: enter concrete domain detail when it clarifies the root inquiry, but do not let detail crowd out the wider pattern, adjacent facets, or long-range trajectory.
- In formal or technical domains, use Tarot to select candidate variables, constraints, relationships, established models, methods, evidence types, and missing measurements or observations.
- Prefer established model families, concepts, and domain methods over invented formulas or pseudo-precision.
- When the subject calls for it, make the answer quantitative, mathematical, operational, or otherwise precise as far as the available information allows.
- When a specific established formula, equation, named law, or model form fits the question, include it explicitly and explain why it fits.
- For quantitative approximations, state assumptions, units, ranges or orders of magnitude, uncertainty, and what would change the estimate.
- Do not invent exact numeric results, constants, citations, mechanisms, or novel equations as if they were established facts.
- If you include an equation or formal model, label whether it is established, a dimensional sketch, or a symbolic heuristic, and define every variable.
- If the data needed for calculation or verification is absent, say what would need to be measured, observed, or researched instead of pretending to settle it.
- Avoid medical, legal, or financial certainty. For such topics, frame the reading as symbolic reflection, not professional advice.

Output format exactly:
SYMBOLIC_NOTES_BEGIN
Brief visible notes about your interpretive route. Do not expose private hidden chain-of-thought; give compact symbolic working notes.
SYMBOLIC_NOTES_END

READING_BEGIN
Title: <short title>

Root research anchor:
<the permanent root question supplied by Python>

Current question / facet:
<question>

Cards:
<one numbered line per supplied card, each as: N. <card> - <role>>

Reading:
<the main interpretation, with clear paragraphs>

Domain / technical lens:
<If the topic benefits from concrete domain detail, name candidate variables, models, methods, constraints, evidence types, specific established formulas/equations if appropriate, and missing measurements or observations. Otherwise say: Not applicable.>

Synthesis with previous readings:
<If relevant prior readings exist, synthesize briefly. Otherwise say: No prior synthesis needed.>

What to keep:
<one compact takeaway>

RELATED_QUESTIONS:
1. <deeper question about the current facet, still tied to the root anchor>
2. <adjacent facet of the root anchor>
3. <zoom-out question reconnecting this finding to the root trajectory>

SYNTHESIS_UPDATE:
<5-8 lines max, a durable note for future readings. It must name the root anchor and how this reading develops that root inquiry.>
READING_END
"""

SPREAD_PLANNER_PROMPT = """You plan Tarot spreads for a Python oracle.

Rules:
- Always answer in English.
- Do not draw cards. Python will draw the cards after you choose the spread.
- Python will supply both a Root research anchor and a Current question/facet.
- The Root research anchor is the permanent inquiry for the whole sequence.
- The Current question/facet is only the present lens. Do not let it replace the root anchor.
- Decide how many cards are needed to examine the current facet well while keeping it useful for the root inquiry.
- Use only as many positions as the question genuinely needs: compact for focused questions, larger for layered questions.
- Give each position a clear role explaining what that card stands for.
- Keep all card positions anchored in the root research subject.
- Design the spread to examine different facets, distinctions, mechanisms, thresholds, or counterpressures inside that subject.
- If the current facet benefits from domain-specific detail, roles may ask for variables, constraints, model families, methods, evidence types, thresholds, counterexamples, or missing measurements/observations.
- Prefer established domain methods and model families over invented formulas or pseudo-precision.
- Do not transform the question into broad ethical or metaphysical generalities unless the user explicitly asks for that scope.
- Return only valid JSON, with no markdown and no commentary.

JSON shape:
{
  "spread_name": "short spread name",
  "rationale": "one short sentence explaining why this spread size and pattern fits",
  "roles": [
    "what card 1 stands for",
    "what card 2 stands for"
  ]
}
"""


@dataclass
class ReadingRecord:
    timestamp: str
    question: str
    spread_name: str
    roles: List[str]
    cards: List[Dict[str, str]]
    notes: str
    reading: str
    related_questions: List[str]
    synthesis_update: str
    plan_rationale: str = ""
    md_path: str = ""
    pdf_path: str = ""
    root_question: str = ""
    current_question: str = ""


@dataclass
class SpreadPlan:
    spread_name: str
    roles: List[str]
    rationale: str = ""



def slugify(text: str, max_len: int = 60) -> str:
    text = re.sub(r"[^\w\s-]", "", text, flags=re.UNICODE).strip().lower()
    text = re.sub(r"[\s_-]+", "_", text)
    return (text[:max_len].strip("_") or "reading")


def safe_filename_part(text: str, fallback: str = "part", max_len: int = 80) -> str:
    text = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", text, flags=re.UNICODE)
    text = re.sub(r"\s+", "_", text.strip())
    text = re.sub(r"_+", "_", text)
    return (text[:max_len].strip("._ ") or fallback)




def choose_spread(randomize: bool = True) -> Tuple[str, List[str]]:
    if randomize:
        return RNG.choice(SPREAD_VARIANTS)
    return SPREAD_VARIANTS[0]


def draw_cards(n: int = 3) -> List[TarotCard]:
    n = max(1, min(len(DECK), int(n)))
    return RNG.sample(DECK, n)


def record_from_dict(data: Dict) -> Optional[ReadingRecord]:
    """Build a ReadingRecord from browser-supplied JSON, ignoring unknown keys."""
    if not isinstance(data, dict):
        return None
    allowed = ReadingRecord.__dataclass_fields__.keys()
    clean = {k: v for k, v in data.items() if k in allowed}
    try:
        rec = ReadingRecord(**clean)
    except TypeError:
        return None
    rec.cards = [c for c in (rec.cards or []) if isinstance(c, dict)]
    rec.roles = [str(r) for r in (rec.roles or [])]
    rec.related_questions = [str(q) for q in (rec.related_questions or [])]
    return rec


def fallback_spread_plan(randomize: bool = True, reason: str = "") -> SpreadPlan:
    spread_name, roles = choose_spread(randomize)
    rationale = reason or "Local fallback spread selected because the planner could not return a valid spread."
    return SpreadPlan(spread_name=spread_name, roles=list(roles), rationale=rationale)


def parse_spread_plan(raw: str, randomize_fallback: bool = True) -> SpreadPlan:
    text = (raw or "").strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.S)
        if not m:
            return fallback_spread_plan(randomize_fallback)
        try:
            data = json.loads(m.group(0))
        except json.JSONDecodeError:
            return fallback_spread_plan(randomize_fallback)

    role_source = data.get("roles") or data.get("cards") or data.get("positions") or []
    roles: List[str] = []
    if isinstance(role_source, list):
        for item in role_source:
            if isinstance(item, dict):
                role = item.get("role") or item.get("meaning") or item.get("position") or item.get("stands_for") or ""
            else:
                role = str(item)
            role = " ".join(str(role).split())
            if role:
                roles.append(role)

    requested_count = data.get("num_cards") or data.get("card_count") or data.get("count")
    try:
        requested_count = int(requested_count)
    except (TypeError, ValueError):
        requested_count = len(roles)
    if requested_count > 0 and len(roles) < requested_count:
        roles.extend(f"Position {i}" for i in range(len(roles) + 1, requested_count + 1))

    roles = roles[: len(DECK)]
    if not roles:
        return fallback_spread_plan(randomize_fallback)

    spread_name = " ".join(str(data.get("spread_name") or data.get("name") or "LLM-planned spread").split())
    rationale = " ".join(str(data.get("rationale") or data.get("reason") or "").split())
    return SpreadPlan(spread_name=spread_name, roles=roles, rationale=rationale)


def keyword_set(text: str) -> set:
    words = re.findall(r"\w+", text.lower(), re.UNICODE)
    stop = {
        "the", "this", "that", "with", "from", "einai", "auto", "pou", "gia", "sto", "stin", "ston",
        "mesa", "mipos", "what", "why", "how", "does", "is", "are", "of", "to", "in",
        "\u03ba\u03b1\u03b9", "\u03c4\u03bf", "\u03b7", "\u03bf", "\u03bd\u03b1", "\u03c3\u03b5", "\u03bc\u03b5",
    }
    return {w for w in words if len(w) > 3 and w not in stop}


def same_root(a: str, b: str) -> bool:
    return bool(a.strip() and b.strip() and a.strip().casefold() == b.strip().casefold())


def select_relevant_history(
    records: List[ReadingRecord],
    root_question: str,
    question: str,
    cards: List[TarotCard],
) -> List[ReadingRecord]:
    qk = keyword_set(root_question + " " + question)
    card_names = {c.name.lower() for c in cards}
    scored = []
    for rec in records:
        rec_root = rec.root_question or ""
        rk = keyword_set(rec_root + " " + rec.question + " " + rec.current_question + " " + rec.synthesis_update + " " + rec.reading[:1200])
        overlap = len(qk & rk)
        card_overlap = sum(1 for c in rec.cards if c.get("name", "").lower() in card_names)
        score = overlap + 2 * card_overlap
        if same_root(rec_root, root_question):
            score += 4
        if score > 0:
            scored.append((score, rec))
    scored.sort(key=lambda x: x[0], reverse=True)
    relevant = [r for _, r in scored[:MAX_RELEVANT_HISTORY]]
    # Preserve the first reading in the same root sequence as the origin point.
    for r in records:
        if same_root(r.root_question or r.question, root_question):
            if r not in relevant:
                relevant.insert(0, r)
            break
    # Also include a few recent readings for continuity, without duplicating.
    for r in records[-MAX_RECENT_HISTORY:]:
        if r not in relevant:
            relevant.append(r)
    return relevant[:MAX_RELEVANT_HISTORY]


def build_history_context(records: List[ReadingRecord]) -> str:
    if not records:
        return "No previous readings supplied."
    chunks = []
    for idx, rec in enumerate(records, 1):
        card_line = ", ".join(c.get("name", "") for c in rec.cards)
        chunks.append(
            f"Previous reading {idx}\n"
            f"Date: {rec.timestamp}\n"
            f"Root anchor: {(rec.root_question or rec.question).strip()}\n"
            f"Question: {rec.question}\n"
            f"Current facet: {(rec.current_question or rec.question).strip()}\n"
            f"Cards: {card_line}\n"
            f"Durable synthesis note: {rec.synthesis_update.strip() or '(none)'}\n"
        )
    return "\n---\n".join(chunks)


def build_user_prompt(
    root_question: str,
    question: str,
    spread_name: str,
    roles: List[str],
    cards: List[TarotCard],
    history_context: str,
    plan_rationale: str = "",
) -> str:
    card_lines = []
    for i, (role, card) in enumerate(zip(roles, cards), 1):
        card_lines.append(
            f"{i}. {card.name}\n"
            f"   Role: {role}\n"
            f"   Arcana/suit: {card.arcana}, {card.suit}\n"
            f"   Keywords: {card.keywords}"
        )
    return f"""Root research anchor:
{root_question}

Current question / facet:
{question}

Spread variant:
{spread_name}

Spread planning rationale:
{plan_rationale or 'No separate rationale supplied.'}

Python RNG Tarot draw:
{chr(10).join(card_lines)}

Relevant previous readings and durable notes:
{history_context}

Focus discipline for this v3 reading:
- Keep the root research anchor as the permanent subject from beginning to end.
- Treat the current question as a facet, probe, or sub-problem of the root anchor.
- Treat each card as a different facet of the current probe only insofar as it clarifies the root inquiry.
- If you open a lateral or stranger path, explicitly bring it back to the root anchor.
- Choose the level of detail that best serves this root inquiry; go deep when depth changes the reading, then reconnect the detail to the broader pattern.
- For domain-specific material, use the cards to choose relevant variables, models, methods, constraints, evidence types, and missing measurements or observations.
- Do not invent exact calculations, mechanisms, or citations; use established models only when they fit, and label speculative sketches clearly.
- When appropriate, make the answer quantitative or operationally precise, using assumptions, ranges, units, and uncertainty rather than false exactness.
- When an established formula, named law, or model form fits, include the specific formula and define its variables.
- Include concrete detail where it helps, while keeping the broader pattern and adjacent facets of the root inquiry visible.
- Propose related questions in this order: current-facet deepening, adjacent root-facet, zoom-out back to the root trajectory.

Please interpret the present reading according to the required output format.
"""


def extract_between(text: str, start: str, end: str) -> str:
    m = re.search(re.escape(start) + r"(.*?)" + re.escape(end), text, re.S | re.I)
    return m.group(1).strip() if m else ""


def parse_related_questions(reading_text: str) -> List[str]:
    m = re.search(r"RELATED_QUESTIONS:\s*(.*?)(?:\n\s*SYNTHESIS_UPDATE:|\n\s*READING_END|$)", reading_text, re.S | re.I)
    if not m:
        return []
    block = m.group(1).strip()
    qs = []
    for line in block.splitlines():
        line = re.sub(r"^\s*[-*\d.)]+\s*", "", line).strip()
        if line:
            qs.append(line)
    return qs[:5]


def parse_synthesis_update(reading_text: str) -> str:
    m = re.search(r"SYNTHESIS_UPDATE:\s*(.*?)(?:\n\s*READING_END|$)", reading_text, re.S | re.I)
    return m.group(1).strip() if m else ""


def make_markdown(record: ReadingRecord) -> str:
    cards = "\n".join(
        f"{i}. **{c['name']}** - {role}\n   - {c.get('arcana','')}, {c.get('suit','')}\n   - {c.get('keywords','')}"
        for i, (c, role) in enumerate(zip(record.cards, record.roles), 1)
    )
    related = "\n".join(f"{i}. {q}" for i, q in enumerate(record.related_questions, 1)) or "(none parsed)"
    root_question = (record.root_question or record.question).strip()
    current_question = (record.current_question or record.question).strip()
    return f"""# Tarot LLM Oracle Reading

**Date:** {record.timestamp}  
**Spread:** {record.spread_name}
**Spread plan rationale:** {record.plan_rationale.strip() or '(none)'}

## Root research anchor

{root_question}

## Current question / facet

{current_question}

## Cards

{cards}

## Symbolic notes / visible thinking

{record.notes.strip() or '(none)'}

## Reading

{record.reading.strip()}

## Related questions proposed by the model

{related}

## Durable synthesis update

{record.synthesis_update.strip() or '(none)'}
"""


def find_pdf_font() -> Optional[str]:
    candidates = [
        r"C:\Windows\Fonts\arial.ttf",
        r"C:\Windows\Fonts\calibri.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
        "/Library/Fonts/Arial Unicode.ttf",
        "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
    ]
    for p in candidates:
        if os.path.exists(p):
            return p
    return None


def build_pdf(record: ReadingRecord) -> Optional[bytes]:
    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        from reportlab.lib.units import cm
        from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.ttfonts import TTFont
    except Exception:
        return None

    buffer = io.BytesIO()

    font_name = "Helvetica"
    font_path = find_pdf_font()
    if font_path:
        try:
            if "OracleFont" not in pdfmetrics.getRegisteredFontNames():
                pdfmetrics.registerFont(TTFont("OracleFont", font_path))
            font_name = "OracleFont"
        except Exception:
            font_name = "Helvetica"

    styles = getSampleStyleSheet()  # fresh sheet per PDF, so repeated add() is safe
    styles.add(ParagraphStyle(name="OracleTitle", parent=styles["Title"], fontName=font_name, fontSize=18, leading=22))
    styles.add(ParagraphStyle(name="OracleHeading", parent=styles["Heading2"], fontName=font_name, fontSize=13, leading=16, spaceBefore=12))
    styles.add(ParagraphStyle(name="OracleBody", parent=styles["BodyText"], fontName=font_name, fontSize=10.5, leading=14, spaceAfter=7))
    styles.add(ParagraphStyle(name="OracleSmall", parent=styles["BodyText"], fontName=font_name, fontSize=9, leading=12, leftIndent=10))

    def esc(s: str) -> str:
        return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("\n", "<br/>")

    root_question = (record.root_question or record.question).strip()
    current_question = (record.current_question or record.question).strip()

    doc = SimpleDocTemplate(
        buffer, pagesize=A4, rightMargin=1.6 * cm, leftMargin=1.6 * cm, topMargin=1.6 * cm, bottomMargin=1.6 * cm
    )
    story = [Paragraph("Tarot LLM Oracle Reading", styles["OracleTitle"])]
    story.append(
        Paragraph(
            f"Date: {esc(record.timestamp)}<br/>Spread: {esc(record.spread_name)}<br/>Plan: {esc(record.plan_rationale or '(none)')}",
            styles["OracleSmall"],
        )
    )
    story.append(Spacer(1, 8))
    story.append(Paragraph("Root research anchor", styles["OracleHeading"]))
    story.append(Paragraph(esc(root_question), styles["OracleBody"]))
    story.append(Paragraph("Current question / facet", styles["OracleHeading"]))
    story.append(Paragraph(esc(current_question), styles["OracleBody"]))
    story.append(Paragraph("Cards", styles["OracleHeading"]))
    for i, (card, role) in enumerate(zip(record.cards, record.roles), 1):
        story.append(Paragraph(f"{i}. <b>{esc(card['name'])}</b> - {esc(role)}<br/>{esc(card.get('keywords',''))}", styles["OracleSmall"]))
    story.append(Paragraph("Symbolic notes / visible thinking", styles["OracleHeading"]))
    story.append(Paragraph(esc(record.notes or "(none)"), styles["OracleBody"]))
    story.append(Paragraph("Reading", styles["OracleHeading"]))
    for para in re.split(r"\n\s*\n", record.reading.strip()):
        story.append(Paragraph(esc(para), styles["OracleBody"]))
    story.append(Paragraph("Related questions", styles["OracleHeading"]))
    for i, q in enumerate(record.related_questions, 1):
        story.append(Paragraph(f"{i}. {esc(q)}", styles["OracleSmall"]))
    story.append(Paragraph("Durable synthesis update", styles["OracleHeading"]))
    story.append(Paragraph(esc(record.synthesis_update or "(none)"), styles["OracleBody"]))
    doc.build(story)
    return buffer.getvalue()
