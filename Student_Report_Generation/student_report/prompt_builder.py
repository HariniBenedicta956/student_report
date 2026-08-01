import json
import re

from scoring import compute_section_hints

# Existing fields that already have special handling elsewhere (pdf_generator draws a
# real bar chart for "section_scores", the PDF header already prints name/branch/year/
# institution). When a requester lists section names, map obvious matches onto these
# instead of minting a new custom key, so the AI's output actually gets that treatment.
_FIELD_ALIASES = [
    (("score", "chart"), "section_scores"),
    (("strength",), "strengths"),
    (("improvement", "weakness", "growth", "focus next", "focus area"), "where_to_focus_next"),
    (("next step", "recommendation", "action plan"), "next_steps"),
    (("interest",), "interest_zone"),
    (("closing", "disclaimer"), "closing_note"),
    (("overall", "summary", "insight", "where you stand", "performance"), "where_you_stand"),
    (("shows up", "evidence", "specific example"), "how_this_shows_up"),
]
# Already covered by the PDF's static title/header (name, branch, year, institution) --
# not something the AI should be asked to generate.
_SKIP_PHRASES = ("cover", "student detail", "report date")


def _looks_like_section_list(text):
    """True for '[A, B, C]' or 'A, B, C' -- a list of short labels, not a sentence."""
    text = text.strip()
    if text.startswith("[") and text.endswith("]"):
        text = text[1:-1]
    if text.count(",") < 2:
        return False
    parts = [p.strip() for p in text.split(",")]
    return all(0 < len(p) < 60 for p in parts) and not any(p.endswith(".") for p in parts[:-1])


def _parse_section_list(text):
    text = text.strip()
    if text.startswith("[") and text.endswith("]"):
        text = text[1:-1]
    return [p.strip() for p in text.split(",") if p.strip()]


def _slugify_section_name(name):
    slug = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
    return slug or "section"


def _map_section_name(name):
    lower = name.lower()
    if any(p in lower for p in _SKIP_PHRASES):
        return None
    for keywords, field in _FIELD_ALIASES:
        if any(k in lower for k in keywords):
            return field
    return _slugify_section_name(name)


def _build_custom_schema(section_names):
    lines = [
        "The report requester listed the exact sections to include, in this order:",
        ", ".join(section_names),
        "",
        "Output a JSON object with one field per section below (skipping any noted as "
        "already covered). Fill each with real generated content -- a paragraph or a "
        'bulleted list of strings, except "section_scores" which uses the normal '
        '{"<key>": {"label": ..., "score": <0-100 integer>}} shape covering all sections '
        "B-G. Never leave a field empty or use placeholder text.",
        "{",
    ]
    seen_keys = set()
    for name in section_names:
        key = _map_section_name(name)
        if key is None:
            lines.append(f'  // "{name}" -- already covered by the PDF header, skip this one')
            continue
        if key in seen_keys:
            # Two different requested sections both aliased to the same known field
            # (e.g. "Overall Performance Summary" and "Personalized Insights" both look
            # like "where_you_stand") -- fall back to a name-derived key so the second
            # one is still generated instead of silently dropped, just without the
            # special rendering treatment the alias would have given it.
            base = _slugify_section_name(name)
            key, suffix = base, 2
            while key in seen_keys:
                key = f"{base}_{suffix}"
                suffix += 1
        seen_keys.add(key)
        lines.append(f'  "{key}": <content for "{name}">,')
    lines.append("}")
    return "\n".join(lines)


