# Career Readiness Report Generator

Turns a CSV of survey answers into a personalized, AI-written PDF report per
student. See `project_requirement .md` for the full spec.

## Setup

```bash
pip install -r requirements.txt
```

`.env` already has the Ollama connection details (self-hosted server, no
local Ollama install needed):

```
OLLAMA_NETWORK=auto
OLLAMA_LAN_URL=http://192.168.68.58:11434
OLLAMA_WG_URL=http://10.0.0.3:11434
OLLAMA_HERMES3_MODEL=hermes3:8b
```

`OLLAMA_NETWORK` picks which host(s) to call:
- `lan` — only the LAN host
- `wg` — only the WireGuard host (works off the LAN)
- `auto` (default) — tries LAN first, falls back to WireGuard if it's down

## Before the first upload: fill in `section_mapping.json`

The system maps CSV columns to the six sections (B–G) through a config
file, never through hardcoded column positions (project_requirement.md §5).

1. Copy `section_mapping.example.json` to `section_mapping.json`.
2. For each identity field (name, roll number, institution, branch, year),
   put the exact CSV column header.
3. For each section (B–G), list every question column that belongs to it,
   with its answer options **in low → high maturity order, exactly as they
   appear in the CSV**, and mark `multi_select: true` for "select up to
   three" / "select all that apply" style questions.
4. Any CSV column *not* listed anywhere in the mapping is still sent to the
   AI as raw context (and may get its own ad-hoc scored bar if the AI judges
   it worth one) — it just won't count toward a fixed B–G section score.
5. Put pure platform bookkeeping columns (a pre-computed score/rank/timestamp
   that isn't actual survey content) in `ignored_columns` instead — these are
   dropped entirely rather than shown to the AI. A raw `Score: 11/Max: 12`
   column leaking into the prompt was observed to anchor the model into
   returning section scores out of ~10 instead of 0–100.

`section_mapping.json` currently reflects the real i45G career-readiness
diagnostic, built from `UIT.md` (the full question bank) cross-referenced
against the actual CSV export's `Qn` numbering:
- Q8–Q47 grouped into sections B–G (see the `_excluded`/`_ordinal_design`
  notes in the file for what's left out and why).
- Sections B–E have full `options_low_to_high` lists where the question is a
  genuine self-report maturity gradient; a few individual questions and all
  of sections F–G are scenario/best-answer questions (UIT.md itself says to
  randomise their option order and hide the correct answer — i.e. one right
  answer plus distractors, not a scale), so those are left fully AI-judged
  instead of force-ranked.

`section_mapping.json` is gitignored-free (it's real config, not a secret) —
commit it once it reflects the real survey.

### Updating the mapping for a *new* survey (new questions / a different form)

`section_mapping.json` is tied to one specific question set. When a new
survey comes in with different or renumbered questions, don't just hand over
a CSV of responses — that only shows *which answers happened to get picked*,
not the *complete* set of options each question offers, which is what
`options_low_to_high` needs to be correct rather than guessed.

To rebuild it properly, provide both:
1. **The full question bank** — the actual form/questions doc listing every
   question, its type (select one / select up to N / select all), and its
   complete list of answer options (like `UIT.md` this time). This is what
   makes correct low→high ordering and multi-select flags possible.
2. **A CSV export sample** (even a few rows) — so the exact column headers
   and `Qn`-style numbering used by *that* export can be matched up against
   the question bank.

From those two, rebuild `section_mapping.json` the same way this one was
built: group questions into sections (or new ad-hoc parameters if they don't
fit B–G), fill in `options_low_to_high` for genuine maturity gradients, leave
scenario/best-answer questions and multi-select empty for AI judgment, and
list any pure bookkeeping columns (score/rank/timestamp) in
`ignored_columns` so they never reach the AI prompt.

## Run

```bash
python app.py
```

Open http://localhost:5000

## How it works

1. **Screen 1** (`/`) — drop a CSV, optionally type an instruction, click
   *Generate reports*.
2. Backend parses the CSV against `section_mapping.json`, computes a
   preliminary 0–100 score per section from single-choice answers
   (`scoring.py`), and builds one compact prompt per student
   (`prompt_builder.py`) combining their raw answers + the instruction text.
3. Each prompt goes to Hermes-3-8B via Ollama (`ollama_client.py`), which
   returns JSON (section scores, narrative, strengths, etc.). Invalid JSON
   is retried once with a stricter reminder, then falls back to a minimal
   computed-only report (`prompt_builder.build_fallback_report`).
4. `pdf_generator.py` renders **only whatever fields are present** in that
   JSON — bar chart only if `section_scores` is present, strengths heading
   only if `strengths` is present, etc. — so an instruction like "just list
   every question and answer" produces a completely different, shorter PDF
   with no bars forced in.
5. Both the JSON and PDF are saved to disk under
   `output/batches/<batch_id>/` (`storage.py`).
6. **Screen 2** (`/batch/<batch_id>`) lists students with status, and lets
   you Explore (view JSON), View (PDF preview), Download one, Download all
   (each PDF individually), or Download as zip.

## Known prototype limitations

- `/generate` processes the whole batch synchronously in one request — fine
  for small batches; a large batch will hold the request open until every
  student is done.
- No auth — anyone who can reach the Flask server can upload/generate.
