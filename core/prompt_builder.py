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

# The evidentiary bar for assigning each tier -- the SINGLE source both the
# generation prompt (below, via _tier_criteria_block) and the content validator
# (app.py's CONTENT_VALIDATION_RUBRIC, which imports this dict directly) are
# built from. Before this, "does the tier match the evidence" existed only as a
# vague instruction repeated in two places by hand, free to drift apart --
# whichever one changed, the other silently didn't.
#
# Developing's criterion doubles as the thin/contradictory-evidence rule: it is
# the default a dimension falls back to whenever the evidence doesn't clearly
# clear the bar for Strength or Focus Required, rather than the model having to
# invent a stronger read than the answers support.
TIER_CRITERIA = {
    "Strength": (
        "Two or more of this dimension's answers consistently show the behaviour, "
        "with no answer in the same dimension contradicting it. Never assign from a "
        "single favourable answer alone."
    ),
    "Developing": (
        "The default tier for this dimension. Use it whenever the evidence is "
        "present but partial, inconsistent, drawn from a single answer only, or "
        "thin/mixed/contradictory -- do not stretch thin or conflicting evidence "
        "into Strength or Focus Required; say the evidence is limited instead."
    ),
    "Focus Required": (
        "Two or more of this dimension's answers consistently show a clear gap or "
        "difficulty, with no answer in the same dimension showing the opposite."
    ),
    "Blind Spot": (
        "Only when two specific answers in this dimension directly conflict -- one "
        "stating a belief or self-assessment, another showing behaviour that "
        "contradicts it. Never assigned from a single answer."
    ),
}


def _tier_criteria_block():
    lines = ["TIER CRITERIA -- the evidentiary bar for assigning each tier to a "
             "dimension. A tier that does not meet its own criterion below is a "
             "validation failure:"]
    for tier in TIERS:
        lines.append(f"  * {tier}: {TIER_CRITERIA[tier]}")
    return "\n".join(lines)


# Strength/Focus Required/Blind Spot all require 2+ evidence_refs per
# TIER_CRITERIA -- the one part of that rule that's mechanically checkable
# (a count), rather than a judgement call. Observed live: stating this rule in
# the prompt (even with a worked example) was not enough on its own -- the
# model still elevated single-citation dimensions to Strength/Focus Required.
# So this is a deterministic backstop, not just a hope: it runs on every
# narrative result before anything downstream (validation, PDF) ever sees it,
# which is what actually guarantees "never assert Strength or Blind Spot on
# weak evidence" rather than relying on the model or a retry to get it right.
MIN_EVIDENCE_FOR_STRONG_TIER = 2
_THIN_EVIDENCE_NOTE = " (Evidence is limited -- based on a single response.)"


def enforce_thin_evidence_rule(report_json):
    """
    Downgrades any dimension asserting Strength / Focus Required / Blind Spot
    from fewer than MIN_EVIDENCE_FOR_STRONG_TIER evidence_refs to Developing,
    appending a short note that the evidence is limited. Mutates and returns
    report_json.

    Only touches "dimensions" -- strong/focus/blindspot cards aren't tied to a
    specific dimension by id in this schema, so a downgrade here doesn't
    cascade to them; the content validator still checks those cards on their
    own claims (see app.py's CONTENT_VALIDATION_RUBRIC).
    """
    for dim in report_json.get("dimensions") or []:
        if not isinstance(dim, dict):
            continue
        tier = dim.get("tier")
        refs = dim.get("evidence_refs")
        thin = not isinstance(refs, list) or len(refs) < MIN_EVIDENCE_FOR_STRONG_TIER
        if thin and tier in ("Strength", "Focus Required", "Blind Spot"):
            dim["tier"] = "Developing"
            description = dim.get("description") or ""
            if _THIN_EVIDENCE_NOTE.strip() not in description:
                dim["description"] = (description.rstrip() + _THIN_EVIDENCE_NOTE).strip()
    return report_json


