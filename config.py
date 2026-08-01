import os
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

def _split_hosts(value):
    return [h.strip().rstrip("/") for h in value.split(",") if h.strip()]


# OLLAMA_NETWORK picks which host(s) to use:
#   lan  -> only the LAN host  (fast, but unreachable if you're off that network)
#   wg   -> only the WireGuard host (works remotely)
#   auto -> try LAN first, then WireGuard (default -- survives either being down)
OLLAMA_NETWORK = os.environ.get("OLLAMA_NETWORK", "auto").strip().lower()
OLLAMA_LAN_HOSTS = _split_hosts(os.environ.get("OLLAMA_LAN_URL", ""))
OLLAMA_WG_HOSTS = _split_hosts(os.environ.get("OLLAMA_WG_URL", ""))

# Back-compat: if the older combined OLLAMA_API_BASE_URL is set and the new
# split vars aren't, fall back to treating the whole list as "auto".
_legacy_hosts = _split_hosts(os.environ.get("OLLAMA_API_BASE_URL", ""))

if OLLAMA_NETWORK == "lan":
    OLLAMA_HOSTS = OLLAMA_LAN_HOSTS or _legacy_hosts
elif OLLAMA_NETWORK == "wg":
    OLLAMA_HOSTS = OLLAMA_WG_HOSTS or _legacy_hosts
else:
    OLLAMA_HOSTS = OLLAMA_LAN_HOSTS + OLLAMA_WG_HOSTS or _legacy_hosts

OLLAMA_HERMES3_MODEL = os.environ.get("OLLAMA_HERMES3_MODEL", "hermes3:8b")
OLLAMA_LLAMA3_MODEL = os.environ.get("OLLAMA_LLAMA3_MODEL", "llama3:8b")

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
REPORT_WORKERS = int(os.environ.get("REPORT_WORKERS", "1"))

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