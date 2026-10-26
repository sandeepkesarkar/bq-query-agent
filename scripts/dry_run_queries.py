#!/usr/bin/env python3
"""Dry-run every query in sql/reference_queries.sql and report the bytes each would process.

No query is executed and nothing is billed. This is the same check the agent's cost_check
node will make before it asks for approval.

Usage, from the repo root:
    uv run python scripts/dry_run_queries.py           # print bytes per query
    uv run python scripts/dry_run_queries.py --write   # also fill in the "derived dates" bytes lines

A dry run proves the query is valid and shows its cost. It does not prove the numbers are
right: run each query in the console and sanity-check the results too.
"""
import argparse
import re
import sys
from pathlib import Path

SQL_FILE = Path(__file__).resolve().parent.parent / "sql" / "reference_queries.sql"
HEADER = re.compile(r"^-- (\d+)\. (\w+)", re.MULTILINE)
BYTES_LINE = re.compile(
    r"^-- bytes estimate \((?:console|dry run), derived dates\):.*$", re.MULTILINE
)


def split_queries(text):
    """Return (number, name, start, end, sql) for each numbered query in the file."""
    matches = list(HEADER.finditer(text))
    queries = []
    for i, m in enumerate(matches):
        start = m.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        lines = [ln for ln in text[start:end].splitlines() if not ln.lstrip().startswith("--")]
        sql = "\n".join(lines).strip().rstrip(";").strip()
        queries.append((int(m.group(1)), m.group(2), start, end, sql))
    return queries


def fmt(n):
    return f"{n:,} bytes ({n / 1024 ** 2:.2f} MiB)"


def write_estimates(text, queries, results):
    """Replace each query's "derived dates" bytes line with its dry-run result."""
    for number, _name, start, end, _sql in reversed(queries):
        if number not in results:
            continue
        new_line = f"-- bytes estimate (dry run, derived dates): {fmt(results[number])}"
        chunk, count = BYTES_LINE.subn(lambda _m: new_line, text[start:end], count=1)
        if count == 0:
            print(f"warning: no 'derived dates' bytes line for query {number}", file=sys.stderr)
        text = text[:start] + chunk + text[end:]
    return text


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--write", action="store_true",
                        help="fill in the 'derived dates' bytes lines in the SQL file")
    parser.add_argument("--file", type=Path, default=SQL_FILE)
    args = parser.parse_args()

    # Imported here so the parsing helpers above can be tested without the library.
    from google.cloud import bigquery

    text = args.file.read_text()
    queries = split_queries(text)
    if not queries:
        sys.exit(f"No numbered queries found in {args.file}")

    client = bigquery.Client()
    print(f"Project: {client.project}")
    config = bigquery.QueryJobConfig(dry_run=True, use_query_cache=False)

    results, failed = {}, False
    for number, name, _start, _end, sql in queries:
        try:
            job = client.query(sql, job_config=config)
            results[number] = job.total_bytes_processed
            print(f"{number}. {name}: {fmt(job.total_bytes_processed)}")
        except Exception as exc:  # report every query, not just the first failure
            failed = True
            print(f"{number}. {name}: FAILED: {exc}")

    if args.write and results:
        args.file.write_text(write_estimates(text, queries, results))
        print(f"Updated {args.file}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
