import requests

from core import hermes_agent_client, ollama_client


def test_gpu_status_uses_in_process_hermes_and_no_http_call(monkeypatch):
    calls = {"requests_get": 0}

    def fake_get(*args, **kwargs):
        calls["requests_get"] += 1
        raise requests.ConnectionError("should not be used")

    monkeypatch.setattr(requests, "get", fake_get)
    monkeypatch.setattr(
        ollama_client,
        "study_gpu_status",
        lambda hosts=None, timeout=(3, 5): {
            "host": "http://127.0.0.1:11434",
            "reachable": True,
            "processor": "100% GPU",
            "on_gpu": True,
            "detail": "model resident via in-process Hermes",
        },
    )

    result = hermes_agent_client.gpu_status()

    assert result["reachable"] is True
    assert result["processor"] == "100% GPU"
    assert result["on_gpu"] is True
    assert calls["requests_get"] == 0
    assert "fallback" not in result["detail"].lower()
