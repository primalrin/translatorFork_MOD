import json
from pathlib import Path


def test_latest_gemini_flash_models_are_configured():
    config_path = Path(__file__).resolve().parents[1] / "config" / "api_providers.json"
    providers = json.loads(config_path.read_text(encoding="utf-8"))
    models = providers["gemini"]["models"]

    expected = {
        "Gemini 3.8 Flash": {
            "id": "gemini-3.8-flash",
            "rpm": 5,
            "rpd": 20,
            "max_concurrent_requests": 10,
        },
        "Gemini 3.7 Flash": {
            "id": "gemini-3.7-flash",
            "rpm": 5,
            "rpd": 20,
            "max_concurrent_requests": 10,
        },
        "Gemini 3.6 Flash": {
            "id": "gemini-3.6-flash",
            "rpm": 5,
            "rpd": 20,
            "max_concurrent_requests": 10,
        },
        "Gemini 3.5 Flash-Lite": {
            "id": "gemini-3.5-flash-lite",
            "rpm": 15,
            "rpd": 500,
            "max_concurrent_requests": 15,
        },
    }

    for display_name, expected_values in expected.items():
        model = models[display_name]
        for key, value in expected_values.items():
            assert model[key] == value
        assert model["tpm"] == 250000
        assert model["context_length"] == 230000
        assert model["max_output_tokens"] == 65536
        assert model["needs_chunking"] is True
        assert model["thinkingLevel"] == ["high", "medium", "low", "minimal"]
        assert model["min_thinking_budget"] == "minimal"
