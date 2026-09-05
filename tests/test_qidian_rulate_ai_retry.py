from threading import Event

import pytest

from gemini_translator.api.errors import OperationCancelledError, PartialGenerationError, TemporaryRateLimitError
from qidian_rulate import workers


class _SettingsManager:
    def __init__(self):
        self.increments = 0
        self.decrements = 0

    def increment_request_count(self, api_key, model_id):
        self.increments += 1

    def decrement_request_count(self, api_key, model_id):
        self.decrements += 1

    def load_proxy_settings(self):
        return None


class _RetryOnceHandler:
    calls = 0

    def __init__(self, worker):
        self.worker = worker

    def setup_client(self, client_override=None, proxy_settings=None):
        return True

    async def execute_api_call(self, *args, **kwargs):
        type(self).calls += 1
        if type(self).calls == 1:
            raise TemporaryRateLimitError("temporary overload", delay_seconds=30)
        return "ok"


def test_qidian_ai_request_retries_temporary_rate_limit(monkeypatch):
    _RetryOnceHandler.calls = 0
    monkeypatch.setattr(
        workers.api_config,
        "api_providers",
        lambda: {"gemini": {"handler_class": "RetryOnceHandler", "is_async": True}},
    )
    monkeypatch.setattr(
        workers.api_config,
        "all_models",
        lambda: {"gemini-test": {"id": "gemini-test"}},
    )
    monkeypatch.setattr(workers.api_config, "default_model_name", lambda: "gemini-test")
    monkeypatch.setattr(workers, "get_api_handler_class", lambda name: _RetryOnceHandler)
    monkeypatch.setattr(workers.time, "sleep", lambda seconds: None)
    logs = []

    result = workers._run_ai_request(
        provider_id="gemini",
        model_settings={"model": "gemini-test"},
        active_keys=["test-key"],
        settings_manager=_SettingsManager(),
        prompt="prompt",
        log_callback=lambda level, message: logs.append((level, message)),
        log_prefix="Qidian -> Rulate catalog",
    )

    assert result == "ok"
    assert _RetryOnceHandler.calls == 2
    assert any("повтор" in message for _, message in logs)


@pytest.fixture
def token_request(monkeypatch):
    class Handler:
        calls = []
        closed = 0
        responses = []

        def __init__(self, worker):
            self.worker = worker

        def setup_client(self, **kwargs):
            return True

        async def execute_api_call(self, prompt, log_prefix, **kwargs):
            self.calls.append((prompt, kwargs))
            response = self.responses.pop(0)
            if isinstance(response, Exception):
                raise response
            return response

        async def _close_thread_session_internal(self):
            type(self).closed += 1

    model = {"id": "test", "max_output_tokens": 32768}
    monkeypatch.setattr(workers.api_config, "api_providers", lambda: {"gemini": {"handler_class": "GeminiApiHandler"}})
    monkeypatch.setattr(workers.api_config, "all_models", lambda: {"test": model})
    monkeypatch.setattr(workers, "get_api_handler_class", lambda _: Handler)
    logs = []

    def run(**kwargs):
        return workers._run_ai_request(
            provider_id="gemini", model_settings=kwargs.pop("model_settings", {"model": "test"}),
            active_keys=["test-key"], settings_manager=None, prompt="original prompt",
            log_callback=lambda *entry: logs.append(entry), log_prefix="test", **kwargs,
        )

    return Handler, model, logs, run


def test_max_tokens_retries_full_request_with_larger_budget(token_request):
    handler, _, logs, run = token_request
    handler.responses = [PartialGenerationError("truncated", '{"title":', "MAX_TOKENS"), "complete"]

    assert run(max_output_tokens=8192) == "complete"
    assert [args["max_output_tokens"] for _, args in handler.calls] == [8192, 16384]
    assert all(prompt == "original prompt" and args["allow_incomplete"] is False for prompt, args in handler.calls)
    assert handler.closed == 2
    assert logs


def test_max_tokens_stops_at_model_limit(token_request):
    handler, model, _, run = token_request
    model["max_output_tokens"] = 6000
    error = PartialGenerationError("truncated", "partial", "MAX_TOKENS")
    handler.responses = [error, error]

    with pytest.raises(PartialGenerationError):
        run(max_output_tokens=4096)

    assert [args["max_output_tokens"] for _, args in handler.calls] == [4096, 6000]
    assert handler.closed == 2


def test_max_tokens_retry_count_is_bounded(token_request):
    handler, _, _, run = token_request
    handler.responses = [PartialGenerationError("truncated", "", "MAX_TOKENS")] * 3

    with pytest.raises(PartialGenerationError):
        run(max_output_tokens=2048)

    assert len(handler.calls) == workers.AI_REQUEST_RETRY_ATTEMPTS
    assert handler.closed == len(handler.calls)


def test_other_partial_generation_reason_is_not_retried(token_request):
    handler, _, _, run = token_request
    handler.responses = [PartialGenerationError("blocked", "", "SAFETY")]
    with pytest.raises(PartialGenerationError):
        run()
    assert len(handler.calls) == handler.closed == 1


def test_gemini_numeric_thinking_budget_has_output_headroom(token_request):
    handler, model, _, run = token_request
    model["min_thinking_budget"] = 1024
    handler.responses = ["complete"]

    assert run(max_output_tokens=8192, model_settings={
        "model": "test", "thinking_enabled": True, "thinking_budget": 16384,
    }) == "complete"
    assert handler.calls[0][1]["max_output_tokens"] == 24576


def test_cancel_between_token_retries_closes_handler_and_stops(token_request):
    handler, _, _, run = token_request
    cancel = Event()
    original_close = handler._close_thread_session_internal

    async def close_and_cancel(self):
        await original_close(self)
        cancel.set()

    handler._close_thread_session_internal = close_and_cancel
    handler.responses = [PartialGenerationError("truncated", "", "MAX_TOKENS")]
    with pytest.raises(OperationCancelledError):
        run(cancel_event=cancel)
    assert len(handler.calls) == 1
