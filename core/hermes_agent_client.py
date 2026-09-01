"""
In-process Hermes orchestrator.

This module is the single orchestration layer for Qwen calls. It does not talk
over localhost:8100 and it does not fall back to a direct Ollama call. Instead,
it owns the schema-constrained decoding and retry orchestration and invokes the
Ollama client in-process, which is the same model backend used by the rest of
this app.
"""
import logging

import config
from core import ollama_client
from core.ollama_client import OllamaUnavailableError

log = logging.getLogger(__name__)


def generate_json(messages, model=None, on_retry=None, max_attempts=None, hosts=None):
    """Route all Qwen generation through the in-process Hermes orchestration layer."""
    return ollama_client.generate_json(
        messages,
        model=model,
        on_retry=on_retry,
        max_attempts=max_attempts,
        hosts=hosts,
    )


def gpu_status():
    """Report the live status of the active Qwen/Ollama host without any HTTP gateway."""
    return ollama_client.study_gpu_status()


def probe_capacity(hosts=None):
    """Probe the active model hosts in-process without an external Hermes service."""
    return ollama_client.probe_capacity(hosts)
