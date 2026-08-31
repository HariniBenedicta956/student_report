import inspect
import io
import logging
import os
import threading
import time
import uuid
import zipfile

from flask import Flask, abort, jsonify, render_template, request, send_file
from werkzeug.utils import secure_filename

import config
from core import csv_ingest
from core import execution_trace
from core import ollama_client
from core import pdf_generator
from core import perf_logging
from core import prompt_builder
from core import report_queue
from core import storage

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

app = Flask(__name__)
os.makedirs(config.UPLOAD_DIR, exist_ok=True)
os.makedirs(config.QUESTION_BANK_DIR, exist_ok=True)
os.makedirs(config.BATCHES_DIR, exist_ok=True)


@app.get("/health")
def health():
    return jsonify({"status": "ok", "ollama_hosts": config.OLLAMA_HOSTS})


@app.get("/")
def index():
    return render_template("index.html")


@app.post("/upload")
def upload():
    if "file" not in request.files:
        return jsonify({"error": "No file uploaded"}), 400
    file = request.files["file"]
    if not file.filename:
        return jsonify({"error": "No file selected"}), 400

    try:
        mapping = csv_ingest.load_section_mapping()
    except csv_ingest.SectionMappingMissingError as exc:
        return jsonify({"error": str(exc)}), 500

    file_bytes = file.read()
    try:
        students = csv_ingest.parse_csv(file_bytes, mapping)
    except Exception as exc:
        log.exception("Failed to parse uploaded CSV")
        return jsonify({"error": f"Could not parse CSV: {exc}"}), 400

    if not students:
        return jsonify({"error": "No student rows found in CSV"}), 400

    upload_id = uuid.uuid4().hex
    safe_name = secure_filename(file.filename)
    saved_path = os.path.join(config.UPLOAD_DIR, f"{upload_id}_{safe_name}")
    with open(saved_path, "wb") as f:
        f.write(file_bytes)

    return jsonify({
        "upload_id": upload_id,
        "filename": file.filename,
        "student_count": len(students),
        "saved_path": saved_path,
        "students": [
            {
                "index": i,
                "name": s["identity"].get("name", ""),
                "branch": s["identity"].get("branch", ""),
                "year": s["identity"].get("year", ""),
            }
            for i, s in enumerate(students)
        ],
    })


@app.post("/upload-question-bank")
def upload_question_bank():
    """
    Saves the uploaded question bank file (e.g. a UIT.md-style document) to disk --
    it is NOT parsed or auto-applied to section_mapping.json. Building that mapping
    involves judgment calls (which section a question belongs to, correct answers for
    scenario questions, low->high ordering) that need review rather than a blind
    automated parse, so this just gets the file to a place it can be read and the
    mapping updated deliberately afterward.
    """
    if "file" not in request.files:
        return jsonify({"error": "No file uploaded"}), 400
    file = request.files["file"]
    if not file.filename:
        return jsonify({"error": "No file selected"}), 400

    safe_name = secure_filename(file.filename)
    saved_path = os.path.join(config.QUESTION_BANK_DIR, f"{uuid.uuid4().hex}_{safe_name}")
    file.save(saved_path)

    return jsonify({"filename": file.filename, "saved_path": saved_path})


def _find_uploaded_file(upload_id):
    for name in os.listdir(config.UPLOAD_DIR):
        if name.startswith(f"{upload_id}_"):
            return os.path.join(config.UPLOAD_DIR, name)
    return None


def _reconstruct_raw_row(student_record):
    """Rebuilds a flat {question: answer} view of a parsed student record -- used
    only for the execution-trace 'Read CSV' step display, since parse_csv doesn't
    keep the original csv.DictReader row around after structuring it."""
    row = dict(student_record["identity"])
    for questions in student_record["sections"].values():
        for q in questions:
            row[q["question"]] = q["answer"]
    for u in student_record["unmapped"]:
        row[u["question"]] = u["answer"]
    return row


def _build_shared_trace_steps(original_filename, csv_size, student_count, mapping):
    """
    'Upload CSV' and 'Validate' are batch-level steps -- identical for every student
    in this batch -- computed once in /generate rather than redone per student.
    """
    upload_entry = {
        "label": execution_trace.STEP_LABELS["upload_csv"],
        "status": "done",
        "code": inspect.getsource(upload),
        "input": {"filename": original_filename},
        "output": {"size_bytes": csv_size, "student_count": student_count},
    }
    mapped_questions = sum(len(s["questions"]) for s in mapping["sections"].values())
    validate_entry = {
        "label": execution_trace.STEP_LABELS["validate"],
        "status": "done",
        "code": execution_trace.get_code(csv_ingest, "load_section_mapping"),
        "input": {"mapping_path": config.SECTION_MAPPING_PATH},
        "output": {
            "sections": list(mapping["sections"].keys()),
            "mapped_questions": mapped_questions,
            "ignored_columns": mapping.get("ignored_columns", []),
        },
    }
    return {"upload_csv": upload_entry, "validate": validate_entry}


@app.post("/generate")
def generate():
    payload = request.get_json(force=True, silent=True) or {}
    upload_id = payload.get("upload_id")
    instructions_text = (payload.get("instructions") or "").strip()
    selected_indices = payload.get("selected_indices")  # None/omitted -> all students

    if not upload_id:
        return jsonify({"error": "upload_id is required"}), 400

    saved_path = _find_uploaded_file(upload_id)
    if not saved_path:
        return jsonify({"error": "Uploaded file not found, please re-upload"}), 404
    original_filename = os.path.basename(saved_path).split("_", 1)[-1]

    mapping = csv_ingest.load_section_mapping()
    t0 = time.perf_counter()
    with open(saved_path, "rb") as f:
        csv_bytes = f.read()
        students = csv_ingest.parse_csv(csv_bytes, mapping)
    csv_parse_s = time.perf_counter() - t0

    if selected_indices is not None:
        selected_set = set(selected_indices)
        students = [s for i, s in enumerate(students) if i in selected_set]

    if not students:
        return jsonify({"error": "No students selected"}), 400

    batch_id, manifest = storage.create_batch(students)
    # Saved once here rather than left to only live in this request's memory --
    # it's what lets /batch/<id>/validate check a report's content against the
    # student's actual answers later, without needing the original CSV to still
    # be sitting in data/uploads/.
    for i, record in enumerate(students):
        storage.save_student_record(batch_id, manifest["students"][i]["student_id"], record)
    perf_logging.log_csv_parse(batch_id, len(students), csv_parse_s)
    shared_trace = _build_shared_trace_steps(
        original_filename, len(csv_bytes), len(students), mapping
    )

    thread = threading.Thread(
        target=_run_batch,
        args=(batch_id, students, manifest, mapping, instructions_text, shared_trace),
        daemon=True,
    )
    thread.start()

    return jsonify({"batch_id": batch_id})


