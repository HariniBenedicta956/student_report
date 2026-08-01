import json
import logging
import time
from contextlib import contextmanager

perf_log = logging.getLogger("perf")


@contextmanager
def timed(timings, key):
    """Records timings[key] = elapsed seconds for the wrapped block."""
    t0 = time.perf_counter()
    try:
        yield
    finally:
        timings[key] = round(time.perf_counter() - t0, 4)


def log_csv_parse(batch_id, student_count, elapsed_s):
    perf_log.info(
        "PERF %s",
        json.dumps({
            "stage": "csv_parse",
            "batch_id": batch_id,
            "student_count": student_count,
            "csv_parse_s": round(elapsed_s, 4),
        }),
    )


def log_student_generation(batch_id, student_id, timings, attempts, ai_metrics):
    """
    One structured log line per student report, covering every pipeline stage:
    prompt building, the AI call (with retry count and Ollama's own token/timing
    counters), PDF generation, file saving, and total wall-clock time -- everything
    needed to see where time is actually going without re-instrumenting by hand.
    """
    entry = {
        "stage": "student_generation",
        "batch_id": batch_id,
        "student_id": student_id,
        "attempts": attempts,
        "retries": max(attempts - 1, 0),
        "stages_s": timings,
    }
    if ai_metrics:
        entry["ai_metrics"] = ai_metrics
    perf_log.info("PERF %s", json.dumps(entry))
