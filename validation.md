# Qwen Validation Study (Final — qwen3.5:4b)

**Status:** Server currently unreachable (WireGuard access issue, being fixed). Implement
everything below now so it's fully ready to run the moment access is restored. Do not attempt any
live execution, testing, or GPU verification until server access is confirmed — those checks
require the actual GPU machine and cannot be faked or run locally.

## Context

This is an **addition to the existing `student_report` app** — report generation already exists
and works. This update adds the **validation layer** on top of it. Append directly as a
continuation of the existing code, in the same file — no new module/folder.

The app generates a PDF report for a student based on answers they previously chose, using a
fixed template. This update validates the underlying **JSON content** the model produces (not the
PDF itself) before it gets rendered.

Comment out (don't delete, use `#`) the existing Hermes calls. Only `qwen3.5:4b` runs actively for
now — used for **both** generation and validation (same model, two roles).

## Mode flag (agent vs direct)

Add a config flag at the top of the script to control which call path runs:

```python
USE_AGENT = True  # or False
# or via CLI: --mode agent|direct
```

Both code paths must exist in the same file, using the same model (`qwen3.5:4b`):
- **Direct path:** raw call to Ollama's HTTP API (`/api/generate` or `/api/chat`).
- **Agent path:** same model invoked through the agent wrapper.

Both paths run generation → validation identically; the flag only changes how generation calls
are made. Time and log both paths separately so they can be compared.

## Pipeline (per candidate)

**Step 1 — Structural validation (always runs first, before Qwen validation).** Use the existing
report schema already defined in the codebase — do not redefine it. Checks:
- All required fields present
- No field empty, null, or whitespace-only
- Correct data type per field (string/number/array)
- `score` is numeric, within 0–100
- Minimum length on text fields (catch truncated output)
- Array fields (dimensions, strengths, focus, etc.) are non-empty
- Tier/category fields contain only an allowed fixed value
- Required single-object fields (e.g. `single_priority`) present and non-empty

→ On failure: log to `structural_failures` with a specific message (e.g. `"missing field:
summary"`, `"score out of range: 142"`, `"empty array: dimensions"`).

**Step 2 — Qwen (qwen3.5:4b) model validation (only if Step 1 passes).** Checks content accuracy
against the student's actual source answers:
- No hallucinated claims — every statement must be traceable to what the student actually answered
- No claims about skills/actions the student didn't actually do
- Score is consistent with the source answers (no mismatch)

→ On failure: log to `validation_failures` with a specific message (e.g. `"claim not supported by
source data"`, `"score mismatch"`).

A candidate is only "valid/complete" after passing **both** steps.

## Retry logic

- On failure at either step, requeue the candidate to the **end** of the generation queue (FIFO,
  no skipping).
- **Always resend the full context on retry** — original source answers + the full previous
  prompt + the specific failure reason from validation. Never send a partial patch/diff
  instruction — full regeneration only, to avoid inconsistent partial edits.
- **Retry cap: 3 attempts total.**
- **After 3 failed attempts:** stop retrying. Log the candidate to a `needs_review` list with its
  full failure history (all error messages from every attempt). This is reviewed manually for now
  — no auto-escalation needed yet at current volume. Do not silently drop the candidate.

## Bad-sample test case

Define one deliberate test explicitly in code (not left to chance):
- Take one real, valid candidate output.
- Corrupt exactly one field — either insert a claim/wording about something the student did not
  actually do, or flip the score to an incorrect value.
- Run this corrupted candidate through the full validation pipeline and confirm it gets flagged
  (ideally at Step 2, or Step 1 if it's a structural corruption).
- Record whether it was successfully caught.

## GPU/CPU verification

**Do not build or trust this check until server access is restored** — it must run on the actual
GPU machine, not locally.

Once access is back:
- Run `ollama ps` before each generation — check the `PROCESSOR` column for `100% GPU` (or a
  GPU/CPU split). If it shows CPU-heavy or 100% CPU, generation is not using GPU.
- Optionally cross-check with `nvidia-smi` during generation (utilization should be non-zero if
  GPU is active).
- Add a small status indicator in the app (e.g. a badge: "Running on: GPU ✅" / "Running on: CPU
  ⚠️") reflecting this check before each run.
- Log the result per run — don't just check silently.

## Logging

- Log every attempt (not just final result): candidate ID, attempt number, method (agent/direct),
  generation time, structural result + error (if any), validation time + error (if any), total
  time, GPU/CPU status.
- Keep failed attempts on record even after a candidate eventually passes — needed for later
  accuracy review/audit, since this is a student-facing report.
- Output format: structured JSON log file (not just console print) — pick one consistent
  destination.

## Output at end of run

- Per-candidate/method summary: generation time, structural result, validation time, total time,
  GPU/CPU status, whether the deliberately-injected bad sample was caught.
- A `structural_failures` list with exact error messages.
- A `validation_failures` list with exact error messages.
- A `needs_review` list of candidates that exhausted 3 retries, with full failure history.
- Clearly indicate which method (agent vs direct) was faster and/or more accurate, based on the
  above — no separate formatted comparison table required, just this data captured per run (a
  plain summary is enough).

## Model reference

```
ollama run qwen3.5:4b
```
Confirmed available on Ollama — used for both the report-generation role and the validation role
in this study, in place of qwen3:14b.

## Note

Do not attempt to run, test, or verify GPU usage against the server until WireGuard access is
confirmed restored. Implement fully and leave ready.