def _run_batch(batch_id, students, manifest, mapping, instructions_text, shared_trace):
    """
    Runs in a background thread so /generate can return immediately -- each
    student's AI call can take 1-3 minutes, and blocking the HTTP response on
    the whole batch left the UI showing "Generating..." with zero feedback
    for minutes at a time. Screen 2 polls /batch/<id>/students instead to
    watch statuses flip from pending to done (or error) as they complete.

    Students are fed through report_queue rather than looped over directly, so a
    failure is contained to the one student it belongs to and throughput is a
    single config knob (config.REPORT_WORKERS) instead of a structural property of
    this function.
    """
    # Built once for the whole batch, not per student. Every student in a batch is
    # answering the same survey, so this text is identical for all of them -- and
    # keeping it byte-identical is what lets Ollama reuse its cached evaluation of
    # it instead of re-reading the same ~7.5KB of question text for every student.
    question_bank = prompt_builder.build_question_bank(mapping, students)

    def handle(item, ctx):
        index, student_id, student_record = item
        _generate_one(batch_id, student_id, index, student_record, mapping,
                       instructions_text, shared_trace, question_bank, ctx)

    def on_retry(item, exc, attempts, delay_s):
        """
        The student stays "pending" -- it is queued for another go, not failed -- but
        the row says so explicitly, otherwise a student waiting out a backoff is
        indistinguishable from one that is simply slow.
        """
        _, student_id, _ = item
        error_type = getattr(exc, "error_type", type(exc).__name__)
        log.warning("Requeuing %s in batch %s (attempt %d, %s), next try in %.0fs",
                     student_id, batch_id, attempts, error_type, delay_s)
        storage.update_student_progress(
            batch_id, student_id, "ai_call",
            note=f"queued for retry {attempts} - {error_type}", restart_clock=False,
        )

    def on_give_up(item, exc, attempts):
        """
        Only reachable when REPORT_MAX_ATTEMPTS is set to a positive number; the
        default is unlimited retries, in which case this never fires and no templated
        report is ever substituted for a real one.
        """
        _, student_id, student_record = item
        log.error("Giving up on %s in batch %s after %d attempts: %s",
                   student_id, batch_id, attempts, exc)
        _write_fallback_report(batch_id, student_id, student_record, mapping,
                                f"{exc} (gave up after {attempts} attempts)")

    # The manifest order is fixed at batch creation and is what Screen 2, the ZIP and
    # Download-all all read from, so tasks completing out of order (a requeued student
    # finishing after later ones) does not affect the order reports are presented in.
    items = [
        (i, manifest["students"][i]["student_id"], record)
        for i, record in enumerate(students)
    ]
    stats = report_queue.process(
        items, handle,
        hosts=None,   # resolved live per task, so host changes mid-batch take effect
        workers=config.REPORT_WORKERS or None,   # 0 -> probe live capacity
        on_retry=on_retry,
        on_give_up=on_give_up,
        label=batch_id,
    )
    perf_logging.log_batch(batch_id, stats.as_dict(), stats.workers_started)


def _write_fallback_report(batch_id, student_id, student_record, mapping, reason):
    """Templated stand-in, written only after the queue has exhausted its retries."""
    report_json = prompt_builder.build_fallback_report(student_record, mapping)
    report_json["_generation_error"] = reason
    pdf_path = storage.student_pdf_path(batch_id, student_id)
    pdf_content = {k: v for k, v in report_json.items() if not k.startswith("_")}
    pdf_generator.generate_pdf(student_record["identity"], pdf_content, pdf_path)
    storage.save_student_report(batch_id, student_id, report_json)
    storage.update_student_status(batch_id, student_id, "done")


