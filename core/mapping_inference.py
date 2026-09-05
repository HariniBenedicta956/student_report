"""
Auto-generates a section_mapping.json-shaped mapping from whatever question
bank and CSV are uploaded in one run -- no reference to any pre-authored
config file, no assumption about section names, question count, column
layout, or "Qn"-style header prefixes. A differently-structured form (more or
fewer sections, a different question count, a mix of single/multi-select)
produces a differently-structured mapping without any code change here.

Two things are deliberately NOT inferred, on purpose, not by omission:
  * options_low_to_high (the low->high maturity ordering for a self-report
    question) requires actually understanding what each option means, which
    text-similarity heuristics can't do reliably. Left empty -- exactly the
    same "AI judges this question live" fallback core/scoring.py and
    core/prompt_builder.py already treat as first-class, not broken.
  * correct_answer (for scenario/best-practice questions with one right
    answer) is a judgment call about the DOMAIN, not something inferable from
    the CSV/question-bank text at all. Left unset -- those questions are
    AI-judged too, same mechanism.

Everything this module produces is shaped to be a drop-in for
csv_ingest.parse_csv -- no changes needed there beyond the optional "qid"
field csv_ingest already supports.
"""
import csv
import difflib
import io
import re

# Below this fuzzy-match score, a column is not confidently mapped to any
# question-bank question -- it goes to `review`, never a forced, wrong
# mapping and never a silent 0% (see infer_mapping).
MIN_MATCH_CONFIDENCE = 0.55

_SECTION_LABELLED_RE = re.compile(
    r'^(?:#{1,3}\s*)?Section\s+([A-Za-z0-9]+)\s*[—\-:]\s*(.+)$', re.IGNORECASE)
_MD_HEADING_RE = re.compile(r'^#{2,3}\s+(.+)$')
_QUESTION_RE = re.compile(r'^\s*(\d+)[\.\)]\s+(.+)$')
_TYPE_RE = re.compile(r'^Type:\s*(.+)$', re.IGNORECASE)
_MULTI_HINT_RE = re.compile(
    r'select all|select up to|choose all|check all|multiple selection', re.IGNORECASE)
_GRID_HINT_RE = re.compile(r'multiple[- ]choice grid|matrix', re.IGNORECASE)
_ROWS_MARKER_RE = re.compile(r'^Rows:\s*$', re.IGNORECASE)
_COLUMNS_MARKER_RE = re.compile(r'^Columns:\s*$', re.IGNORECASE)
_DELIM_RE = re.compile(r'\s*[;|]\s*|\s*,\s*(?=[A-Z])')

_IDENTITY_KEYWORDS = {
    "name": ("student name", "full name", "name"),
    "roll_number": ("roll", "register", "enrollment", "enrolment", "student id", "id number"),
    "institution": ("college", "institution", "school name", "university"),
    # "program"/"programme" deliberately excluded -- it's also a substring
    # false-positive magnet (e.g. "programming language" questions), and
    # "degree/programme" columns are closer to identity than a real signal
    # of branch anyway; leaving it out means those go to review instead of
    # a wrong guess, which is the correct fallback (see infer_mapping).
    "branch": ("branch", "department", "specialisation", "specialization", "major"),
    "year": ("year of study", "current year", "year"),
    "email": ("email",),
}


class MappingInferenceError(RuntimeError):
    pass


def _normalize(text):
    return re.sub(r'[^a-z0-9 ]', ' ', (text or "").lower()).strip()


def _fuzzy_score(a, b):
    return difflib.SequenceMatcher(None, _normalize(a), _normalize(b)).ratio()


