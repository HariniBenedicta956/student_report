import json

import config
from core.scoring import compute_section_hints

# NOTE: there used to be a "section list" branch here -- a heuristic that decided an
# instruction containing two or more commas was a list of section NAMES, and built a
# JSON schema with one field per comma-separated phrase. It misfired on ordinary
# prose: "focus on career guidelines, critical thinking, system thinking and make
# sure the ui interface is good" became the fields focus_on_career_guidelines,
# critical_thinking and system_thinking_and_make_sure_the_ui_interface_is_good. The
# requester's sentence was turned into headings instead of being followed. It is
# deleted rather than tightened -- the instruction now outranks the schema directly
# (see _SCHEMA_INSTRUCTION_LED), so nothing needs to guess at the shape of the
# request in the first place.


# The four tier labels the Personal Learning Growth Report template renders as
# coloured pills. A fixed, closed set -- the PDF has a colour per tier and nothing
# to draw for a value outside it, so anything else is a validation failure rather
# than something to render loosely.
TIERS = ("Strength", "Developing", "Focus Required", "Blind Spot")

# The report shape the PDF template actually renders. This is deliberately a fixed
# schema again: the template has named, laid-out regions (profile dimensions with
# tier pills, strength cards, focus cards with a "Try this" action, blind-spot
# cards, one dark priority panel), so the model cannot invent its own fields
# without producing something the template has nowhere to put.
#
# Note it is entirely QUALITATIVE -- tiers, not numbers. The template says so
# explicitly ("qualitative bands, not numeric or percentile"), so there is no
# 0-100 score anywhere in this schema.
_SCHEMA_REPORT = """
Output EXACTLY this JSON shape. Every field is required. The PDF template has a
fixed place for each one, so a missing or renamed field leaves a hole in the report.

{
  "intro_message": "<2-3 sentences addressed to the student BY NAME, framing what
                     this report is. Warm, specific to them, not generic.>",

  "dimensions": [
    {
      "name": "<short dimension name, 2-4 words, e.g. 'Learning Consistency'>",
      "description": "<ONE sentence, evidence-based, pointing at what their own
                       answers actually showed. Not advice -- an observation.>",
      "tier": "<exactly one of: Strength | Developing | Focus Required | Blind Spot>"
    }
    // one per dimension you judge from the answers -- aim for 5-7, ordered
    // strongest first. Cover the range of the question bank rather than
    // clustering on one theme.
  ],

  "strong": [
    {"headline": "<short phrase naming the pattern>",
     "body": "<1-2 sentences on how this shows up in their answers>"}
    // their strongest patterns -- 2-3 entries
  ],

  "focus": [
    {"headline": "<short phrase naming what to work on>",
     "body": "<1-2 sentences, framed as opportunity, never as a flaw>",
     "action": "<one concrete thing they could actually do this week>"}
    // where focus pays off most -- 2-3 entries
  ],

  "blindspot": [
    {"headline": "<short phrase naming the mismatch>",
     "body": "<1-2 sentences on where what they believe and what they do differ>",
     "action": "<one concrete thing they could actually do this week>"}
    // belief vs behaviour -- 1-2 entries. Only include a blind spot the answers
    // genuinely evidence; an empty-handed guess here is worse than none.
  ],

  "single_priority": {
    "headline": "<the ONE thing to focus on for the bootcamp>",
    "body": "<1-2 sentences on why this one, ahead of everything else>"
  }
}

Rules that matter:
  * "tier" must be one of the four labels exactly as spelled above. Any other value
    cannot be rendered.
  * This report is qualitative. Do NOT invent numeric scores, percentages,
    percentiles or ratings anywhere -- the template deliberately has no place for
    them and they read as precision the answers do not support.
  * Every claim must be traceable to this student's actual answers. Do not assert
    experience, projects, internships or achievements they did not report.
  * "action" fields must be doable in a week, not a career plan.
  * Never emit an empty string, an empty array, a placeholder, or a field left as
    the description above.
""".strip()

_SCHEMA_INSTRUCTION_NOTE = """
The report requester's instruction ABOVE steers the CONTENT of these fields -- what
to emphasise, which dimensions to pick, the tone and depth of the writing. It does
not change the SHAPE: the template renders these fields and no others, so produce
every field above regardless, with the instruction applied to what goes in them.
""".strip()


def _schema_description(instructions_text):
    if instructions_text:
        return _SCHEMA_REPORT + "\n\n" + _SCHEMA_INSTRUCTION_NOTE
    return _SCHEMA_REPORT

TONE_RULES = (
    "Always write in encouraging, non-comparative language. Never describe the student "
    "in a way that could feel like a judgment of their worth -- only in terms of where "
    "they are now and what would help them grow. Never compare this student to peers."
)


