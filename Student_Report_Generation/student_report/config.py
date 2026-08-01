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

UPLOAD_DIR = os.path.join(BASE_DIR, "data", "uploads")
QUESTION_BANK_DIR = os.path.join(BASE_DIR, "data", "question_banks")
BATCHES_DIR = os.path.join(BASE_DIR, "output", "batches")

SECTION_MAPPING_PATH = os.path.join(BASE_DIR, "section_mapping.json")

# A section score below this is "needs improvement" (amber); at/above is "meets expectations" (teal).
# project_requirement.md §8 says "below roughly 60-65" -- using the upper end of that range.
SCORE_PASS_THRESHOLD = 65

COLOR_NEEDS_IMPROVEMENT = "#EF9F27"  #amber
COLOR_MEETS_EXPECTATIONS = "#5DCAA5"  #teal