def _token_overlap_score(a, b):
    """
    Word-set (Jaccard) similarity -- used for identity-phrase matching
    instead of _fuzzy_score's character-sequence ratio, which conflates
    short phrases sharing one common word (e.g. "Full name" scored 0.60-0.67
    against "roll number"/"college name"/"school name", all just because
    they share the substring "name", not because they mean the same thing).
    Token overlap needs most/all of the actual WORDS to match instead.
    """
    ta, tb = set(_normalize(a).split()), set(_normalize(b).split())
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def parse_question_bank(text):
    """
    Extracts sections and questions from free-form question-bank text using
    only generic structural signals: numbered questions ("12. Which..."),
    "Section X — Label" or markdown "## Label" headings, a "Type:" line
    (select-all/select-up-to phrasing -> multi_select, "multiple-choice
    grid"/"matrix" -> expand into one sub-question per grid row). Anything
    else (option lines, notes, instructions) is intentionally ignored -- the
    mapping only needs the question's text and its multi/single-select
    nature, never its exact option text (see module docstring).

    Returns [{"key": ..., "label": ..., "questions": [{"number", "qid",
    "text", "multi_select"}, ...]}, ...], in document order.
    """
    sections = []
    current_section = None
    current_question = None
    mode = None
    section_counter = 0

    def start_section(label, key=None):
        nonlocal current_section, section_counter
        section_counter += 1
        current_section = {"key": key or str(section_counter), "label": label.strip(),
                            "questions": []}
        sections.append(current_section)

    def flush_question():
        nonlocal current_question
        if current_question is not None and current_section is not None:
            current_section["questions"].append(current_question)
        current_question = None

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        m = _SECTION_LABELLED_RE.match(line)
        if m:
            flush_question()
            start_section(m.group(2), key=m.group(1))
            mode = None
            continue

        m = _MD_HEADING_RE.match(line)
        if m and not _QUESTION_RE.match(line):
            flush_question()
            start_section(m.group(1))
            mode = None
            continue

        m = _QUESTION_RE.match(line)
        if m:
            flush_question()
            if current_section is None:
                start_section("General")
            current_question = {"number": int(m.group(1)), "text": m.group(2).strip(),
                                 "multi_select": False, "rows": None, "columns": None}
            mode = None
            continue

        if current_question is None:
            continue  # preamble / instructions before the first question

        m = _TYPE_RE.match(line)
        if m:
            type_text = m.group(1)
            current_question["multi_select"] = bool(_MULTI_HINT_RE.search(type_text))
            if _GRID_HINT_RE.search(type_text):
                current_question["rows"] = []
                current_question["columns"] = []
            continue

        if current_question.get("rows") is not None:
            if _ROWS_MARKER_RE.match(line):
                mode = "rows"
                continue
            if _COLUMNS_MARKER_RE.match(line):
                mode = "columns"
                continue
            if mode == "rows":
                current_question["rows"].append(line)
                continue
            if mode == "columns":
                current_question["columns"].append(line)
                continue
        # Any other line (an option, a note, instructions) carries nothing
        # this mapper needs -- see module docstring on what's deliberately
        # not inferred.
    flush_question()

    expanded = []
    for section in sections:
        questions = []
        for q in section["questions"]:
            if q.get("rows") is not None:
                for ridx, row_text in enumerate(q["rows"] or []):
                    suffix = chr(ord('a') + ridx) if ridx < 26 else str(ridx)
                    questions.append({"number": q["number"], "qid": f"Q{q['number']}{suffix}",
                                       "text": row_text, "multi_select": False})
            else:
                questions.append({"number": q["number"], "qid": f"Q{q['number']}",
                                   "text": q["text"], "multi_select": q["multi_select"]})
        if questions:
            expanded.append({"key": section["key"], "label": section["label"], "questions": questions})
    return expanded


def _guess_identity_key(column):
    norm = f" {_normalize(column)} "
    for key, keywords in _IDENTITY_KEYWORDS.items():
        # Word-boundary containment, not raw substring -- "program" inside
        # "programming" is a real false positive this caught (see
        # infer_mapping's docstring on why "program"/"programme" is excluded
        # entirely rather than just boundary-matched).
        if any(f" {kw} " in norm for kw in keywords):
            return key
    return None


def match_columns_to_questions(fieldnames, all_questions):
    """
    Matches each CSV column header to the question-bank question it most
    plausibly answers, by text similarity alone -- works whether or not
    headers happen to be "Qn"-prefixed (a leading number matching the
    question's own number is used as one extra signal, not a requirement).

    Greedy best-first over every (column, question) pair, so two similar
    columns can't both claim the same question.

    Returns (matches, unmatched): matches is {column: (question, score)} for
    everything clearing MIN_MATCH_CONFIDENCE; unmatched is
    [{"column", "best_score", "reason"}] for the rest.
    """
    candidates = []
    for col in fieldnames:
        for qi, q in enumerate(all_questions):
            score = _fuzzy_score(col, q["text"])
            if re.match(rf'^\s*Q?{q["number"]}\b', col, re.IGNORECASE):
                score = max(score, 0.85)
            candidates.append((score, col, qi))
    candidates.sort(key=lambda t: -t[0])

    matches = {}
    assigned_cols = set()
    used_questions = set()
    for score, col, qi in candidates:
        if score < MIN_MATCH_CONFIDENCE or col in assigned_cols or qi in used_questions:
            continue
        matches[col] = (all_questions[qi], score)
        assigned_cols.add(col)
        used_questions.add(qi)

    unmatched = []
    for col in fieldnames:
        if col in assigned_cols:
            continue
        best = max((_fuzzy_score(col, q["text"]) for q in all_questions), default=0.0)
        unmatched.append({"column": col, "best_score": round(best, 2),
                           "reason": "no question-bank text matched confidently enough"})
    return matches, unmatched