# Worked examples of the gap between a claim that would fail validation and one
# that would pass -- deliberately generic (illustrative question ids, not this
# survey's real questions) so this stays correct if the question bank changes.
# A rule ("every claim must be traceable") is abstract; a model follows a
# contrast pair more reliably than a rule stated once in prose.
_CLAIM_EXAMPLES = """
EXAMPLES -- bad claim vs good claim:

  1. BAD:  description: "Shows strong leadership and has led multiple teams."
           evidence_refs: []
           Wrong because: nothing was cited, and "led multiple teams" is asserted,
           not something the answers say.
     GOOD: description: "When faced with an unfamiliar problem (Q34), they said
           they break it into smaller sub-tasks before starting -- a specific,
           repeated approach, not just a stated preference."
           evidence_refs: ["Q34"]
           Right because: one exact answer is cited, and the description says only
           what that answer shows, not more.

  2. BAD:  name: "Growth Mindset", description: "Open to learning and improving."
           Wrong because: generic enough to paste into any student's report
           unchanged -- it isn't tied to anything this student specifically said.
     GOOD: name: "Learning Under Deadline Pressure", description: "Answered that
           they skip documentation entirely for tools they only need once (Q22),
           while spending over 10 hours weekly on self-directed learning
           otherwise (Q21) -- learning effort is real but selectively applied."
           evidence_refs: ["Q21", "Q22"]
           Right because: it names a pattern specific to two of this student's own
           answers, not a trait that could belong to anyone.

  3. BAD:  single_priority.body: "Keep working hard and stay motivated -- you
           will succeed if you believe in yourself."
           Wrong because: generic encouragement, true of any student, names no
           concrete action.
     GOOD: single_priority.body: "Their answers show they can break a problem
           down (Q34) but consistently skip documenting the result (Q22) --
           spend one focused session writing up their next solved problem
           before moving to the next one."
           Right because: names one specific action tied to a specific,
           evidenced gap.

  4. BAD:  name: "Documentation Habits", description: "Rarely documents work."
           evidence_refs: ["Q28"], tier: "Focus Required"
           Wrong because: only ONE answer is cited. TIER CRITERIA requires two
           or more consistent answers for Focus Required (or Strength) -- one
           answer, however clear, is thin evidence, not a pattern.
     GOOD: name: "Documentation Habits", description: "Evidence on this is
           limited -- one answer (Q28) suggests work often goes undocumented,
           but there isn't a second answer to confirm it's a consistent
           pattern rather than a one-off."
           evidence_refs: ["Q28"], tier: "Developing"
           Right because: with only one citation, the tier defaults to
           Developing and says so explicitly, instead of asserting a stronger
           pattern the evidence doesn't yet establish.
""".strip()

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
      "tier": "<exactly one of: Strength | Developing | Focus Required | Blind Spot>",
      "evidence_refs": ["<the exact question id(s) from the QUESTION BANK this
                          dimension's description and tier are based on, e.g.
                          'Q8', 'Q19', 'U3' -- every id listed must be one this
                          student actually has an answer for below, never invented>"]
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
  * "evidence_refs" must list only real question ids this student actually
    answered (from the ids in the QUESTION BANK below) -- never an invented id,
    never left empty. Write the description and tier FROM these answers, not
    the other way around: pick the evidence first, then describe only what it
    shows.
  * "action" fields must be doable in a week, not a career plan.
  * Never emit an empty string, an empty array, a placeholder, or a field left as
    the description above.
""".strip()

# A real JSON Schema for the same shape _SCHEMA_REPORT describes in prose, passed
# to Ollama's `format` field (core/ollama_client.py) instead of the generic "json"
# string. Ollama grammar-samples generation against this, so a missing required
# field, a wrong type, or a tier outside the four labels becomes something the
# model literally cannot emit, rather than something caught after the fact by
# app.py's validate_structure(). That is the direct lever on first-attempt
# structural pass rate -- see refinedversion.md's "schema-constrained decoding".
#
# Deliberately narrow to the widely-supported JSON Schema subset (type,
# properties, required, items, enum, additionalProperties) and skips length
# constraints (minLength/minItems) -- older Ollama/llama.cpp builds don't
# reliably grammar-sample those, and getting them wrong risks the whole call
# failing outright. validate_structure() still enforces minimum lengths and
# non-empty text as a Python-side safety net; this schema's job is only to make
# the shape/type/enum failures structurally impossible, not to replace that check.
_CARD_ITEM_SCHEMA = {
    "type": "object",
    "properties": {
        "headline": {"type": "string"},
        "body": {"type": "string"},
        "action": {"type": "string"},
    },
    "required": ["headline", "body"],
    "additionalProperties": False,
}
_FOCUS_BLINDSPOT_ITEM_SCHEMA = {
    **_CARD_ITEM_SCHEMA,
    "required": ["headline", "body", "action"],
}
REPORT_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "intro_message": {"type": "string"},
        "dimensions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "description": {"type": "string"},
                    "tier": {"type": "string", "enum": list(TIERS)},
                    "evidence_refs": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["name", "description", "tier", "evidence_refs"],
                "additionalProperties": False,
            },
        },
        "strong": {"type": "array", "items": _CARD_ITEM_SCHEMA},
        "focus": {"type": "array", "items": _FOCUS_BLINDSPOT_ITEM_SCHEMA},
        "blindspot": {"type": "array", "items": _FOCUS_BLINDSPOT_ITEM_SCHEMA},
        "single_priority": {
            "type": "object",
            "properties": {
                "headline": {"type": "string"},
                "body": {"type": "string"},
            },
            "required": ["headline", "body"],
            "additionalProperties": False,
        },
    },
    # blindspot excluded on purpose -- the prompt says to include one only when
    # genuinely evidenced, so its absence is a legitimate, deliberate choice, not
    # a structural failure. strong/focus have no such carve-out, and omitting
    # them entirely was observed live (no schema constraint stopped it, since
    # they weren't required here before): app.py's STUDY_REQUIRED_FIELDS agrees
    # with this list so the same shape is enforced both at decode time and by
    # validate_structure() afterward.
    "required": ["intro_message", "dimensions", "single_priority", "strong", "focus"],
    "additionalProperties": False,
}


_SCHEMA_INSTRUCTION_NOTE = """
The report requester's instruction ABOVE steers the CONTENT of these fields -- what
to emphasise, which dimensions to pick, the tone and depth of the writing. It does
not change the SHAPE: the template renders these fields and no others, so produce
every field above regardless, with the instruction applied to what goes in them.
""".strip()


def _schema_description(instructions_text):
    parts = [_SCHEMA_REPORT, "", _tier_criteria_block(), "", _CLAIM_EXAMPLES]
    if instructions_text:
        parts += ["", _SCHEMA_INSTRUCTION_NOTE]
    return "\n".join(parts)

TONE_RULES = (
    "Always write in encouraging, non-comparative language. Never describe the student "
    "in a way that could feel like a judgment of their worth -- only in terms of where "
    "they are now and what would help them grow. Never compare this student to peers."
)


def _instruction_schema_and_output_tail(instructions_text):
    """
    Shared by _build_system_message (single-step, kept for the CLI study/older
    callers) and build_narrative_messages (the live two-step path) -- one
    source for the instruction-placement logic instead of two prompts that
    could drift apart on exactly this point.

    Order matters here, and not just for readability. The requester's
    instruction is repeated at the very END on purpose -- a model weights the
    tail of its prompt most heavily, and burying the instruction mid-message
    is the difference between it being followed and quietly ignored (observed
    twice on this project).
    """
    schema_description = _schema_description(instructions_text)
    parts = [
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
    return parts


def _build_system_message(mapping, instructions_text, question_bank_text=""):
    """
    Single-step system message: raw answers straight to a full report in one
    call. Kept for the CLI study tool's --mode comparisons; the live app now
    generates through build_evidence_extraction_messages +
    build_narrative_messages instead (see those for why: this single call lets
    the model assert anything, cited or not, while the two-step path
    structurally cannot).

    All of this is still byte-identical across students in a batch -- the
    instruction is batch-level too -- so keeping shared material (the question
    bank) ahead of the reminder costs nothing in prefix-cache reuse.
    """
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
    parts += _instruction_schema_and_output_tail(instructions_text)
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


def valid_answer_ids(student_record, question_bank=None):
    """
    The exact set of question ids this student has an answer for -- the same key
    space _build_student_payload sends the model under "answers". Used to check
    a generated dimension's evidence_refs actually cite something this student
    was shown, rather than a fabricated or misattributed id (see REPORT_JSON_SCHEMA).

    question_bank is needed for unmapped ids ("U1", "U2", ...) since those are
    assigned batch-wide, in first-seen order across students, not derivable from
    one student's record alone -- omitting it checks mapped answers only.
    """
    ids = {q["qid"] for questions in student_record["sections"].values() for q in questions}
    if question_bank:
        ids |= set(question_bank.get("unmapped_ids", {}).values())
    return ids


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


# --- two-step generation: extract evidence, then write from only that -------
#
# Report generation used to be the single call above: raw answers straight to
# a full report, which lets the model assert anything whether or not an
# answer actually backs it up -- catching that was entirely the content
# validator's job, after the fact. Splitting into two calls makes it
# structural instead: step 1 (below) does nothing but restate which answers
# are relevant to which dimension, with no interpretation; step 2
# (build_narrative_messages) never sees the raw answers at all, only step 1's
# extraction, so it cannot cite or assert anything beyond what was already
# pulled out.

EVIDENCE_EXTRACTION_SCHEMA = {
    "type": "object",
    "properties": {
        "dimensions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "evidence": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "qid": {"type": "string"},
                                "note": {"type": "string"},
                            },
                            "required": ["qid", "note"],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": ["name", "evidence"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["dimensions"],
    "additionalProperties": False,
}

_EXTRACTION_SCHEMA_PROSE = """
Output EXACTLY this JSON shape:

{
  "dimensions": [
    {
      "name": "<short dimension name, 2-4 words, e.g. 'Learning Consistency'>",
      "evidence": [
        {"qid": "<exact question id from the QUESTION BANK, e.g. 'Q8' or 'U3'>",
         "note": "<a literal, neutral restatement of what THIS answer says --
                   no adjectives, no judgment words like 'strong'/'good'/
                   'consistent'/'weak', just what was said, e.g. 'rated 2 out
                   of 5' or 'said they use AI to generate code and check the
                   output'>"}
        // every answer relevant to this dimension -- one entry per qid. Cite
        // as many or as few as the answers actually support: do not pad, and
        // do not omit an answer that is genuinely relevant.
      ]
    }
    // propose 6-9 candidate dimensions covering the range of the question
    // bank -- narrative writing (a separate step) picks the final 5-7 and
    // may drop a dimension with too little evidence.
  ]
}

Rules that matter:
  * This is extraction only -- no interpretation, no tier, no opinion, no
    quality judgment. "note" restates what the answer says, nothing more.
  * "qid" must be a real id from the QUESTION BANK that this student actually
    has an answer for below. Never invent one.
  * If a dimension has only one relevant answer, cite only that one -- do not
    invent a second just to reach a count.
  * Skip a candidate dimension entirely if you cannot find real evidence for
    it in this student's answers, rather than forcing one in.
""".strip()


def build_evidence_extraction_messages(student_record, mapping, question_bank=None):
    """
    Step 1 of 2 (see build_narrative_messages for step 2). Asks only for a
    mechanical extraction -- which answers are relevant to which candidate
    dimension, restated literally, with no interpretation or tier -- because
    step 2 never sees the raw answers, only this.
    """
    if question_bank is None:
        question_bank = build_question_bank(mapping, [student_record])
    parts = [
        "You are extracting evidence from one student's survey answers. This is "
        "step 1 of 2 -- a separate step writes the report afterward using only "
        "what you extract here, so anything you don't cite here cannot be used "
        "in the report at all.",
        "The next message (role: user) contains this one student's answers, keyed "
        "by the question ids in the QUESTION BANK below. A question id missing "
        "from it means this student left that question blank.",
        "",
        question_bank["text"],
        "",
        _EXTRACTION_SCHEMA_PROSE,
        "",
        "Output ONLY valid JSON. No markdown fences, no commentary.",
    ]
    system_content = "\n".join(parts)
    return [
        {"role": "system", "content": system_content},
        {"role": "user", "content": _build_student_payload(student_record, mapping, question_bank)},
    ]


def build_narrative_messages(evidence, student_record, instructions_text, retry_feedback=None):
    """
    Step 2 of 2 (see build_evidence_extraction_messages for step 1). The user
    message below carries ONLY the extraction -- never this student's raw
    answers -- so nothing beyond what step 1 already cited is available to
    draw from here; a claim step 1 didn't extract structurally cannot appear.

    Reuses _instruction_schema_and_output_tail so the instruction-placement
    logic (and the schema/tier-criteria/examples block) is the exact same text
    _build_system_message uses, not a separately maintained copy of it.

    retry_feedback, when given ({"previous_output": str, "reasons": [...]}),
    appends the previous attempt and exactly why it failed -- full
    regeneration, same reasoning as _study_retry_messages: asking the model to
    patch one field invites an edit that no longer agrees with the rest of the
    report, so it's asked to produce the whole thing again, corrected, from
    the same EVIDENCE (extraction is regenerated separately, fresh, by the
    caller -- this is step 2's half of that "never a patch" regeneration).
    """
    identity = student_record["identity"]
    parts = [
        "You are writing a personalized career-readiness report for one student, "
        "using ONLY the EVIDENCE given in the next message -- already extracted "
        "from their answers by a separate step, organized per candidate "
        "dimension. Do not use any claim, fact, or detail that is not in one of "
        "these evidence notes; if EVIDENCE doesn't support something, don't say "
        "it. You may drop a candidate dimension if its evidence is too thin to "
        "write about honestly, but never add a dimension EVIDENCE doesn't cover.",
        TONE_RULES,
    ]
    parts += _instruction_schema_and_output_tail(instructions_text)
    system_content = "\n".join(parts)

    user_payload = {
        "student": {
            "name": identity.get("name", ""),
            "branch": identity.get("branch", ""),
            "year": identity.get("year", ""),
            "institution": identity.get("institution", ""),
        },
        "EVIDENCE": evidence.get("dimensions", []),
    }
    messages = [
        {"role": "system", "content": system_content},
        {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False,
                                                separators=(",", ":"))},
    ]
    if retry_feedback:
        reasons = retry_feedback.get("reasons") or []
        reason_text = "; ".join(reasons) if reasons else "unspecified validation failure"
        messages += [
            {"role": "assistant", "content": retry_feedback.get("previous_output") or "(no output produced)"},
            {"role": "user", "content":
                f"That output failed validation for this reason: {reason_text}\n\n"
                "Regenerate the COMPLETE report from scratch using ONLY the same "
                "EVIDENCE given above. Do not patch or edit the previous output -- "
                "produce the whole report again, corrected. Output ONLY valid JSON."},
        ]
    return messages


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
