"""
In-process Hermes orchestrator.

This module is the single orchestration layer for Qwen calls. It does not talk
over localhost:8100 and it does not fall back to a direct Ollama call. Instead,
it owns the schema-constrained decoding and retry orchestration and invokes the
Ollama client in-process, which is the same model backend used by the rest of
this app.
"""
import json
import logging

import config
from core import ollama_client
from core import prompt_builder
from core.ollama_client import OllamaUnavailableError

log = logging.getLogger(__name__)


def generate_json(messages, model=None, on_retry=None, max_attempts=None, hosts=None,
                   json_schema=None, temperature=None):
    """Route all Qwen generation through the in-process Hermes orchestration layer."""
    return ollama_client.generate_json(
        messages,
        model=model,
        on_retry=on_retry,
        max_attempts=max_attempts,
        hosts=hosts,
        json_schema=json_schema,
        temperature=temperature,
    )


def generate_report_two_step(student_record, mapping, instructions_text, question_bank=None,
                              model=None, on_retry=None, hosts=None, retry_feedback=None):
    """
    Report generation as two real, separate model calls -- extract evidence
    (no interpretation), then write the narrative from ONLY that extraction,
    never the raw answers. See prompt_builder.build_evidence_extraction_messages
    / build_narrative_messages for why this makes grounding structural rather
    than something the content validator has to catch after the fact.

    Returns a result shaped like generate_json's ({"parsed", "attempts",
    "metrics", "raw_text", "host"}), so a caller can swap a single generate_json
    call for this one with no other changes needed. "attempts" and "metrics"
    cover both calls combined (metrics also nests "extraction_metrics"
    separately); "raw_text"/"host" are step 2's, the call that actually
    produced the report. The cleaned extraction itself is returned too, under
    "extraction", for tracing/debugging.

    retry_feedback ({"previous_output": str, "reasons": [...]}), when given,
    is a validation failure on a prior full attempt -- extraction is still
    regenerated fresh (never a patch), and the failure reason is passed to
    step 2 so it doesn't just repeat the same mistake against new evidence.
    """
    if question_bank is None:
        question_bank = prompt_builder.build_question_bank(mapping, [student_record])

    extraction_messages = prompt_builder.build_evidence_extraction_messages(
        student_record, mapping, instructions_text, question_bank)
    extraction_result = ollama_client.generate_json(
        extraction_messages, model=model, on_retry=on_retry, hosts=hosts,
        json_schema=prompt_builder.EVIDENCE_EXTRACTION_SCHEMA,
        temperature=config.OLLAMA_GENERATION_TEMPERATURE,
    )
    extraction = extraction_result["parsed"] or {}

    # Sanitize rather than trust: drop any cited qid that isn't real (a
    # hallucinated citation at the extraction stage would otherwise flow
    # straight into evidence_refs and pass the report's own structural check,
    # since step 2 has no way to know it was never real).
    valid_ids = prompt_builder.valid_answer_ids(student_record, question_bank)
    cleaned_dimensions = []
    dropped = []
    for dim in extraction.get("dimensions") or []:
        if not isinstance(dim, dict):
            continue
        evidence = [
            e for e in (dim.get("evidence") or [])
            if isinstance(e, dict) and e.get("qid") in valid_ids
        ]
        if evidence:
            cleaned_dimensions.append({"name": dim.get("name"), "evidence": evidence})
        elif dim.get("name"):
            dropped.append(dim.get("name"))
    if not cleaned_dimensions:
        # Equivalent to an "invalid_json" failure -- nothing usable came out of
        # this attempt, so it goes back through the same retry path as a
        # malformed response rather than proceeding to write a report from
        # nothing.
        error = OllamaUnavailableError(
            f"evidence extraction produced no dimension with a real, valid "
            f"citation (candidate dimensions seen: {len(extraction.get('dimensions') or [])}, "
            f"all dropped: {dropped})")
        error.error_type = "invalid_json"
        error.attempts = extraction_result["attempts"]
        raise error
    cleaned_extraction = {"dimensions": cleaned_dimensions}

    narrative_messages = prompt_builder.build_narrative_messages(
        cleaned_extraction, student_record, instructions_text, retry_feedback=retry_feedback)
    narrative_result = ollama_client.generate_json(
        narrative_messages, model=model, on_retry=on_retry, hosts=hosts,
        json_schema=prompt_builder.REPORT_JSON_SCHEMA,
        temperature=config.OLLAMA_GENERATION_TEMPERATURE,
    )

    parsed = narrative_result["parsed"]
    raw_text = narrative_result.get("raw_text")
    if parsed is not None:
        # Deterministic backstop for the thin-evidence tier rule (see
        # enforce_thin_evidence_rule) -- applied here so every caller of this
        # function gets it, rather than each call site remembering to.
        parsed = prompt_builder.enforce_thin_evidence_rule(parsed)
        # raw_text is shown in the trace/UI and replayed as "previous_output"
        # on a retry -- it has to reflect the corrected JSON, not the model's
        # pre-downgrade text, or a retry would be fed a report that no longer
        # matches what's actually stored.
        raw_text = json.dumps(parsed, ensure_ascii=False)

    metrics = dict(narrative_result["metrics"] or {})
    metrics["extraction_metrics"] = extraction_result["metrics"]
    metrics["extraction_dimensions"] = len(cleaned_dimensions)
    metrics["extraction_dropped"] = dropped

    return {
        "parsed": parsed,
        "attempts": extraction_result["attempts"] + narrative_result["attempts"],
        "metrics": metrics,
        "raw_text": raw_text,
        "host": narrative_result.get("host"),
        "extraction": cleaned_extraction,
    }


def gpu_status():
    """
    Live status of the active Qwen/Ollama host, with everything backing it a
    real, current check -- not an assumed "healthy". study_gpu_status() is
    /api/ps (VRAM residency, the authoritative GPU/CPU signal). nvidia_smi is
    a real cross-check (GPU compute utilization, not just memory) when this
    process happens to run on the GPU box itself; None -- explicitly, not
    just absent -- when it doesn't, since this deployment normally runs on a
    separate machine reaching Ollama over the network, and there is no way to
    invoke nvidia-smi on a remote host over HTTP. recent_calls is real
    latency/token data from actual completed calls (see
    core/ollama_client._record_call), the closest equivalent this self-hosted
    setup has to an API's latency/usage dashboard -- there is no "cost" here
    since nothing is metered or billed.
    """
    status = ollama_client.study_gpu_status()
    status["nvidia_smi"] = ollama_client.nvidia_smi_utilization()
    status["recent_calls"] = ollama_client.recent_calls()
    return status


def warm_model():
    """Loads the active model into memory so the badge doesn't sit on "no model
    resident" until whoever's turn it is happens to run a real generation."""
    return ollama_client.warm_model(model=config.get_active_ollama_model(),
                                     hosts=config.get_active_hosts())


def probe_capacity(hosts=None):
    """Probe the active model hosts in-process without an external Hermes service."""
    return ollama_client.probe_capacity(hosts)