DEFAULT_SCHEMA_DESCRIPTION = """
Default output shape (use this unless "Extra instructions from the report requester"
below clearly ask for something different). This shape is FIXED across every student
in this batch -- always include every field below with these exact keys, whether or
not there's much to say for a given student. Do not rename, drop, restructure, or
merge these fields, and do not replace this shape with a different one on your own
initiative -- only "Extra instructions from the report requester" below can do that:
{
  "section_scores": {
    "<section key, e.g. B>": {"label": "<section label>", "score": <integer 0-100>}
    // one entry per section that has questions (always -- section_scores itself is
    // never optional). You may ADD one or two extra entries here for a genuinely
    // notable ad-hoc parameter from unmapped_questions -- that's the only part of
    // this shape that's allowed to vary between students.
  },
  // IMPORTANT: every "score" is on a 0-100 scale (0 = lowest maturity, 100 = highest).
  // It is NOT out of 10, NOT out of the number of questions in the section, and NOT a
  // percentile. Judge maturity from the answers themselves and place it on the full
  // 0-100 range -- do not compress your scores into a narrow low band by default.
  // A section with computed_hint null still MUST get a real numeric score -- that just
  // means there's no pre-computed number to anchor on, so judge it entirely from how
  // good/correct the answers to that section's questions were. NEVER set a score to
  // null, omit it, or skip a section because computed_hint was null.
  "where_you_stand": "<paragraph reading the pattern across ALL scored bars>",
  "how_this_shows_up": "<paragraph with 1-2 concrete examples from this student's actual answers>",
  "strengths": ["<evidence-based strength>", ...],
  "where_to_focus_next": ["<framed as opportunity, never a flaw>", ...],
  "interest_zone": "<what the pattern of answers suggests they're drawn toward>",
  "next_steps": ["<3-5 concrete, doable actions>", ...],
  "closing_note": "<short reminder this is a snapshot, not a permanent label>"
}

If "Extra instructions from the report requester" below ask for something structurally
different (e.g. "list every question and answer, then give the final result alone"),
IGNORE the default shape above and return ONLY the JSON fields that fulfill that
request (for example {"qa_listing": [{"question": "...", "answer": "..."}],
"final_result": "..."}). Only include a field if you are actually filling it with real
content -- never include an empty or placeholder field.

If asked to list questions and answers (all of them, or a filtered subset like "just
the wrong ones"), you MUST go through every single section in the user message's
"sections" object plus every entry in "unmapped_questions" and include EVERY one that
matches the request -- do not sample, summarize, or stop after a handful. The data has
roughly 40 section questions plus any unmapped ones; a short list is very likely an
incomplete one.

Instructions may be phrased as a sentence, OR as a plain list of section names
(comma-separated, or wrapped in brackets like "[Cover Page, Key Strengths, Next
Steps]"). A list of section names is just as authoritative as a sentence -- it means
"produce exactly these sections, in this order, each with real written content", not
decoration to ignore.
""".strip()

TONE_RULES = (
    "Always write in encouraging, non-comparative language. Never describe the student "
    "in a way that could feel like a judgment of their worth -- only in terms of where "
    "they are now and what would help them grow. Never compare this student to peers."
)


def _build_system_message(mapping, instructions_text):
    is_section_list = bool(instructions_text) and _looks_like_section_list(instructions_text)
    schema_description = (
        _build_custom_schema(_parse_section_list(instructions_text))
        if is_section_list
        else DEFAULT_SCHEMA_DESCRIPTION
    )

    parts = [
        "You are writing a personalized career-readiness report for one student.",
        "The next message (role: user) contains the student's data as a JSON object: "
        "identity, their answers grouped by section (each section includes a "
        "computed_hint, which is a preliminary 0-100 score computed directly from the "
        "answers -- null means there's no pre-computed number and you must judge that "
        "section entirely yourself), and any unmapped_questions that don't belong to a "
        "fixed section. Each answer also has is_correct: true/false for scenario "
        "questions with a known right answer, or null where there's no single correct "
        "answer to judge against. If asked about wrong answers, mistakes, or what to "
        "improve on specific questions, use this is_correct field as ground truth -- "
        "don't re-judge correctness yourself.",
        TONE_RULES,
        "",
        "Extra instructions from the report requester (this is the most important part "
        "of this message -- it overrides the default shape below whenever they conflict):",
        instructions_text or "(none given -- use the default report shape)",
        "",
        schema_description,
    ]
    if instructions_text and not is_section_list:
        parts += [
            "",
            f'REMINDER: the report requester specifically asked for this: '
            f'"{instructions_text}". Follow it even where it means deviating from the '
            f'default shape above -- their instruction takes priority over the default shape.',
        ]
    parts += ["", "Output ONLY valid JSON. No markdown fences, no commentary."]
    return "\n".join(parts)


def _build_data_payload(student_record, mapping):
    identity = student_record["identity"]
    sections = student_record["sections"]
    unmapped = student_record["unmapped"]
    hints = compute_section_hints(sections)

    sections_payload = {}
    for section_key, section_cfg in mapping["sections"].items():
        sections_payload[section_key] = {
            "label": section_cfg.get("label", section_key),
            "computed_hint": hints.get(section_key),
            "answers": [
                {
                    "question": q["question"],
                    "answer": q["answer"],
                    "multi_select": q["multi_select"],
                    # true/false for scenario questions with a known correct answer
                    # (sections F/G), null where there's no single right answer to
                    # judge against (e.g. self-report maturity questions) -- lets a
                    # request like "list the wrong answers" use real ground truth
                    # instead of the model having to re-derive correctness itself.
                    "is_correct": q["is_correct"],
                }
                for q in sections.get(section_key, [])
            ],
        }

    return {
        "student": {
            "name": identity.get("name", ""),
            "branch": identity.get("branch", ""),
            "year": identity.get("year", ""),
            "institution": identity.get("institution", ""),
        },
        "sections": sections_payload,
        "unmapped_questions": [
            {"question": u["question"], "answer": u["answer"]} for u in unmapped
        ],
    }


