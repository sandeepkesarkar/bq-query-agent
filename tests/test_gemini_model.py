"""GeminiModel with a fake client. No cloud access.

The fake returns real google-genai response objects, so the checks run on the same fields
the SDK fills in. Retrying a 429 or 5xx is the SDK's job; the test here only checks that the
client is built with those retry options.
"""
import json
from types import SimpleNamespace

import pytest
from google.genai import types

from agent import gemini_model
from agent.gemini_model import GeminiModel, ModelError
from agent.models import ROUTE_SCHEMA, SUMMARY_SCHEMA, plan_schema
from functions.catalog import load

CONFIG = {
    "provider": "gemini", "location": "global", "temperature": 0,
    "request_timeout_seconds": 60, "retry_attempts": 2, "max_token_retries": 3,
    "tasks": {
        "route": {"model": "gemini-route", "max_output_tokens": 1024,
                  "max_output_tokens_ceiling": 8192, "thinking_level": "low"},
        "plan": {"model": "gemini-plan", "max_output_tokens": 2048,
                 "max_output_tokens_ceiling": 8192},
        "summarize": {"model": "gemini-summary", "max_output_tokens": 3000,
                      "max_output_tokens_ceiling": 8192, "thinking_level": "low"},
    },
}


def reply(text='{"route": "answer", "reason": "ok"}', finish=types.FinishReason.STOP,
          usage=(10, 5, 20), candidates=True):
    return types.GenerateContentResponse(
        candidates=[types.Candidate(
            content=types.Content(role="model", parts=[types.Part(text=text)]) if text is not None else None,
            finish_reason=finish)] if candidates else [],
        usage_metadata=types.GenerateContentResponseUsageMetadata(
            prompt_token_count=usage[0], candidates_token_count=usage[1],
            thoughts_token_count=usage[2]),
    )


class FakeClient:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []
        self.models = SimpleNamespace(generate_content=self.generate_content)

    def generate_content(self, **kwargs):
        self.calls.append(kwargs)
        return self.responses.pop(0)


def model_with(*responses):
    client = FakeClient(*responses)
    return GeminiModel(CONFIG, client=client), client


def test_request_settings_come_from_the_task():
    model, client = model_with(reply())
    schema = plan_schema(load())
    model.complete_structured("a prompt", {**ROUTE_SCHEMA})
    (call,) = client.calls
    assert call["model"] == "gemini-route" and call["contents"] == "a prompt"
    config = call["config"]
    assert config.response_mime_type == "application/json"
    assert config.response_json_schema == ROUTE_SCHEMA
    assert config.temperature == 0 and config.max_output_tokens == 1024

    model, client = model_with(reply('{"function": "x", "params": {}}'), reply('{"summary": "s"}'))
    model.complete_structured("p", schema)
    model.complete_structured("p", SUMMARY_SCHEMA)
    plan_call, summary_call = client.calls
    assert (plan_call["model"], plan_call["config"].max_output_tokens) == ("gemini-plan", 2048)
    assert plan_call["config"].response_json_schema == schema
    assert (summary_call["model"], summary_call["config"].max_output_tokens) == ("gemini-summary", 3000)


def test_returns_the_parsed_dict():
    model, _ = model_with(reply('{"route": "refuse", "reason": "no"}'))
    assert model.complete_structured("p", ROUTE_SCHEMA) == {"route": "refuse", "reason": "no"}


def test_token_totals_add_up_across_calls():
    model, _ = model_with(reply(usage=(10, 5, 20)), reply(usage=(7, 3, 0)))
    model.complete_structured("p", ROUTE_SCHEMA)
    model.complete_structured("p", ROUTE_SCHEMA)
    assert (model.prompt_tokens, model.output_tokens, model.thinking_tokens) == (17, 8, 20)


def test_name_is_the_model_id_or_lists_each_task():
    same = {**CONFIG, "tasks": {t: {**s, "model": "gemini-3.8-flash"} for t, s in CONFIG["tasks"].items()}}
    assert GeminiModel(same, client=FakeClient()).name == "gemini-3.8-flash"
    assert GeminiModel(CONFIG, client=FakeClient()).name == (
        "route=gemini-route,plan=gemini-plan,summarize=gemini-summary")


@pytest.mark.parametrize("response, message", [
    (reply(text=""), "empty"),
    (reply(text="   \n"), "empty"),
    (reply(text=None), "empty"),
    (reply(finish=types.FinishReason.SAFETY), "SAFETY"),
    (reply(finish=types.FinishReason.PROHIBITED_CONTENT), "PROHIBITED_CONTENT"),
    (reply(text="{not json"), "not valid JSON"),
    (reply(text='["route"]'), "list, not an object"),
    (reply(text='"answer"'), "str, not an object"),
    (reply(candidates=False), "no answer"),
])
def test_unusable_answers_raise(response, message):
    model, _ = model_with(response)
    with pytest.raises(ModelError, match=message):
        model.complete_structured("p", ROUTE_SCHEMA)