def _build_system_message(mapping, instructions_text, question_bank_text=""):
    """
    Order matters here, and not just for readability. The requester's instruction
    is repeated at the very END of this message on purpose -- a model weights the
    tail of its prompt most heavily, and burying the instruction mid-message is
    the difference between it being followed and quietly ignored (observed twice
    on this project). So the question bank, which is bulk reference material,
    goes in the middle: after the data description that refers to it, but before
    the instruction reminder that has to stay last.

    All of this is still byte-identical across students in a batch -- the
    instruction is batch-level too -- so ordering it this way costs nothing in
    prefix-cache reuse.
    """
    schema_description = _schema_description(instructions_text)

    parts = [
        "You are writing a personalized career-readiness report for one student.",
        "The next message (role: user) contains ONLY this one student's data, as a "
        "compact JSON object with three keys. "
        '"student" is their identity. '
        '"section_hints" gives a preliminary 0-100 score per section computed directly '
        "from their answers -- null means there is no pre-computed number and you must "
        "judge that section entirely yourself. "
        '"answers" is keyed by the question ids in the QUESTION BANK below: each value '
        'has "a" (this student\'s answer) and, only for scenario questions with a known '
        'right answer, "correct": true/false. Where "correct" is absent there is no '
        "single right answer to judge against. If asked about wrong answers, mistakes, "
        'or what to improve on specific questions, use "correct" as ground truth -- '
        "don't re-judge correctness yourself. "
        "Read every answer against its question in the QUESTION BANK; a question id "
        "missing from \"answers\" means this student left it blank.",
        TONE_RULES,
    ]
    if question_bank_text:
        parts += ["", question_bank_text]
    parts += [
        "",
        "Extra instructions from the report requester (this is the most important part "
        "of this message for CONTENT -- what to emphasise, which dimensions to pick, "
        "tone and depth. The report template below is fixed and takes priority over "
        "this instruction whenever the two would conflict on SHAPE: never add, drop, "
        "or rename a field because of what is asked here.):",
        instructions_text or "(none given)",
        "",
        schema_description,
    ]
    if instructions_text:
        parts += [
            "",
            f'REMINDER: the report requester asked for exactly this: '
            f'"{instructions_text}". Apply it to the CONTENT of every field in the '
            f'template above -- what you emphasise, which dimensions you pick, tone '
            f'and depth. Still output every field the template defines, exactly as '
            f'shaped, and no additional or renamed fields. Do not fall back to '
            f'generic, unspecific content -- ground everything in this request and '
            f'this student\'s own answers.',
        ]
    parts += ["", "Output ONLY valid JSON. No markdown fences, no commentary."]
    return "\n".join(parts)


def build_question_bank(mapping, student_records):
    """
    The part of the prompt that is byte-identical for every student in a batch:
    section labels and the full text of every question, each tagged with the stable
    id the per-student message uses to attach that student's answer.

    Built once per batch and placed in the *system* message, ahead of any
    per-student data. That ordering is the entire point. Ollama/llama.cpp reuses
    its KV cache only for the longest identical *leading* run of tokens between
    consecutive prompts, so anything shared has to come first to be reused at all.
    Measured against the real host: prompt evaluation is 55% of a report's total
    time, and moving the repeated question text out of the per-student payload and
    in here took evaluation for the second and later students from 146.6s to 40.5s.

    Returns {"text": <bank>, "unmapped_ids": {question_text: id}} -- the id map is
    returned rather than recomputed per student so the ids in the bank and the ids
    in each student's payload cannot drift apart.
    """
    lines = [
        "QUESTION BANK -- the user message gives one student's answers keyed by the "
        "ids below. Read each answer against its question here."
    ]
    for section_key, section_cfg in mapping["sections"].items():
        lines.append("")
        lines.append(f"[{section_key}] {section_cfg.get('label', section_key)}")
        for question in section_cfg["questions"]:
            text = question.get("full_question") or question["column"]
            multi = " (multi-select)" if question.get("multi_select") else ""
            lines.append(f"{question['column']}. {text}{multi}")

    # Questions present in the CSV but not in section_mapping.json. Their text is
    # the same for everyone in a batch (same export, same columns), so they belong
    # in the shared bank too -- they're over half the questions here. Unioned across
    # the whole batch in first-seen order, because a student who left one blank
    # simply won't have it (parse_csv drops empty answers) and the bank still has to
    # cover everyone.
    unmapped_ids = {}
    for record in student_records:
        for u in record["unmapped"]:
            if u["question"] not in unmapped_ids:
                unmapped_ids[u["question"]] = f"U{len(unmapped_ids) + 1}"
    if unmapped_ids:
        lines.append("")
        lines.append("[U] Other questions")
        for question_text, uid in unmapped_ids.items():
            lines.append(f"{uid}. {question_text}")

    return {"text": "\n".join(lines), "unmapped_ids": unmapped_ids}


