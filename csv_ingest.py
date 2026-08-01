import csv
import io
import json
import os
import re

import config

QNUM_PATTERN = re.compile(r"^(Q\d+)\b")


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

    students = []
    for row in reader:
        identity = {
            key: (row.get(col, "") or "").strip() if col else ""
            for key, col in resolved_identity.items()
        }

        sections = {key: [] for key in mapping["sections"]}
        unmapped = []

        for column, answer in row.items():
            if column in identity_real_columns or column in ignored_real_columns:
                continue
            answer = (answer or "").strip()
            if not answer:
                continue

            if column in mapped_cols:
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
                    "qid": q_config["column"],
                    "question": question_text,
                    "answer": answer,
                    "multi_select": is_multi,
                    "option_index": option_index,
                    "options_total": len(options),
                    "is_correct": is_correct,
                })
            else:
                unmapped.append({"question": column, "answer": answer})

        students.append({
            "identity": identity,
            "sections": sections,
            "unmapped": unmapped,
        })

    return students
