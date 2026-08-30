import inspect
import time

TRACE_STEPS = [
    ("upload_csv", "Upload CSV"),
    ("read_csv", "Read CSV"),
    ("csv_to_json", "CSV -> JSON"),
    ("validate", "Validate"),
    ("build_prompt", "Build Prompt"),
    ("send_to_hermes", "Send to Hermes"),
    ("ai_processing", "AI Processing"),
    ("response", "Response"),
    ("parse", "Parse"),
    ("generate_pdf", "Generate PDF"),
    ("save", "Save"),
]
STEP_KEYS = [key for key, _ in TRACE_STEPS]
STEP_LABELS = dict(TRACE_STEPS)


def new_trace():
    """One entry per pipeline step, all starting pending -- filled in as execution
    actually reaches each stage, so a client polling mid-generation sees steps
    complete one by one instead of only a final all-or-nothing result."""
    return {key: {"label": label, "status": "pending"} for key, label in TRACE_STEPS}


_CODE_CACHE = {}


def get_code(module, func_name):
    """
    Pulls the exact source of the function actually running that step via
    inspect.getsource -- this is real code straight from the running module, not a
    hand-copied snippet that can drift out of sync with what's actually executing.
    """
    cache_key = (module.__name__, func_name)
    if cache_key in _CODE_CACHE:
        return _CODE_CACHE[cache_key]
    try:
        source = inspect.getsource(getattr(module, func_name))
    except (OSError, TypeError, AttributeError):
        source = f"# source unavailable for {module.__name__}.{func_name}"
    _CODE_CACHE[cache_key] = source
    return source


def set_step(trace, step, status, code="", input_data=None, output_data=None,
             duration_s=None, extra=None):
    entry = {"label": STEP_LABELS[step], "status": status}
    if status == "running":
        entry["started_at"] = time.time()
    if code:
        entry["code"] = code
    if input_data is not None:
        entry["input"] = input_data
    if output_data is not None:
        entry["output"] = output_data
    if duration_s is not None:
        entry["duration_s"] = round(duration_s, 4)
    if extra:
        entry["extra"] = extra
    trace[step] = entry


def update_step_extra(trace, step, **fields):
    """
    Merges fields into a step's "extra" without touching its status or started_at.

    Used for live retry reporting: set_step(status="running") would stamp a fresh
    started_at on every retry, so the dashboard's elapsed counter would reset to
    zero each time and hide how long the step has really been stuck.
    """
    entry = trace.get(step)
    if entry is None:
        return
    entry.setdefault("extra", {}).update(fields)


def with_live_elapsed(trace):
    """
    For any step still "running" when the trace is served, adds a live elapsed_s
    computed fresh against its started_at -- lets the dashboard show a ticking
    "AI Processing: 42s elapsed" instead of a frozen number while a long call (the
    AI request routinely takes 1-3+ minutes) is still in flight.
    """
    now = time.time()
    for entry in trace.values():
        if entry.get("status") == "running" and entry.get("started_at"):
            entry["elapsed_s"] = round(now - entry["started_at"], 1)
    return trace
