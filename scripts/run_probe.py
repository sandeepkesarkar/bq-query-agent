#!/usr/bin/env python3
"""Compare configurations of the agent on a few questions, round robin.

A variant is a name plus --set overrides (the same KEY=VALUE as run_eval.py). Each round runs
every variant for --per-round runs of every chosen question, then the next round starts, so
all variants meet the same Vertex conditions (a slow hour hits each of them, not just the one
that happened to be running). --runs is the total per question per variant.

    uv run python scripts/run_probe.py --model gemini --label dates --ids q02,q06,q07 \\
        --runs 10 --per-round 2 --workers 2 \\
        --variant "P0" \\
        --variant "P1 tasks.plan.thinking_level=medium" \\
        --variant "P2 date_schema=period"

Writes eval/results/probe-<label>.json and .md (the audit lines go to
eval/results/audit-probe-<label>-<variant>.jsonl). The JSON is rewritten after every round, so
a stopped probe keeps what it finished.

Per variant, the report gives:
  first-plan pass   the first plan, once flattened, would score as a pass (no first plan,
                    from an error before or at the plan call, counts as not passing)
  first-plan lone   the first plan has start_date without end_date, or the reverse
  final pass        the request passed the evaluation scoring (run_eval.py)
  model errors      by kind. A request that ended in a model error counts against final pass.
  plan seconds      p50, p90 and max of the plan calls that succeeded
Nothing is dropped from a denominator.
"""
import argparse
import json
import math
import re
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, timezone
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from run_eval import QUESTIONS, ROOT, apply_sets, parse_sets, run_set, score, totals  # noqa: E402

from functions import load  # noqa: E402

ERROR_SHARE_LIMIT = 0.05   # above this, a variant's errors make the comparison unreliable


def parse_variant(text):
    """'P1 tasks.plan.thinking_level=medium date_schema=period' -> ('P1', {...})"""
    name, *pairs = text.split()
    return name, parse_sets(pairs)


def percentile(values, q):
    """Nearest-rank percentile of a list (None if empty)."""
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(q / 100 * len(ordered)) - 1)]


def error_kind(text):
    if re.search(r"\b504\b|DEADLINE_EXCEEDED", text or ""):
        return "504"
    if re.search(r"\b429\b|RESOURCE_EXHAUSTED", text or ""):
        return "429"
    return "other"


def lone_date(plan):
    params = (plan or {}).get("params") if isinstance(plan, dict) else None
    if not isinstance(params, dict):
        return False
    return sum(params.get(d) is not None for d in ("start_date", "end_date")) == 1


def first_plan_passes(record, catalog):
    flat = record.get("first_plan_flat")
    if not isinstance(flat, dict):
        return False
    return score(record["expect"], {"route": "answer", "final_plan": flat, "status": None}, catalog)[0]


def metrics(records, catalog):
    n = len(records)
    kinds = Counter(error_kind(e["error"]) for r in records for e in r["model_errors"])
    plan_secs = [c["seconds"] for r in records for c in r["calls"] if c["task"] == "plan" and not c["failed"]]
    by_task = defaultdict(list)
    for r in records:
        for c in r["calls"]:
            if not c["failed"]:
                by_task[c["task"]].append(c["seconds"])
    per_question = defaultdict(lambda: [0, 0])
    for r in records:
        per_question[r["id"]][0] += r["passed"]
        per_question[r["id"]][1] += 1
    errored = sum(r["status"] == "error" for r in records)
    return {
        "requests": n,
        "first_plan_pass": sum(first_plan_passes(r, catalog) for r in records),
        "first_plan_lone": sum(lone_date(r.get("first_plan_flat")) for r in records),
        "final_pass": sum(r["passed"] for r in records),
        "rejections": sum(r["rejected"] for r in records),
        "replans": sum(r["replanned"] for r in records),
        "errored_requests": errored,
        "error_share": errored / n if n else 0,
        "model_errors": {"504": kinds["504"], "429": kinds["429"], "other": kinds["other"]},
        "plan_seconds": {"p50": percentile(plan_secs, 50), "p90": percentile(plan_secs, 90),
                         "max": max(plan_secs, default=None), "calls": len(plan_secs)},
        "call_seconds_by_task": {t: sorted(v) for t, v in by_task.items()},
        "total_seconds": round(sum(r["seconds"] for r in records), 1),
        "tokens": totals(records)["total_tokens"],
        "below_80_percent": {q: f"{p}/{k}" for q, (p, k) in sorted(per_question.items()) if p < 0.8 * k},
    }


def pct(a, b):
    return f"{a}/{b} ({a / b:.0%})" if b else "n/a"


def secs(x):
    return "n/a" if x is None else f"{x:.1f}"


