"""The evaluation scorer and the run counting. No cloud access, no model."""
import pytest

from functions.catalog import load
from run_eval import describe, score, tally, totals


@pytest.fixture(scope="module")
def catalog():
    return load()


def got(route="answer", function="top_products", params=None, **kw):
    record = {"route": route, "status": "declined", "final_plan": None, "rejected": False,
              "validation_errors": [], "model_errors": []}
    if route == "answer":
        record["final_plan"] = {"function": function, "params": params or {}}
    return {**record, **kw}


def expect(function="top_products", **params):
    return {"route": "answer", "function": function, "params": params}


# --- parameters ---

def test_omitted_parameter_equals_its_default(catalog):
    # top_products defaults: metric revenue, n 10, no category, whole window.
    assert score(expect(), got(), catalog) == (True, "")
    assert score(expect(), got(params={"metric": "revenue", "n": 10}), catalog) == (True, "")


def test_explicit_default_in_the_expectation_matches_an_omission(catalog):
    assert score(expect(metric="revenue"), got(), catalog) == (True, "")


def test_matching_values_pass(catalog):
    params = {"metric": "profit", "n": 5, "start_date": "2026-01-01", "end_date": "2026-09-30"}
    assert score(expect(**params), got(params=dict(params)), catalog) == (True, "")


def test_an_extra_parameter_is_a_miss(catalog):
    assert score(expect(), got(params={"n": 3}), catalog) == (False, "extra param n=3")


def test_a_wrong_value_is_a_miss_with_both_values(catalog):
    want = expect(start_date="2026-01-01", end_date="2026-09-30")
    have = got(params={"start_date": "2026-01-01", "end_date": "2026-12-31"})
    assert score(want, have, catalog) == (False, "end_date 2026-12-31 instead of 2026-09-30")


def test_an_omitted_required_value_is_a_miss(catalog):
    want = expect(start_date="2025-01-01", end_date="2025-12-31")
    passed, reason = score(want, got(), catalog)
    assert not passed and reason == ("start_date omitted instead of 2025-01-01; "
                                     "end_date omitted instead of 2025-12-31")


def test_an_unknown_parameter_is_a_miss(catalog):
    passed, reason = score(expect(), got(params={"sql": "SELECT 1"}), catalog)
    assert not passed and "unknown param sql=SELECT 1" in reason


# --- function and route ---

def test_wrong_function(catalog):
    assert score(expect(), got(function="revenue_by_period"), catalog) == (
        False, "wrong function revenue_by_period instead of top_products")


def test_a_rejected_plan_is_a_miss(catalog):
    have = got(rejected=True, status="rejected",
               validation_errors=["start_date given without end_date: give both"])
    passed, reason = score(expect(), have, catalog)
    assert not passed and reason.startswith("rejected after re-plan: start_date given")


@pytest.mark.parametrize("wanted, route, reason", [
    ("answer", "clarify", "asked a follow-up instead of answering"),
    ("answer", "refuse", "refused instead of answering"),
    ("refuse", "answer", "answered instead of refusing"),
    ("refuse", "clarify", "asked a follow-up instead of refusing"),
    ("clarify", "refuse", "refused instead of asking a follow-up"),
    ("clarify", "answer", "answered instead of asking a follow-up"),
])
def test_wrong_route(catalog, wanted, route, reason):
    have = got(route=route)
    assert score({"route": wanted, "function": "top_products", "params": {}}, have, catalog) == (False, reason)


@pytest.mark.parametrize("route", ["refuse", "clarify"])
def test_right_route_without_an_answer_passes(catalog, route):
    assert score({"route": route}, got(route=route), catalog) == (True, "")


# --- model errors ---

ERROR = {"task": "route", "error": "ModelError: route: ...", "finish_reason": "MAX_TOKENS"}


def test_a_route_error_is_a_miss_even_when_a_refusal_was_wanted(catalog):
    # The graph turns a route error into route "refuse", which would otherwise look right.
    have = got(route="refuse", status="error", model_errors=[ERROR])
    assert score({"route": "refuse"}, have, catalog) == (False, "model error: MAX_TOKENS")


def test_a_plan_that_never_arrived_is_a_model_error(catalog):
    have = got(status="rejected", rejected=True, model_errors=[{**ERROR, "task": "plan"}] * 2)
    have["final_plan"] = None
    assert score(expect(), have, catalog) == (False, "model error: MAX_TOKENS")


def test_a_model_error_without_a_finish_reason_shows_the_message(catalog):
    have = got(route="refuse", status="error",
               model_errors=[{"task": "route", "error": "403 PERMISSION_DENIED", "finish_reason": None}])
    assert score({"route": "refuse"}, have, catalog) == (False, "model error: 403 PERMISSION_DENIED")


