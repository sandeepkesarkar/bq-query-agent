#!/usr/bin/env python3
"""Run the evaluation question set (eval/questions.yaml) against a model and score it.

Every request goes through the real graph with `today` pinned to the file's date, and with a
fake BigQuery client, so nothing is queried. A request that reaches the approval step is
declined, so nothing is executed. Only the model calls cost anything: route and plan, and
never summarize.

Usage, from the repo root:
    uv run python scripts/run_eval.py                          # keyword stub, free
    uv run python scripts/run_eval.py --model gemini --runs 3  # real model; bills Vertex AI
    uv run python scripts/run_eval.py --model gemini --runs 3 --workers 4 --label after-fix1
    uv run python scripts/run_eval.py --model gemini --ids q02,q15 --set date_schema=period \\
        --set tasks.plan.thinking_level=medium

--set KEY=VALUE changes one config value for this run only, without editing a file. KEY is
date_schema or vocabulary_in_prompts (config/thelook.yaml), or a path into config/model.yaml
such as tasks.plan.thinking_level or request_timeout_seconds; a VALUE of null removes the key.
--ids q01,q02 runs only those questions.

Writes eval/results/<label>-<date>.md and .json (the label defaults to the model kind; the
date is today's, not the pinned one), and the audit lines for the run set to
eval/results/audit-<label>-<date>.jsonl. --workers N runs N different questions at a time;
the runs of one question stay in order.

Scoring (the rules are in the questions file's header): the route must match. For "answer"
the function must match and the parameters must match exactly, after treating an omitted
parameter as its catalog default and ignoring an extra one that equals its default. A model
error that stops the request is a miss, whatever the route was supposed to be.
"""
import argparse
import dataclasses
import json
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from langgraph.types import Command  # noqa: E402

from agent import GeminiModel, KeywordModel, build_graph  # noqa: E402
from agent.gemini_model import DEFAULT_CONFIG as MODEL_CONFIG  # noqa: E402
from agent.models import flatten_plan  # noqa: E402
from functions import load  # noqa: E402
from functions.catalog import DATE_SCHEMAS  # noqa: E402
from functions.fake_client import StubClient  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
QUESTIONS = ROOT / "eval" / "questions.yaml"
GROUPS = ["plain", "relative_date", "refuse", "clarify", "defaults"]
TASKS = ["route", "plan", "summary"]
CATALOG_KEYS = ("date_schema", "vocabulary_in_prompts")   # config/thelook.yaml; the rest is model.yaml


# --- recording what the model did ---

class Recorder:
    """Wraps a model for one request and records every call: the task, the output or error,
    the attempts it took, the cap it finished with, tokens and time. Make one per request, so
    requests running in parallel do not mix."""

    def __init__(self, model):
        self.model = model
        self.name = getattr(model, "name", type(model).__name__)
        self.calls = []

    @property
    def last_call(self):
        return getattr(self.model, "last_call", None)  # the graph reads this for the audit line

    def complete_structured(self, prompt, schema):
        start = time.perf_counter()
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
            info = self.last_call or {}
            entry["attempts"] = info.get("attempts", 1)
            entry["cap"] = info.get("cap")
            entry["tokens"] = dict(info.get("tokens") or {"prompt": 0, "output": 0, "thinking": 0})
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

def run_one(graph, recorder, question):
    """Send one question through the graph, decline at approval, and describe what happened.
    `recorder` must be the one the graph was built with, and used for this request only."""
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
        "first_plan": first,                      # as the model wrote it
        "first_plan_flat": flatten_plan(first),   # the same, as start_date and end_date
        "final_plan": state.get("plan"),
        "replanned": len(plans) > 1,
        "rejected": state.get("status") == "rejected",
        "validation_errors": state.get("validation_errors") or [],
        "error": state.get("error"),
        "model_errors": [{"task": c["task"], "error": c["error"], "finish_reason": c["finish_reason"]}
                         for c in recorder.calls if c["error"]],
        "calls": [{k: c[k] for k in ("task", "attempts", "cap", "finish_reason", "seconds")}
                  | {"failed": bool(c["error"])} for c in recorder.calls],
        "tokens": tokens,
        "seconds": round(seconds, 3),
    }


def parse_sets(items):
    """['a.b=1', 'c=true'] -> {'a.b': 1, 'c': True}. The value is read as YAML."""
    out = {}
    for item in items or []:
        key, sep, value = item.partition("=")
        if not sep or not key.strip():
            raise ValueError(f"--set wants KEY=VALUE, not {item!r}")
        out[key.strip()] = yaml.safe_load(value)
    return out


def apply_sets(catalog, config, sets):
    """Return (catalog, model config) with the --set values applied. Neither input is changed.
    A key that does not exist in the file it belongs to is an error, so a typo cannot pass."""
    config = json.loads(json.dumps(config))
    changes = {}
    for key, value in sets.items():
        if key in CATALOG_KEYS:
            if key == "date_schema" and value not in DATE_SCHEMAS:
                raise ValueError(f"date_schema must be one of {list(DATE_SCHEMAS)}, not {value!r}")
            if key == "vocabulary_in_prompts" and not isinstance(value, bool):
                raise ValueError("vocabulary_in_prompts must be true or false")
            changes[key] = value
            continue
        *path, leaf = key.split(".")
        node = config
        for part in path:
            if not isinstance(node, dict) or part not in node:
                raise ValueError(f"--set {key}: {part!r} is not in config/model.yaml")
            node = node[part]
        if not isinstance(node, dict) or (leaf not in node and not (path and path[0] == "tasks")):
            raise ValueError(f"--set {key}: not a key of config/model.yaml")
        if value is None:
            node.pop(leaf, None)
        else:
            node[leaf] = value
    return dataclasses.replace(catalog, **changes), config


