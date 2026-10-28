"""resolve_period against hand-worked dates, and against the expected dates in the question set."""
from datetime import date

import pytest
import yaml
from resolve_period import resolve_period

from agent.graph import _terms
from functions.catalog import ConfigError, load
from run_eval import QUESTIONS

TODAY = date(2026, 10, 28)

@pytest.mark.parametrize("phrase, today, expected", [
    ("last year", TODAY, ("2025-01-01", "2025-12-31")),
    ("last quarter", TODAY, ("2026-07-01", "2026-09-30")),
    ("last quarter", date(2027, 2, 10), ("2026-10-01", "2026-12-31")),
    ("last month", TODAY, ("2026-09-01", "2026-09-30")),
    ("last month", date(2026, 1, 5), ("2025-12-01", "2025-12-31")),
    ("this year", TODAY, ("2026-01-01", "2026-10-28")),
    ("year to date", TODAY, ("2026-01-01", "2026-10-28")),
    ("this quarter", TODAY, ("2026-10-01", "2026-10-28")),
    ("this month", TODAY, ("2026-10-01", "2026-10-28")),
    ("September", TODAY, ("2026-09-01", "2026-09-30")),
    ("October", TODAY, ("2026-10-01", "2026-10-31")),      # started, though not complete
    ("November", TODAY, ("2025-11-01", "2025-11-30")),      # not started yet: last year's
    ("February", date(2024, 3, 1), ("2024-02-01", "2024-02-29")),
    ("Q4", TODAY, ("2026-10-01", "2026-12-31")),
    ("Q4", date(2026, 9, 30), ("2025-10-01", "2025-12-31")),
    ("Q2 2025", TODAY, ("2025-04-01", "2025-06-30")),
    ("last 3 months", TODAY, ("2026-07-01", "2026-09-30")),
    ("last 3 months", date(2026, 2, 10), ("2025-11-01", "2026-01-31")),
    ("last 12 months", TODAY, ("2025-10-01", "2026-09-30")),
    ("first half of this year", TODAY, ("2026-01-01", "2026-06-30")),
    ("second half of last year", TODAY, ("2025-07-01", "2025-12-31")),
    ("March 2026", TODAY, ("2026-03-01", "2026-03-31")),
    ("all time", TODAY, ("2019-01-01", "2026-10-28")),
])
def test_phrase(phrase, today, expected):
    assert resolve_period(phrase, today) == tuple(date.fromisoformat(d) for d in expected)


@pytest.mark.parametrize("phrase", ["last week", "yesterday", "fiscal 2025", "Q5", "this hour"])
def test_unsupported_phrases_have_no_rule(phrase):
    with pytest.raises(ValueError):
        resolve_period(phrase, TODAY)


def test_every_annotated_range_in_the_questions_matches():
    spec = yaml.safe_load(QUESTIONS.read_text())
    today = spec["today"]
    annotated = []
    for q in spec["questions"]:
        params = q["expect"].get("params") or {}
        if "start_date" not in params:
            assert "period" not in q, f"{q['id']} has a period but no expected dates"
            continue
        want = (date.fromisoformat(str(params["start_date"])), date.fromisoformat(str(params["end_date"])))
        if "period" in q:
            assert q["period"].lower() in q["question"].lower(), f"{q['id']}: period is not in the question"
            assert resolve_period(q["period"], today) == want, f"{q['id']}: {q['period']!r}"
            annotated.append(q["id"])
        else:  # a literal range: both dates must be written in the question
            assert params["start_date"] in q["question"] and params["end_date"] in q["question"], \
                f"{q['id']} has a date range that is neither a literal nor annotated with a period"
    assert len(annotated) == 9


def test_the_vocabulary_is_in_the_config_and_off_until_measured():
    catalog = load()
    assert len(catalog.vocabulary) == 11
    assert catalog.vocabulary_in_prompts is False
    assert _terms(catalog, False) == ""
    text = _terms(catalog, True)
    assert text.startswith("Terms:\n- ") and "no fiscal calendar" in text
    assert "give both start_date and end_date" in text


def test_vocabulary_in_prompts_must_be_a_boolean(tmp_path):
    from functions.catalog import DEFAULT_CONFIG
    bad = tmp_path / "t.yaml"
    bad.write_text(DEFAULT_CONFIG.read_text().replace("vocabulary_in_prompts: false", "vocabulary_in_prompts: no thanks"))
    with pytest.raises(ConfigError):
        load(bad)


def test_the_terms_reach_the_route_and_plan_prompts_only_when_on():
    from agent import KeywordModel, build_graph
    from functions.fake_client import StubClient
    catalog = load()
    seen = []

    class Spy(KeywordModel):
        def complete_structured(self, prompt, schema):
            seen.append((schema.get("title"), "Terms:" in prompt))
            return super().complete_structured(prompt, schema)

    for flag in (False, True):
        seen.clear()
        graph = build_graph(catalog, Spy(catalog), client=StubClient(1000), today=TODAY,
                            audit_path="/dev/null", vocabulary=flag)
        graph.invoke({"question": "Top 5 products by profit"}, {"configurable": {"thread_id": str(flag)}})
        assert {t for t, _ in seen} >= {"route", "plan"}
        assert all(has == flag for _, has in seen)
