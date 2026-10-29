# bq-query-agent: design

A small LangGraph agent that answers plain-English questions about a BigQuery dataset by choosing from a fixed set of parameterized query functions. The model proposes. Code decides. A human approves every query before it runs.

Status: built for learning and demonstration. Not a production system.

## Non-goals

- No free-form SQL from the model, ever.
- No user-level or personal data in any result.
- No write access to the source data.
- No claim of production readiness.

## Dataset (config, not code)

Default: `bigquery-public-data.thelook_ecommerce` (synthetic, read-only, US location).

Tables used: orders, order_items, products, inventory_items, users. Known join path: order_items -> inventory_items -> products. order_items also carries user_id.

Everything dataset-specific lives in one config file: table names, allowed enum values, the function catalog below, and plain-language descriptions. The graph, guardrails and audit log must not import anything from it directly. That is what lets a second dataset (for example CMS Medicare charge data) drop in later.

Every column and enum value used below was checked against the real tables with `SELECT DISTINCT` and a schema check (`sql/metrics_checks.sql`, results in `METRICS.md`). If this document and `METRICS.md` or the console disagree, they win.

## Function catalog

Common rules for every function:
- All values are bound as query parameters. Group-by columns come from a code-side map keyed by an enum, never from interpolated text.
- Date ranges must be valid and no wider than the dataset's span. `n` is capped at 20. Every query has `LIMIT 100`.
- Results are aggregates only. No function returns rows about an individual user.

Week-one functions (6):

1. `revenue_by_period(group_by: month|quarter|year, start_date, end_date, status)`
2. `top_products(metric: revenue|units|profit, n, start_date, end_date, category?)`
3. `order_status_breakdown(start_date, end_date)`
4. `average_order_value(group_by: month|quarter, start_date, end_date)`
5. `return_rate_by_category(start_date, end_date, min_orders)`
6. `customers_by_traffic_source(start_date, end_date)` (counts only)

Stretch (only if everything else is done): `inventory_aging(days_threshold)`, `profit_by_category(start_date, end_date)`.

`status` values come from the data check in `METRICS.md` (Shipped, Complete, Processing, Cancelled, Returned), not from memory.

## Graph

State fields: question, route (answer | clarify | refuse), plan (function name and params), validation_errors, estimated_bytes, approved, result_rows, summary, audit_id.

The graph runs route, plan, validate, cost_check, approval, execute, summarize and audit. A request leaves early, straight to audit, when route says clarify or refuse, when validate rejects the plan twice, when the cost check fails or is over the cap, when the person declines, or when the query fails.

As built, the state also carries route_reason, attempts (plans made), raw_plan (the plan as the model wrote it, before dates are flattened), model_attempts (per step), summary_fallback, result_columns, bytes_processed, bytes_billed, status (answered, clarify, refused, rejected, over_cap, declined, error), error and message (what the person is shown). The audit record needs these fields.

Nodes:
1. route (model): classify as answer, clarify or refuse. Out-of-scope and adversarial questions end here.
2. plan (model): emit a JSON plan validated against a typed schema. Unknown function name means reject. With `date_schema: period` (the default) the plan carries one `period` (null, or both dates) and the node flattens it into `start_date` and `end_date`. If the model call itself fails, the plan is empty and the call's error is kept as the validation error.
3. validate (code): enum checks, bounds, date sanity. A plan must give both `start_date` and `end_date` or neither, because one end alone is valid but silently wrong (an omitted `end_date` runs to the latest data). One re-plan allowed, with the errors listed. After that a bad plan ends the request with status `rejected`, and a plan call that failed ends it with status `error` and its own error text. With the `period` schema the lone-date rule can no longer fire on a model plan; it stays as the backstop.
4. cost_check (code): BigQuery dry run. Compare bytes processed with the cap. Over the cap means stop, no approval offered.
5. approval (code): `interrupt()` shows the function, params and estimated bytes. Resume with `Command(resume=...)`.
6. execute (code): run the parameterized query with a maximum-bytes-billed limit as a second cap. The design calls for a read-only service account; it is not built, and the query runs as whoever is logged in to `gcloud`.
7. summarize (model): write a short answer from the returned rows. Show the function and params used.
8. audit (code): append a record for every run, including rejected and refused ones.

The model is only in route, plan and summarize. Every decision that matters is in code.

Known behavior to design around: on resume, LangGraph re-runs the interrupted node from its start. Put no side effects before the interrupt in the approval node. Execution and logging happen in later nodes.

Checkpointer: in-memory for the demo. Production would need a durable one. Say so out loud.

## Guardrails (all deterministic)

| Guardrail | Where |
|---|---|
| Function allowlist | validate |
| Enum and bounds checks | validate |
| Both ends of a date window, or neither | the plan schema (`period`) and validate |
| Parameterized queries only | execute |
| Dry-run bytes cap before approval | cost_check |
| Human approval | approval |
| Maximum bytes billed on the job | execute |
| Read-only service account | infra (not built: queries run as the logged-in user) |
| Aggregates-only results | function catalog |
| Audit record for every run | audit |

## Model interface

One function: `complete_structured(prompt, schema) -> dict`. The model name comes from config. Swapping models must not touch the graph.

First real model: Gemini 3.8 Flash on Vertex AI, through Google's Python SDK (`google-genai`), using the same Google login as BigQuery. It is built as `src/agent/gemini_model.py`, with settings in `config/model.yaml` (a model ID for each of route, plan and summarize, location, temperature, per-task output caps and ceilings, thinking level, timeout, retry counts). A stronger model can be tried for the plan step if the evaluation shows it is needed. The stub stays for tests and is the default of `scripts/ask.py`.

