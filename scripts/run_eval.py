#!/usr/bin/env python3
"""Run the evaluation question set (eval/questions.yaml) against a model and score it.

Every request goes through the real graph with `today` pinned to the file's date, and with a
fake BigQuery client, so nothing is queried. A request that reaches the approval step is
declined, so nothing is executed. Only the model calls cost anything: route and plan, and
never summarize.

Usage, from the repo root:
    uv run python scripts/run_eval.py                          # keyword stub, free
    uv run python scripts/run_eval.py --model gemini --runs 3  # real model; bills Vertex AI

Writes eval/results/<model>-<date>.md and .json (the date is today's, not the pinned one),
and the audit lines for the run set to eval/results/audit-<model>-<date>.jsonl.

Scoring (the rules are in the questions file's header): the route must match. For "answer"
the function must match and the parameters must match exactly, after treating an omitted
parameter as its catalog default and ignoring an extra one that equals its default. A model
error that stops the request is a miss, whatever the route was supposed to be.
"""
import argparse
import json
import sys
import time
from collections import Counter
from datetime import date, datetime, timezone
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from langgraph.types import Command  # noqa: E402

from agent import GeminiModel, KeywordModel, build_graph  # noqa: E402
from functions import load  # noqa: E402
from functions.fake_client import StubClient  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
QUESTIONS = ROOT / "eval" / "questions.yaml"
GROUPS = ["plain", "relative_date", "refuse", "clarify", "defaults"]
TASKS = ["route", "plan", "summary"]


# --- recording what the model did ---

class Recorder:
    """Wraps a model and records every call: the task, the output or error, tokens, time."""

    def __init__(self, model):
        self.model = model
        self.name = getattr(model, "name", type(model).__name__)
        self.calls = []

    def _tokens(self):
        return [getattr(self.model, attr, 0) for attr in
                ("prompt_tokens", "output_tokens", "thinking_tokens")]

    def complete_structured(self, prompt, schema):
        before, start = self._tokens(), time.perf_counter()
        entry = {"task": schema["title"], "output": None, "error": None, "finish_reason": None}
        self.calls.append(entry)
        try:
            entry["output"] = self.model.complete_structured(prompt, schema)
            return entry["output"]
        except Exception as exc:
            entry["error"] = f"{type(exc).__name__}: {exc}"
            entry["finish_reason"] = getattr(exc, "finish_reason", None)
            raise
        finally:
            after = self._tokens()
            entry["tokens"] = dict(zip(("prompt", "output", "thinking"),
                                       (a - b for a, b in zip(after, before))))
            entry["seconds"] = round(time.perf_counter() - start, 3)


# --- scoring ---

def normalized(fn, params):
    """Every parameter of the function, with an omitted one (or None) as its catalog default."""
    out = {}
    for name, p in fn.params.items():
        value = (params or {}).get(name)
        out[name] = p.default if value is None else value
    return out


def describe(got):
    """One short phrase for what the agent did."""
    if got.get("model_errors") and got.get("status") == "error":
        return "model error: " + _error_text(got["model_errors"][0])
    if got.get("route") == "answer" or got.get("final_plan"):
        plan = got.get("final_plan") or {}
        prefix = "rejected " if got.get("rejected") else ""
        return f"{prefix}{plan.get('function')} {json.dumps(plan.get('params'), sort_keys=True)}"
    return str(got.get("route"))


def _error_text(error):
    return error.get("finish_reason") or error["error"]


def score(expect, got, catalog):
    """Return (passed, reason). `got` is a result record from run_one."""
    errors = got.get("model_errors") or []
    plan = got.get("final_plan")
    wanted = expect["route"]
    # A model error that stopped the request is a miss, even where a refusal was wanted.
    if got.get("status") == "error" or (wanted == "answer" and errors and plan is None):
        return False, ("model error: " + _error_text(errors[0]) if errors
                       else f"error: {got.get('error')}")
    route = got.get("route")
    if route != wanted:
        if route == "clarify":
            return False, f"asked a follow-up instead of {'answering' if wanted == 'answer' else 'refusing'}"
        if route == "refuse":
            return False, f"refused instead of {'answering' if wanted == 'answer' else 'asking a follow-up'}"
        return False, f"answered instead of {'refusing' if wanted == 'refuse' else 'asking a follow-up'}"
    if wanted != "answer":
        return True, ""

    function = plan.get("function") if isinstance(plan, dict) else None
    if function != expect["function"]:
        return False, f"wrong function {function} instead of {expect['function']}"
    if got.get("rejected"):
        return False, "rejected after re-plan: " + "; ".join(got.get("validation_errors") or [])
    fn = catalog.functions[expect["function"]]
    want = normalized(fn, expect.get("params"))
    have = normalized(fn, plan.get("params"))
    problems = [f"unknown param {n}={v}" for n, v in (plan.get("params") or {}).items()
                if n not in fn.params]
    for name in fn.params:
        if str(have[name]) == str(want[name]):
            continue
        given = (plan.get("params") or {}).get(name)
        if name in (expect.get("params") or {}):
            problems.append(f"{name} omitted instead of {want[name]}" if given is None
                            else f"{name} {have[name]} instead of {want[name]}")
        else:
            problems.append(f"extra param {name}={have[name]}")
    return (False, "; ".join(problems)) if problems else (True, "")