def _generate_one(batch_id, student_id, student_index, student_record, mapping,
                   instructions_text, shared_trace, question_bank=None, ctx=None):
    trace = execution_trace.new_trace()
    trace["upload_csv"] = shared_trace["upload_csv"]
    trace["validate"] = shared_trace["validate"]
    execution_trace.set_step(
        trace, "read_csv", "done",
        code=execution_trace.get_code(csv_ingest, "parse_csv"),
        input_data={"row_index": student_index,
                    "source_file": shared_trace["upload_csv"]["input"]["filename"]},
        output_data=_reconstruct_raw_row(student_record),
    )
    execution_trace.set_step(
        trace, "csv_to_json", "done",
        code=execution_trace.get_code(csv_ingest, "parse_csv"),
        input_data={"note": "the same parse_csv() call above also produces this "
                             "structured record -- shown as a separate step here "
                             "since it's a distinct transformation of the same row"},
        output_data=student_record,
    )
    storage.save_student_trace(batch_id, student_id, trace)

    timings = {}
    t_total = time.perf_counter()

    storage.update_student_progress(batch_id, student_id, "prompt_build")
    t_stage = time.perf_counter()
    with perf_logging.timed(timings, "prompt_build_s"):
        is_qa_request = prompt_builder.wants_qa_listing(instructions_text)
        qa_listing = None
        if is_qa_request:
            wrong_only = prompt_builder.wants_wrong_answers_only(instructions_text)
            qa_listing = prompt_builder.build_qa_listing(student_record, wrong_only=wrong_only)
            messages = prompt_builder.build_advice_messages(
                student_record, mapping, instructions_text, qa_listing
            )
            prompt_code = execution_trace.get_code(prompt_builder, "build_advice_messages")
        else:
            messages = prompt_builder.build_messages(
                student_record, mapping, instructions_text, question_bank
            )
            prompt_code = execution_trace.get_code(prompt_builder, "build_messages")
    execution_trace.set_step(
        trace, "build_prompt", "done",
        code=prompt_code,
        input_data={"instructions_text": instructions_text, "is_qa_request": is_qa_request},
        output_data={"messages": messages},
        duration_s=time.perf_counter() - t_stage,
    )
    storage.save_student_trace(batch_id, student_id, trace)

    failure_reason = None
    attempts = 0
    ai_metrics = None
    raw_text = None
    host_used = None
    storage.update_student_progress(batch_id, student_id, "ai_call")
    selected_model = config.get_active_ollama_model()
    hermes_request_info = {
        "hosts_tried_in_order": list(ctx.hosts) if ctx else config.get_active_hosts(),
        "model": selected_model,
        "message_count": len(messages),
        "payload_chars": sum(len(m["content"]) for m in messages),
    }
    # A requeued attempt starts from a fresh trace, so carry the reason the previous
    # one failed into this one. Without it a report that only succeeded on the fourth
    # try looks identical to one that worked immediately, and the failure history --
    # the actually useful part when debugging -- is lost the moment it recovers.
    prior_failure = None
    if ctx is not None and ctx.attempt > 1:
        prior = ctx.last_error
        prior_failure = {
            "queue_attempt": ctx.attempt,
            "previous_error_type": getattr(prior, "error_type", type(prior).__name__)
            if prior else "unknown",
            "previous_error": str(prior)[:300] if prior else None,
            "failed_stage": execution_trace.STEP_LABELS["send_to_hermes"],
        }
    execution_trace.set_step(trace, "send_to_hermes", "running",
                              code=execution_trace.get_code(ollama_client, "_call_host"),
                              input_data=hermes_request_info,
                              extra=prior_failure)
    execution_trace.set_step(trace, "ai_processing", "running",
                              code=execution_trace.get_code(ollama_client, "generate_json"))
    storage.save_student_trace(batch_id, student_id, trace)

    def _on_retry(attempt, error_type, detail, delay_s):
        """
        Live retry reporting. ollama_client keeps retrying with backoff until it
        succeeds, so without this a stalled student would look identical to a slow
        one -- "Calling AI model, 300s elapsed" with no hint that anything is wrong.
        This puts the failure type, the stage it happened at, and the retry count in
        front of whoever is watching, on both Screen 2 and the dashboard.
        """
        execution_trace.update_step_extra(
            trace, "send_to_hermes",
            attempt=attempt + 1,
            last_error_type=error_type,
            last_error=detail[:400],
            failed_stage=execution_trace.STEP_LABELS["send_to_hermes"],
            retrying_in_s=delay_s,
        )
        execution_trace.update_step_extra(
            trace, "ai_processing",
            attempt=attempt + 1, last_error_type=error_type, retrying_in_s=delay_s,
        )
        storage.save_student_trace(batch_id, student_id, trace)
        # restart_clock=False so the elapsed time keeps counting across retries
        # instead of resetting each time and hiding how long this has really taken.
        storage.update_student_progress(
            batch_id, student_id, "ai_call",
            note=f"retry {attempt} - {error_type}", restart_clock=False,
        )

    t_stage = time.perf_counter()
    try:
        with perf_logging.timed(timings, "ai_call_s"):
            result = ollama_client.generate_json(
                messages,
                model=selected_model,
                on_retry=_on_retry,
                hosts=ctx.hosts if ctx else None,
            )
        report_json = result["parsed"]
        attempts = result["attempts"]
        ai_metrics = result["metrics"]
        raw_text = result.get("raw_text")
        host_used = result.get("host")
    except ollama_client.OllamaUnavailableError as exc:
        # Deliberately propagated instead of falling back here. report_queue catches
        # it, requeues this student with backoff and hands the worker the next one --
        # so a student that cannot be generated right now delays only itself. The
        # trace is left showing exactly where and why it stopped.
        error_type = getattr(exc, "error_type", "unreachable")
        execution_trace.set_step(
            trace, "send_to_hermes", "error",
            code=execution_trace.get_code(ollama_client, "_call_host"),
            input_data=hermes_request_info,
            output_data={"error": str(exc)},
            extra={"last_error_type": error_type,
                   "failed_stage": execution_trace.STEP_LABELS["send_to_hermes"],
                   "queue_attempt": ctx.attempt if ctx else 1},
        )
        execution_trace.set_step(
            trace, "ai_processing", "error",
            code=execution_trace.get_code(ollama_client, "generate_json"),
            output_data={"error": str(exc)}, extra={"last_error_type": error_type},
            duration_s=time.perf_counter() - t_stage,
        )
        storage.save_student_trace(batch_id, student_id, trace)
        raise
    ai_call_duration = time.perf_counter() - t_stage
    storage.update_student_progress(batch_id, student_id, "ai_call", note=None)

    retry_summary = dict(trace.get("send_to_hermes", {}).get("extra") or {})
    retry_summary.pop("retrying_in_s", None)
    if prior_failure:
        retry_summary.update(prior_failure)
    execution_trace.set_step(
        trace, "send_to_hermes", "done" if host_used else "error",
        code=execution_trace.get_code(ollama_client, "_call_host"),
        input_data=hermes_request_info,
        output_data={"host_used": host_used} if host_used else {"error": failure_reason},
        # Carries the retry history forward onto the finished step, so the dashboard
        # still shows that (say) attempt 4 succeeded after three "unreachable"
        # failures, instead of a clean "done" that hides what it took to get there.
        extra={**retry_summary, "attempts": attempts} if retry_summary else None,
    )
    execution_trace.set_step(
        trace, "ai_processing", "done" if (report_json is not None or raw_text) else "error",
        code=execution_trace.get_code(ollama_client, "generate_json"),
        output_data={"attempts": attempts, "retries": max(attempts - 1, 0)},
        duration_s=ai_call_duration,
        extra=ai_metrics,
    )
    execution_trace.set_step(
        trace, "response", "done" if raw_text else "error",
        code=execution_trace.get_code(ollama_client, "generate_raw"),
        output_data={"raw_text": raw_text} if raw_text else {"error": failure_reason},
        extra=ai_metrics,
    )
    storage.save_student_trace(batch_id, student_id, trace)

    # json_parse_s is measured inside generate_json (per attempt) and surfaced via
    # ai_metrics -- it's part of the ai_call_s wall-clock time above, not a separate
    # stage, since parsing happens inline between retries rather than after them all.
    if ai_metrics and "json_parse_s" in ai_metrics:
        timings["json_parse_s"] = ai_metrics["json_parse_s"]

    execution_trace.set_step(
        trace, "parse", "done" if report_json is not None else "error",
        code=execution_trace.get_code(ollama_client, "_try_parse_json"),
        input_data={"raw_text": raw_text},
        # dict(...) snapshot -- report_json is mutated below (adding "_perf"), and
        # since Python dicts are stored by reference, the trace's "parse" output
        # would otherwise silently pick up those later additions too, making it look
        # like the model itself returned them.
        output_data=dict(report_json) if report_json is not None else {"error": "not valid JSON"},
        duration_s=(ai_metrics or {}).get("json_parse_s"),
    )
    storage.save_student_trace(batch_id, student_id, trace)

    if report_json is None:
        # Only reached once ollama_client has exhausted its whole retry budget, so
        # this is a genuinely persistent failure rather than a single bad call.
        log.warning("Falling back to templated report for %s: %s", student_id, failure_reason)
        if is_qa_request:
            # The listing itself never depended on the AI -- keep it and just note
            # that the written advice couldn't be generated this time.
            report_json = {"qa_listing": qa_listing}
        else:
            report_json = prompt_builder.build_fallback_report(student_record, mapping)
        # Kept out of the PDF (pdf_generator drops "_"-prefixed keys) but saved to the
        # JSON so Explore shows *why* this student got the fallback, not just that it
        # happened -- otherwise diagnosing this requires reading server-side logs.
        report_json["_generation_error"] = failure_reason
    elif is_qa_request:
        # Merge the AI's advice with the Python-built listing rather than trusting the
        # model to reproduce the listing itself -- guarantees qa_listing stays exactly
        # what was actually parsed from the CSV, regardless of what the model returns.
        report_json = {"qa_listing": qa_listing, **report_json}

    storage.update_student_progress(batch_id, student_id, "pdf_generate")
    t_stage = time.perf_counter()
    with perf_logging.timed(timings, "pdf_generate_s"):
        pdf_path = storage.student_pdf_path(batch_id, student_id)
        pdf_content = {k: v for k, v in report_json.items() if not k.startswith("_")}
        pdf_generator.generate_pdf(student_record["identity"], pdf_content, pdf_path)
    pdf_size = os.path.getsize(pdf_path) if os.path.exists(pdf_path) else 0
    execution_trace.set_step(
        trace, "generate_pdf", "done",
        code=execution_trace.get_code(pdf_generator, "generate_pdf"),
        input_data=pdf_content,
        output_data={"path": pdf_path, "size_bytes": pdf_size},
        duration_s=time.perf_counter() - t_stage,
    )
    storage.save_student_trace(batch_id, student_id, trace)

    timings["total_s"] = round(time.perf_counter() - t_total, 4)
    # Kept out of the PDF (pdf_generator drops "_"-prefixed keys) but saved to the
    # JSON so Explore can show timing/token data in the UI instead of requiring
    # server-log access.
    report_json["_perf"] = {
        "attempts": attempts,
        "retries": max(attempts - 1, 0),
        "stages_s": timings,
        "ai_metrics": ai_metrics,
    }

    storage.update_student_progress(batch_id, student_id, "file_save")
    t_stage = time.perf_counter()
    with perf_logging.timed(timings, "file_save_s"):
        json_path = storage.save_student_report(batch_id, student_id, report_json)
        storage.update_student_status(batch_id, student_id, "done")
    execution_trace.set_step(
        trace, "save", "done",
        code=execution_trace.get_code(storage, "save_student_report"),
        output_data={"json_path": json_path, "pdf_path": pdf_path},
        duration_s=time.perf_counter() - t_stage,
    )
    storage.save_student_trace(batch_id, student_id, trace)

    perf_logging.log_student_generation(batch_id, student_id, timings, attempts, ai_metrics)


@app.get("/batch/<batch_id>")
def batch_page(batch_id):
    try:
        manifest = storage.load_manifest(batch_id)
    except FileNotFoundError:
        abort(404)
    return render_template("students.html", batch_id=batch_id, students=manifest["students"])


@app.get("/batch/<batch_id>/students")
def batch_students(batch_id):
    try:
        manifest = storage.load_manifest(batch_id)
    except FileNotFoundError:
        abort(404)
    return jsonify(storage.with_live_progress(manifest))


@app.get("/batch/<batch_id>/students/<student_id>")
def student_report_json(batch_id, student_id):
    try:
        report = storage.load_student_report(batch_id, student_id)
    except FileNotFoundError:
        abort(404)
    return jsonify(report)


@app.get("/batch/<batch_id>/students/<student_id>/trace")
def student_trace(batch_id, student_id):
    try:
        trace = storage.load_student_trace(batch_id, student_id)
    except FileNotFoundError:
        trace = execution_trace.new_trace()
    return jsonify(execution_trace.with_live_elapsed(trace))


@app.get("/batch/<batch_id>/students/<student_id>/dashboard")
def student_dashboard(batch_id, student_id):
    try:
        manifest = storage.load_manifest(batch_id)
    except FileNotFoundError:
        abort(404)
    student = next((s for s in manifest["students"] if s["student_id"] == student_id), None)
    if not student:
        abort(404)
    return render_template(
        "dashboard.html",
        batch_id=batch_id,
        student_id=student_id,
        student_name=student["name"],
        steps=execution_trace.TRACE_STEPS,
    )


@app.get("/batch/<batch_id>/students/<student_id>/pdf")
def student_pdf(batch_id, student_id):
    pdf_path = storage.student_pdf_path(batch_id, student_id)
    if not os.path.exists(pdf_path):
        abort(404)
    as_attachment = request.args.get("download") == "1"
    return send_file(pdf_path, mimetype="application/pdf", as_attachment=as_attachment,
                      download_name=f"{student_id}.pdf")