- The adapter raises on empty text, a finish reason other than STOP, JSON that does not parse, and a result that is not an object. It retries a timeout, 429 or 5xx, using the SDK's retry options: 3 attempts (two retries), and route and plan have their own request timeouts (70 s and 80 s) in `config/model.yaml`, about 3 times the p99 of single-attempt calls in the date probe. Worst case for calls that keep timing out: a route that never answers ends the request after 3 x 70 = 210 s; a plan that never answers ends it after 2 x 3 x 80 = 480 s (the first plan and the one re-plan), after a route call that timed out twice and then answered (about 140 s): about 620 s, plus a few seconds of backoff.
- Retry on `MAX_TOKENS`: a call that ends with `MAX_TOKENS`, and only that, is retried with the output cap doubled, never above the task's ceiling, at most `max_token_retries` (3) times, so at most 4 attempts. A call already cut off at the ceiling stops at once and raises with finish reason `MAX_TOKENS`. This count is separate from the 429/5xx retry. The tokens of every attempt are counted, and each call records its attempts and final cap; the audit line has an `attempts` field for route, plan and summarize.
- Route, plan and summarize ask for a low thinking level (plan's was chosen by the date probe, LESSONS.md 21). A route prompt that does not state today's date or list the functions makes the model refuse valid questions and think too long (LESSONS.md 19).
- The plan schema is generated from the catalog (function name as an enum, every parameter listed with its allowed values and bounds). The code checks stay as the real guard. `date_schema` in `config/thelook.yaml` is `period` (the default): `start_date` and `end_date` are replaced by one required `period`, null or an object with both dates, so a lone date cannot be written; `flatten_plan` turns the reply back into `start_date` and `end_date` before validation. `flat` keeps the two separate optional dates. The run record keeps both the raw and the flattened first plan.
- The route and plan prompts get today's date, and the question text is escaped before it enters a prompt. The plan prompt has one date rule, worded for the schema: with `period`, null when no period is named and both dates otherwise; with `flat`, neither date when no period is named and both otherwise. The menu says the dates go together. A `vocabulary` section in `config/thelook.yaml` can add "Terms" (what "last quarter" and "September" mean) to the route and plan prompts; `vocabulary_in_prompts` is false because the test showed no gain (LESSONS.md 20). When the plan call itself fails, the error is kept and the request ends with status `error`; it is not reported as an invalid plan.
- After summarize, every number in the summary must appear in the rows. If one does not, the summary is replaced by a plain one built by code, and the audit line records it.
- A failed audit write is reported on stderr and does not stop the answer.

Tested without cloud access: the adapter (fake client), the schema, the prompt, the escaping, the summary check, the date-pair rule and the audit failure. Five tests against real Gemini passed on the last run, with the period schema (before it, the "last year" one could fail, because the model sometimes omitted `end_date`; LESSONS.md 18 and 21). `scripts/run_eval.py` and `scripts/run_probe.py` exist; their results are in `LESSONS.md` (19 to 21).

## Audit record

JSON lines to start. Fields: timestamp, audit_id, question, route, status, function, params, estimated_bytes, approved, bytes_processed, bytes_billed, row_count, error, model_name, summary_fallback, attempts.

## Eval

The questions are written by hand, not generated. `eval/questions.yaml` holds 24 of them: 12 plain (two per function), 4 with relative dates ("last quarter"), 4 that must be refused, 3 that need a follow-up and 1 with no dates, which must use the defaults. Relative dates are resolved against a pinned date (2026-10-28) so expected values do not go stale. Every window ends on or before 2026-09-30.

Per question: id, group, text, and `expect` (route, and for an answer the function and the parameters it fixes). Scoring compares route, function and parameters; a parameter left out counts as its default. Nothing runs on BigQuery while scoring, because `tests/test_reference_match.py` already checks each function against its reference SQL, so a correct function with correct parameters is a correct answer. The earlier plan to compare answers against reference SQL results, and to report average bytes, was dropped for that reason. The run records tokens, seconds per request, re-plans, rejections and model errors.

`scripts/run_eval.py` runs the set (`--runs N`, `--workers`, `--ids`, `--set KEY=VALUE`) and writes a report under `eval/results/`. Each question is asked three times because the model is not deterministic at temperature 0. `scripts/run_probe.py` compares settings round robin on chosen questions. A separate held-out set of 24 questions, written after the prompts were frozen, is run through the same scorer once and kept out of the repository. The numbers are in the README ("Evaluation") and `LESSONS.md` (19 to 21).

Some questions are hard enough that failures are expected. They are reported, not removed.

## Repo layout

```
bq-query-agent/
  README.md  DESIGN.md  METRICS.md  LESSONS.md
  config/{thelook.yaml, model.yaml}
  src/agent/{graph.py, models.py, gemini_model.py, summary_check.py, audit.py}   (the nodes live in graph.py)
  src/functions/{catalog.py, queries.py, fake_client.py}
  scripts/{ask.py, run_eval.py, run_probe.py, dry_run_queries.py, run_queries.py, vertex_smoke_test.py}
  tests/
  eval/{questions.yaml, results/}   (results are written by the scripts and not committed)
  sql/{reference_queries.sql, metrics_checks.sql, results/}
  infra/   (Terraform, not built)
```

## What may be claimed

Only what is built and in the repo. Label it independent open-source work. Say it was built to ramp on GCP and LangGraph. Leave employer systems and internal vocabulary out of the repo.