def tally(records):
    """Per-question and per-group counts from a list of scored result records."""
    per_question, per_group = {}, {}
    for r in records:
        q = per_question.setdefault(r["id"], {"group": r["group"], "runs": 0, "passed": 0,
                                              "reasons": Counter(), "got": Counter()})
        q["runs"] += 1
        q["passed"] += r["passed"]
        q["got"][r["got"]] += 1
        if not r["passed"]:
            q["reasons"][r["reason"]] += 1
        g = per_group.setdefault(r["group"], {"runs": 0, "passed": 0})
        g["runs"] += 1
        g["passed"] += r["passed"]
    return per_question, per_group


# --- running ---

def run_one(graph, recorder, question, audit_path):
    """Send one question through the graph, decline at approval, and describe what happened."""
    recorder.calls.clear()
    config = {"configurable": {"thread_id": f"{question['id']}-{time.monotonic_ns()}"}}
    start = time.perf_counter()
    state = graph.invoke({"question": question["question"]}, config)
    if "__interrupt__" in state:
        state = graph.invoke(Command(resume=False), config)
    seconds = time.perf_counter() - start

    plans = [c for c in recorder.calls if c["task"] == "plan"]
    first = plans[0]["output"] if plans else None
    tokens = {t: {"prompt": 0, "output": 0, "thinking": 0} for t in TASKS}
    for c in recorder.calls:
        for k, v in c["tokens"].items():
            tokens[c["task"]][k] += v
    return {
        "route": state.get("route"),
        "route_reason": state.get("route_reason"),
        "status": state.get("status"),
        "first_plan": first,
        "final_plan": state.get("plan"),
        "replanned": len(plans) > 1,
        "rejected": state.get("status") == "rejected",
        "validation_errors": state.get("validation_errors") or [],
        "error": state.get("error"),
        "model_errors": [{"task": c["task"], "error": c["error"], "finish_reason": c["finish_reason"]}
                         for c in recorder.calls if c["error"]],
        "tokens": tokens,
        "seconds": round(seconds, 3),
    }


def run_set(questions, model_kind, runs, today, out_dir, stamp):
    catalog = load()
    model = GeminiModel() if model_kind == "gemini" else KeywordModel(catalog)
    recorder = Recorder(model)
    audit_path = out_dir / f"audit-{model_kind}-{stamp}.jsonl"
    audit_path.unlink(missing_ok=True)
    graph = build_graph(catalog, recorder, client=StubClient(1000), audit_path=audit_path, today=today)

    records = []
    for q in questions:
        for run in range(1, runs + 1):
            got = run_one(graph, recorder, q, audit_path)
            passed, reason = score(q["expect"], got, catalog)
            got.update(id=q["id"], group=q["group"], question=q["question"], run=run,
                       expect=q["expect"], passed=passed, reason=reason)
            got["got"] = describe(got)
            records.append(got)
            print(f"{q['id']} run {run}: {'pass' if passed else 'MISS ' + reason}", flush=True)
    return recorder.name, records, audit_path


# --- the report ---

def cell(text):
    return str(text).replace("|", "\\|").replace("\n", " ")


def expected_text(expect):
    if expect["route"] != "answer":
        return expect["route"]
    return f"{expect['function']} {json.dumps(expect.get('params') or {}, sort_keys=True)}"


def totals(records):
    tokens = {t: {"prompt": 0, "output": 0, "thinking": 0} for t in TASKS}
    for r in records:
        for t in TASKS:
            for k in tokens[t]:
                tokens[t][k] += r["tokens"][t][k]
    errors = Counter(e["finish_reason"] or e["error"] for r in records for e in r["model_errors"])
    return {
        "requests": len(records),
        "replans": sum(r["replanned"] for r in records),
        "rejections": sum(r["rejected"] for r in records),
        "model_errors": sum(errors.values()),
        "model_error_kinds": dict(errors),
        "tokens": tokens,
        "total_tokens": sum(sum(t.values()) for t in tokens.values()),
        "seconds": round(sum(r["seconds"] for r in records), 1),
    }


