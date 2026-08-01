def compute_section_hints(sections):
    """
    Computes a preliminary 0-100 score per section from two kinds of single-choice
    answers:
      - maturity-ordered questions: position on the low->high scale (options_low_to_high)
      - scenario/best-answer questions: % of answers matching their correct_answer
    Multi-select answers aren't scored here -- project_requirement.md section 10 says
    the AI judges the quality of a multi-select answer as part of the section score,
    so those are passed to the AI as raw text instead (see prompt_builder.py) rather
    than reduced to a number.

    Returns {section_key: float 0-100 | None}. None means there was nothing to compute
    a hint from -- the AI must judge that section entirely from raw context.
    """
    hints = {}
    for section_key, questions in sections.items():
        positions = []
        correctness = []
        for q in questions:
            if q["multi_select"]:
                continue
            if q.get("is_correct") is not None:
                correctness.append(100 if q["is_correct"] else 0)
                continue
            if q["option_index"] is None or q["options_total"] <= 1:
                continue
            positions.append(q["option_index"] / (q["options_total"] - 1) * 100)

        if positions:
            hints[section_key] = round(sum(positions) / len(positions), 1)
        elif correctness:
            hints[section_key] = round(sum(correctness) / len(correctness), 1)
        else:
            hints[section_key] = None
    return hints