@app.get("/batch/<batch_id>/zip")
def download_zip(batch_id):
    try:
        manifest = storage.load_manifest(batch_id)
    except FileNotFoundError:
        abort(404)

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for student in manifest["students"]:
            pdf_path = storage.student_pdf_path(batch_id, student["student_id"])
            if os.path.exists(pdf_path):
                zf.write(pdf_path, arcname=f"{student['student_id']}.pdf")
    buffer.seek(0)
    return send_file(buffer, mimetype="application/zip", as_attachment=True,
                      download_name=f"{batch_id}_reports.zip")


# ===========================================================================
# QWEN VALIDATION STUDY -- see validation.md
#
# Appended as a continuation of this file rather than a new module, per the
# study spec. Everything below is additive: no report-generation code above it
# changes behaviour, and none of the study runs unless app.py is started with
# --mode, so `python app.py` still just serves the web app.
#
# What it adds: after a report is generated, the JSON it produced (not the PDF)
# is validated in two steps -- a structural check in Python, then a content
# check by the same model against the student's own answers -- with failures
# requeued FIFO and every attempt recorded.
# ===========================================================================

import argparse   # noqa: E402  -- study-only imports, kept with the study code
import json       # noqa: E402
import requests   # noqa: E402
from collections import deque             # noqa: E402
from datetime import datetime, timezone   # noqa: E402

# --- study configuration ---------------------------------------------------

# Which call path generation uses. --mode agent|direct overrides this at the CLI.
USE_AGENT = True

# Only qwen3.5:4b runs actively for this study, in both roles -- generation and
# validation. The Hermes call the app would otherwise make is commented out
# rather than deleted, so restoring it is a one-line change.
# STUDY_GENERATION_MODEL = config.OLLAMA_HERMES3_MODEL   # Hermes -- disabled for this study
STUDY_GENERATION_MODEL = os.environ.get("STUDY_MODEL", "qwen3.5:4b")
STUDY_VALIDATION_MODEL = STUDY_GENERATION_MODEL   # same model, two roles

# The two call paths being compared. Same model, same prompt, same validation --
# the ONLY difference is how the generation request reaches Ollama.
STUDY_PATHS = {
    "agent": ("agent  -- qwen3.5:4b through the app's own ollama_client wrapper "
               "(the existing Hermes-era agent call: streaming, host failover, "
               "per-turn retry with backoff, Ollama token/timing metrics)"),
    "direct": ("direct -- qwen3.5:4b via one raw HTTP POST to Ollama's /api/chat "
                "(no wrapper, no streaming, no retry, no failover)"),
}

# Which path the VALIDATOR uses -- deliberately fixed, and deliberately not tied
# to USE_AGENT. The spec is explicit that the flag "only changes how generation
# calls are made": if --mode switched the validator too, an agent-vs-direct
# comparison would be moving two variables at once and neither timing nor
# catch-rate would be attributable to the generation path. Held on the agent
# wrapper because it retries and fails over, so a validator hiccup does not get
# mistaken for a content failure.
STUDY_VALIDATION_USES_AGENT = True

STUDY_RETRY_CAP = 3          # total attempts per candidate, then needs_review
STUDY_MIN_TEXT_CHARS = 40    # below this a narrative field reads as truncated
STUDY_LOG_PATH = os.path.join(config.BASE_DIR, "output", "validation_study.jsonl")
STUDY_SUMMARY_PATH = os.path.join(config.BASE_DIR, "output", "validation_summary.json")

# The schema is the Personal Learning Growth Report template -- the same field
# names validation.md was written against (dimensions, single_priority, tier,
# strong/focus/blindspot). Taken from prompt_builder rather than restated, so the
# validator cannot drift from what the model is actually asked to produce.
STUDY_REQUIRED_FIELDS = ("intro_message", "dimensions", "single_priority")
STUDY_CARD_FIELDS = {          # array field -> keys each card must carry
    "strong": ("headline", "body"),
    "focus": ("headline", "body", "action"),
    "blindspot": ("headline", "body", "action"),
}
STUDY_ALLOWED_TIERS = prompt_builder.TIERS

# One deviation from validation.md worth being explicit about: it asks for
# `score` to be numeric and within 0-100, but the template it describes is
# qualitative throughout ("qualitative bands, not numeric or percentile") and has
# no score field anywhere. There is nothing to range-check, so that rule is
# applied where it actually lands in this schema -- `tier`, which must be one of
# a closed set of four values ("Tier/category fields contain only an allowed
# fixed value"). A numeric score appearing in output is treated as a failure,
# since the template has nowhere to render it.
STUDY_MIN_HEADLINE_CHARS = 8


# --- GPU / CPU verification ------------------------------------------------

def study_gpu_status(hosts=None, timeout=(3, 5)):
    """
    The equivalent of `ollama ps`'s PROCESSOR column, read over HTTP so it works
    against the remote host instead of only on the box itself.

    `ollama ps` derives PROCESSOR from the same numbers /api/ps returns: weights
    entirely in VRAM show "100% GPU", none in VRAM shows "100% CPU", and a
    partial offload shows the split. size_vram is the authoritative signal --
    Ollama falls back to CPU silently, so a host counts as GPU only when it
    actually reports weights resident in VRAM.

    Never raises: an unreachable host is a result worth logging, not an
    exception that should abort a run. Returns processor=None when nothing is
    loaded, because with no resident model there is genuinely nothing to read --
    that is unknown, not CPU.
    """
    last_error = last_host = None
    for host in (hosts or config.get_active_hosts()):
        try:
            resp = requests.get(f"{host}/api/ps", timeout=timeout)
            resp.raise_for_status()
            models = resp.json().get("models", [])
        except (requests.ConnectionError, requests.Timeout, requests.HTTPError,
                ValueError) as exc:
            # Fall through to the next host rather than reporting the first
            # failure as the answer. OLLAMA_HOSTS is a failover chain, so with
            # the LAN address down but WireGuard up, returning here would show
            # the host as dead while generation was in fact working fine.
            last_error, last_host = exc, host
            continue
        if not models:
            return {"host": host, "reachable": True, "processor": None, "on_gpu": None,
                    "detail": "no model resident -- run a generation first"}
        model = models[0]
        size = model.get("size") or 0
        vram = model.get("size_vram") or 0
        pct = round(100 * vram / size) if size else 0
        if pct >= 99:
            processor = "100% GPU"
        elif pct <= 0:
            processor = "100% CPU"
        else:
            processor = f"{pct}% GPU / {100 - pct}% CPU"
        return {"host": host, "reachable": True, "processor": processor,
                "on_gpu": vram > 0, "model": model.get("name"),
                "size_bytes": size, "size_vram_bytes": vram,
                "detail": f"{vram / 1e9:.2f}GB of {size / 1e9:.2f}GB in VRAM"}
    if last_error is not None:
        return {"host": last_host, "reachable": False, "processor": None, "on_gpu": None,
                "detail": f"{ollama_client._classify(last_error)}: {str(last_error)[:120]}"}
    return {"host": None, "reachable": False, "processor": None, "on_gpu": None,
            "detail": "no hosts configured"}


def nvidia_smi_utilization():
    """
    The optional cross-check from validation.md: GPU utilisation straight from
    the driver rather than from Ollama's own accounting.

    Only works when this process is running ON the GPU box -- nvidia-smi is a
    local tool with no remote equivalent, and the app normally runs on a
    different machine from Ollama. Returns None when it is not available, which
    is the expected case; that is a "could not cross-check", not a failure, so
    size_vram from study_gpu_status stays the authoritative signal.
    """
    import shutil
    import subprocess

    if shutil.which("nvidia-smi") is None:
        return None
    try:
        out = subprocess.run(
            ["nvidia-smi",
             "--query-gpu=utilization.gpu,memory.used,memory.total",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=10, check=True,
        ).stdout.strip()
    except (subprocess.SubprocessError, OSError):
        return None
    gpus = []
    for line in out.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) == 3:
            gpus.append({"utilization_pct": parts[0],
                          "memory_used_mb": parts[1],
                          "memory_total_mb": parts[2]})
    return gpus or None


