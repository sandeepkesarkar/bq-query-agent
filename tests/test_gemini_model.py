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
    "request_timeout_seconds": 60, "retry_attempts": 2,
    "tasks": {
        "route": {"model": "gemini-route", "max_output_tokens": 1024},
        "plan": {"model": "gemini-plan", "max_output_tokens": 2048},
        "summarize": {"model": "gemini-summary", "max_output_tokens": 3000},
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
    (reply(finish=types.FinishReason.MAX_TOKENS), "MAX_TOKENS"),
    (reply(text="", finish=types.FinishReason.MAX_TOKENS), "MAX_TOKENS"),  # the thinking-token case
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
    model, _ = model_with(reply(text="", finish=types.FinishReason.MAX_TOKENS, usage=(10, 0, 1024)))
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
    caps = {task: spec["max_output_tokens"] for task, spec in model.tasks.items()}
    assert caps["route"] >= 1024 and caps["plan"] >= 2048 and caps["summarize"] >= 2048
    assert model.config["location"] == "global" and model.config["temperature"] == 0
    assert "key" not in json.dumps(model.config).lower()
