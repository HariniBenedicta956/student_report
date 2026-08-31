import json
import logging
import time

import requests

import config

log = logging.getLogger(__name__)

CONNECT_TIMEOUT_SECONDS = 10  # fail fast on an unreachable host instead of hanging
READ_TIMEOUT_SECONDS = 240  # a real generation call can legitimately take 1-3+ minutes

STRICT_JSON_REMINDER = (
    "Your previous response was not valid JSON. "
    "Respond with ONLY a single valid JSON object. "
    "No markdown code fences, no commentary before or after, no trailing commas."
)

NS_PER_SECOND = 1_000_000_000


class OllamaUnavailableError(RuntimeError):
    """Raised when no configured Ollama host could be reached."""


def _extract_metrics(data):
    """
    Ollama's /api/chat response includes its own timing/token counters -- no need to
    estimate these ourselves. All duration fields come back in nanoseconds.
    """
    prompt_tokens = data.get("prompt_eval_count")
    output_tokens = data.get("eval_count")
    eval_duration_s = data.get("eval_duration", 0) / NS_PER_SECOND
    tokens_per_sec = (
        round(output_tokens / eval_duration_s, 1)
        if output_tokens and eval_duration_s > 0
        else None
    )
    return {
        "prompt_tokens": prompt_tokens,
        "output_tokens": output_tokens,
        "model_load_s": round(data.get("load_duration", 0) / NS_PER_SECOND, 3),
        "prompt_eval_s": round(data.get("prompt_eval_duration", 0) / NS_PER_SECOND, 3),
        "eval_s": round(eval_duration_s, 3),
        "ollama_total_s": round(data.get("total_duration", 0) / NS_PER_SECOND, 3),
        "tokens_per_sec": tokens_per_sec,
    }


def _call_host(host, model, messages):
    # stream=True -- with stream=False the connection sits completely silent (no
    # bytes either direction) for the entire prompt-eval + generation time, which
    # for our payload size is 100-200+ seconds. That's well past the idle-connection
    # timeout of most NAT/firewall paths (observed directly: WireGuard-routed calls
    # were getting reset mid-request -- "connection forcibly closed by the remote
    # host" -- while a trivial low-latency request over the same path succeeded
    # fine). Streaming keeps data actually flowing once generation starts, which
    # avoids tripping that idle timeout for the back half of the request.
    resp = requests.post(
        f"{host}/api/chat",
        json={
            "model": model,
            "messages": messages,
            "stream": True,
            "format": "json",
            # qwen3.5:4b is a hybrid-thinking model: by default it emits a hidden
            # chain-of-thought into a SEPARATE "thinking" field before (and instead
            # of, once num_predict runs out) the actual answer in "content". Verified
            # directly against the live host: with this left on, a real report
            # prompt produced 3727 tokens of "thinking", hit num_predict, and
            # returned an entirely EMPTY "content" (done_reason "length") -- which
            # then fails _try_parse_json every single time and burns a full retry
            # (another ~50-90s of pure waste) before failing again. This is what
            # "invalid_json" retries and slow generation on a GPU host actually were.
            # With thinking off: done_reason "stop", ~700 tokens, valid JSON,
            # first try. _consume_stream below also never reads "thinking" -- if
            # this is ever re-enabled, that has to be read too or the same failure
            # returns.
            "think": False,
            # Hold the model (and its KV cache) in memory between calls instead of
            # letting Ollama unload it after its ~5 minute idle default. The load
            # time this saves is minor (~3.4s); what matters is that unloading also
            # throws away the cached evaluation of our shared system prompt, which
            # is the single biggest saving available here -- see config.OLLAMA_KEEP_ALIVE.
            "keep_alive": config.OLLAMA_KEEP_ALIVE,
            "options": {
                # Lower temperature -- this call only needs reliably well-formed JSON
                # that follows instructions, not creative variety, and a wandering/
                # high-temp completion is more likely to produce something that fails
                # to parse.
                "temperature": config.OLLAMA_TEMPERATURE,
                # Ollama defaults num_ctx to as little as 2048-4096 tokens unless told
                # otherwise. Our system+user payload (schema + question bank + answers)
                # runs 4000-5000+ tokens -- past a small default, Ollama silently
                # truncates older context rather than erroring, which would explain
                # both instructions getting dropped (system message pushed out) and
                # incomplete answer listings (later questions never reaching the
                # model). A direct chat session with a short, one-off question would
                # never hit this ceiling, which is the real reason it can look like
                # Hermes "can't do the same thing" through this app.
                "num_ctx": config.OLLAMA_NUM_CTX,
                # Generous output cap so a long request (e.g. "list every question and
                # answer") has room to complete instead of being cut off mid-response.
                "num_predict": config.OLLAMA_NUM_PREDICT,
            },
        },
        timeout=(CONNECT_TIMEOUT_SECONDS, READ_TIMEOUT_SECONDS),
        stream=True,
    )
    resp.raise_for_status()
    return _consume_stream(resp)