def markdown(label, stamp, variants, records_by_variant, results, ids, runs, per_round, workers, model, today):
    out = [f"# Probe: {label}, {stamp}", "",
           f"- Model: {model}. Questions: {', '.join(ids)}. Runs per question per variant: {runs} "
           f"({runs // per_round} rounds of {per_round}; the JSON has the rounds finished so far). Workers: {workers}. Pinned date: {today}.",
           "- Variants run round robin. BigQuery was not called.",
           "- Final pass uses the evaluation scoring; a request that ended in a model error is a miss.", "",
           "| Variant | Settings | First-plan pass | First-plan lone date | Final pass | Rejections | Re-plans "
           "| Errors 504 / 429 / other | Plan s p50 / p90 / max | Total s | Tokens (with thinking) |",
           "|---|---|---|---|---|---|---|---|---|---|---|"]
    for name, sets in variants:
        m = results[name]
        n = m["requests"]
        e = m["model_errors"]
        ps = m["plan_seconds"]
        out.append(
            f"| {name} | {', '.join(f'{k}={v}' for k, v in sets.items()) or 'current setup'} "
            f"| {pct(m['first_plan_pass'], n)} | {pct(m['first_plan_lone'], n)} | {pct(m['final_pass'], n)} "
            f"| {m['rejections']} | {m['replans']} | {e['504']} / {e['429']} / {e['other']} "
            f"| {secs(ps['p50'])} / {secs(ps['p90'])} / {secs(ps['max'])} | {m['total_seconds']} | {m['tokens']:,} |")
    out += ["", "## Questions below 8/10 final", ""]
    for name, _ in variants:
        low = results[name]["below_80_percent"]
        out.append(f"- {name}: " + (", ".join(f"{q} {v}" for q, v in low.items()) if low else "none"))
    out += ["", "## Reliability", ""]
    for name, _ in variants:
        m = results[name]
        flag = "ABOVE the 5% limit: unreliable" if m["error_share"] > ERROR_SHARE_LIMIT else "within the 5% limit"
        out.append(f"- {name}: {m['errored_requests']} of {m['requests']} requests ended in a model error "
                   f"({m['error_share']:.1%}), {flag}.")
    return "\n".join(out) + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--model", choices=["stub", "gemini"], default="gemini")
    parser.add_argument("--variant", action="append", required=True, metavar='"NAME [KEY=VALUE ...]"')
    parser.add_argument("--ids", required=True, help="question ids, comma separated")
    parser.add_argument("--runs", type=int, default=10, help="runs per question per variant (default 10)")
    parser.add_argument("--per-round", type=int, default=2, help="runs per question in one round (default 2)")
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--label", required=True)
    parser.add_argument("--out", type=Path, default=ROOT / "eval" / "results")
    parser.add_argument("--questions", type=Path, default=QUESTIONS)
    args = parser.parse_args()
    if args.runs % args.per_round or min(args.runs, args.per_round, args.workers) < 1:
        parser.error("--runs must be a positive multiple of --per-round")
    try:
        variants = [parse_variant(v) for v in args.variant]
    except ValueError as exc:
        parser.error(str(exc))
    if len({n for n, _ in variants}) != len(variants):
        parser.error("variant names must differ")

    spec = yaml.safe_load(args.questions.read_text())
    today = spec["today"] if isinstance(spec["today"], date) else date.fromisoformat(str(spec["today"]))
    ids = [i.strip() for i in args.ids.split(",")]
    questions = [q for q in spec["questions"] if q["id"] in ids]
    if {q["id"] for q in questions} != set(ids):
        parser.error("unknown question ids")
    catalog = load()
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    args.out.mkdir(parents=True, exist_ok=True)
    base = args.out / f"probe-{args.label}"

    records = {name: [] for name, _ in variants}
    model_name = args.model
    rounds = args.runs // args.per_round
    for rnd in range(rounds):
        for name, sets in variants:
            print(f"--- round {rnd + 1}/{rounds}, variant {name} ---", flush=True)
            model_name, recs, _ = run_set(questions, args.model, args.per_round, today, args.out,
                                          f"probe-{args.label}-{name}", stamp, args.workers, sets,
                                          fresh=(rnd == 0))
            for r in recs:
                r["run"] += rnd * args.per_round
                r["round"] = rnd + 1
                r["variant"] = name
            records[name] += recs
        results = {name: metrics(recs, catalog) for name, recs in records.items()}
        base.with_suffix(".json").write_text(json.dumps({
            "label": args.label, "model": model_name, "date": stamp, "pinned_today": str(today),
            "ids": ids, "runs": args.runs, "per_round": args.per_round, "workers": args.workers,
            "rounds_done": rnd + 1, "variants": {n: s for n, s in variants},
            "results": results, "records": records}, indent=2, default=str))
        base.with_suffix(".md").write_text(markdown(
            args.label, stamp, variants, records, results, ids, (rnd + 1) * args.per_round, args.per_round,
            args.workers, model_name, today))
    print()
    print(base.with_suffix(".md").read_text())
    print(f"Wrote {base.with_suffix('.md')} and {base.with_suffix('.json')}")


if __name__ == "__main__":
    main()
