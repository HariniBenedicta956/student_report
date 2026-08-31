"""
Hermes agent -- the orchestration/tool-calling layer between the main report app
and the underlying model, per refinedversion.md: "a separate GPU-enabled server
running the Hermes agent, which calls the qwen3.5:4b model for both report
generation and content validation ... a Gateway on Server 2 that checks an API
key on every request before forwarding it internally to Hermes, so the model
server itself is never exposed directly."

Runs as its OWN process, on its own port -- not imported by app.py. The main
app talks to it only over HTTP, authenticated with an API key (core/
hermes_agent_client.py is the other side of that call). Only this process (and
whatever runs inside it) ever calls Ollama directly.

IMPORTANT -- naming: "Hermes agent" is this orchestration layer. It is not the
"Hermes-3-8B" model (config.OLLAMA_HERMES3_MODEL) -- no Hermes model is loaded
or called anywhere in this file. The model this agent calls underneath is
qwen3.5:4b, same as config.get_active_ollama_model() everywhere else in this
project.

On this single dev machine there is no separate physical server, Netbird
tunnel, or standalone Gateway process yet -- those are real infrastructure
decisions refinedversion.md itself flags as still unconfirmed (Server 2's
hosting/operator status). What's real here is the network boundary: this is a
genuine second process, reachable only over HTTP, that the main app must
authenticate to -- not a Python function call into Ollama's client code, which
now only executes inside this process.

Run standalone:
    python -m hermes_agent.app
"""
import logging
import os

from flask import Flask, jsonify, request

import config
from core import ollama_client

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

app = Flask(__name__)

API_KEY = os.environ.get("HERMES_AGENT_API_KEY", "").strip()


@app.before_request
def _require_api_key():
    # /health is deliberately unauthenticated -- a liveness probe shouldn't need
    # a credential, and it reveals nothing beyond "this process is up".
    if request.path == "/health":
        return None
    provided = request.headers.get("X-API-Key", "")
    if not API_KEY or provided != API_KEY:
        return jsonify({"error": "missing or invalid X-API-Key"}), 401


@app.get("/health")
def health():
    return jsonify({"status": "ok", "model": config.get_active_ollama_model()})


@app.post("/v1/generate")
def generate():
    """
    The one call the main app makes for BOTH generation and content validation
    -- both are "run these chat messages through qwen3.5:4b, get back parsed
    JSON", just with different messages built upstream (prompt_builder.
    build_messages for generation, build_validation_messages for the content
    check). This is where schema-constrained decoding (format:"json"),
    thinking-mode suppression, per-turn retry and host failover actually run --
    see core/ollama_client.generate_json, unchanged in what it does, just now
    called from here instead of from the main app process directly.
    """
    payload = request.get_json(force=True, silent=True) or {}
    messages = payload.get("messages")
    if not messages:
        return jsonify({"error": "messages is required"}), 400
    model = payload.get("model") or config.get_active_ollama_model()

    try:
        result = ollama_client.generate_json(
            messages,
            model=model,
            max_attempts=payload.get("max_attempts"),
            hosts=payload.get("hosts"),
        )
    except ollama_client.OllamaUnavailableError as exc:
        # A failure to produce valid output after our own retry budget -- not an
        # infra problem on the caller's end, so the client (core/
        # hermes_agent_client.py) does not retry this itself; report_queue's
        # existing requeue-with-backoff handles it same as before.
        return jsonify({
            "error": str(exc),
            "error_type": getattr(exc, "error_type", "unreachable"),
            "attempts": getattr(exc, "attempts", None),
        }), 503
    return jsonify(result)


@app.get("/v1/gpu-status")
def gpu_status():
    status = ollama_client.study_gpu_status()
    smi = ollama_client.nvidia_smi_utilization()
    if smi:
        status["nvidia_smi"] = smi
    return jsonify(status)


@app.get("/v1/capacity")
def capacity():
    """Backs report_queue's live worker-pool sizing -- see ollama_client.probe_capacity."""
    hosts_param = request.args.get("hosts")
    hosts = [h for h in hosts_param.split(",") if h] if hosts_param else None
    total_slots, detail = ollama_client.probe_capacity(hosts)
    return jsonify({"total_slots": total_slots, "detail": detail})


if __name__ == "__main__":
    if not API_KEY:
        raise SystemExit(
            "HERMES_AGENT_API_KEY is not set -- refusing to start an "
            "unauthenticated model gateway. Set it in .env."
        )
    port = int(os.environ.get("HERMES_AGENT_PORT", "8100"))
    log.info("Hermes agent listening on :%d -- underlying model: %s",
              port, config.get_active_ollama_model())
    app.run(host="0.0.0.0", port=port)