@app.get("/gpu-status")
def gpu_status():
    """Backs the "Running on:" badge on Screen 1, checked before each run."""
    status = study_gpu_status()
    smi = nvidia_smi_utilization()
    if smi:
        status["nvidia_smi"] = smi
    return jsonify(status)


# --- step 1: structural validation -----------------------------------------

def validate_structure(report_json, mapping, required_fields=None):
    """
    Step 1, pure Python, always runs before the model validator.

    Returns a list of specific error strings (empty means it passed). Specific
    is the point -- "missing field: summary" and "score out of range: 142" are
    what get fed back into the retry prompt, so a vague message costs the model
    its only clue about what to fix.
    """
    errors = []
    if not isinstance(report_json, dict):
        return [f"report is not a JSON object (got {type(report_json).__name__})"]

    def text_ok(value, where, minimum=STUDY_MIN_TEXT_CHARS):
        if value is None:
            errors.append(f"missing field: {where}")
        elif not isinstance(value, str):
            errors.append(f"wrong type: {where} is {type(value).__name__}, expected string")
        elif not value.strip():
            errors.append(f"empty field: {where}")
        elif len(value.strip()) < minimum:
            errors.append(f"text too short (possible truncation): {where} = "
                           f"{len(value.strip())} chars, minimum {minimum}")

    for field in (required_fields or STUDY_REQUIRED_FIELDS):
        if field not in report_json:
            errors.append(f"missing field: {field}")

    if "intro_message" in report_json:
        text_ok(report_json["intro_message"], "intro_message")

    # --- dimensions: the profile on page 1, with the tier pills ---------------
    dims = report_json.get("dimensions")
    if dims is not None:
        if not isinstance(dims, list):
            errors.append(f"wrong type: dimensions is {type(dims).__name__}, expected array")
        elif not dims:
            errors.append("empty array: dimensions")
        else:
            for i, dim in enumerate(dims):
                if not isinstance(dim, dict):
                    errors.append(f"wrong type: dimensions[{i}] is "
                                   f"{type(dim).__name__}, expected object")
                    continue
                text_ok(dim.get("name"), f"dimensions[{i}].name", minimum=3)
                text_ok(dim.get("description"), f"dimensions[{i}].description")
                tier = dim.get("tier")
                if tier is None:
                    errors.append(f"missing field: dimensions[{i}].tier")
                elif not isinstance(tier, str):
                    errors.append(f"wrong type: dimensions[{i}].tier is "
                                   f"{type(tier).__name__}, expected string")
                elif tier.strip() not in STUDY_ALLOWED_TIERS:
                    errors.append(f"tier not an allowed value: {tier!r} at "
                                   f"dimensions[{i}] (allowed: "
                                   f"{list(STUDY_ALLOWED_TIERS)})")

    # --- the page 2 card arrays ----------------------------------------------
    for field, required_keys in STUDY_CARD_FIELDS.items():
        if field not in report_json:
            continue
        value = report_json[field]
        if not isinstance(value, list):
            errors.append(f"wrong type: {field} is {type(value).__name__}, expected array")
            continue
        if not value:
            # blindspot is the one array allowed to be empty: the schema says only
            # to include a blind spot the answers actually evidence, so forcing one
            # would be asking the model to invent it.
            if field != "blindspot":
                errors.append(f"empty array: {field}")
            continue
        for i, card in enumerate(value):
            if not isinstance(card, dict):
                errors.append(f"wrong type: {field}[{i}] is "
                               f"{type(card).__name__}, expected object")
                continue
            for key in required_keys:
                minimum = STUDY_MIN_HEADLINE_CHARS if key == "headline" else STUDY_MIN_TEXT_CHARS
                text_ok(card.get(key), f"{field}[{i}].{key}", minimum=minimum)

    # --- single_priority: the required single-object field --------------------
    priority = report_json.get("single_priority")
    if priority is not None:
        if not isinstance(priority, dict):
            errors.append(f"wrong type: single_priority is "
                           f"{type(priority).__name__}, expected object")
        elif not priority:
            errors.append("empty object: single_priority")
        else:
            text_ok(priority.get("headline"), "single_priority.headline",
                     minimum=STUDY_MIN_HEADLINE_CHARS)
            text_ok(priority.get("body"), "single_priority.body")

    # --- nothing numeric anywhere --------------------------------------------
    # The template is explicit that the bands are qualitative, so a score or
    # percentage the model has invented has nowhere to render and implies a
    # precision the answers do not support.
    for stray in ("score", "scores", "section_scores", "percentage", "percentile",
                   "rating", "overall_score"):
        if stray in report_json:
            errors.append(f"unexpected numeric field: {stray} -- this report is "
                           f"qualitative and the template has no place for it")

    return errors


# --- step 2: model validation ----------------------------------------------

def build_validation_messages(report_json, student_record, mapping, question_bank):
    """
    Asks the model whether the report is supported by the student's own answers.

    The source answers are handed over in exactly the shape the generation call
    used (prompt_builder's own payload builder), so "traceable to what the
    student actually answered" means traceable to the same text the generator
    saw, not to a paraphrase of it that could differ in a way that matters.
    """
    system = "\n".join([
        "You are validating a generated student report for factual accuracy.",
        "",
        "You are given SOURCE_ANSWERS (what the student actually answered) and "
        "REPORT (what was generated about them). REPORT is qualitative -- "
        "dimensions with a tier label, not a numeric score. Check ONLY these "
        "three things:",
        "1. Hallucinated claims -- every statement in REPORT must be traceable to "
        "SOURCE_ANSWERS. Flag anything asserted that the source does not support.",
        "2. Claims about skills, actions, experience or achievements the student "
        "did not actually report doing.",
        "3. Tier consistency -- each dimension's tier (Strength / Developing / "
        "Focus Required / Blind Spot) must be defensible from the answers to that "
        "dimension. Flag a clear mismatch (e.g. 'Strength' where the answers show "
        "little or no evidence of it, or 'Focus Required' where the answers show "
        "clear strength), not a borderline judgement call between two adjacent tiers.",
        "",
        "Do NOT flag tone, encouragement, writing style, formatting, or advice about "
        "what the student could do next. Advice is not a factual claim about them.",
        "",
        'Respond with ONLY this JSON: {"verdict": "pass" | "fail", "errors": ["..."]}',
        'On pass, "errors" must be an empty array. On fail, each entry is one short, '
        "specific sentence naming the offending claim or dimension/tier.",
    ])
    payload = {
        "SOURCE_ANSWERS": json.loads(
            prompt_builder._build_student_payload(student_record, mapping, question_bank)),
        "REPORT": report_json,
    }
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False,
                                                separators=(",", ":"))},
    ]


def validate_with_model(report_json, student_record, mapping, question_bank, hosts=None):
    """
    Step 2. Returns (errors, elapsed_s, raw_text). Empty errors means it passed.

    Runs on STUDY_VALIDATION_USES_AGENT no matter which path generation used --
    --mode changes generation only, so the validator stays a constant across both
    runs and the comparison measures what it claims to.
    """
    messages = build_validation_messages(report_json, student_record, mapping, question_bank)
    t0 = time.perf_counter()
    try:
        raw, _host, _metrics = study_generate(messages, STUDY_VALIDATION_MODEL,
                                               STUDY_VALIDATION_USES_AGENT, hosts)
    except Exception as exc:  # noqa: BLE001 -- a dead validator is a result, not a crash
        return [f"validator call failed: {exc}"], round(time.perf_counter() - t0, 3), None
    elapsed = round(time.perf_counter() - t0, 3)

    parsed = ollama_client._try_parse_json(raw)
    if not isinstance(parsed, dict) or "verdict" not in parsed:
        return ["validator did not return a usable verdict"], elapsed, raw
    if str(parsed.get("verdict", "")).strip().lower() == "pass":
        return [], elapsed, raw
    errors = parsed.get("errors") or ["validator returned fail with no reason given"]
    if isinstance(errors, str):
        errors = [errors]
    return [str(e) for e in errors], elapsed, raw


# --- on-demand validation for an already-generated batch (web UI) ----------
#
# Reuses validate_structure/validate_with_model above -- the same two checks the
# study runs -- against reports that already exist on disk, instead of also
# regenerating them. No agent-vs-direct comparison, no corrupted-sample
# injection, no GPU probing: this is the practical "is this batch OK" page, not
# the research study those exist for.

