import json
import os
import re
import threading
import time
import uuid
from datetime import datetime, timezone

import config

# update_student_status/update_student_progress are read-modify-write against a
# single manifest file: load the whole thing, change one student's entry, write the
# whole thing back. With one worker that's fine. With more than one it is not --
# two workers can load the same manifest, each apply their own student's change to
# their own copy, and the second write silently discards the first student's update,
# leaving a finished report showing as "pending" forever. Guarding the whole
# read-modify-write keeps that safe regardless of config.REPORT_WORKERS. The critical
# section is a few milliseconds of local file I/O against minutes of generation, so
# serializing it costs nothing worth measuring.
_manifest_lock = threading.Lock()

STAGE_LABELS = {
    "prompt_build": "Building prompt",
    "ai_call": "Calling AI model",
    "pdf_generate": "Generating PDF",
    "file_save": "Saving files",
}


def _slugify(text, fallback):
    text = (text or "").strip().lower()
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    return text or fallback


def make_student_id(identity, index):
    base = identity.get("roll_number") or identity.get("name") or f"student-{index}"
    return _slugify(base, f"student-{index}")


def batch_dir(batch_id):
    return os.path.join(config.BATCHES_DIR, batch_id)


def _json_dir(batch_id):
    return os.path.join(batch_dir(batch_id), "json")


def _pdf_dir(batch_id):
    return os.path.join(batch_dir(batch_id), "pdf")


def _trace_dir(batch_id):
    return os.path.join(batch_dir(batch_id), "trace")


def _records_dir(batch_id):
    return os.path.join(batch_dir(batch_id), "records")


def manifest_path(batch_id):
    return os.path.join(batch_dir(batch_id), "manifest.json")


def validation_path(batch_id):
    return os.path.join(batch_dir(batch_id), "validation.json")


def mapping_path(batch_id):
    return os.path.join(batch_dir(batch_id), "mapping.json")


def save_batch_mapping(batch_id, mapping):
    """
    The exact mapping (static or auto-generated -- see core/mapping_inference.py)
    that /generate parsed this batch's CSV against, saved with the batch itself.
    /batch/<id>/validate reads it back from here rather than always reaching for
    the static section_mapping.json, so a batch generated from a differently-
    structured, auto-mapped form is still validated against the mapping that
    actually matches it, not a mismatched default.
    """
    with open(mapping_path(batch_id), "w", encoding="utf-8") as f:
        json.dump(mapping, f, indent=2)


def load_batch_mapping(batch_id):
    with open(mapping_path(batch_id), "r", encoding="utf-8") as f:
        return json.load(f)


def create_batch(student_records):
    batch_id = f"{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
    os.makedirs(_json_dir(batch_id), exist_ok=True)
    os.makedirs(_pdf_dir(batch_id), exist_ok=True)
    os.makedirs(_trace_dir(batch_id), exist_ok=True)
    os.makedirs(_records_dir(batch_id), exist_ok=True)

    students = []
    for i, record in enumerate(student_records):
        student_id = make_student_id(record["identity"], i)
        students.append({
            "student_id": student_id,
            "name": record["identity"].get("name", ""),
            "branch": record["identity"].get("branch", ""),
            "year": record["identity"].get("year", ""),
            "institution": record["identity"].get("institution", ""),
            "status": "pending",
        })

    manifest = {
        "batch_id": batch_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "students": students,
    }
    save_manifest(batch_id, manifest)
    return batch_id, manifest


def save_manifest(batch_id, manifest):
    with open(manifest_path(batch_id), "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)


def load_manifest(batch_id):
    with open(manifest_path(batch_id), "r", encoding="utf-8") as f:
        return json.load(f)


def update_student_status(batch_id, student_id, status):
    with _manifest_lock:
        manifest = load_manifest(batch_id)
        for s in manifest["students"]:
            if s["student_id"] == student_id:
                s["status"] = status
                s.pop("current_stage", None)
                s.pop("stage_started_at", None)
        save_manifest(batch_id, manifest)


def update_student_progress(batch_id, student_id, stage, note=None, restart_clock=True):
    """
    Marks which pipeline stage a student's generation is currently in, with a wall-
    clock start time -- lets Screen 2 show live "currently: AI call, 42s elapsed"
    progress while a report is still generating, instead of only a final summary
    once it's done.

    note appends detail to the stage label (e.g. "retry 3 - unreachable"), so a stalled
    student reads as retrying rather than as silently hung. restart_clock=False keeps
    the original stage_started_at, so the elapsed time shown covers the whole stage
    including its retries rather than resetting on each one.
    """
    with _manifest_lock:
        manifest = load_manifest(batch_id)
        for s in manifest["students"]:
            if s["student_id"] == student_id:
                s["current_stage"] = stage
                if restart_clock or not s.get("stage_started_at"):
                    s["stage_started_at"] = time.time()
                if note:
                    s["stage_note"] = note
                else:
                    s.pop("stage_note", None)
        save_manifest(batch_id, manifest)


def with_live_progress(manifest):
    """Adds current_stage_label/stage_elapsed_s to pending students, computed fresh."""
    now = time.time()
    for s in manifest["students"]:
        if s.get("status") == "pending" and s.get("stage_started_at"):
            label = STAGE_LABELS.get(s["current_stage"], s["current_stage"])
            if s.get("stage_note"):
                label = f"{label} ({s['stage_note']})"
            s["current_stage_label"] = label
            s["stage_elapsed_s"] = round(now - s["stage_started_at"], 1)
    return manifest


def save_student_report(batch_id, student_id, report_json):
    path = os.path.join(_json_dir(batch_id), f"{student_id}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(report_json, f, indent=2)
    return path


def load_student_report(batch_id, student_id):
    path = os.path.join(_json_dir(batch_id), f"{student_id}.json")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_student_record(batch_id, student_id, student_record):
    """
    Persists the parsed CSV record (identity + section answers) a student's report
    was generated from -- not just the report itself. Without this, a later
    validation pass has no source answers to check the report's content against
    once the request that generated it is long over. Batches created before this
    existed simply have no file here: load_student_record raises FileNotFoundError,
    and the caller treats the content/accuracy check as unavailable for that batch
    rather than failing.
    """
    path = os.path.join(_records_dir(batch_id), f"{student_id}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(student_record, f, indent=2, ensure_ascii=False)
    return path


def load_student_record(batch_id, student_id):
    path = os.path.join(_records_dir(batch_id), f"{student_id}.json")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_batch_validation(batch_id, validation):
    with open(validation_path(batch_id), "w", encoding="utf-8") as f:
        json.dump(validation, f, indent=2, default=str)


def load_batch_validation(batch_id):
    with open(validation_path(batch_id), "r", encoding="utf-8") as f:
        return json.load(f)


def student_pdf_path(batch_id, student_id):
    return os.path.join(_pdf_dir(batch_id), f"{student_id}.pdf")


def save_student_trace(batch_id, student_id, trace):
    """
    Overwrites the student's execution trace on disk -- called after every pipeline
    step transition (not just once at the end), so a client polling this file mid-
    generation sees each step fill in as it actually happens.
    """
    path = os.path.join(_trace_dir(batch_id), f"{student_id}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(trace, f, indent=2, default=str)


def load_student_trace(batch_id, student_id):
    path = os.path.join(_trace_dir(batch_id), f"{student_id}.json")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def list_batches():
    if not os.path.isdir(config.BATCHES_DIR):
        return []
    return sorted(os.listdir(config.BATCHES_DIR), reverse=True)