def _consume_stream(resp):
    content_parts = []
    final_chunk = None
    for line in resp.iter_lines():
        if not line:
            continue
        chunk = json.loads(line)
        content_parts.append(chunk.get("message", {}).get("content", ""))
        if chunk.get("done"):
            final_chunk = chunk
    if final_chunk is None:
        raise requests.ConnectionError("Ollama stream ended without a final chunk")
    final_chunk["message"] = {"content": "".join(content_parts)}
    return final_chunk


def _classify(exc):
    """
    The error label shown on the dashboard. Distinguishing these matters for
    debugging: "timeout" means the host answered but too slowly (usually a long
    prompt evaluation on CPU), "unreachable" means nothing answered at all
    (wrong host in .env, Ollama down, tunnel dropped) -- very different fixes.
    """
    if isinstance(exc, requests.Timeout):
        return "timeout"
    if isinstance(exc, requests.ConnectionError):
        return "unreachable"
    if isinstance(exc, requests.HTTPError):
        return "http_error"
    return type(exc).__name__


def probe_capacity(hosts=None):
    """
    How many reports can genuinely be generated at once, asked of the hosts rather
    than assumed.

    A host running on GPU can serve several requests concurrently; a CPU-only host
    cannot -- measured on this project's own host, three concurrent generations took
    8.4s against 8.1s run sequentially, because it serves one at a time and one
    generation already saturates the cores. So GPU hosts contribute
    REPORT_WORKERS_PER_GPU_HOST slots and CPU hosts contribute one, and capacity is
    the sum across every reachable host -- which is what makes "add another server"
    a real scaling path rather than a config change with no effect.

    Returns (total_slots, per_host_detail). size_vram is the authoritative GPU
    signal: Ollama falls back to CPU silently, so a host is only counted as GPU when
    it actually reports weights resident in VRAM. When no model is loaded there is
    nothing to read, and the host is counted conservatively as CPU.
    """
    hosts = list(hosts or config.get_active_hosts())
    detail = []
    machines = {}   # fingerprint -> slots, so one machine is only counted once

    for host in hosts:
        try:
            resp = requests.get(f"{host}/api/ps", timeout=(3, 5))
            resp.raise_for_status()
            models = resp.json().get("models", [])
        except (requests.ConnectionError, requests.Timeout, requests.HTTPError,
                ValueError) as exc:
            detail.append({"host": host, "reachable": False, "slots": 0,
                            "error": str(exc)[:120]})
            continue

        if not models:
            mode, slots = "idle", config.REPORT_WORKERS_PER_CPU_HOST
        elif any(m.get("size_vram", 0) > 0 for m in models):
            mode, slots = "gpu", config.REPORT_WORKERS_PER_GPU_HOST
        else:
            mode, slots = "cpu", config.REPORT_WORKERS_PER_CPU_HOST

        # OLLAMA_HOSTS is a failover chain, not a pool -- the LAN address and the
        # WireGuard address in this project's own .env are two routes to the SAME
        # box. Summing per host would double its capacity and put two workers on one
        # CPU machine, which measured slower than one. Two hosts showing the same
        # loaded model with the same expiry are therefore treated as one machine.
        # While nothing is loaded there is no fingerprint to compare, so every
        # unidentifiable host collapses into a single "unknown" machine: under-
        # counting costs a little throughput, over-counting costs correctness.
        if models:
            fingerprint = tuple(sorted(
                (m.get("digest"), m.get("expires_at")) for m in models))
        else:
            fingerprint = "unidentified"
        machines[fingerprint] = max(machines.get(fingerprint, 0), slots)
        detail.append({"host": host, "reachable": True, "mode": mode, "slots": slots,
                        "machine": "unidentified" if fingerprint == "unidentified"
                                    else hash(fingerprint) & 0xFFFF})

    return max(sum(machines.values()), 1), detail


def generate_raw(messages, model=None, hosts=None):
    """
    Send chat messages to the first reachable Ollama host.
    Returns (content, metrics, host) -- metrics is Ollama's own token/timing counters,
    host is whichever one actually answered (for tracing/display purposes).

    hosts overrides the order tried, so each queue worker can prefer a different
    server and spread load instead of every worker starting at the same one.
    """
    model = model or config.get_active_ollama_model()
    last_error = None
    for host in (hosts or config.OLLAMA_HOSTS):
        try:
            data = _call_host(host, model, messages)
            return data["message"]["content"], _extract_metrics(data), host
        except (requests.ConnectionError, requests.Timeout, requests.HTTPError) as exc:
            log.warning("Ollama host %s failed (%s): %s", host, _classify(exc), exc)
            last_error = exc
            continue
    error = OllamaUnavailableError(
        f"None of the configured Ollama hosts responded: {config.get_active_hosts()}"
    )
    error.error_type = _classify(last_error) if last_error else "unreachable"
    raise error from last_error