def test_a_recovered_plan_error_still_passes(catalog):
    have = got(model_errors=[{**ERROR, "task": "plan"}])
    assert score(expect(), have, catalog) == (True, "")


def test_describe(catalog):
    assert describe(got(params={"n": 3})) == 'top_products {"n": 3}'
    assert describe(got(route="clarify")) == "clarify"
    assert describe(got(route="refuse", status="error", model_errors=[ERROR])) == "model error: MAX_TOKENS"


# --- counting over N runs ---

def record(qid, group, passed, reason="", got_text="x", **kw):
    return {"id": qid, "group": group, "passed": passed, "reason": reason, "got": got_text,
            "tokens": {t: {"prompt": 1, "output": 2, "thinking": 3} for t in ("route", "plan", "summary")},
            "replanned": False, "rejected": False, "model_errors": [], "seconds": 1.0, **kw}


def test_n_run_counting():
    records = [
        record("q1", "plain", True), record("q1", "plain", False, "extra param n=3"),
        record("q1", "plain", True),
        record("q2", "refuse", False, "answered instead of refusing"),
        record("q2", "refuse", False, "answered instead of refusing"),
        record("q2", "refuse", False, "model error: MAX_TOKENS"),
    ]
    per_question, per_group = tally(records)
    assert (per_question["q1"]["passed"], per_question["q1"]["runs"]) == (2, 3)
    assert per_question["q1"]["reasons"] == {"extra param n=3": 1}
    assert (per_question["q2"]["passed"], per_question["q2"]["runs"]) == (0, 3)
    assert per_question["q2"]["reasons"]["answered instead of refusing"] == 2
    assert per_group == {"plain": {"runs": 3, "passed": 2}, "refuse": {"runs": 3, "passed": 0}}


def test_totals_count_replans_rejections_errors_and_tokens():
    records = [record("q1", "plain", True, replanned=True),
               record("q2", "plain", False, rejected=True, replanned=True),
               record("q3", "plain", False, model_errors=[ERROR, ERROR])]
    t = totals(records)
    assert (t["requests"], t["replans"], t["rejections"], t["model_errors"]) == (3, 2, 1, 2)
    assert t["model_error_kinds"] == {"MAX_TOKENS": 2}
    assert t["tokens"]["plan"] == {"prompt": 3, "output": 6, "thinking": 9}
    assert t["total_tokens"] == 3 * 3 * 6  # three requests, three tasks, 1 + 2 + 3 tokens each


# --- the runner, end to end with the keyword stub ---

def test_run_set_with_the_stub(tmp_path):
    from datetime import date

    import yaml

    from run_eval import QUESTIONS, markdown, run_set
    spec = yaml.safe_load(QUESTIONS.read_text())
    questions = [q for q in spec["questions"] if q["id"] in ("q03", "q17")]
    name, records, audit_path = run_set(questions, "stub", 2, date(2026, 10, 28), tmp_path, "2026-10-28")
    assert name == "keyword-stub" and len(records) == 4
    assert [r["run"] for r in records] == [1, 2, 1, 2]
    assert all(r["passed"] for r in records)
    assert records[0]["status"] == "declined"  # stopped at approval, nothing executed
    assert audit_path.parent == tmp_path and len(audit_path.read_text().splitlines()) == 4
    report = markdown(name, records, 2, date(2026, 10, 28), "2026-10-28")
    assert "**4/4 passed (100%).**" in report and "| q03 | plain |" in report and "2/2" in report


def test_the_plan_prompt_uses_the_pinned_date(catalog, tmp_path):
    from datetime import date

    from agent import build_graph
    from functions.fake_client import StubClient
    from run_eval import Recorder, run_one

    class Capture:
        name = "capture"
        prompts = []

        def complete_structured(self, prompt, schema):
            self.prompts.append((schema["title"], prompt))
            return ({"route": "answer", "reason": "ok"} if schema["title"] == "route"
                    else {"function": "order_status_breakdown", "params": {}})

    model = Capture()
    recorder = Recorder(model)
    graph = build_graph(catalog, recorder, client=StubClient(1000), audit_path=tmp_path / "a.jsonl",
                        today=date(2026, 10, 28))
    run_one(graph, recorder, {"id": "q", "question": "order status"}, tmp_path / "a.jsonl")
    plan_prompt = next(p for t, p in model.prompts if t == "plan")
    assert "Today's date (UTC) is 2026-10-28" in plan_prompt
