import csv
import io
import json
import os
import re

import config

QNUM_PATTERN = re.compile(r"^(Q\d+)\b")

# Hyphen placeholders some exports write for a skipped question instead of
# leaving the cell truly empty -- treated as unanswered for completion %.
# Deliberately narrow: "n/a"/"none"/"nil" are left OUT because a real option
# can legitimately read exactly that (e.g. a multi-select's "None of the
# above"), which a hyphen can never be.
NOT_ANSWERED_PLACEHOLDERS = {"-", "--", "—", "–"}


class SectionMappingMissingError(RuntimeError):
    pass


def load_section_mapping():
    if not os.path.exists(config.SECTION_MAPPING_PATH):
        raise SectionMappingMissingError(
            "section_mapping.json not found. Copy section_mapping.example.json to "
            "section_mapping.json and fill it in with the real CSV column headers "
            "and answer options before uploading a file."
        )
    with open(config.SECTION_MAPPING_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def _qnum_prefix(column_name):
    """
    Extracts a leading 'Qn' token from a column header, e.g. 'Q8: What are...' -> 'Q8',
    'Q48 · Good marks...' -> 'Q48'. Returns None if the header has no such prefix.
    """
    match = QNUM_PATTERN.match(column_name.strip())
    return match.group(1) if match else None


def _build_qnum_lookup(fieldnames):
    """Maps 'Q8' -> the actual CSV header text that starts with it (first match wins)."""
    lookup = {}
    for col in fieldnames:
        qnum = _qnum_prefix(col)
        if qnum and qnum not in lookup:
            lookup[qnum] = col
    return lookup


def _resolve(identifier, fieldnames, qnum_lookup):
    """
    Resolves a configured identifier (either an exact header like 'Name', or a stable
    'Qn' prefix like 'Q8') to the actual column name present in this CSV. Matching by
    Qn prefix -- rather than the full question text -- makes the mapping robust to
    exports where headers get truncated or re-encoded, as long as the Qn numbering
    is stable within one export.
    """
    if identifier in fieldnames:
        return identifier
    return qnum_lookup.get(identifier)


def _resolve_identity_columns(identity_cols, fieldnames, qnum_lookup):
    return {key: _resolve(col, fieldnames, qnum_lookup) for key, col in identity_cols.items()}


def _resolve_mapped_columns(mapping, fieldnames, qnum_lookup):
    """Returns {actual_column_name: (section_key, question_config)} for every mapped question."""
    lookup = {}
    for section_key, section in mapping["sections"].items():
        for question in section["questions"]:
            actual_col = _resolve(question["column"], fieldnames, qnum_lookup)
            if actual_col:
                lookup[actual_col] = (section_key, question)
    return lookup


def parse_csv(file_bytes, mapping):
    """
    Parses uploaded CSV bytes into a list of per-student records:
    {
      "identity": {"name": ..., "roll_number": ..., "institution": ..., "branch": ..., "year": ...},
      "sections": {
        "B": [{"question": ..., "answer": ..., "multi_select": bool,
                "option_index": int | None, "options_total": int}, ...],
        ...
      },
      "unmapped": [{"question": ..., "answer": ...}, ...]
    }
    """
    text = file_bytes.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))
    fieldnames = reader.fieldnames or []
    qnum_lookup = _build_qnum_lookup(fieldnames)

    identity_cols = mapping["identity_columns"]
    resolved_identity = _resolve_identity_columns(identity_cols, fieldnames, qnum_lookup)
    identity_real_columns = {col for col in resolved_identity.values() if col}
    mapped_cols = _resolve_mapped_columns(mapping, fieldnames, qnum_lookup)
    ignored_real_columns = {
        _resolve(col, fieldnames, qnum_lookup)
        for col in mapping.get("ignored_columns", [])
    }
    ignored_real_columns.discard(None)

    # Denominator for completion_pct below: how many mapped questions this CSV
    # actually has columns for -- not the mapping's full question count, which
    # would be wrong if a different export drops or renames a column. Skipped
    # rather than raised on (parse_csv is used against test/partial exports too).
    total_mapped = len(mapped_cols)

    students = []
    for row in reader:
        identity = {
            key: (row.get(col, "") or "").strip() if col else ""
            for key, col in resolved_identity.items()
        }

        sections = {key: [] for key in mapping["sections"]}
        unmapped = []
        answered_mapped_columns = set()

        for column, answer in row.items():
            if column in ignored_real_columns:
                continue
            # A column can be BOTH an identity column and a real mapped
            # question at once -- an auto-generated mapping (see
            # core/mapping_inference.py) deliberately does this for personal-
            # ization when a form has no separate identity section (e.g. "1.
            # What is your name?" is both this student's identity.name AND a
            # completion-counted question). Only skip it here when it is
            # identity-ONLY: still checking it for completion below is what
            # makes that dual registration not silently mark a real answer
            # as blank.
            if column in identity_real_columns and column not in mapped_cols:
                continue
            answer = (answer or "").strip()
            # A blank cell isn't always literally empty -- some exports write
            # a placeholder (a lone "-", "--", "n/a") for a skipped question
            # instead of leaving it empty. Treated the same as empty for
            # completion purposes; NOT_ANSWERED_PLACEHOLDERS is intentionally
            # generic text, not specific to any one export tool's convention.
            if not answer or answer.lower() in NOT_ANSWERED_PLACEHOLDERS:
                continue

            if column in mapped_cols:
                answered_mapped_columns.add(column)
                section_key, q_config = mapped_cols[column]
                is_multi = q_config.get("multi_select", False)
                options = q_config.get("options_low_to_high", [])
                option_index = None
                if not is_multi and answer in options:
                    option_index = options.index(answer)
                correct_answer = q_config.get("correct_answer")
                is_correct = (answer == correct_answer) if correct_answer is not None else None
                # The CSV export truncates long headers with "..." -- use the full,
                # untruncated question text from the mapping (sourced from the actual
                # question bank) when available, so the AI isn't working from a cut-off
                # question when asked to list or reference specific questions.
                question_text = q_config.get("full_question") or column
                sections[section_key].append({
                    # The mapping's own stable identifier for this question (e.g.
                    # "Q31"). Prompt-building keys each student's answers by this
                    # rather than by position in the list: blank answers are skipped
                    # above, so two students can end up with different-length section
                    # lists, and positional numbering would silently pair a student's
                    # answer with the wrong question.
                    #
                    # A separate "qid" is optional in the mapping -- the static,
                    # hand-authored section_mapping.json doesn't set one, since its
                    # "column" values (e.g. "Q8") already double as short ids. An
                    # auto-generated mapping's "column" is the real CSV header
                    # instead (needed to resolve it), which can be long and messy,
                    # so it sets "qid" separately to a short id like "Q12" or "Q51a".
                    "qid": q_config.get("qid") or q_config["column"],
                    "question": question_text,
                    "answer": answer,
                    "multi_select": is_multi,
                    "option_index": option_index,
                    "options_total": len(options),
                    "is_correct": is_correct,
                })
            else:
                unmapped.append({"question": column, "answer": answer})

        answered = sum(len(questions) for questions in sections.values())
        completion_pct = round(answered / total_mapped * 100, 1) if total_mapped else 0.0

        # Exactly which mapped questions this student left null/empty/whitespace-
        # only -- for showing an ineligible student's real gaps (Sync Eligibility),
        # not just their percentage. mapped_cols already excludes identity/ignored
        # columns, so this is precisely "answered questions ÷ total mapped
        # questions" from the other side.
        unanswered_questions = [
            {"qid": q_config.get("qid") or q_config["column"],
             "question": q_config.get("full_question") or col}
            for col, (_section_key, q_config) in mapped_cols.items()
            if col not in answered_mapped_columns
        ]

        students.append({
            "identity": identity,
            "sections": sections,
            "unmapped": unmapped,
            "completion_pct": completion_pct,
            "unanswered_questions": unanswered_questions,
        })

    return students