def build_messages(student_record, mapping, instructions_text):
    """
    Returns an Ollama /api/chat messages list: a system message carrying the tone
    rules, output schema and the requester's instructions, and a user message
    carrying the student's data as a clean JSON object -- separating "what to do"
    from "the data" instead of concatenating everything into one text blob, which
    is both the natural format for a chat-tuned model like Hermes-3 and much less
    likely to bury the requester's instructions where the model stops noticing them.
    """
    system_content = _build_system_message(mapping, instructions_text)
    data_payload = _build_data_payload(student_record, mapping)
    user_content = json.dumps(data_payload, indent=2, ensure_ascii=False)
    return [
        {"role": "system", "content": system_content},
        {"role": "user", "content": user_content},
    ]


_QA_LISTING_KEYWORDS = (
    "list all", "list every", "list the question", "list question",
    "questions answered", "q&a", "q & a", "list the answer",
)
_WRONG_ONLY_KEYWORDS = (
    "wrong answer", "wrong ones", "wrong one", "mistake", "incorrect answer",
    "picked wrong", "answered wrong", "got wrong",
)


def wants_qa_listing(instructions_text):
    """
    True when the requester is asking to see the raw questions/answers themselves,
    as opposed to a narrative that merely references them. An 8B model reliably
    fails to enumerate everything on its own (tested: asked for all ~46 answers,
    returned 6) no matter how explicitly it's told not to sample -- so this listing
    is built in Python instead (see build_qa_listing) and the AI is only asked for
    the narrative/advice that goes with it.
    """
    if not instructions_text:
        return False
    lower = instructions_text.lower()
    return any(k in lower for k in _QA_LISTING_KEYWORDS)


def wants_wrong_answers_only(instructions_text):
    if not instructions_text:
        return False
    lower = instructions_text.lower()
    return any(k in lower for k in _WRONG_ONLY_KEYWORDS)


def build_qa_listing(student_record, wrong_only=False):
    """
    Builds the question/answer list directly from parsed data -- guaranteed complete
    and accurate, since it's not going through the model at all. When wrong_only,
    keeps only answers with a known-false is_correct (scenario questions with a
    marked correct_answer); answers with no ground truth (is_correct is None) are
    left out rather than guessed at.
    """
    items = []
    for questions in student_record["sections"].values():
        for q in questions:
            if wrong_only:
                if q.get("is_correct") is False:
                    items.append({"question": q["question"], "answer": q["answer"]})
            else:
                items.append({"question": q["question"], "answer": q["answer"]})
    if not wrong_only:
        for u in student_record["unmapped"]:
            items.append({"question": u["question"], "answer": u["answer"]})
    return items


def build_advice_messages(student_record, mapping, instructions_text, qa_listing):
    """
    Messages for the "list Q&A + give advice" path. The listing itself is already
    built (see build_qa_listing) and handed to the model as read-only, already-
    correct context -- the model's only job is to write the accompanying advice,
    which is a much smaller and more reliable task than also having to reproduce
    dozens of Q&A pairs verbatim.
    """
    identity = student_record["identity"]
    system_content = "\n".join([
        "You are writing career guidance for one student. The user message contains "
        "the student's identity and a pre-built qa_listing -- it is already complete "
        "and correct, so do NOT recreate, summarize, or re-filter it yourself.",
        TONE_RULES,
        "",
        f"What the report requester asked for: {instructions_text}",
        "",
        'Output a JSON object with exactly one field, "advice": a written response '
        "(a paragraph, or a bulleted list of strings if that fits the request better) "
        "that fulfills what the requester asked for, based on the qa_listing provided. "
        "Do not include the qa_listing in your output -- it will be attached separately.",
        "",
        "Output ONLY valid JSON. No markdown fences, no commentary.",
    ])
    user_payload = {
        "student": {
            "name": identity.get("name", ""),
            "branch": identity.get("branch", ""),
            "year": identity.get("year", ""),
            "institution": identity.get("institution", ""),
        },
        "qa_listing": qa_listing,
    }
    return [
        {"role": "system", "content": system_content},
        {"role": "user", "content": json.dumps(user_payload, indent=2, ensure_ascii=False)},
    ]


def build_fallback_report(student_record, mapping):
    """Minimal templated report used when the AI never returns valid JSON."""
    sections = student_record["sections"]
    hints = compute_section_hints(sections)
    section_scores = {}
    for section_key, section_cfg in mapping["sections"].items():
        hint = hints.get(section_key)
        section_scores[section_key] = {
            "label": section_cfg.get("label", section_key),
            "score": round(hint) if hint is not None else 0,
        }
    return {
        "section_scores": section_scores,
        "where_you_stand": (
            "We weren't able to generate a full narrative for this report right now. "
            "The scores above are computed directly from your answers."
        ),
        "closing_note": "This is a snapshot from one assessment, not a permanent label.",
    }
