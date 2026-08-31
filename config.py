import os
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

def _split_hosts(value):
    return [h.strip().rstrip("/") for h in value.split(",") if h.strip()]


# NETWORK_MODE is the common switch for both Hermes and Qwen:
#   lan  -> only the LAN host  (fast, but unreachable if you're off that network)
#   wg   -> only the WireGuard host (works remotely)
#   auto -> try LAN first, then WireGuard (default -- survives either being down)
NETWORK_MODE = os.environ.get("NETWORK_MODE", "auto").strip().lower()
OLLAMA_NETWORK = NETWORK_MODE
QWEN_NETWORK = os.environ.get("QWEN_NETWORK", "").strip().lower() or NETWORK_MODE
OLLAMA_LAN_HOSTS = _split_hosts(os.environ.get("OLLAMA_LAN_URL", ""))
OLLAMA_WG_HOSTS = _split_hosts(os.environ.get("OLLAMA_WG_URL", ""))
QWEN_LAN_HOSTS = _split_hosts(os.environ.get("QWEN_LAN_URL", "")) or OLLAMA_LAN_HOSTS
QWEN_WG_HOSTS = _split_hosts(os.environ.get("QWEN_WG_URL", "")) or OLLAMA_WG_HOSTS

# Back-compat: if the older combined OLLAMA_API_BASE_URL is set and the new
# split vars aren't, fall back to treating the whole list as "auto".
_legacy_hosts = _split_hosts(os.environ.get("OLLAMA_API_BASE_URL", ""))


def _resolve_hosts(network_value, lan_hosts, wg_hosts):
    if network_value == "lan":
        return lan_hosts or _legacy_hosts
    if network_value == "wg":
        return wg_hosts or _legacy_hosts
    return (lan_hosts + wg_hosts) or _legacy_hosts


OLLAMA_HOSTS = _resolve_hosts(OLLAMA_NETWORK, OLLAMA_LAN_HOSTS, OLLAMA_WG_HOSTS)
QWEN_HOSTS = _resolve_hosts(QWEN_NETWORK, QWEN_LAN_HOSTS, QWEN_WG_HOSTS)

# The Hermes agent (hermes_agent/app.py) -- the orchestration layer generation
# and content validation now route through, per refinedversion.md, instead of
# core/ollama_client.py talking to OLLAMA_HOSTS/QWEN_HOSTS above directly. Only
# the Hermes agent process itself still reads those; core/hermes_agent_client.py
# (what app.py and core/report_queue.py use) only ever needs this URL + key.
# NOT the "Hermes-3-8B" model (OLLAMA_HERMES3_MODEL below) -- unrelated names.
HERMES_AGENT_URL = os.environ.get("HERMES_AGENT_URL", "http://localhost:8100").rstrip("/")
HERMES_AGENT_API_KEY = os.environ.get("HERMES_AGENT_API_KEY", "")

# Hermes is not called at present -- validation.md puts qwen3.5:4b in both roles
# for the study. The constant stays defined (not deleted) so switching back is a
# one-line change: MODEL_SELECTION=hermes in .env.
OLLAMA_HERMES3_MODEL = os.environ.get("OLLAMA_HERMES3_MODEL", "hermes3:8b")
OLLAMA_LLAMA3_MODEL = os.environ.get("OLLAMA_LLAMA3_MODEL", "llama3:8b")
# qwen3.5:4b, in place of qwen3:14b. It runs both roles in the validation study
# (generation and validation) -- see validation.md.
OLLAMA_QWEN3_MODEL = os.environ.get("OLLAMA_QWEN3_MODEL", "qwen3.5:4b")

# Defaults to qwen so only qwen3.5:4b runs actively, per validation.md. The
# previous Hermes default is kept commented rather than deleted.
# MODEL_SELECTION = os.environ.get("MODEL_SELECTION", "hermes").strip().lower()
MODEL_SELECTION = os.environ.get("MODEL_SELECTION", "qwen").strip().lower()


def get_active_ollama_model():
    if MODEL_SELECTION in {"qwen", "qwen3"}:
        return OLLAMA_QWEN3_MODEL
    return OLLAMA_HERMES3_MODEL


def get_active_hosts(model_selection=None):
    selection = (model_selection or MODEL_SELECTION or "hermes").strip().lower()
    if selection in {"qwen", "qwen3"}:
        return QWEN_HOSTS
    return OLLAMA_HOSTS


OLLAMA_MODEL = get_active_ollama_model()

