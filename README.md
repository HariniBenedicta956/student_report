# Career Readiness Report Generator

Turns a CSV of survey answers into a personalized, AI-written PDF report per
student. See `docs/project_requirement.md` for the full spec.

## Project layout

```
app.py                    entry point -- routes, request handling, orchestration
config.py                 env vars, paths, feature knobs (stays at root: paths
                           inside it are resolved relative to its own location)
section_mapping.json       real config -- CSV columns -> sections (see below)
section_mapping.example.json  template to copy from for a new survey

core/                      backend logic, imported by app.py
  csv_ingest.py              CSV -> structured student records
  scoring.py                 computed 0-100 section hints
  prompt_builder.py          builds the Ollama messages + output schema
  hermes_agent_client.py     HTTP client to the Hermes agent (below) -- app.py's
                              only path to a model call now, never Ollama directly
  ollama_client.py           HTTP calls to Ollama, retry/backoff, capacity probing --
                              now only imported by hermes_agent/app.py, not app.py
  report_queue.py            priority queue + dynamic worker pool
  pdf_generator.py           renders the final PDF
  storage.py                 batch/manifest/report/trace persistence
  execution_trace.py         per-step data for the Execution Dashboard
  perf_logging.py            structured PERF log lines

hermes_agent/              separate process/service -- the orchestration layer
  app.py                     generation and content validation route through this
                              (API-key authenticated), which calls qwen3.5:4b via
                              core/ollama_client.py. NOT the "Hermes-3-8B" model --
                              see refinedversion.md and the module's own docstring.

static/, templates/        Screen 1/2 + dashboard frontend (served by Flask)
data/, output/              runtime data -- uploads and generated reports
                            (gitignored; not part of the source layout)
docs/                       reference docs (spec, architecture, GPU setup, question bank)
```

## Setup

```bash
pip install -r requirements.txt
```

`.env` already has the Ollama connection details (self-hosted server, no
local Ollama install needed):

```
NETWORK_MODE=auto
MODEL_SELECTION=qwen
OLLAMA_LAN_URL=http://192.168.68.58:11434
OLLAMA_WG_URL=http://10.0.0.3:11434
OLLAMA_QWEN3_MODEL=qwen3.5:4b
OLLAMA_HERMES3_MODEL=hermes3:8b
```

`NETWORK_MODE` picks which host(s) to call:
- `lan` — only the LAN host
- `wg` — only the WireGuard host (works off the LAN)
- `auto` (default) — tries LAN first, falls back to WireGuard if it's down

`MODEL_SELECTION` picks the model:
- `qwen` (default) — `qwen3.5:4b`, which also runs both roles in the validation
  study (see `validation.md`)
- `hermes` — `hermes3:8b`; not called at present, kept so switching back is one line

See `.env.example` for the full set of knobs (workers, retries, context size).

## Before the first upload: fill in `section_mapping.json`

The system maps CSV columns to the six sections (B–G) through a config
file, never through hardcoded column positions (docs/project_requirement.md §5).

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
diagnostic, built from `docs/UIT.md` (the full question bank) cross-referenced
against the actual CSV export's `Qn` numbering:
- Q8–Q47 grouped into sections B–G (see the `_excluded`/`_ordinal_design`
  notes in the file for what's left out and why).
- Sections B–E have full `options_low_to_high` lists where the question is a
  genuine self-report maturity gradient; a few individual questions and all
  of sections F–G are scenario/best-answer questions (docs/UIT.md itself says to
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
   complete list of answer options (like `docs/UIT.md` this time). This is what
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

Two processes now, not one -- the main app never calls Ollama directly, only
the Hermes agent (see `hermes_agent/app.py`'s docstring and refinedversion.md).
Start the agent first:

```bash
python -m hermes_agent.app
```

It refuses to start unless `HERMES_AGENT_API_KEY` is set in `.env` (generate
one with `python -c "import secrets; print(secrets.token_hex(24))"`). Then, in
a second terminal:

```bash
python app.py
```

Open http://localhost:5000

## How it works

1. **Screen 1** (`/`) — drop a CSV, optionally type an instruction, click
   *Generate reports*.
2. Backend parses the CSV against `section_mapping.json`, computes a
   preliminary 0–100 score per section from single-choice answers
   (`core/scoring.py`), and builds one compact prompt per student
   (`core/prompt_builder.py`) combining their raw answers + the instruction text.
3. Each prompt goes to `qwen3.5:4b` via Ollama (`core/ollama_client.py`), which
   returns JSON (section scores, narrative, strengths, etc.). Invalid JSON or an
   unreachable host is retried with backoff by `core/report_queue.py`, which
   requeues the student to the back of the queue rather than blocking the ones
   behind it.
4. `core/pdf_generator.py` renders **only whatever fields are present** in that
   JSON — bar chart only if `section_scores` is present, strengths heading
   only if `strengths` is present, etc. — so an instruction like "just list
   every question and answer" produces a completely different, shorter PDF
   with no bars forced in.
5. Both the JSON and PDF are saved to disk under
   `output/batches/<batch_id>/` (`core/storage.py`).
6. **Screen 2** (`/batch/<batch_id>`) lists students with status, and lets
   you Explore (view JSON), View (PDF preview), Download one, Download all
   (each PDF individually), or Download as zip.

## Known prototype limitations

- `/generate` processes the whole batch synchronously in one request — fine
  for small batches; a large batch will hold the request open until every
  student is done.
- No auth — anyone who can reach the Flask server can upload/generate.
