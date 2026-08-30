# Project requirement — AI-powered student career-readiness report generator

This document explains the entire project from scratch. It assumes no prior
context — if you're new to this project, read top to bottom and you'll
understand what it does, why, and exactly how it should look and behave.

## 1. Background

A student takes a survey (a Google Form export, one row per student, each
column a question). Right now, someone would have to manually read every
student's answers and write feedback. This project automates that: it reads
the survey answers, scores the student across a few key dimensions, and uses
an AI model to write a personalized, encouraging report — then produces a
polished PDF.

## 2. Goal

Turn a CSV of survey answers into a personalized, two-page PDF report per
student, with almost no manual work — just upload the file, add a short
instruction, and click one button.

## 3. Who this is for

A teacher, placement coordinator, or exam organizer who has run a survey
(career readiness, systems-thinking assessment, etc.) and wants readable,
individualized feedback for every student without reading and writing each
report by hand.

## 4. The workflow, plain English

1. Upload the CSV of student answers.
2. Type a short instruction for the AI (optional but recommended) — e.g.
   "focus on career readiness" or "list every question and answer."
3. Click **Generate reports**.
4. The system reads each student's answers, scores them section by section,
   and asks a local AI model to write the narrative parts of the report.
5. A two-page PDF is created per student, plus a JSON version of the same
   content.
6. You can look at any student's result, view/download their PDF one at a
   time, or download everyone at once as a zip.

No internet-hosted AI service is used — the AI model (Hermes-3-8B) runs
locally, so there's no per-use billing.

## 5. The input — the CSV

- One row per student.
- A few "identity" columns: name, roll number, college/school, branch, year.
- The rest of the columns are survey questions, each belonging to one of six
  sections:
  - **B — Career clarity & direction**
  - **C — Curiosity & reading depth**
  - **D — AI-use maturity & learning discipline**
  - **E — Practical exposure & building**
  - **F — Coachability & problem-solving approach**
  - **G — Systems & critical thinking**