# How many students the report queue works on at once.
#
# Default 1 on purpose. Measured against the real Ollama host (hermes3:8b,
# CPU-only): 3 concurrent requests took 8.4s vs 8.1s for the same 3 run
# sequentially -- 0.96x, i.e. no gain at all. The requests simply queued
# server-side (finishing in a clean 2.8s staircase), because Ollama was serving
# one request at a time and a single generation already saturates the CPU.
#
# So the queue exists for control -- ordering, isolation, bounded retries,
# per-student failure containment, and a single place to raise throughput later
# -- not because parallel requests are currently faster. Raising this only pays
# off once the Ollama host can genuinely serve more than one request at a time:
# a GPU with OLLAMA_NUM_PARALLEL>1, or a second host in OLLAMA_HOSTS. Raise this
# and the server's OLLAMA_NUM_PARALLEL together -- either one alone changes
# nothing. Each parallel slot carries its own num_ctx worth of KV cache, so the
# host's VRAM has to grow with it, and each slot caches the shared prompt prefix
# separately. See GPU_SETUP.md.
# 0 = size the pool from live host capacity (see ollama_client.probe_capacity) and
# grow it mid-run if capacity increases. Any positive value pins it instead.
# Auto-detection is conservative: a CPU-only host reports one slot, which is what
# measurement supports -- three concurrent generations on this CPU host took 8.4s
# against 8.1s sequential (0.96x), because it serves one request at a time and a
# single generation already saturates the cores. A GPU host reports
# REPORT_WORKERS_PER_GPU_HOST, and adding a second host to OLLAMA_HOSTS adds its
# capacity on top.
REPORT_WORKERS = int(os.environ.get("REPORT_WORKERS", "0"))

# Per-host slot counts used by the capacity probe. Raise the GPU figure together with
# the server's own OLLAMA_NUM_PARALLEL -- either alone changes nothing, and each
# parallel slot carries its own num_ctx worth of KV cache, so VRAM has to cover it.
REPORT_WORKERS_PER_GPU_HOST = int(os.environ.get("REPORT_WORKERS_PER_GPU_HOST", "2"))
REPORT_WORKERS_PER_CPU_HOST = int(os.environ.get("REPORT_WORKERS_PER_CPU_HOST", "1"))
REPORT_WORKERS_MAX = int(os.environ.get("REPORT_WORKERS_MAX", "16"))
# How often the running batch re-checks capacity and scales up if it has grown.
REPORT_CAPACITY_PROBE_S = int(os.environ.get("REPORT_CAPACITY_PROBE_S", "60"))

# Queue-level retries. 0 = retry forever: a real report is worth more than a fast
# failure, and the templated fallback is a poor substitute. Because a failed student
# is requeued rather than retried in place, retrying forever costs nobody else their
# turn. Set a positive number to bound it, and the fallback report becomes reachable
# again once a student exhausts it.
REPORT_MAX_ATTEMPTS = int(os.environ.get("REPORT_MAX_ATTEMPTS", "0"))
REPORT_RETRY_BASE_DELAY_S = int(os.environ.get("REPORT_RETRY_BASE_DELAY_S", "10"))
REPORT_RETRY_MAX_DELAY_S = int(os.environ.get("REPORT_RETRY_MAX_DELAY_S", "300"))

# Quick attempts inside ONE queue turn before the task is handed back to the queue.
# Deliberately small: long retry loops belong to the queue, where they don't hold a
# worker and block everyone behind them.
OLLAMA_ATTEMPTS_PER_TURN = int(os.environ.get("OLLAMA_ATTEMPTS_PER_TURN", "2"))

# Keeps the model resident between requests and between batches. Ollama
# otherwise unloads it after ~5 minutes idle (confirmed: /api/ps was empty
# between runs), which costs a cold load on the next batch -- but far more
# importantly it discards the KV cache that lets the shared part of our prompt
# be reused instead of re-evaluated. Prompt evaluation is the single largest
# cost in a report (measured 55% of total time), so holding that cache is worth
# far more than the load time it also saves.
OLLAMA_KEEP_ALIVE = os.environ.get("OLLAMA_KEEP_ALIVE", "30m")

# Ollama defaults num_ctx to as little as 2048-4096 unless told otherwise, and
# silently drops older context rather than erroring past that -- which quietly
# truncates the question bank and the requester's instructions.
OLLAMA_NUM_CTX = int(os.environ.get("OLLAMA_NUM_CTX", "8192"))
OLLAMA_NUM_PREDICT = int(os.environ.get("OLLAMA_NUM_PREDICT", "4096"))
# Low temperature: this call needs reliably well-formed, instruction-following
# JSON, not creative variety.
OLLAMA_TEMPERATURE = float(os.environ.get("OLLAMA_TEMPERATURE", "0.3"))

UPLOAD_DIR = os.path.join(BASE_DIR, "data", "uploads")
QUESTION_BANK_DIR = os.path.join(BASE_DIR, "data", "question_banks")
BATCHES_DIR = os.path.join(BASE_DIR, "output", "batches")

SECTION_MAPPING_PATH = os.path.join(BASE_DIR, "section_mapping.json")

# A section score below this is "needs improvement" (amber); at/above is "meets expectations" (teal).
# project_requirement.md §8 says "below roughly 60-65" -- using the upper end of that range.
SCORE_PASS_THRESHOLD = 65

COLOR_NEEDS_IMPROVEMENT = "#EF9F27"  #amber
COLOR_MEETS_EXPECTATIONS = "#5DCAA5"  #teal