def test_a_failed_answer_still_counts_its_tokens():
    model, _ = model_with(reply(text="", finish=types.FinishReason.SAFETY, usage=(10, 0, 1024)))
    with pytest.raises(ModelError):
        model.complete_structured("p", ROUTE_SCHEMA)
    assert model.thinking_tokens == 1024


def test_unknown_schema_is_an_error():
    model, client = model_with()
    with pytest.raises(ValueError, match="no model task"):
        model.complete_structured("p", {"title": "other"})
    assert client.calls == []


def test_client_is_built_with_one_retry_on_429_and_5xx(monkeypatch):
    built = {}
    monkeypatch.setattr(gemini_model.genai, "Client", lambda **kw: built.update(kw) or "client")
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "my-project")
    assert GeminiModel(CONFIG)._get_client() == "client"
    assert (built["vertexai"], built["project"], built["location"]) == (True, "my-project", "global")
    options = built["http_options"]
    assert options.timeout == 60_000
    assert options.retry_options.attempts == 2  # the first try and one retry
    assert options.retry_options.http_status_codes == [429, 500, 502, 503, 504]


def test_the_shipped_config_loads_and_has_generous_caps():
    model = GeminiModel(client=FakeClient())
    for spec in model.tasks.values():
        assert spec["max_output_tokens"] == 2048 and spec["max_output_tokens_ceiling"] == 8192
    assert model.max_token_retries == 3
    assert model.tasks["route"]["thinking_level"] == "low"
    assert model.tasks["summarize"]["thinking_level"] == "low"
    assert "thinking_level" not in model.tasks["plan"] and "thinking_budget" not in model.tasks["plan"]
    assert model.config["location"] == "global" and model.config["temperature"] == 0
    assert "key" not in json.dumps(model.config).lower()


# --- thinking ---

def test_thinking_level_is_sent_for_route_and_summarize_only():
    model, client = model_with(reply(), reply('{"function": "x", "params": {}}'), reply('{"summary": "s"}'))
    model.complete_structured("p", ROUTE_SCHEMA)
    model.complete_structured("p", plan_schema(load()))
    model.complete_structured("p", SUMMARY_SCHEMA)
    route, plan, summary = (c["config"].thinking_config for c in client.calls)
    assert route.thinking_level == types.ThinkingLevel.LOW and route.thinking_budget is None
    assert plan is None  # plan keeps the model's default thinking
    assert summary.thinking_level == types.ThinkingLevel.LOW


def test_a_thinking_budget_is_sent_alone():
    config = {**CONFIG, "tasks": {**CONFIG["tasks"], "route": {
        "model": "m", "max_output_tokens": 1024, "thinking_budget": 256}}}
    client = FakeClient(reply())
    GeminiModel(config, client=client).complete_structured("p", ROUTE_SCHEMA)
    thinking = client.calls[0]["config"].thinking_config
    assert thinking.thinking_budget == 256 and thinking.thinking_level is None


def test_level_and_budget_together_are_refused_at_load():
    both = {**CONFIG, "tasks": {**CONFIG["tasks"], "route": {
        "model": "m", "max_output_tokens": 1024, "thinking_level": "low", "thinking_budget": 256}}}
    with pytest.raises(ValueError, match="not both"):
        GeminiModel(both, client=FakeClient())


# --- retry on MAX_TOKENS ---

CUT = dict(text="", finish=types.FinishReason.MAX_TOKENS)


def caps(client):
    return [c["config"].max_output_tokens for c in client.calls]


def with_caps(start, ceiling, retries=3):
    task = {"model": "m", "max_output_tokens": start, "max_output_tokens_ceiling": ceiling}
    return {**CONFIG, "max_token_retries": retries, "tasks": {**CONFIG["tasks"], "route": task}}


def test_max_tokens_twice_then_success_doubles_the_cap_each_time():
    client = FakeClient(reply(usage=(10, 0, 2048), **CUT), reply(usage=(10, 0, 4096), **CUT),
                        reply('{"route": "answer", "reason": "ok"}', usage=(10, 7, 300)))
    model = GeminiModel(with_caps(2048, 8192), client=client)
    assert model.complete_structured("p", ROUTE_SCHEMA) == {"route": "answer", "reason": "ok"}
    assert caps(client) == [2048, 4096, 8192]
    assert model.last_call == {"task": "route", "attempts": 3, "cap": 8192,
                               "tokens": {"prompt": 30, "output": 7, "thinking": 6444}}
    # every attempt is in the running totals
    assert (model.prompt_tokens, model.output_tokens, model.thinking_tokens) == (30, 7, 6444)


def test_still_cut_off_at_the_ceiling_raises_with_max_tokens():
    client = FakeClient(*[reply(**CUT) for _ in range(5)])
    model = GeminiModel(with_caps(2048, 8192), client=client)
    with pytest.raises(ModelError, match="MAX_TOKENS") as err:
        model.complete_structured("p", ROUTE_SCHEMA)
    assert err.value.finish_reason == "MAX_TOKENS"
    assert caps(client) == [2048, 4096, 8192]  # stops at once when the ceiling attempt is cut off
    assert model.last_call["attempts"] == 3 and model.last_call["cap"] == 8192