def run_set(questions, model_kind, runs, today, out_dir, label, stamp, workers=1, sets=None, fresh=True):
    catalog, config = apply_sets(load(), yaml.safe_load(MODEL_CONFIG.read_text()), sets or {})
    if model_kind == "gemini":
        model = GeminiModel(config=config)
    elif set(sets or {}) <= set(CATALOG_KEYS):
        model = KeywordModel(catalog)
    else:
        raise ValueError("--set of a model.yaml key needs --model gemini")
    audit_path = out_dir / f"audit-{label}-{stamp}.jsonl"
    if fresh:
        audit_path.unlink(missing_ok=True)

    def run_question(q):
        records = []
        for run in range(1, runs + 1):
            # A graph and a recorder per request, so parallel requests keep separate records.
            recorder = Recorder(model)
            graph = build_graph(catalog, recorder, client=StubClient(1000),
                                audit_path=audit_path, today=today)
            got = run_one(graph, recorder, q)
            passed, reason = score(q["expect"], got, catalog)
            got.update(id=q["id"], group=q["group"], question=q["question"], run=run,
                       expect=q["expect"], passed=passed, reason=reason)
            got["got"] = describe(got)
            records.append(got)
            print(f"{q['id']} run {run}: {'pass' if passed else 'MISS ' + reason}", flush=True)
        return records

    with ThreadPoolExecutor(max_workers=workers) as pool:
        records = [r for batch in pool.map(run_question, questions) for r in batch]
    return getattr(model, "name", type(model).__name__), records, audit_path


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
    # Per task: how many calls were made, how many needed more than one attempt, and how many
    # failed after their last attempt. Kept apart from the pass rate so escalation is visible.
    calls = {t: {"calls": 0, "escalated": 0, "failed": 0} for t in TASKS}
    for r in records:
        for c in r["calls"]:
            row = calls[c["task"]]
            row["calls"] += 1
            row["escalated"] += c["attempts"] > 1
            row["failed"] += c["failed"]
    return {
        "calls": calls,
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
    lines.append("Calls needing more than one attempt / failed after the last attempt / calls: "
                 + ", ".join(f"{task} {v['escalated']}/{v['failed']}/{v['calls']}"
                             for task, v in t["calls"].items() if v["calls"]) + ".")
    lines.append(f"Tokens {t['total_tokens']:,} (including thinking), time {t['seconds']}s over {t['requests']} requests.")
    return lines


def markdown(model_name, records, runs, today, stamp, label=None):
    per_question, per_group = tally(records)
    t = totals(records)
    passed, total = sum(r["passed"] for r in records), len(records)
    out = [f"# Evaluation: {label or model_name}, {stamp}", "",
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
            "", "## Escalation (separate from the score)", "",
            "A call is escalated when it needed more than one attempt (the output cap was doubled after "
            "MAX_TOKENS). Failed means it still ended in an error after its last attempt.", "",
            "| Task | Calls | Needed more than one attempt | Failed after the last attempt |",
            "|---|---|---|---|"]
    for task, v in t["calls"].items():
        out.append(f"| {task} | {v['calls']} | {v['escalated']} | {v['failed']} |")
    out += ["", "## Tokens and time", "",
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
    parser.add_argument("--questions", type=Path, default=QUESTIONS,
                        help="the questions file (default eval/questions.yaml)")
    parser.add_argument("--label", help="names the output files (default: the model kind)")
    parser.add_argument("--workers", type=int, default=1,
                        help="questions to run at the same time (default 1)")
    parser.add_argument("--set", action="append", default=[], metavar="KEY=VALUE", dest="sets",
                        help="override one config value for this run (see the top of this file)")
    parser.add_argument("--ids", help="only these question ids, comma separated")
    args = parser.parse_args()
    if args.runs < 1 or args.workers < 1:
        parser.error("--runs and --workers must be at least 1")
    label = args.label or args.model

    spec = yaml.safe_load(args.questions.read_text())
    today = spec["today"] if isinstance(spec["today"], date) else date.fromisoformat(str(spec["today"]))
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    args.out.mkdir(parents=True, exist_ok=True)

    questions = spec["questions"]
    if args.ids:
        wanted = [i.strip() for i in args.ids.split(",")]
        unknown = set(wanted) - {q["id"] for q in questions}
        if unknown:
            parser.error(f"unknown question ids: {sorted(unknown)}")
        questions = [q for q in questions if q["id"] in wanted]
    try:
        sets = parse_sets(args.sets)
        apply_sets(load(), yaml.safe_load(MODEL_CONFIG.read_text()), sets)   # fail before any call
    except ValueError as exc:
        parser.error(str(exc))

    name, records, audit_path = run_set(questions, args.model, args.runs, today, args.out,
                                        label, stamp, args.workers, sets)

    base = args.out / f"{label}-{stamp}"
    base.with_suffix(".md").write_text(markdown(name, records, args.runs, today, stamp, label))
    per_question, per_group = tally(records)
    base.with_suffix(".json").write_text(json.dumps({
        "model": name, "label": label, "workers": args.workers, "sets": sets, "date": stamp, "pinned_today": str(today), "runs": args.runs,
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
