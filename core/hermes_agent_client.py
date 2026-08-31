"""
Thin HTTP client the main app uses to reach the Hermes agent (hermes_agent/app.py)
instead of calling Ollama directly -- see refinedversion.md: "the app sends
[Questions + CSV answers + Script Prompt] through the tunnel and Gateway to
Hermes, which calls qwen". This is the client side of that call: a real HTTP
request, authenticated with an API key, to a separate process. Nothing in this
module talks to Ollama -- that only happens inside hermes_agent/app.py now.

Mirrors core.ollama_client.generate_json's signature and return shape closely
enough that call sites barely changed -- what moved is WHERE the retry/schema-
constrained-decoding logic executes (server-side, inside the Hermes agent),
not what it does or what it returns.
"""
import logging
import time

import requests

import config
from core.ollama_client import OllamaUnavailableError  # reused deliberately: still
# means "the model backend could not be reached / could not produce a usable
# result" -- just one hop further away than before.

log = logging.getLogger(__name__)

CONNECT_TIMEOUT_SECONDS = 10
READ_TIMEOUT_SECONDS = 240  # a real generation call can legitimately take 1-3+ minutes

# Infra-level retries against the Hermes agent's HTTP endpoint itself (connection
# refused, DNS failure, timeout just reaching it) -- separate from the JSON/
# schema retries that already happen INSIDE the agent via ollama_client.
# generate_json before it ever answers us. This is refinedversion.md's
# "infrastructure failures ... get 3 fast retries at roughly 2s/8s/30s" budget;
# it is deliberately small and fast, distinct from the slower regeneration
# budget report_queue already applies on top of this.
AGENT_CONNECT_ATTEMPTS = 3
AGENT_RETRY_DELAYS_S = (2, 8, 30)


def _headers():
    return {"X-API-Key": config.HERMES_AGENT_API_KEY}


def generate_json(messages, model=None, on_retry=None, max_attempts=None, hosts=None):
    """
    Same return shape as core.ollama_client.generate_json:
      {"parsed", "attempts", "metrics", "raw_text", "host"}

    on_retry here fires only for INFRA failures reaching the Hermes agent itself
    (error_type "unreachable"/"timeout") -- the JSON/schema retries reflected in
    a successful response's "attempts" field already happened server-side.
    """
    url = f"{config.HERMES_AGENT_URL}/v1/generate"
    payload = {"messages": messages}
    if model:
        payload["model"] = model
    if max_attempts:
        payload["max_attempts"] = max_attempts
    if hosts:
        payload["hosts"] = list(hosts)

    last_exc = None
    for attempt in range(1, AGENT_CONNECT_ATTEMPTS + 1):
        try:
            resp = requests.post(
                url, json=payload, headers=_headers(),
                timeout=(CONNECT_TIMEOUT_SECONDS, READ_TIMEOUT_SECONDS),
            )
        except (requests.ConnectionError, requests.Timeout) as exc:
            last_exc = exc
            error_type = "timeout" if isinstance(exc, requests.Timeout) else "unreachable"
            if attempt >= AGENT_CONNECT_ATTEMPTS:
                break
            delay = AGENT_RETRY_DELAYS_S[min(attempt - 1, len(AGENT_RETRY_DELAYS_S) - 1)]
            log.warning("Hermes agent unreachable (attempt %d/%d, %s): %s -- retrying in %ds",
                        attempt, AGENT_CONNECT_ATTEMPTS, error_type, exc, delay)
            if on_retry:
                on_retry(attempt, error_type, str(exc), delay)
            time.sleep(delay)
            continue

        if resp.status_code == 401:
            error = OllamaUnavailableError(
                "Hermes agent rejected the request: invalid or missing API key")
            error.error_type = "auth_error"
            raise error
        if resp.status_code == 503:
            # The agent exhausted ITS OWN retry budget against qwen (JSON/schema
            # failures) -- not an infra problem on our end, so this call does not
            # get another attempt here. report_queue's existing requeue-with-
            # backoff is what handles it, same as before this refactor.
            body = resp.json() if resp.content else {}
            error = OllamaUnavailableError(
                body.get("error") or f"Hermes agent returned {resp.status_code}")
            error.error_type = body.get("error_type", "invalid_json")
            error.attempts = body.get("attempts")
            raise error
        resp.raise_for_status()
        return resp.json()

    error = OllamaUnavailableError(
        f"Hermes agent at {url} did not respond after "
        f"{AGENT_CONNECT_ATTEMPTS} attempts: {last_exc}")
    error.error_type = "unreachable"
    raise error from last_exc


def gpu_status():
    """Used for the live GPU badge -- never raises, an unreachable agent is a
    result to show ("host unreachable"), not an exception that should break the
    page. Mirrors the shape core.ollama_client.study_gpu_status returns."""
    try:
        resp = requests.get(f"{config.HERMES_AGENT_URL}/v1/gpu-status",
                             headers=_headers(), timeout=(5, 10))
        resp.raise_for_status()
        return resp.json()
    except (requests.ConnectionError, requests.Timeout, requests.HTTPError) as exc:
        return {"host": None, "reachable": False, "processor": None, "on_gpu": None,
                "detail": f"Hermes agent unreachable: {exc}"}


def probe_capacity(hosts=None):
    """Mirrors core.ollama_client.probe_capacity's (total_slots, detail) return
    shape -- backs report_queue's live worker-pool sizing. Falls back to a
    single conservative slot rather than raising, for the same reason
    gpu_status never raises: sizing a queue shouldn't crash because a status
    check failed."""
    try:
        params = {"hosts": ",".join(hosts)} if hosts else None
        resp = requests.get(f"{config.HERMES_AGENT_URL}/v1/capacity",
                             headers=_headers(), params=params, timeout=(5, 10))
        resp.raise_for_status()
        data = resp.json()
        return data["total_slots"], data["detail"]
    except (requests.ConnectionError, requests.Timeout, requests.HTTPError,
            KeyError, ValueError) as exc:
        log.warning("Could not reach Hermes agent for capacity probe: %s", exc)
        return 1, [{"host": config.HERMES_AGENT_URL, "reachable": False,
                    "error": str(exc)[:120]}]
