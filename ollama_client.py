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


def generate_raw(messages, model=None):
    """
    Send chat messages to the first reachable Ollama host.
    Returns (content, metrics, host) -- metrics is Ollama's own token/timing counters,
    host is whichever one actually answered (for tracing/display purposes).
    """
    model = model or config.OLLAMA_HERMES3_MODEL
    last_error = None
    for host in config.OLLAMA_HOSTS:
        try:
            data = _call_host(host, model, messages)
            return data["message"]["content"], _extract_metrics(data), host
        except (requests.ConnectionError, requests.Timeout) as exc:
            log.warning("Ollama host %s unreachable: %s", host, exc)
            last_error = exc
            continue
    raise OllamaUnavailableError(
        f"None of the configured Ollama hosts responded: {config.OLLAMA_HOSTS}"
    ) from last_error


MAX_ATTEMPTS = 3  # 1 initial + 2 retries -- JSON validity isn't fully deterministic per call


def generate_json(messages, model=None):
    """
    Calls Ollama's chat endpoint and parses the response as JSON, retrying with a
    stricter reminder message if a response isn't valid JSON. A transient
    OllamaUnavailableError on one attempt (e.g. a slow response tripping the read
    timeout) also gets retried rather than aborting immediately -- only raises if
    EVERY attempt failed to even get a response.

    Returns a dict:
      "parsed": the report dict, or None if every attempt that got a response failed
                to parse (caller falls back to a minimal templated report)
      "attempts": how many attempts were actually used (1 = succeeded first try)
      "metrics": Ollama's token/timing counters from the last attempt that got a
                 response (None if every attempt was unreachable), plus
                 "json_parse_s" for how long parsing the response took
      "raw_text": the exact text the model returned on the last attempt that got a
                  response (before JSON parsing) -- None if every attempt was
                  unreachable. Kept so callers can show the real response, not just
                  the parsed result, e.g. for a step-by-step execution trace.
      "host": whichever Ollama host actually answered the last attempt that got a
              response, or None
    """
    last_unavailable_error = None
    last_metrics = None
    last_raw = None
    last_host = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        attempt_messages = (
            messages if attempt == 1
            else messages + [{"role": "user", "content": STRICT_JSON_REMINDER}]
        )
        try:
            raw, metrics, host = generate_raw(attempt_messages, model=model)
        except OllamaUnavailableError as exc:
            log.warning("Ollama unavailable on attempt %d/%d: %s", attempt, MAX_ATTEMPTS, exc)
            last_unavailable_error = exc
            continue
        last_unavailable_error = None

        t0 = time.perf_counter()
        parsed = _try_parse_json(raw)
        metrics["json_parse_s"] = round(time.perf_counter() - t0, 4)
        last_metrics = metrics
        last_raw = raw
        last_host = host

        if parsed is not None:
            return {
                "parsed": parsed, "attempts": attempt, "metrics": metrics,
                "raw_text": raw, "host": host,
            }
        log.warning("Ollama response on attempt %d/%d was not valid JSON", attempt, MAX_ATTEMPTS)

    if last_unavailable_error is not None:
        raise OllamaUnavailableError(
            f"No configured Ollama host responded after {MAX_ATTEMPTS} attempts"
        ) from last_unavailable_error

    log.error("Ollama response still not valid JSON after %d attempts", MAX_ATTEMPTS)
    return {
        "parsed": None, "attempts": MAX_ATTEMPTS, "metrics": last_metrics,
        "raw_text": last_raw, "host": last_host,
    }


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