def summary_lines(model_name, records, runs):
    per_question, per_group = tally(records)
    passed, total = sum(r["passed"] for r in records), len(records)
    t = totals(records)
    lines = [f"Overall: {passed}/{total} ({passed / total:.0%}) over {len(per_question)} questions x {runs} run(s)."]
    lines += [f"  {g}: {per_group[g]['passed']}/{per_group[g]['runs']}" for g in GROUPS if g in per_group]
    lines.append(f"Re-plans {t['replans']}, rejections {t['rejections']}, model errors {t['model_errors']}"
                 + (f" {t['model_error_kinds']}" if t["model_errors"] else "") + ".")
    lines.append(f"Tokens {t['total_tokens']:,} (including thinking), time {t['seconds']}s over {t['requests']} requests.")
    return lines


def markdown(model_name, records, runs, today, stamp):
    per_question, per_group = tally(records)
    t = totals(records)
    passed, total = sum(r["passed"] for r in records), len(records)
    out = [f"# Evaluation: {model_name}, {stamp}", "",
           f"- Model: `{model_name}`. Questions: {len(per_question)}. Runs per question: {runs}.",
           f"- Pinned date for relative phrases and the plan prompt: {today}.",
           "- BigQuery was not called (fake client). Every request that reached approval was declined.",
           "- Scoring: route must match; for an answer, the function must match and the parameters must match "
           "exactly after omitted parameters are read as catalog defaults (rules in `eval/questions.yaml`).", "",
           "## Result", "",
           f"**{passed}/{total} passed ({passed / total:.0%}).**", "",
           "| Group | Passed | Runs | Rate |", "|---|---|---|---|"]
    for g in GROUPS:
        if g in per_group:
            v = per_group[g]
            out.append(f"| {g} | {v['passed']} | {v['runs']} | {v['passed'] / v['runs']:.0%} |")
    out += ["", "## Questions", "",
            f"| Id | Group | Question | Expected | Got | Passed of {runs} | Reasons for misses |",
            "|---|---|---|---|---|---|---|"]
    by_id = {}
    for r in records:
        by_id.setdefault(r["id"], r)
    for qid, q in per_question.items():
        first = by_id[qid]
        got = "<br>".join(f"{cell(g)}" + (f" x{n}" if n > 1 else "") for g, n in q["got"].items())
        reasons = "<br>".join(f"{cell(r)}" + (f" x{n}" if n > 1 else "") for r, n in q["reasons"].items())
        out.append(f"| {qid} | {q['group']} | {cell(first['question'])} | {cell(expected_text(first['expect']))} "
                   f"| {got} | {q['passed']}/{q['runs']} | {reasons} |")
    out += ["", "## Re-plans, rejections and errors", "",
            f"- Requests: {t['requests']}. Re-plans: {t['replans']}. Rejected: {t['rejections']}. "
            f"Model errors: {t['model_errors']}" + (f" ({t['model_error_kinds']})" if t["model_errors"] else "") + ".",
            "", "## Tokens and time", "",
            "| Task | Prompt | Output | Thinking |", "|---|---|---|---|"]
    for task in TASKS:
        v = t["tokens"][task]
        out.append(f"| {task} | {v['prompt']:,} | {v['output']:,} | {v['thinking']:,} |")
    out += ["", f"Total tokens, including thinking: {t['total_tokens']:,}. "
            f"Time: {t['seconds']}s in model calls and graph, {t['seconds'] / t['requests']:.1f}s per request.", ""]
    return "\n".join(out)


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--model", choices=["stub", "gemini"], default="stub")
    parser.add_argument("--runs", type=int, default=1, help="times to ask each question (default 1)")
    parser.add_argument("--out", type=Path, default=ROOT / "eval" / "results")
    parser.add_argument("--questions", type=Path, default=QUESTIONS)
    args = parser.parse_args()
    if args.runs < 1:
        parser.error("--runs must be at least 1")

    spec = yaml.safe_load(args.questions.read_text())
    today = spec["today"] if isinstance(spec["today"], date) else date.fromisoformat(str(spec["today"]))
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    args.out.mkdir(parents=True, exist_ok=True)

    name, records, audit_path = run_set(spec["questions"], args.model, args.runs, today, args.out, stamp)

    base = args.out / f"{args.model}-{stamp}"
    base.with_suffix(".md").write_text(markdown(name, records, args.runs, today, stamp))
    per_question, per_group = tally(records)
    base.with_suffix(".json").write_text(json.dumps({
        "model": name, "date": stamp, "pinned_today": str(today), "runs": args.runs,
        "per_group": per_group,
        "per_question": {k: {**v, "reasons": dict(v["reasons"]), "got": dict(v["got"])}
                         for k, v in per_question.items()},
        "totals": totals(records), "records": records,
    }, indent=2, default=str))
    print()
    print("\n".join(summary_lines(name, records, args.runs)))
    print(f"\nWrote {base.with_suffix('.md')}, {base.with_suffix('.json')} and {audit_path}")


if __name__ == "__main__":
    main()