def _build_student_payload(student_record, mapping, question_bank):
    """
    Only what actually differs between students: identity, the computed section
    hints, and the answers themselves -- keyed by question id, since the question
    text now lives once in the shared bank instead of being repeated per student.

    Serialized compactly (no indent, no spaces after separators). In the previous
    shape 45% of this payload was indentation and repeated key names, which cost
    real prompt-evaluation seconds on every single student while carrying no
    information at all.
    """
    identity = student_record["identity"]
    hints = compute_section_hints(student_record["sections"])

    answers = {}
    for questions in student_record["sections"].values():
        for q in questions:
            entry = {"a": q["answer"]}
            # Only for scenario questions with a known correct answer (sections
            # F/G). Omitted rather than sent as null where there's no single right
            # answer to judge against -- absence says the same thing in fewer
            # tokens, and the system message spells out that reading.
            if q["is_correct"] is not None:
                entry["correct"] = q["is_correct"]
            answers[q["qid"]] = entry
    for u in student_record["unmapped"]:
        uid = question_bank["unmapped_ids"].get(u["question"])
        if uid:
            answers[uid] = {"a": u["answer"]}

    payload = {
        "student": {
            "name": identity.get("name", ""),
            "branch": identity.get("branch", ""),
            "year": identity.get("year", ""),
            "institution": identity.get("institution", ""),
        },
        "section_hints": {key: hints.get(key) for key in mapping["sections"]},
        "answers": answers,
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def build_messages(student_record, mapping, instructions_text, question_bank=None):
    """
    Returns an Ollama /api/chat messages list: a system message carrying the tone
    rules, output schema, the requester's instructions and the shared question
    bank, and a user message carrying only this student's own answers -- separating
    "what to do" from "who this is" instead of concatenating everything into one
    text blob, which is both the natural format for a chat-tuned model like
    Hermes-3 and much less likely to bury the requester's instructions where the
    model stops noticing them.

    question_bank is normally built once per batch and passed in, so it is
    identical across students and its KV cache can be reused. Omitting it builds a
    single-student bank on the spot, which keeps this function usable on its own
    (tests, one-off calls) at the cost of that reuse.
    """
    if question_bank is None:
        question_bank = build_question_bank(mapping, [student_record])
    system_content = _build_system_message(
        mapping, instructions_text, question_bank_text=question_bank["text"]
    )
    return [
        {"role": "system", "content": system_content},
        {"role": "user", "content": _build_student_payload(student_record, mapping, question_bank)},
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
        # Compact separators, same reasoning as _build_student_payload: pretty-print
        # whitespace in a ~40-question listing is thousands of prompt tokens the
        # model gains nothing from, and prompt evaluation is the dominant cost here.
        {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False,
                                                separators=(",", ":"))},
    ]


def build_fallback_report(student_record, mapping):
    """
    Last-resort templated report, used only after the retry budget in
    ollama_client is fully exhausted -- i.e. the host stayed unreachable or kept
    returning unparseable output for the whole window, not on a single failure.

    The dimension tiers here are derived in Python from the computed section hints,
    so they are real even though the written narrative could not be generated. The
    banding matches how the template reads: the report is qualitative, so a hint is
    turned into a tier rather than shown as a number.
    """
    sections = student_record["sections"]
    hints = compute_section_hints(sections)
    name = (student_record["identity"].get("name") or "there").split()[0]

    dimensions = []
    for section_key, section_cfg in mapping["sections"].items():
        hint = hints.get(section_key)
        if hint is None:
            tier = "Developing"
        elif hint >= 75:
            tier = "Strength"
        elif hint >= config.SCORE_PASS_THRESHOLD:
            tier = "Developing"
        else:
            tier = "Focus Required"
        dimensions.append({
            "name": section_cfg.get("label", section_key),
            "description": "Derived directly from your answers in this area.",
            "tier": tier,
        })

    return {
        "intro_message": (
            f"{name}, we weren't able to generate the full written commentary for "
            "this report right now. The profile below is derived directly from the "
            "answers you gave, so it still reflects your own responses."
        ),
        "dimensions": dimensions,
        # strong/focus/blindspot are omitted rather than sent as empty arrays --
        # the written cards genuinely could not be produced, and an absent field
        # renders as "no section" while an empty one reads as a bug.
        "single_priority": {
            "headline": "Review this report with your mentor",
            "body": ("The written sections could not be generated on this run. Your "
                      "profile above is accurate; the commentary is worth revisiting."),
        },
    }
