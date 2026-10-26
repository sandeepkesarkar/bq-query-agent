#!/usr/bin/env python3
"""Run every numbered query in a SQL file and save the results as a Markdown report.

Each query is dry-run first. One that would process more than the per-query cap is refused,
which is the same rule the agent's cost_check node will apply. A query that passes runs with
the cap also set as maximum_bytes_billed. The report records, for each query, the dry-run
estimate, the bytes the job processed and the bytes it was billed, so the estimate can be
compared with what actually happened.

Usage, from the repo root:
    uv run python scripts/run_queries.py                          # sql/metrics_checks.sql
    uv run python scripts/run_queries.py --file sql/reference_queries.sql
    uv run python scripts/run_queries.py --only 1,3

The report is written to sql/results/<sql file name>.md (with --only, to
sql/results/<sql file name>_only_<numbers>.md, so a partial run never overwrites a full one).
"""
import argparse
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from dry_run_queries import fmt, split_queries  # lives in the same folder

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_FILE = ROOT / "sql" / "metrics_checks.sql"
RESULTS_DIR = ROOT / "sql" / "results"
CAP_BYTES = 250 * 1024 ** 2  # per-query cap; moves to config/thelook.yaml when the agent is built
MAX_ROWS = 100


def cell(value):
    """Format one value for a Markdown table cell."""
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, int):
        return f"{value:,}"
    if isinstance(value, (float, Decimal)):
        return f"{value:,.4f}" if abs(value) < 10 else f"{value:,.2f}"  # keep ratios readable
    return str(value).replace("|", "\\|").replace("\n", " ")


def table(columns, rows):
    lines = ["| " + " | ".join(columns) + " |", "|" + "---|" * len(columns)]
    for row in rows:
        lines.append("| " + " | ".join(cell(v) for v in row) + " |")
    return "\n".join(lines)


def run_one(client, sql, cap):
    """Dry-run, enforce the cap, then execute. Returns a dict describing what happened."""
    from google.cloud import bigquery

    result = {"status": "ok"}
    try:
        dry = client.query(
            sql, job_config=bigquery.QueryJobConfig(dry_run=True, use_query_cache=False)
        )
        result["estimate"] = dry.total_bytes_processed
        if result["estimate"] > cap:
            result["status"] = "refused"
            return result
        job = client.query(
            sql,
            job_config=bigquery.QueryJobConfig(use_query_cache=False, maximum_bytes_billed=cap),
        )
        iterator = job.result()
        rows = list(iterator)
        result.update(
            processed=job.total_bytes_processed,
            billed=job.total_bytes_billed,
            columns=[f.name for f in iterator.schema],
            rows=[tuple(r.values()) for r in rows],
        )
    except Exception as exc:  # report every query, not just the first failure
        result["status"] = "error"
        result["error"] = str(exc)
    return result


def render(number, name, result, cap=CAP_BYTES):
    lines = [f"## {number}. {name}", ""]
    if "estimate" in result:
        lines.append(f"- Dry-run estimate: {fmt(result['estimate'])}")
    if result["status"] == "refused":
        lines.append(f"- REFUSED: estimate is over the cap of {fmt(cap)}. The query was not run.")
    elif result["status"] == "error":
        lines.append(f"- ERROR: {result['error']}")
    else:
        lines.append(f"- Bytes processed: {fmt(result['processed'])}")
        lines.append(f"- Bytes billed: {fmt(result['billed'])}")
        rows = result["rows"]
        lines.append(f"- Rows: {len(rows):,}")
        lines.append("")
        lines.append(table(result["columns"], rows[:MAX_ROWS]))
        if len(rows) > MAX_ROWS:
            lines.append("")
            lines.append(f"(first {MAX_ROWS} of {len(rows):,} rows shown)")
    lines.append("")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--file", type=Path, default=DEFAULT_FILE)
    parser.add_argument("--only", help="comma-separated query numbers to run, e.g. 1,3")
    args = parser.parse_args()

    from google.cloud import bigquery

    queries = split_queries(args.file.read_text())
    if args.only:
        wanted = {int(n) for n in args.only.split(",")}
        queries = [q for q in queries if q[0] in wanted]
    if not queries:
        sys.exit(f"No matching numbered queries in {args.file}")

    client = bigquery.Client()
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    sections = [
        f"# Results: {args.file.name}",
        "",
        f"Run {stamp}. Project {client.project}. Per-query cap {fmt(CAP_BYTES)}.",
        "",
    ]
    all_ok = True
    for number, name, _start, _end, sql in queries:
        result = run_one(client, sql, CAP_BYTES)
        all_ok = all_ok and result["status"] == "ok"
        print(f"{number}. {name}: {result['status']}")
        sections.append(render(number, name, result))

    RESULTS_DIR.mkdir(exist_ok=True)
    suffix = "_only_" + args.only.replace(",", "_") if args.only else ""
    out = RESULTS_DIR / f"{args.file.stem}{suffix}.md"
    out.write_text("\n".join(sections))
    print(f"Wrote {out}")
    sys.exit(0 if all_ok else 1)


if __name__ == "__main__":
    main()
