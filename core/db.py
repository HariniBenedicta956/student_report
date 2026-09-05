"""
Real, inspectable database for the two things the admin workflow needs
persisted and queryable independent of the file-based batch storage:
eligibility decisions (Sync Eligibility) and, once WhatsApp is actually
integrated, consent replies.

SQLite, not a bigger engine -- this app has one process, one machine, no
concurrent-writer-across-servers requirement, and SQLite is a single file any
DB browser (DB Browser for SQLite, the sqlite3 CLI, a VS Code SQLite
extension) can open directly with zero setup. See DB_PATH below for exactly
where that file lives.
"""
import os
import sqlite3
from datetime import datetime, timezone

import config

DB_PATH = os.path.join(config.BASE_DIR, "data", "app.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS eligibility (
    upload_id       TEXT NOT NULL,
    student_index   INTEGER NOT NULL,
    student_id      TEXT,
    name            TEXT,
    completion_pct  REAL NOT NULL,
    overall_score   REAL,
    threshold_pct   REAL NOT NULL,
    min_score       REAL,
    eligible        INTEGER NOT NULL,   -- 1 = Yes, 0 = No (SQLite has no bool type)
    unanswered_json TEXT,               -- JSON list of {"qid","question"} -- exact gaps, not just the %
    computed_at     TEXT NOT NULL,
    PRIMARY KEY (upload_id, student_index)
);

-- Not populated yet -- WhatsApp isn't integrated (left as-is, per instruction).
-- Created now so the schema for "Yes/No consent, reply stored in DB" already
-- exists and doesn't need a migration the day that integration is built.
CREATE TABLE IF NOT EXISTS consent (
    upload_id     TEXT NOT NULL,
    student_index INTEGER NOT NULL,
    student_id    TEXT,
    reply         TEXT,       -- "yes" / "no" / "no_response"
    replied_at    TEXT,
    PRIMARY KEY (upload_id, student_index)
);
"""


def get_connection():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with get_connection() as conn:
        conn.executescript(_SCHEMA)


def record_eligibility(upload_id, rows):
    """
    rows: [{"index", "student_id", "name", "completion_pct", "overall_score",
             "eligible", "unanswered_questions"}, ...]
    threshold_pct/min_score are the criteria this run was computed against --
    stored per row (not just once per run) so a later re-sync with a
    different threshold doesn't make old rows ambiguous about which
    criteria produced them.
    """
    now = datetime.now(timezone.utc).isoformat()
    with get_connection() as conn:
        for r in rows:
            conn.execute(
                """
                INSERT INTO eligibility
                    (upload_id, student_index, student_id, name, completion_pct,
                     overall_score, threshold_pct, min_score, eligible,
                     unanswered_json, computed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(upload_id, student_index) DO UPDATE SET
                    student_id=excluded.student_id, name=excluded.name,
                    completion_pct=excluded.completion_pct,
                    overall_score=excluded.overall_score,
                    threshold_pct=excluded.threshold_pct,
                    min_score=excluded.min_score, eligible=excluded.eligible,
                    unanswered_json=excluded.unanswered_json,
                    computed_at=excluded.computed_at
                """,
                (upload_id, r["index"], r.get("student_id"), r.get("name"),
                 r["completion_pct"], r.get("overall_score"), r["threshold_pct"],
                 r.get("min_score"), 1 if r["eligible"] else 0,
                 r.get("unanswered_json"), now),
            )


def get_eligibility(upload_id):
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM eligibility WHERE upload_id = ? ORDER BY student_index",
            (upload_id,),
        ).fetchall()
        return [dict(r) for r in rows]