- Answer options for each question are ordered from "lower maturity" to
  "higher maturity" (e.g. "I stopped working on it" → "I recorded what
  happened, tested causes, improved it"). This ordering is what lets a whole
  section be turned into a single 0–100 score.

**Important — must generalize to any question count.** The example file
used during design had 54 questions. A future file might have 45, 75, or any
other number. The system must map columns to sections **by a configurable
column-name/section mapping**, never by a hardcoded column count or fixed
position. As long as each question is tagged to one of the six sections, the
same scoring and report pipeline works unchanged.

## 6. Screen 1 — Upload & instructions

What's on this screen:
- A **drop zone** to upload the CSV. Shows the filename and student count
  once uploaded.
- A single **free-text instructions box** — one plain text field, no
  category buttons, no dropdown. Whatever is typed here is passed straight to
  the AI as an extra instruction on top of each student's data. Examples:
  - "Include explicit strength / weakness / needs-improvement columns."
  - "List every question and the answer they gave, then generate the report."
  - "Focus mainly on practical exposure and systems thinking."
- One button: **Generate reports**.

There is intentionally **no** audience/template picker (no "College /
School / Custom" style buttons) — that idea was tried and dropped. The
instructions box alone controls how the AI approaches the report.

## 7. Screen 2 — Student list & results

- A list of students, each row showing: name, class/branch/year, and status
  (Pending / Done).
- Each row has an **Explore** button. Clicking it opens that student's report
  as JSON, with two buttons: **View** (shows the PDF preview) and
  **Download** (saves that student's PDF).
- Two top-level buttons above the list: **Download all** and **Download as
  zip** — so the user can grab everyone's reports at once instead of one by
  one.
- The whole thing works whether the user wants to generate one student's
  report or all of them in a batch — it's their choice at generate time.

## 8. Visual style — exact look to match

The interface uses a plain, flat, neutral design — no gradients, no heavy
shadows, no illustration. Two shades of a light neutral surface color are
used to separate panels from their background (a slightly darker outer
background, lighter inner cards), with a thin, low-contrast border (roughly
0.5px, subtle gray) around cards and table rows. Buttons are simple
rectangles with an icon + label, black background with white text for the
primary action, and outlined/neutral for secondary actions. Text is dark
gray/near-black for primary content and a lighter gray for secondary/meta
text (dates, status, captions).

The only accent colors in the whole product are the two bar-chart colors,
and they always mean the same thing everywhere they appear (screen JSON
preview, PDF page 1):

- **Amber `#EF9F27`** — a section score that needs improvement (below roughly
  60–65).
- **Teal `#5DCAA5`** — a section score that meets expectations.

These two colors, with a small two-item legend under the chart, are the only
splash of color in the entire product. Everything else stays neutral gray/black/white
so the two performance colors stand out and are easy to read at a glance.

## 9. The AI generation step

For each selected student:
1. Their raw answers (grouped by section) plus the instructions-box text are
   formatted into a compact prompt.
2. The prompt is sent to **Hermes-3-8B**, running locally via **Ollama**
   (`http://localhost:11434/api/generate`) — not a hosted API, so there is no
   per-token cost, only local compute time.
3. The model returns a structured JSON response: section scores, an overall
   insight, strengths, growth zones, interest zones, and recommendations —
   always written in encouraging, non-comparative language. A student is
   never described in a way that could feel like a judgment of their worth —
   only in terms of where they are now and what would help them grow.
4. If the model doesn't return valid JSON, the system retries once with a
   stricter reminder, then falls back to a minimal templated report rather
   than failing silently.

## 10. Scoring logic

- Each of the six sections (B–G) gets one score, 0–100.
- Single-choice questions: the selected option's position on its low→high
  maturity scale determines its contribution to the section score.
- Multi-select questions ("choose up to three," "select all that apply"):
  there's no fixed points-per-item formula — the AI judges the overall
  quality of the selection as part of the section score, since counting
  items would reward quantity over substance.
- The six scores are what get plotted as the six bars on the PDF and in the
  Explore view.

## 11. PDF report — content, in detail

The report is always **two pages**. The specific wording changes per student
(driven by their actual answers and the instructions-box text), but the
structure and visual style stay fixed:

**Page 1**
- Report title and a subtitle describing the assessment.
- Student's name, branch/class, year, and institution in the header, right-aligned.
- Six horizontal bars, one per section (B–G), each labeled and ending in its
  numeric score, colored amber or teal per the rule above.
- A small legend under the bars explaining the two colors.
- A short "Where you stand" paragraph — reads the pattern across all six
  bars (not just one), written in plain, encouraging language.
- A short "How this shows up in your answers" paragraph — one or two
  concrete examples pulled from that specific student's actual answers, so
  the report doesn't read as generic.

**Page 2**
- **Strengths** — evidence-based, referencing what they actually did well.
- **Where to focus next** — framed as opportunity, never as a flaw or deficiency.
- **Interest zone** — what the pattern of answers suggests they're drawn
  toward (e.g. backend engineering, research, design).
- **Suggested next steps** — 3–5 concrete, doable actions.
- A short closing note reminding the reader this is a snapshot from one
  assessment, not a permanent label.

If the instructions box asked for a full question-and-answer listing, that
becomes an extra page inserted before the closing note — it does not get
squeezed onto page 1 or 2.

### The instructions box can fully override this structure

The two-page layout above (bars → where-you-stand → strengths → growth →
interest → next steps) is the **default**, used whenever the instructions box
is left as-is or gives a general steer (e.g. "focus on career readiness").

But the instructions box is not just a flavor on top of a fixed template —
it can replace the structure entirely. Example: if the instructions box says
*"list every question the student answered and give the final result
alone,"* the generated PDF should contain **only** that — a plain Q&A
listing and a final result — with no bar chart, no strengths/growth/interest
sections forced in.

**Rule for the PDF generator:** it renders whatever fields the AI actually
returns in its JSON — nothing more, nothing forced. If the AI's response
(driven by the prompt) doesn't include section scores, no bar chart is
drawn. If it doesn't include a strengths section, that heading doesn't
appear. The default two-page structure happens because the default prompt
produces JSON with all of those fields — not because the PDF template
hardcodes them.

## 12. Saving results

Every generated report — both the JSON and the PDF — is saved to disk (not
just kept in memory), organized by batch, so a batch of reports is still
there if the server restarts. The Explore, View, and Download actions read
from these saved files rather than needing to regenerate anything.

## 13. What "done" looks like

- Any CSV, regardless of exact question count, can be uploaded and processed
  as long as its columns are mapped to the six sections.
- Every report is personalized — two students with similar scores can still
  get different specific examples and recommendations, because the AI reads
  their actual answers, not just the numbers.
- No student's report reads as negative, judgmental, or comparative to
  peers.
- A user with zero technical background can upload a file, type one
  sentence, click one button, and get a batch of readable, well-designed
  PDF reports without touching any code.

## 14. Glossary (for newcomers)

- **Section (B–G)** — one of six groups of related survey questions that
  together produce one 0–100 score.
- **Hermes-3-8B** — the AI model used to write the narrative parts of the
  report. Runs locally via Ollama, not a paid cloud API.
- **Explore** — the button that shows one student's report as raw JSON
  before/alongside the PDF.
- **Batch** — one run of "Generate reports" covering one or more selected
  students, saved together on disk.