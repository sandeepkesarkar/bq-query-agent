"""run_probe's arithmetic, and a round-robin run with the keyword stub. No cloud access."""
from datetime import date

import pytest
import yaml

from functions.catalog import load
from run_eval import QUESTIONS, apply_sets, parse_sets
from run_probe import error_kind, lone_date, metrics, parse_variant, percentile
from run_eval import run_set


def test_parse_variant_and_sets():
    assert parse_variant("P1 tasks.plan.thinking_level=medium date_schema=period") == \
        ("P1", {"tasks.plan.thinking_level": "medium", "date_schema": "period"})
    assert parse_variant("P0") == ("P0", {})
    assert parse_sets(["vocabulary_in_prompts=true", "x=null"]) == {"vocabulary_in_prompts": True, "x": None}
    with pytest.raises(ValueError):
        parse_sets(["no equals sign"])


def test_apply_sets_changes_copies_only():
    catalog = load()
    config = {"temperature": 0, "tasks": {"plan": {"model": "m"}}}
    c2, cfg2 = apply_sets(catalog, config, {"date_schema": "flat", "tasks.plan.thinking_level": "medium",
                                            "temperature": 0.5})
    assert c2.date_schema == "flat" and catalog.date_schema == "period"
    assert cfg2["tasks"]["plan"]["thinking_level"] == "medium" and cfg2["temperature"] == 0.5
    assert config == {"temperature": 0, "tasks": {"plan": {"model": "m"}}}
    _, cfg3 = apply_sets(catalog, cfg2, {"tasks.plan.thinking_level": None})
    assert "thinking_level" not in cfg3["tasks"]["plan"]


@pytest.mark.parametrize("sets", [{"date_schema": "wide"}, {"vocabulary_in_prompts": "yes"},
                                  {"tasks.nope.thinking_level": "low"}, {"temprature": 1}])
def test_apply_sets_refuses_typos_and_bad_values(sets):
    with pytest.raises(ValueError):
        apply_sets(load(), {"temperature": 0, "tasks": {"plan": {}}}, sets)


def test_percentile_and_error_kind():
    assert percentile([], 90) is None
    assert [percentile([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], q) for q in (50, 90, 100)] == [5, 9, 10]
    assert error_kind("ServerError: 504 DEADLINE_EXCEEDED") == "504"
    assert error_kind("ClientError: 429 RESOURCE_EXHAUSTED") == "429"
    assert error_kind("MAX_TOKENS") == "other"


def test_lone_date():
    p = lambda **kw: {"function": "f", "params": kw}  # noqa: E731
    assert lone_date(p(start_date="2025-01-01")) and lone_date(p(end_date="2025-01-01"))
    assert not lone_date(p(start_date="2025-01-01", end_date="2025-12-31"))
    assert not lone_date(p()) and not lone_date(None) and not lone_date(p(start_date=None))


def test_round_robin_run_with_the_stub(tmp_path):
    questions = [q for q in yaml.safe_load(QUESTIONS.read_text())["questions"] if q["id"] in ("q03", "q05")]
    catalog = load()
    by_variant = {}
    for name, sets in [("P0", {}), ("P2", {"date_schema": "period"})]:
        recs = []
        for rnd in range(2):
            _, got, path = run_set(questions, "stub", 2, date(2026, 10, 28), tmp_path, f"probe-t-{name}",
                                   "2026-10-28", 1, sets, fresh=(rnd == 0))
            recs += got
        by_variant[name] = recs
        assert len(path.read_text().splitlines()) == 8      # the audit file kept all rounds
    m = metrics(by_variant["P0"], catalog)
    assert m["requests"] == 8 and m["final_pass"] == 8 and m["first_plan_pass"] == 8
    assert m["first_plan_lone"] == 0 and m["model_errors"] == {"504": 0, "429": 0, "other": 0}
    assert m["plan_seconds"]["calls"] == 8 and m["below_80_percent"] == {}