RETRY_BASE_DELAY_S = 5
RETRY_MAX_DELAY_S = 60


def _backoff_delay(attempt):
    """5s, 10s, 20s, 40s, then 60s from there on."""
    return min(RETRY_BASE_DELAY_S * (2 ** (attempt - 1)), RETRY_MAX_DELAY_S)


def generate_json(messages, model=None, on_retry=None, max_attempts=None, hosts=None):
    """
    Calls Ollama's chat endpoint and parses the response as JSON, making a small
    number of quick attempts and then RAISING rather than looping forever.

    The long retry loop lives in report_queue, not here. Retrying indefinitely inside
    this call would hold the calling worker for as long as the host stayed down, and
    with a small pool that blocks every student queued behind this one. Raising hands
    the task back to the queue, which requeues it with backoff and lets the worker
    pick up the next student immediately. Nothing is given up on -- the retrying just
    happens somewhere it costs nobody else their turn.

    on_retry(attempt, error_type, detail, delay_s) fires before each in-turn wait so
    the caller can surface live retry state on the dashboard. error_type is one of
    "unreachable", "timeout", "http_error" or "invalid_json".

    Raises OllamaUnavailableError (carrying .error_type and .attempts) when the
    per-turn attempts are used up.

    Returns a dict:
      "parsed":   the report dict
      "attempts": how many attempts were used (1 = succeeded first try)
      "metrics":  Ollama's token/timing counters, plus "json_parse_s"
      "raw_text": the exact text the model returned before JSON parsing
      "host":     whichever Ollama host actually answered
    """
    max_attempts = max_attempts or config.OLLAMA_ATTEMPTS_PER_TURN
    attempt = 0
    while True:
        attempt += 1
        attempt_messages = (
            messages if attempt == 1
            else messages + [{"role": "user", "content": STRICT_JSON_REMINDER}]
        )
        try:
            raw, metrics, host = generate_raw(attempt_messages, model=model, hosts=hosts)
        except OllamaUnavailableError as exc:
            error_type = getattr(exc, "error_type", "unreachable")
            detail = str(exc)
        else:
            t0 = time.perf_counter()
            parsed = _try_parse_json(raw)
            metrics["json_parse_s"] = round(time.perf_counter() - t0, 4)
            if parsed is not None:
                return {
                    "parsed": parsed, "attempts": attempt, "metrics": metrics,
                    "raw_text": raw, "host": host,
                }
            error_type = "invalid_json"
            detail = f"model returned {len(raw)} chars that did not parse as JSON"

        if attempt >= max_attempts:
            error = OllamaUnavailableError(
                f"{error_type} after {attempt} attempt(s): {detail}")
            error.error_type = error_type
            error.attempts = attempt
            raise error

        delay = _backoff_delay(attempt)
        log.warning("Ollama attempt %d/%d failed (%s): %s -- retrying in %ds",
                     attempt, max_attempts, error_type, detail, delay)
        if on_retry is not None:
            try:
                on_retry(attempt, error_type, detail, delay)
            except Exception:  # noqa: BLE001 -- reporting must never break generation
                log.exception("on_retry callback failed")
        time.sleep(delay)


def _try_parse_json(text):
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
        text = text.strip()
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None


# --- GPU / CPU verification --------------------------------------------------
#
# Moved here from app.py when generation/validation started routing through the
# Hermes agent (hermes_agent/app.py) -- this is genuinely about the state of
# the Ollama host, the same domain as everything else in this module, and it's
# what hermes_agent/app.py's /v1/gpu-status endpoint calls now. app.py's own
# /gpu-status route no longer calls these directly; it asks the Hermes agent
# via core.hermes_agent_client.gpu_status() instead.

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
                "detail": f"{_classify(last_error)}: {str(last_error)[:120]}"}
    return {"host": None, "reachable": False, "processor": None, "on_gpu": None,
            "detail": "no hosts configured"}


def nvidia_smi_utilization():
    """
    The optional cross-check from validation.md: GPU utilisation straight from
    the driver rather than from Ollama's own accounting.

    Only works when this process is running ON the GPU box -- nvidia-smi is a
    local tool with no remote equivalent. Returns None when it is not
    available, which is the expected case; that is a "could not cross-check",
    not a failure, so size_vram from study_gpu_status stays the authoritative
    signal.
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
