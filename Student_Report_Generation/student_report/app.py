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
import csv_ingest
import execution_trace
import ollama_client
import pdf_generator
import perf_logging
import prompt_builder
import storage

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
    """
    for i, student_record in enumerate(students):
        student_id = manifest["students"][i]["student_id"]
        try:
            _generate_one(batch_id, student_id, i, student_record, mapping,
                           instructions_text, shared_trace)
        except Exception:
            log.exception("Unexpected failure generating %s in batch %s", student_id, batch_id)
            storage.update_student_status(batch_id, student_id, "error")


def _generate_one(batch_id, student_id, student_index, student_record, mapping,
                   instructions_text, shared_trace):
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
            messages = prompt_builder.build_messages(student_record, mapping, instructions_text)
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
    hermes_request_info = {
        "hosts_tried_in_order": config.OLLAMA_HOSTS,
        "model": config.OLLAMA_HERMES3_MODEL,
        "message_count": len(messages),
        "payload_chars": sum(len(m["content"]) for m in messages),
    }
    execution_trace.set_step(trace, "send_to_hermes", "running",
                              code=execution_trace.get_code(ollama_client, "_call_host"),
                              input_data=hermes_request_info)
    execution_trace.set_step(trace, "ai_processing", "running",
                              code=execution_trace.get_code(ollama_client, "generate_json"))
    storage.save_student_trace(batch_id, student_id, trace)

    t_stage = time.perf_counter()
    with perf_logging.timed(timings, "ai_call_s"):
        try:
            result = ollama_client.generate_json(messages)
            report_json = result["parsed"]
            attempts = result["attempts"]
            ai_metrics = result["metrics"]
            raw_text = result.get("raw_text")
            host_used = result.get("host")
            if report_json is None:
                failure_reason = (
                    f"Ollama responded but never returned valid JSON after "
                    f"{attempts} attempts"
                )
        except ollama_client.OllamaUnavailableError as exc:
            log.exception("Ollama unavailable while generating %s", student_id)
            report_json = None
            attempts = ollama_client.MAX_ATTEMPTS
            failure_reason = f"No configured Ollama host was reachable: {exc}"
    ai_call_duration = time.perf_counter() - t_stage

    execution_trace.set_step(
        trace, "send_to_hermes", "done" if host_used else "error",
        code=execution_trace.get_code(ollama_client, "_call_host"),
        input_data=hermes_request_info,
        output_data={"host_used": host_used} if host_used else {"error": failure_reason},
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
        # dict(...) snapshot -- report_json is mutated below (adding "_perf" /
        # "_generation_error"), and since Python dicts are stored by reference, the
        # trace's "parse" output would otherwise silently pick up those later
        # additions too, making it look like the model itself returned them.
        output_data=dict(report_json) if report_json is not None else {"error": "not valid JSON"},
        duration_s=(ai_metrics or {}).get("json_parse_s"),
    )
    storage.save_student_trace(batch_id, student_id, trace)

    if report_json is None:
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


if __name__ == "__main__":
    app.run(host="0.0.0.0",port=5000,debug=True)