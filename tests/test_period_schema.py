"""The "period" date schema, flatten_plan, and a plan call that fails. No cloud access."""
import jsonschema
import pytest

from agent.gemini_model import ModelError
from agent.graph import build_graph
from agent.models import flatten_plan, plan_schema
from functions.catalog import ConfigError, DEFAULT_CONFIG, load
from functions.fake_client import StubClient
from test_agent import TODAY, Agent, ScriptedModel
from test_hardening import ANSWER, prompt_for


@pytest.fixture(scope="module")
def catalog():
    return load()


@pytest.fixture(scope="module")
def flat(catalog):
    return plan_schema(catalog, "flat")


@pytest.fixture(scope="module")
def period(catalog):
    return plan_schema(catalog, "period")


def plan(period_value, **params):
    return {"function": "revenue_by_period", "params": {"period": period_value, **params}}


def test_the_shipped_default_is_period(catalog, flat, period):
    assert catalog.date_schema == "period"
    assert plan_schema(catalog) == period
    assert "start_date" in flat["properties"]["params"]["properties"]
    assert "required" not in flat["properties"]["params"]


def test_the_period_schema_is_valid_json_schema(period):
    jsonschema.Draft202012Validator.check_schema(period)


def test_period_replaces_the_two_dates(flat, period):
    props = period["properties"]["params"]["properties"]
    assert "start_date" not in props and "end_date" not in props
    flat_props = flat["properties"]["params"]["properties"]
    assert {k: v for k, v in props.items() if k != "period"} == \
        {k: v for k, v in flat_props.items() if k not in ("start_date", "end_date")}
    assert period["properties"]["params"]["required"] == ["period"]
    assert period["title"] == "plan"


def test_period_accepts_null_or_both_dates(period):
    v = jsonschema.Draft202012Validator(period)
    assert v.is_valid(plan(None))
    assert v.is_valid(plan({"start_date": "2025-01-01", "end_date": "2025-12-31"}, group_by="month"))


@pytest.mark.parametrize("bad", [
    plan({"start_date": "2025-01-01"}),
    plan({"end_date": "2025-12-31"}),
    plan({}),
    plan({"start_date": "2025-01-01", "end_date": "2025-12-31", "extra": 1}),
    plan("2025"),
    {"function": "revenue_by_period", "params": {}},                       # period is required
    {"function": "revenue_by_period", "params": {"period": None, "start_date": "2025-01-01"}},
])
def test_period_rejects(period, bad):
    assert not jsonschema.Draft202012Validator(period).is_valid(bad)


def test_flat_mode_still_takes_separate_dates(flat):
    v = jsonschema.Draft202012Validator(flat)
    assert v.is_valid({"function": "revenue_by_period", "params": {"start_date": "2025-01-01"}})
    assert not v.is_valid(plan(None))


def test_flatten_null_period_gives_no_dates():
    assert flatten_plan(plan(None, group_by="month")) == \
        {"function": "revenue_by_period", "params": {"group_by": "month"}}


def test_flatten_round_trip():
    got = flatten_plan(plan({"start_date": "2025-01-01", "end_date": "2025-12-31"}, group_by="quarter"))
    assert got == {"function": "revenue_by_period",
                   "params": {"group_by": "quarter", "start_date": "2025-01-01", "end_date": "2025-12-31"}}


@pytest.mark.parametrize("p", [
    {"function": "revenue_by_period", "params": {"start_date": "2025-01-01"}},   # already flat
    {"function": "x", "params": {"period": "2025"}},                              # not null or object
    None, "not a plan", {"function": "x"}, {"function": "x", "params": []},
])
def test_flatten_leaves_other_shapes_alone(p):
    assert flatten_plan(p) == p


def test_flatten_keeps_a_half_period_so_the_pair_check_still_fires():
    got = flatten_plan(plan({"start_date": "2025-01-01"}))
    assert got["params"] == {"start_date": "2025-01-01"}


def test_date_schema_must_be_known(tmp_path):
    bad = tmp_path / "t.yaml"
    bad.write_text(DEFAULT_CONFIG.read_text().replace("date_schema: period", "date_schema: wide"))
    with pytest.raises(ConfigError):
        load(bad)


# --- through the graph ---

def test_the_graph_flattens_before_validation_and_approval(catalog, tmp_path):
    model = ScriptedModel(route=[ANSWER], plan=[plan({"start_date": "2025-01-01", "end_date": "2025-12-31"})])
    graph = build_graph(catalog, model, client=StubClient(1000), audit_path=tmp_path / "a.jsonl",
                        today=TODAY, date_schema="period")
    state = graph.invoke({"question": "revenue for 2025"}, {"configurable": {"thread_id": "t"}})
    (pending,) = state["__interrupt__"]
    assert pending.value["params"] == {"start_date": "2025-01-01", "end_date": "2025-12-31"}
    assert state["plan"]["params"] == pending.value["params"]
    assert state["raw_plan"]["params"]["period"] == {"start_date": "2025-01-01", "end_date": "2025-12-31"}


def test_null_period_runs_the_default_window(catalog, tmp_path):
    model = ScriptedModel(route=[ANSWER], plan=[plan(None)])
    graph = build_graph(catalog, model, client=StubClient(1000), audit_path=tmp_path / "a.jsonl",
                        today=TODAY, date_schema="period")
    state = graph.invoke({"question": "revenue"}, {"configurable": {"thread_id": "t"}})
    assert state["__interrupt__"][0].value["params"] == {}


def test_the_prompt_matches_the_schema(catalog, tmp_path):
    for mode, has in (("flat", "start_date: "), ("period", "period: null")):
        model = ScriptedModel(route=[ANSWER], plan=[plan(None) if mode == "period" else
                                                    {"function": "revenue_by_period", "params": {}}])
        graph = build_graph(catalog, model, client=StubClient(1000), audit_path=tmp_path / "a.jsonl",
                            today=TODAY, date_schema=mode)
        graph.invoke({"question": "revenue"}, {"configurable": {"thread_id": mode}})
        text = prompt_for(model, "plan")
        assert has in text
        if mode == "period":
            assert "period is null when the question names no period" in text
            assert "start_date: " not in text and "give neither start_date" not in text
        else:
            assert "give neither start_date nor end_date" in text and "period: null" not in text


# --- a plan call that fails keeps its own error ---

class FailingPlan(ScriptedModel):
    def complete_structured(self, prompt, schema):
        if schema["title"] == "plan":
            self.prompts.append(("plan", prompt))
            raise ModelError("ServerError: 504 DEADLINE_EXCEEDED. Deadline expired")
        return super().complete_structured(prompt, schema)


def test_a_failed_plan_call_is_reported_as_that_error(catalog, tmp_path):
    agent = Agent(catalog, tmp_path, model=FailingPlan(route=[ANSWER]))
    state = agent.ask("Top 5 products by profit")
    assert agent.model.titles().count("plan") == 2          # one re-plan, as before
    assert state["status"] == "error"
    assert "504 DEADLINE_EXCEEDED" in state["error"] and "not an object" not in state["error"]
    assert "504 DEADLINE_EXCEEDED" in state["message"]
    (line,) = agent.audit_lines()
    assert line["status"] == "error" and "504 DEADLINE_EXCEEDED" in line["error"]
    assert "not an object" not in line["error"]


def test_a_bad_plan_is_still_rejected_not_an_error(catalog, tmp_path):
    bad = {"function": "nope", "params": {}}
    agent = Agent(catalog, tmp_path, model=ScriptedModel(route=[ANSWER], plan=[bad, bad]))
    state = agent.ask("anything")
    assert state["status"] == "rejected"