def _run_batch_validation(batch_id):
    """
    Runs sequentially, one model call per student needing a content check -- same
    reasoning as REPORT_WORKERS' default in config.py: a single CPU-bound Ollama
    host gains nothing from concurrent requests, they just queue behind each other.
    Writes validation.json after every student so /batch/<id>/validate/status
    always reflects real progress, not just the final result.
    """
    manifest = storage.load_manifest(batch_id)
    mapping = csv_ingest.load_section_mapping()
    done = [s for s in manifest["students"] if s["status"] == "done"]

    # Only batches generated after this feature shipped have source answers saved
    # per student -- older ones simply have nothing here, and content_checked
    # stays False for every student below rather than the run failing.
    records_by_id = {}
    for s in done:
        try:
            records_by_id[s["student_id"]] = storage.load_student_record(batch_id, s["student_id"])
        except FileNotFoundError:
            pass
    question_bank = (
        prompt_builder.build_question_bank(mapping, list(records_by_id.values()))
        if records_by_id else None
    )

    validation = {
        "batch_id": batch_id,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "finished_at": None,
        "students": [{"student_id": s["student_id"], "name": s["name"], "status": "pending"}
                     for s in done],
    }
    storage.save_batch_validation(batch_id, validation)

    for entry in validation["students"]:
        sid = entry["student_id"]
        entry["status"] = "running"
        storage.save_batch_validation(batch_id, validation)

        try:
            report_json = storage.load_student_report(batch_id, sid)
        except FileNotFoundError:
            entry.update(status="error", passed=False, structural_ok=None,
                         structural_errors=["report JSON not found on disk"],
                         content_checked=False, content_ok=None, content_errors=[])
            storage.save_batch_validation(batch_id, validation)
            continue

        # Same "_"-prefixed keys pdf_generator drops (_perf, _generation_error) --
        # those are this app's own bookkeeping, not part of the report being checked.
        report_content = {k: v for k, v in report_json.items() if not k.startswith("_")}
        structural_errors = validate_structure(report_content, mapping)

        record = records_by_id.get(sid)
        content_errors, content_checked = [], False
        if not structural_errors and record is not None:
            content_errors, _elapsed, _raw = validate_with_model(
                report_content, record, mapping, question_bank)
            content_checked = True

        entry.update(
            status="done",
            passed=not structural_errors and (not content_checked or not content_errors),
            structural_ok=not structural_errors,
            structural_errors=structural_errors,
            content_checked=content_checked,
            content_ok=(not content_errors) if content_checked else None,
            content_errors=content_errors,
        )
        storage.save_batch_validation(batch_id, validation)

    validation["finished_at"] = datetime.now(timezone.utc).isoformat()
    storage.save_batch_validation(batch_id, validation)


@app.get("/validate")
def validate_picker():
    """The batch picker -- lists every batch that has at least one generated
    report, newest first (storage.list_batches() already sorts that way)."""
    batches = []
    for batch_id in storage.list_batches():
        try:
            m = storage.load_manifest(batch_id)
        except FileNotFoundError:
            continue
        done_count = sum(1 for s in m["students"] if s["status"] == "done")
        batches.append({
            "batch_id": batch_id,
            "total": len(m["students"]),
            "done": done_count,
        })
    return render_template("validate.html", batches=batches)


@app.post("/batch/<batch_id>/validate")
def start_batch_validation(batch_id):
    try:
        storage.load_manifest(batch_id)
    except FileNotFoundError:
        abort(404)
    thread = threading.Thread(target=_run_batch_validation, args=(batch_id,), daemon=True)
    thread.start()
    return jsonify({"status": "started"})


@app.get("/batch/<batch_id>/validate")
def batch_validate_page(batch_id):
    try:
        storage.load_manifest(batch_id)
    except FileNotFoundError:
        abort(404)
    return render_template("validate_results.html", batch_id=batch_id)


@app.get("/batch/<batch_id>/validate/status")
def batch_validate_status(batch_id):
    """
    started=False (no validation.json yet) is what tells the results page's JS to
    POST /batch/<id>/validate itself on first load -- so a plain link to this page,
    from either the picker or the batch's own students page, is enough to kick a
    run off; revisiting a finished one just shows the stored result instead of
    starting over.
    """
    try:
        validation = storage.load_batch_validation(batch_id)
    except FileNotFoundError:
        return jsonify({"batch_id": batch_id, "started": False, "finished_at": None,
                         "students": []})
    validation["started"] = True
    return jsonify(validation)


# --- generation call paths (agent vs direct) -------------------------------

def study_generate(messages, model, use_agent, hosts=None):
    """Both paths use the same model; only how the call is made differs."""
    if use_agent:
        return _study_generate_agent(messages, model, hosts)
    return _study_generate_direct(messages, model, hosts)


def _study_generate_agent(messages, model, hosts=None):
    """
    Agent path: the same model through the app's own ollama_client wrapper --
    streaming, host failover, per-turn retry with backoff, and Ollama's token and
    timing counters. This is the path production report generation already uses.
    """
    result = ollama_client.generate_json(messages, model=model, hosts=hosts)
    raw = result.get("raw_text") or json.dumps(result.get("parsed"), ensure_ascii=False)
    return raw, result.get("host"), result.get("metrics")


def _study_generate_direct(messages, model, hosts=None):
    """
    Direct path: one raw HTTP POST to /api/chat, no wrapper -- no streaming, no
    retry, no failover beyond trying the next host in the list.

    Worth knowing when comparing the two: stream=False leaves the connection
    silent for the whole prompt-eval plus generation window, which on this
    project's WireGuard path has already been measured long enough to trip a NAT
    idle timeout. If the direct path shows connection resets the agent path does
    not, that is the likely cause rather than the model behaving differently.
    """
    last_error = None
    for host in (hosts or config.get_active_hosts()):
        try:
            resp = requests.post(
                f"{host}/api/chat",
                json={
                    "model": model,
                    "messages": messages,
                    "stream": False,
                    "format": "json",
                    "keep_alive": config.OLLAMA_KEEP_ALIVE,
                    "options": {
                        "temperature": config.OLLAMA_TEMPERATURE,
                        "num_ctx": config.OLLAMA_NUM_CTX,
                        "num_predict": config.OLLAMA_NUM_PREDICT,
                    },
                },
                timeout=(ollama_client.CONNECT_TIMEOUT_SECONDS,
                          ollama_client.READ_TIMEOUT_SECONDS),
            )
            resp.raise_for_status()
            data = resp.json()
            return (data["message"]["content"], host,
                    ollama_client._extract_metrics(data))
        except (requests.ConnectionError, requests.Timeout, requests.HTTPError,
                ValueError, KeyError) as exc:
            last_error = exc
            continue
    error = ollama_client.OllamaUnavailableError(
        f"direct call failed on every host: {last_error}")
    error.error_type = (ollama_client._classify(last_error)
                         if last_error else "unreachable")
    raise error


# --- logging ---------------------------------------------------------------