def test_a_start_of_1024_can_use_all_three_retries():
    client = FakeClient(*[reply(**CUT) for _ in range(5)])
    model = GeminiModel(with_caps(1024, 65536), client=client)
    with pytest.raises(ModelError) as err:
        model.complete_structured("p", ROUTE_SCHEMA)
    assert err.value.finish_reason == "MAX_TOKENS"
    assert caps(client) == [1024, 2048, 4096, 8192]  # four attempts: the first and three retries
    assert model.last_call["attempts"] == 4


def test_a_start_of_3000_with_a_ceiling_of_4096_tries_3000_then_4096_and_stops():
    client = FakeClient(reply(**CUT), reply(**CUT), reply())
    model = GeminiModel(with_caps(3000, 4096), client=client)
    with pytest.raises(ModelError, match="MAX_TOKENS"):
        model.complete_structured("p", ROUTE_SCHEMA)
    assert caps(client) == [3000, 4096] and len(client.responses) == 1


def test_zero_retries_means_one_attempt():
    client = FakeClient(reply(**CUT), reply())
    with pytest.raises(ModelError, match="MAX_TOKENS"):
        GeminiModel(with_caps(2048, 8192, retries=0), client=client).complete_structured("p", ROUTE_SCHEMA)
    assert caps(client) == [2048]


@pytest.mark.parametrize("finish", [
    types.FinishReason.SAFETY, types.FinishReason.PROHIBITED_CONTENT,
    types.FinishReason.RECITATION, types.FinishReason.OTHER,
])
def test_other_finish_reasons_are_not_retried(finish):
    client = FakeClient(reply(finish=finish), reply())
    model = GeminiModel(with_caps(2048, 8192), client=client)
    with pytest.raises(ModelError) as err:
        model.complete_structured("p", ROUTE_SCHEMA)
    assert err.value.finish_reason == finish.name
    assert caps(client) == [2048] and model.last_call["attempts"] == 1


def test_an_empty_answer_that_finished_with_stop_is_not_retried():
    client = FakeClient(reply(text=""), reply())
    with pytest.raises(ModelError, match="empty"):
        GeminiModel(with_caps(2048, 8192), client=client).complete_structured("p", ROUTE_SCHEMA)
    assert len(client.calls) == 1


def test_a_429_does_not_use_up_a_token_retry():
    """The SDK retries a 429 or 5xx inside generate_content, with the cap unchanged. Here the
    real SDK runs against a fake HTTP server: 429, a cut-off, 503, then a good answer."""
    import httpx
    from google import genai

    def body(text, finish, thinking=0):
        parts = [{"text": text}] if text else []
        return {"candidates": [{"content": {"role": "model", "parts": parts}, "finishReason": finish}],
                "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": len(text),
                                  "thoughtsTokenCount": thinking}}

    script = [
        (429, {"error": {"code": 429, "message": "quota", "status": "RESOURCE_EXHAUSTED"}}),
        (200, body("", "MAX_TOKENS", 2000)),
        (503, {"error": {"code": 503, "message": "busy", "status": "UNAVAILABLE"}}),
        (200, body('{"route": "answer", "reason": "ok"}', "STOP", 5)),
    ]
    sent = []

    def handler(request):
        sent.append(json.loads(request.content)["generationConfig"]["maxOutputTokens"])
        status, payload = script.pop(0)
        return httpx.Response(status, json=payload)

    model = GeminiModel(with_caps(2048, 8192, retries=1))  # one cap retry only
    options = model.http_options(httpx_client=httpx.Client(transport=httpx.MockTransport(handler)))
    options.retry_options.initial_delay = 0.01
    model._client = genai.Client(api_key="not-used", http_options=options)

    assert model.complete_structured("p", ROUTE_SCHEMA) == {"route": "answer", "reason": "ok"}
    # Four HTTP requests, but only two attempts: the 429 and the 503 were the SDK's retries and
    # left the cap alone, so the single allowed cap retry was still there for the cut-off.
    assert sent == [2048, 2048, 4096, 4096]
    assert model.last_call["attempts"] == 2 and model.last_call["cap"] == 4096


def test_last_call_belongs_to_the_calling_thread():
    import threading
    model, _ = model_with(reply())
    seen = []
    thread = threading.Thread(target=lambda: seen.append(model.last_call))
    model.complete_structured("p", ROUTE_SCHEMA)
    thread.start(); thread.join()
    assert model.last_call["task"] == "route" and seen == [None]


def test_threads_share_one_client(monkeypatch):
    import threading
    import time

    built = []

    def slow_client(**kwargs):
        time.sleep(0.05)  # long enough for every thread to reach the check
        built.append(kwargs)
        return FakeClient(*[reply() for _ in range(8)])

    monkeypatch.setattr(gemini_model.genai, "Client", slow_client)
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "my-project")
    model = GeminiModel(CONFIG)
    threads = [threading.Thread(target=lambda: model.complete_structured("p", ROUTE_SCHEMA))
               for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(built) == 1