def looks_multi_select(sample_values):
    """
    Real-data signal, independent of what the question bank's "Type:" line
    claims: do this column's own non-empty answers often contain a
    delimiter pattern consistent with several selections concatenated
    together? Used to cross-check (see infer_mapping), not replace, the
    question-bank-derived signal.
    """
    non_empty = [v for v in sample_values if v and v.strip()]
    if not non_empty:
        return False
    delimited = sum(1 for v in non_empty if _DELIM_RE.search(v))
    return (delimited / len(non_empty)) >= 0.3


_IDENTITY_PHRASES = {
    "name": ("full name", "student name", "name of student", "what is your name", "your name"),
    "roll_number": ("college roll number", "roll number", "register number", "student id"),
    "institution": ("college name", "institution name", "school name"),
    "branch": ("branch specialisation", "branch specialization", "department"),
    "year": ("current year of study", "year of study"),
    "email": ("email address",),
}
IDENTITY_FROM_QUESTION_MIN_SCORE = 0.5


def _find_identity_among_questions(mapping_sections):
    """
    A survey's own "Section A" often numbers identity fields (name, roll
    number, ...) as regular questions rather than a separate identity block
    -- they're kept as normal questions (so they still count toward
    completion%, matching how the source document itself treats them), but
    also registered here so personalization (the student's name in the
    report/PDF) still works without a dedicated identity section existing.
    """
    found = {}
    for sec in mapping_sections.values():
        for q in sec["questions"]:
            for key, phrases in _IDENTITY_PHRASES.items():
                if key in found:
                    continue
                best = max((_token_overlap_score(q["full_question"], p) for p in phrases), default=0.0)
                if best >= IDENTITY_FROM_QUESTION_MIN_SCORE:
                    found[key] = q["column"]
    return found


def infer_mapping(csv_bytes, question_bank_text):
    """
    Builds a section_mapping.json-shaped dict from this run's own CSV +
    question bank. Returns (mapping, review):
      mapping -- ready for csv_ingest.parse_csv unchanged.
      review  -- every CSV column that couldn't be confidently placed
                 (identity or question), each with why, for an admin to
                 assign by hand rather than have it silently scored 0% or
                 the whole upload rejected.
    """
    text = csv_bytes.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))
    fieldnames = reader.fieldnames or []
    rows = list(reader)

    sections = parse_question_bank(question_bank_text)
    all_questions = [
        {**q, "section_key": s["key"], "section_label": s["label"]}
        for s in sections for q in s["questions"]
    ]
    if not all_questions:
        raise MappingInferenceError(
            "Could not find any numbered questions in the question bank text -- "
            "expected lines like '12. Which statement...'. Check the uploaded "
            "file is the actual question list, not a summary or instructions page."
        )

    matches, unmatched = match_columns_to_questions(fieldnames, all_questions)
    unmatched_by_col = {u["column"]: u for u in unmatched}

    identity_columns = {}
    mapping_sections = {}
    review = []

    for col in fieldnames:
        if col not in matches:
            key = _guess_identity_key(col)
            if key and key not in identity_columns:
                identity_columns[key] = col
            else:
                review.append(unmatched_by_col[col])
            continue

        q, score = matches[col]
        sample_values = [row.get(col, "") for row in rows[:50]]
        multi = q["multi_select"] or looks_multi_select(sample_values)
        sec = mapping_sections.setdefault(
            q["section_key"], {"label": q["section_label"], "questions": []})
        sec["questions"].append({
            "column": col,
            "qid": q["qid"],
            "multi_select": multi,
            "options_low_to_high": [],
            "full_question": q["text"],
        })
        # Low-confidence matches still get used (better than discarding a
        # real answer) but are surfaced too, so an admin can sanity-check
        # rather than trust a borderline text match blindly.
        if score < 0.75:
            review.append({"column": col, "best_score": round(score, 2),
                            "reason": f"mapped to '{q['text'][:80]}' but the text match is "
                                      f"only {round(score * 100)}% confident -- worth checking"})

    for key, col in _find_identity_among_questions(mapping_sections).items():
        identity_columns.setdefault(key, col)

    mapping = {
        "_source": "auto-generated from this upload's question bank + CSV -- no static config used",
        "identity_columns": identity_columns,
        "sections": mapping_sections,
        "ignored_columns": [],
    }
    return mapping, review