def _study_log(entry):
    """
    One JSON object per line, appended. Every attempt is written as it happens
    rather than buffered to the end, so a run that is interrupted still leaves
    the attempts it did complete on record -- and failed attempts stay on record
    even after a candidate later passes, which is the point for an audit of a
    student-facing report.
    """
    os.makedirs(os.path.dirname(STUDY_LOG_PATH), exist_ok=True)
    entry = {"ts": datetime.now(timezone.utc).isoformat(), **entry}
    with open(STUDY_LOG_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
    return entry


# --- retry prompt ----------------------------------------------------------

def _study_retry_messages(base_messages, previous_output, reasons):
    """
    Full regeneration, never a patch.

    The model gets the original prompt again in full, plus what it produced last
    time and exactly why that was rejected, and is asked to produce the entire
    report again. Asking it to fix one field invites an edit that no longer
    agrees with the rest of the report -- a corrected score with a narrative
    still describing the old one is worse than either.
    """
    reason_text = "; ".join(reasons) if reasons else "unspecified validation failure"
    return list(base_messages) + [
        {"role": "assistant", "content": previous_output or "(no output produced)"},
        {"role": "user", "content":
            f"That output failed validation for this reason: {reason_text}\n\n"
            "Regenerate the COMPLETE report from scratch using the same source "
            "answers given above. Do not patch or edit the previous output -- "
            "produce the whole report again, corrected. Every statement must be "
            "supported by the student's actual answers. Output ONLY valid JSON."},
    ]


# --- deliberate bad sample -------------------------------------------------

# An unambiguous fabrication: nothing in the survey asks about internships of
# this kind, so no student's answers can support it. Kept as a named constant so
# the test is defined explicitly in code rather than left to chance.
STUDY_BAD_SAMPLE_CLAIM = (
    "She has completed a six-month industrial internship at a multinational "
    "semiconductor firm, where she led a team of twelve engineers and shipped "
    "a production compiler toolchain."
)


def make_bad_sample(report_json):
    """
    Takes a real, valid report and corrupts exactly one field by appending a
    claim about something the student did not do. Targets step 2 -- it is
    structurally perfect, so only the content check can catch it.

    Returns (corrupted_report, field_name, what_was_injected).
    """
    bad = json.loads(json.dumps(report_json))   # deep copy, leave the original intact

    # intro_message is the natural target: free prose, addressed to the student,
    # and structurally identical whether or not the claim in it is true -- so the
    # corrupted report passes step 1 and only step 2 can catch it.
    if isinstance(bad.get("intro_message"), str) and bad["intro_message"].strip():
        bad["intro_message"] = f"{bad['intro_message']} {STUDY_BAD_SAMPLE_CLAIM}".strip()
        return bad, "intro_message", STUDY_BAD_SAMPLE_CLAIM

    for field in ("strong", "focus", "blindspot"):
        cards = bad.get(field)
        if isinstance(cards, list) and cards and isinstance(cards[0], dict) \
                and isinstance(cards[0].get("body"), str):
            cards[0]["body"] = f"{cards[0]['body']} {STUDY_BAD_SAMPLE_CLAIM}".strip()
            return bad, f"{field}[0].body", STUDY_BAD_SAMPLE_CLAIM

    # Nothing to inject prose into -- fall back to a structural corruption, which
    # step 1 catches instead.
    dims = bad.get("dimensions")
    if isinstance(dims, list) and dims and isinstance(dims[0], dict):
        dims[0]["tier"] = "Exceptional"      # not one of the four allowed tiers
        return bad, "dimensions[0].tier", "tier flipped to a disallowed value"
    return bad, None, None


def run_bad_sample_check(valid_report, student_record, mapping, question_bank, hosts=None):
    """Runs the corrupted candidate through the full pipeline and records the outcome."""
    bad, field, injected = make_bad_sample(valid_report)
    if field is None:
        return {"ran": False, "reason": "no valid report available to corrupt"}

    structural = validate_structure(bad, mapping)
    if structural:
        return {"ran": True, "corrupted_field": field, "injected": injected,
                "caught": True, "caught_at": "step 1 (structural)",
                "errors": structural}

    errors, elapsed, _raw = validate_with_model(
        bad, student_record, mapping, question_bank, hosts)
    return {"ran": True, "corrupted_field": field, "injected": injected,
            "caught": bool(errors), "caught_at": "step 2 (model)" if errors else None,
            "errors": errors, "validation_time_s": elapsed}


# --- the study runner ------------------------------------------------------

def run_validation_study(students, mapping, use_agent=None, instructions_text="",
                          limit=None, hosts=None, run_bad_sample=True):
    """
    Generate and validate each candidate, retrying failures through a FIFO queue.

    A failed candidate is appended to the END of the queue rather than retried in
    place, so it never blocks the candidates behind it and no one gets skipped.
    This is a plain deque rather than core.report_queue on purpose: that queue
    demotes retries to a lower priority band and applies its own backoff, which
    would change the arrival order this study is specified to keep.

    Returns a summary dict; also writes one JSONL line per attempt as it goes.
    """
    use_agent = USE_AGENT if use_agent is None else use_agent
    method = "agent" if use_agent else "direct"
    records = students[:limit] if limit else list(students)
    question_bank = prompt_builder.build_question_bank(mapping, records)
    run_id = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")

    pending = deque()
    for i, record in enumerate(records):
        pending.append({
            "index": i,
            "candidate_id": storage.make_student_id(record["identity"], i),
            "record": record,
            "attempt": 0,
            "history": [],
            "last_output": None,
        })

    results, structural_failures, validation_failures, needs_review = [], [], [], []
    first_valid = None
    started = time.perf_counter()

    log.info("validation study %s: %d candidate(s), model=%s", run_id, len(pending),
              STUDY_GENERATION_MODEL)
    log.info("  generation path: %s", STUDY_PATHS[method])
    log.info("  validation path: %s (fixed -- --mode changes generation only)",
              STUDY_PATHS["agent" if STUDY_VALIDATION_USES_AGENT else "direct"])

    while pending:
        cand = pending.popleft()
        cand["attempt"] += 1
        attempt = cand["attempt"]
        # Checked before every generation, not once per run: a model can be
        # evicted and reloaded onto different hardware partway through a batch.
        gpu = study_gpu_status(hosts)
        smi = nvidia_smi_utilization()
        if smi:
            gpu = {**gpu, "nvidia_smi": smi}

        base_messages = prompt_builder.build_messages(
            cand["record"], mapping, instructions_text, question_bank)
        messages = base_messages
        if attempt > 1:
            messages = _study_retry_messages(
                base_messages, cand["last_output"],
                cand["history"][-1].get("errors") if cand["history"] else None)

        t0 = time.perf_counter()
        gen_error = None
        raw = parsed = host = metrics = None
        try:
            raw, host, metrics = study_generate(
                messages, STUDY_GENERATION_MODEL, use_agent, hosts)
            parsed = ollama_client._try_parse_json(raw)
            if parsed is None:
                gen_error = "model output was not valid JSON"
        except Exception as exc:  # noqa: BLE001 -- recorded, then retried
            gen_error = f"generation failed: {exc}"
        gen_s = round(time.perf_counter() - t0, 3)

        structural_errors, validation_errors, val_s = [], [], None
        if gen_error:
            structural_errors = [gen_error]
        else:
            structural_errors = validate_structure(parsed, mapping)
            if not structural_errors:
                validation_errors, val_s, _ = validate_with_model(
                    parsed, cand["record"], mapping, question_bank, hosts)

        errors = structural_errors or validation_errors
        passed = not errors
        total_s = round(time.perf_counter() - t0, 3)

        entry = _study_log({
            "run_id": run_id,
            "candidate_id": cand["candidate_id"],
            "attempt": attempt,
            "method": method,
            "model": STUDY_GENERATION_MODEL,
            "host": host,
            "generation_time_s": gen_s,
            "structural_ok": not structural_errors,
            "structural_errors": structural_errors,
            "validation_time_s": val_s,
            "validation_ok": bool(not structural_errors and not validation_errors),
            "validation_errors": validation_errors,
            "total_time_s": total_s,
            "gpu_status": gpu,
            "passed": passed,
            "ai_metrics": metrics,
        })
        cand["history"].append({"attempt": attempt, "errors": errors, **{
            k: entry[k] for k in ("generation_time_s", "validation_time_s", "total_time_s")}})
        cand["last_output"] = raw

        if structural_errors:
            structural_failures.extend(
                f"{cand['candidate_id']} (attempt {attempt}): {e}" for e in structural_errors)
        if validation_errors:
            validation_failures.extend(
                f"{cand['candidate_id']} (attempt {attempt}): {e}" for e in validation_errors)

        # Per-candidate row for the end-of-run summary. structural_result is the
        # outcome of step 1 on the FINAL attempt -- "pass" on a candidate that
        # needed three goes still means the last one was structurally clean, and
        # the earlier failures are in structural_failures and failure_history.
        row = {
            "candidate_id": cand["candidate_id"],
            "method": method,
            "attempts": attempt,
            "generation_time_s": gen_s,
            "structural_result": "fail" if structural_errors else "pass",
            "structural_errors": structural_errors,
            "validation_time_s": val_s,
            "validation_result": ("skipped" if structural_errors
                                   else ("fail" if validation_errors else "pass")),
            "validation_errors": validation_errors,
            "total_time_s": total_s,
            "gpu_status": gpu.get("processor"),
        }

        if passed:
            results.append({**row, "status": "valid"})
            if first_valid is None:
                first_valid = (parsed, cand["record"])
        elif attempt >= STUDY_RETRY_CAP:
            needs_review.append({
                "candidate_id": cand["candidate_id"],
                "attempts": attempt,
                "failure_history": cand["history"],
            })
            results.append({**row, "status": "needs_review"})
        else:
            pending.append(cand)   # to the END of the queue -- FIFO, no skipping

    bad_sample = {"ran": False, "reason": "no candidate passed, nothing valid to corrupt"}
    if run_bad_sample and first_valid is not None:
        valid_report, valid_record = first_valid
        bad_sample = run_bad_sample_check(
            valid_report, valid_record, mapping, question_bank, hosts)

    summary = {
        "run_id": run_id,
        "method": method,
        "generation_path": STUDY_PATHS[method],
        "validation_path": STUDY_PATHS["agent" if STUDY_VALIDATION_USES_AGENT else "direct"],
        "model": STUDY_GENERATION_MODEL,
        "candidates": len(records),
        "valid": sum(1 for r in results if r["status"] == "valid"),
        "needs_review_count": len(needs_review),
        "total_attempts": sum(r["attempts"] for r in results),
        "elapsed_s": round(time.perf_counter() - started, 2),
        "gpu_status": study_gpu_status(hosts),
        "results": results,
        "structural_failures": structural_failures,
        "validation_failures": validation_failures,
        "needs_review": needs_review,
        "bad_sample": bad_sample,
    }
    _study_log({"run_id": run_id, "event": "run_summary", **{
        k: v for k, v in summary.items() if k != "results"}})
    return summary


# --- run output ------------------------------------------------------------

def print_study_summary(summary):
    valid_times = [r["total_time_s"] for r in summary["results"] if r["status"] == "valid"]
    avg = round(sum(valid_times) / len(valid_times), 1) if valid_times else None
    gpu = summary["gpu_status"]

    print(f"\n{'=' * 72}")
    print(f"VALIDATION STUDY {summary['run_id']}  method={summary['method']}  "
           f"model={summary['model']}")
    print(f"{'=' * 72}")
    print(f"  generation path : {summary['generation_path']}")
    print(f"  validation path : {summary['validation_path']}")
    print(f"  running on      : {gpu.get('processor') or 'unknown'} "
           f"({gpu.get('detail')})")
    print(f"  candidates      : {summary['candidates']}")
    print(f"  valid           : {summary['valid']}")
    print(f"  needs review    : {summary['needs_review_count']}")
    print(f"  total attempts  : {summary['total_attempts']}")
    print(f"  elapsed         : {summary['elapsed_s']}s"
           + (f"   (avg {avg}s per valid candidate)" if avg else ""))

    print("\n  per candidate:")
    for r in summary["results"]:
        print(f"    {r['candidate_id'][:22]:<22} {r['status']:<13} "
               f"attempts={r['attempts']}  "
               f"gen={r['generation_time_s']}s  "
               f"struct={r['structural_result']:<4}  "
               f"val={r['validation_result']:<7} {r['validation_time_s']}s  "
               f"total={r['total_time_s']}s  [{r['gpu_status'] or 'unknown'}]")

    for title, items in (("STRUCTURAL FAILURES", summary["structural_failures"]),
                          ("VALIDATION FAILURES", summary["validation_failures"])):
        print(f"\n  {title} ({len(items)}):")
        for item in items or ["    (none)"]:
            print(f"    {item}" if items else item)

    print(f"\n  NEEDS REVIEW ({len(summary['needs_review'])}):")
    if not summary["needs_review"]:
        print("    (none)")
    for item in summary["needs_review"]:
        print(f"    {item['candidate_id']} after {item['attempts']} attempts:")
        for h in item["failure_history"]:
            print(f"       attempt {h['attempt']}: {'; '.join(h['errors'])}")

    bad = summary["bad_sample"]
    print("\n  BAD-SAMPLE TEST:")
    if not bad.get("ran"):
        print(f"    not run -- {bad.get('reason')}")
    else:
        print(f"    corrupted field : {bad['corrupted_field']}")
        print(f"    caught          : {'YES' if bad['caught'] else 'NO'}"
               + (f" at {bad['caught_at']}" if bad.get("caught_at") else ""))
        for e in bad.get("errors") or []:
            print(f"      - {e}")
    print(f"\n  full log: {STUDY_LOG_PATH}")


def compare_study_runs(summaries):
    """Which method was faster, and which was more accurate, from the data above."""
    print(f"\n{'=' * 72}")
    print("AGENT vs DIRECT")
    print(f"{'=' * 72}")
    for s in summaries:
        valid_times = [r["total_time_s"] for r in s["results"] if r["status"] == "valid"]
        avg = round(sum(valid_times) / len(valid_times), 1) if valid_times else None
        print(f"  {s['method']:<7} valid={s['valid']}/{s['candidates']}  "
               f"attempts={s['total_attempts']}  elapsed={s['elapsed_s']}s  "
               f"avg_per_valid={avg}  "
               f"bad_sample_caught={s['bad_sample'].get('caught')}")

    ranked = [s for s in summaries if s["valid"]]
    if len(ranked) < 2:
        print("\n  Not enough completed runs to compare.")
        return
    faster = min(ranked, key=lambda s: s["elapsed_s"])
    # Fewer attempts per valid candidate means fewer rejected outputs -- the
    # closest thing to an accuracy signal this run produces.
    def attempts_per_valid(s):
        return s["total_attempts"] / s["valid"]
    accurate = min(ranked, key=attempts_per_valid)
    print(f"\n  faster           : {faster['method']} ({faster['elapsed_s']}s)")
    print(f"  fewer retries    : {accurate['method']} "
           f"({attempts_per_valid(accurate):.2f} attempts per valid candidate)")


def _newest_uploaded_csv():
    paths = [os.path.join(config.UPLOAD_DIR, n)
             for n in os.listdir(config.UPLOAD_DIR) if n.lower().endswith(".csv")]
    if not paths:
        raise SystemExit(f"No CSV found in {config.UPLOAD_DIR} -- upload one first, "
                          f"or pass --csv")
    return max(paths, key=os.path.getmtime)


def run_study_cli(args):
    mapping = csv_ingest.load_section_mapping()
    csv_path = args.csv or _newest_uploaded_csv()
    with open(csv_path, "rb") as f:
        students = csv_ingest.parse_csv(f.read(), mapping)
    print(f"source CSV : {csv_path}  ({len(students)} students)")

    gpu = study_gpu_status()
    print(f"running on : {gpu.get('processor') or 'unknown'} -- {gpu.get('detail')}")
    if gpu.get("on_gpu") is False:
        print("  WARNING: generation is not using the GPU.")

    modes = ["agent", "direct"] if args.mode == "both" else [args.mode]
    summaries = []
    for mode in modes:
        summary = run_validation_study(
            students, mapping,
            use_agent=(mode == "agent"),
            instructions_text=args.instructions,
            limit=args.limit,
        )
        print_study_summary(summary)
        summaries.append(summary)

    if len(summaries) > 1:
        compare_study_runs(summaries)

    os.makedirs(os.path.dirname(STUDY_SUMMARY_PATH), exist_ok=True)
    with open(STUDY_SUMMARY_PATH, "w", encoding="utf-8") as f:
        json.dump(summaries, f, indent=2, ensure_ascii=False, default=str)
    print(f"\nsummary written to {STUDY_SUMMARY_PATH}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Career readiness report generator")
    parser.add_argument("--mode", choices=["agent", "direct", "both"],
                        help="run the Qwen validation study instead of the web app")
    parser.add_argument("--csv", help="source CSV (default: newest in data/uploads)")
    parser.add_argument("--limit", type=int, help="only study the first N students")
    parser.add_argument("--instructions", default="",
                        help="context prompt to generate with (default: none)")
    cli_args = parser.parse_args()

    if cli_args.mode:
        run_study_cli(cli_args)
    else:
        app.run(host="0.0.0.0", port=5000, debug=True)