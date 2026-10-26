# Notes for Claude Code

A small LangGraph agent that answers plain-English questions about a BigQuery dataset by choosing from a fixed menu of query functions. Read README.md for the idea, DESIGN.md for the plan, METRICS.md for what every number means, and LESSONS.md for what has already gone wrong.

## Commands

- Install: `uv sync`. Add packages with `uv add <package>` (`uv add --dev <package>` for test tools). Do not use `pip install` or a requirements file.
- Python is pinned to 3.12 in `.python-version`. Keep that file.
- Run scripts with `uv run python scripts/<name>.py`.
- Run the reference queries: `uv run python scripts/run_queries.py --file sql/reference_queries.sql`.

## Rules for this project

- METRICS.md wins over code. If a function and METRICS.md disagree, the function is wrong.
- Every sale is dated by its order's `created_at`. Never use an item's `created_at`.
- Evaluation questions pin literal dates that end on or before 2026-09-30.
- The model never writes SQL. Functions come from a fixed allowlist, and every value is a bound parameter.
- Per-query cap is `250 * 1024**2` bytes. Dry-run first, refuse anything over the cap, and set the same cap as `maximum_bytes_billed`. Log both estimated and billed bytes, since BigQuery bills at least 10 MiB per table.
- Never commit credentials, keys or `.env` files. Do not run `gcloud` commands that create, change or delete cloud resources without asking first.
- Do not claim in the README anything that is not built and in the repo. Keep its status table true.
- When something surprising turns up in the data or the tooling, add it to LESSONS.md.

## Working style

- Ask before committing or pushing. Commit titles are short and in the imperative. Do not put session links in commit messages.
- Keep docs plain and specific. No marketing language.
