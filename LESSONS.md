# Lessons learned

Working notes from building this project, written as things happened. Each entry says what went wrong or surprised me, what was done about it, and what it changed.

## 1. A valid query is not a correct query

A BigQuery dry run proves the SQL parses and reports how many bytes it would scan. It says nothing about whether the numbers are right. A misspelled status such as `'Cancelled'` would parse cleanly and quietly match nothing, so the five status values were checked against the data (Shipped, Complete, Processing, Cancelled, Returned; 180,996 items in total) before any result was trusted.

## 2. The bytes a dry run reports are not the bytes billed

BigQuery bills a minimum of 10 MiB per table a query references, and the dry run does not apply it. A single-table query that scanned 2 MiB was billed 10 MiB. Joins were billed 10 MiB per table referenced: 20 MiB for two tables that scanned 3 to 6 MiB, and 40 MiB for four tables that scanned 14 to 21 MiB. The audit record therefore logs both the estimate and the billed bytes, and the cost check compares the estimate to the cap knowing the bill can be higher for small queries.

## 3. At this scale the byte cap catches mistakes, not budgets

A full scan of `order_items` is about 13 MB, which costs a fraction of a cent. The 250 MiB per-query cap is about 12 times the largest reference query (21 MiB), so a query over it is almost certainly a bug, such as a bad join or a missing filter. The same arithmetic on a warehouse with terabyte tables is where the cap stops being a nicety. A full scan of the `events` table (about 367 MiB) would be refused. Both caps have now been tested on real jobs by lowering the cap in a test: at 1 MiB the dry-run check refuses the top-products query, and at 5 MiB the order-status query passes the dry run (about 2 MiB) but BigQuery stops the job, because the bill would be at least 10 MiB (lesson 14).

## 4. Define the metric before writing the query

"Revenue" and "return rate" sound obvious and are not. Revenue excludes Cancelled and Returned items and is split into final (order at least 30 days old) and pending. The 30 days came from the data: returns land a median 5 days after creation, 95% within 8, none after 11. Pending is not a forecast, because items inside the window can still be cancelled or returned. The return rate divides by non-cancelled items whose window has closed, so recent items do not drag it down. Every rule lives in `METRICS.md`, and the code follows that file, not the other way round.

## 5. Check the assumption, then check the fix

The newest month in the revenue query looked too large. The first check found 1,291 items dated after the current time, up to four days ahead, and the first fix was a filter on the current time. A second check showed that no order is dated in the future, and that an item's `created_at` differs from its order's for all but 5 of 180,996 items: later for 78,499 (by up to four days) and earlier for 102,492. The filter was treating a symptom. It was replaced with a rule: every sale is dated by its order's `created_at`, and item timestamps are not used. The question to ask when a fix is a filter is what produced the rows it removes.

## 6. Write the anomalies down

Beyond the timestamp clocks: 668 returned items were returned before they were created, and 35,912 items shipped before they were created. These are listed in `METRICS.md` and belong in the README. Metrics use only the order's `created_at` and the status, so nothing depends on a column known to be unreliable.

## 7. Derive dates in the query, pin them in the evals

The newest data moves as the dataset refreshes, so a hard-coded date range goes stale and a range derived from the data makes yesterday's reference answer wrong today. The functions derive their dates. The eval questions pin literal dates, so a reference answer stays the same until someone changes it on purpose.

## 8. Make checks produce a file, not a conversation

The checks run from a script that enforces the byte cap, executes the queries and writes a Markdown report with the rows and the bytes. The evidence can be re-run after a definition changes, which happened more than once in the first day, and nobody has to copy numbers by hand.

## 9. Pin the Python version

With no Python version pinned, `uv` picked a release too new for one dependency's build and failed. A `.python-version` file fixed it. The project uses Python 3.12; LangGraph needs 3.10 or newer.

## 10. Look at the edge of the data before choosing a window

The newest month looked about twice as busy as the months before it. Dating every sale by its order did not change that, so the next check split it by order count, order size and day. Items per order, average sale price and the share cancelled or returned are flat across 14 months, so the jump is more orders, not different orders. Daily volume sits at about 200 through October 13 and then climbs, steeply from the 20th: 573 on the 22nd, 1,771 on the 25th. Over half of October's orders were created in the last six days of the data. The cause is not established. Whatever it is, the newest weeks do not represent the rest, so eval questions pin windows that end on or before September 30, 2026, and any answer about the current month carries that caveat.

## 11. Define metrics so they can check each other

Average order value was defined with the same rule as revenue, which gave two free tests with no extra queries. Its revenue column matches the sum of final and pending revenue from the revenue query for all 12 months, to the cent. Its order count (37,514) matches the Shipped, Complete and Processing orders from the status breakdown. Neither check proves the definition is the right one, but a query that disagrees with its siblings is wrong somewhere, and these checks catch that.

## 12. Break ties on purpose

The top-products query ranked by revenue with no tie-break, and four products tied at 2,709.00 around places 8 to 10. A re-run after adding `ORDER BY revenue DESC, product_id` returned a different set for those places: one product dropped out and another came in. The first answer had never been wrong, only arbitrary, and an eval that scores against an arbitrary reference fails for no reason. Every ranking now breaks ties by id.

## 13. The dataset is regenerated, not appended

The integration tests passed on Oct 27, but the byte estimates were smaller than the day before, and that should not happen when data is only added. The `orders` table had been rewritten at 02:59 UTC: 124,669 rows instead of 124,732, newest order Oct 27 00:30 UTC, and the first order now on 2019-01-11. Every past month's order count had changed too. September 2025 went from 2,516 to 2,530, and September 2026 from 5,560 to 5,534. So the whole history is regenerated, not extended. The reference-match test still passes because both sides derive their dates from the same snapshot when they run. Lesson 7 assumed that pinning dates would keep a reference answer fixed, and on this dataset that is not true. The counts in `METRICS.md` describe the Oct 26 snapshot.

## 14. BigQuery reports the bytes-billed limit as a server error

When `maximum_bytes_billed` stops a job, the client raises `InternalServerError`, an HTTP 500, with reason `bytesBilledLimitExceeded` and the message "Query exceeded limit for bytes billed: 5242880. 10485760 or higher required." A 500 normally means a server-side fault that can be retried. This one fails the same way every time, so a generic retry would only repeat it. The runner checks the reason and raises its own `BilledLimitError` instead. The stopped job recorded no bytes processed or billed. The same check shows the dry-run cap and the billed cap measure different things: a 2 MiB estimate can still be a 10 MiB bill, so a cap under 10 MiB per referenced table would stop every query. At 250 MiB that is far away.

## 15. Not every resume value resumes

The approval step pauses with `interrupt()` and carries on with `Command(resume=...)`. Two values passed to `resume` did not behave like answers in LangGraph 1.2.17. `Command(resume=None)` raised `UnboundLocalError` inside the library. `Command(resume={})` was read as an empty map of interrupt ids to answers, so nothing resumed and the request stayed paused. Neither runs a query, so both fail safe. Still, the approval node accepts only `True` or `{"approved": True}` and treats every other value as a decline, the demo always sends `True` or `False`, and a test pins the `{}` case so an upgrade that changes it gets noticed.

## 16. A new project can have no quota for partner models

The first real call to Claude on Vertex AI (Sonnet 5.5 and Haiku 5.5, global endpoint) returned HTTP 429 `RESOURCE_EXHAUSTED` on `global_online_prediction_requests_per_base_model`. Authentication, the Vertex AI API and the model enablement were all fine, so the error was about the project's quota, not a mistake in the request. The quota API reported the project as eligible for an increase, but the increase could not be obtained in the time available, and Google's forums describe the same pattern for new projects. The model sits behind one interface, so the fix was a different provider, not a change to the graph. The lesson for next time: send one real request to the chosen model on day one, before building anything on top of it, because access and quota are decided outside the code.

## 17. Gemini thinking tokens count against the output cap

Gemini 3.x models think before they answer, and the thinking tokens count against `max_output_tokens`. With a small cap, a request came back with an empty reply and no error: the cap was spent on thinking and nothing was left for the answer. The adapter now uses a generous cap per task (1024 for route, 2048 for plan and summarize), records thinking tokens separately, and raises on empty text or any finish reason other than STOP, so a cut-off reply fails loudly instead of passing as an empty answer.

## 18. A plan can be valid and still silently wrong

For "revenue by month for last year", Gemini gave `start_date: 2025-01-01` and left out `end_date` in about half the runs, at temperature 0 (2 of 5, then 3 of 6, then 3 of 6). The plan passed every check, because an omitted `end_date` is allowed and means "up to the latest data", so the answer would have covered 2025 and 2026 under the heading of last year. Two prompt changes did not move the rate: telling the model to give both dates, and adding a worked example for "last year". Both were reverted. A validator rule now rejects a date window with only one end, and the error tells the model what to do ("give both, or give neither"); the re-plan the graph already had gets one more try. In 6 runs of the same question, 3 first plans were rejected, the re-plan fixed 1 of them, and the other 2 repeated the mistake and were rejected. So the rule does not make the model right. It turns a silently wrong answer (2025 and 2026 reported as last year) into either a correct plan (4 of 6) or a visible rejection (2 of 6). The same rule catches a lone `end_date`, which would start the window at the first data. The general point: when a default fills a gap silently, a schema check cannot see it and a prompt asks without enforcing. Put the rule where code enforces it, and measure what the model does after it.

## 19. Most of the first evaluation's misses were prompt and budget problems

The baseline run of the 24 hand-written questions (Gemini 3.8 Flash, 3 runs each) passed 52 of 72 (72%). Of the 20 misses, 13 were route failures on plain questions, and they had two causes.

- **Thinking budget.** 7 route calls ended in `MAX_TOKENS` against a 1024-token cap. Route spent 26,142 thinking tokens against 1,981 output tokens over 72 calls, so the cap was spent before the answer began. The graph turned each error into a refusal.
- **No date in the route prompt.** 6 valid questions were refused with a reason about future dates, and "Order status breakdown for September" was met with "which year?", because the route step, unlike the plan step, was never told today's date.

The other misses were "What were the top products?" (a follow-up where defaults were wanted) and a lone `start_date` (lesson 18). Reading the plan prompt next to the menu showed a third cause: the plan prompt said to omit dates unless the question gave them, while the menu said "omit for the default window". Those two sentences were pulling in different directions.

The fixes were made together: route gets today's date, the function names with a one-line description each, and rules for when to answer, clarify and refuse (answer when a function fits, because the person approves the exact call before it runs); route and summarize ask for `thinking_level: low`; every step starts at 2048 output tokens, and a `MAX_TOKENS` ending is retried with the cap doubled up to 8192; the menu says the two dates go together; the plan prompt has one date rule (no period named: give neither date; a period named: give both). Gemini 3.8 Flash rejects `thinking_level: minimal`, accepts `low` and `medium`, ignores `thinking_budget: 0` as an off switch (thinking continued), and rejects a level and a budget in the same request.

Rerun with the same questions, scoring and 3 runs each (4 workers): 68 of 72 (94%). By group: plain 24/36 to 34/36, relative_date 7/12 to 10/12, refuse 12/12 to 12/12, clarify 9/9 to 9/9, defaults 0/3 to 3/3. Route thinking fell from 26,142 to 4,873 tokens, route errors from 7 to 0, and no call needed a second attempt, so the `MAX_TOKENS` retry never ran on the real model. The cost went the other way: 110,058 tokens became 160,942 (route prompt tokens 14,589 to 38,061, because the prompt is longer; plan prompt tokens 56,197 to 98,574, with 11 re-plans against 6).

Four misses remain. "Show quarterly revenue for 2025" went from 3/3 to 1/3: two runs gave a lone `start_date`, again after the re-plan, and were rejected. "Order status breakdown for September" is 1/3: one run was rejected the same way, and one hit a 504 from Vertex AI. Everything else that changed, changed for the better. Lesson 18's validator rule is still doing work: 4 requests ended rejected.

Two cautions. The causes were found by reading the baseline results for these 24 questions, and the prompts were then changed to fix them, so the second score is not a clean measure of how the agent generalises. It shows that the fixes work on the questions that exposed the problems. And the fixes were not tried one at a time, so how much each one contributed is unknown.

## 20. Defining the period words did not help, because the failure is mechanical

The hypothesis was that the model drops `end_date` because it does not know what phrases like "last quarter" or "September" mean. A vocabulary section (11 one-sentence definitions in `config/thelook.yaml`) was rendered as "Terms" in the route and plan prompts, and the 24 questions were run 3 times each with it on (`v6-vocab-on`, 4 workers), against the run before it (`after-fix1`, vocabulary off, same code). Numbers recomputed from the two result files:

| | vocabulary off | vocabulary on |
|---|---|---|
| Passed | 68/72 | 58/72 |
| First plan has a lone `start_date` or `end_date` | 11 of 72 requests | 22 of 72 requests |
| ...of those, the question states both dates | 3 (q05, q08, q11) | 10 (q01, q05, q07, q08, q11) |
| Model errors | 1 | 10 (9 x 504, 1 x 429) |
| Seconds spent in the failed calls | 117.0 | 951.7 |
| Route calls that failed | 0 | 4 |
| Summed request time | 1,089.6 s | 2,831.2 s |

The drop from 68 to 58 is mostly Vertex errors, not wrong plans: 10 errors took 951.7 s of the 1,741.6 s extra run time, and 4 of them were route calls, whose prompt is short and thinking is low, so they look like slowness or throttling at 4 workers, not prompt length. The wrong-plan numbers are the useful ones. The lone-date rate doubled, and it includes questions that print both dates ("from 2026-07-01 to 2026-09-30"). A model that has the dates in front of it and writes only one cannot be helped by a definition of "last quarter". The failure is in how the plan is written, not in what the words mean. The vocabulary switch (`vocabulary_in_prompts`, default off), `tests/resolve_period.py` and the `period:` annotations on the questions stay in the repo; the switch is off.

## 21. A plan schema that cannot write a lone date removed the lone dates

Lesson 20 left the cause of the lone `start_date` as mechanical: the model writes one date even when it has both. The schema was the place to stop it. `date_schema: period` replaces `start_date` and `end_date` in the plan schema with one required `period`, which is null or an object with both dates required. The graph flattens the reply back to `start_date` and `end_date` before validation, so everything after the plan sees the same params as before, and the validator's date-pair rule stays as a backstop. Vertex accepted the `anyOf` of null and an object in `response_json_schema` on the first try.

A probe (`scripts/run_probe.py`, `eval/results/probe-dates.md`) ran five variants round robin on the ten date-heavy questions (q02, q06, q07, q08, q09, q11, q12, q13, q15, q16), 2 workers, vocabulary off. It was planned for 10 runs per question and stopped after round 2 (4 runs per question, 40 per variant) to save time, so every rate below has a denominator of 40.

| Variant | First-plan pass | First-plan lone date | Final pass | Rejections | Re-plans | Errors 504 / 429 | Plan s p50 / p90 / max | Tokens |
|---|---|---|---|---|---|---|---|---|
| P0 flat, default thinking | 21/40 | 19/40 (48%) | 36/40 | 4 | 19 | 0 / 0 | 4.6 / 11.3 / 62.9 | 137,071 |
| P1 flat, plan thinking medium | 26/40 | 12/40 (30%) | 36/40 | 2 | 12 | 0 / 2 | 4.5 / 39.7 / 75.8 | 118,497 |
| P2 period, default thinking | 40/40 | 0/40 | 40/40 | 0 | 0 | 0 / 0 | 4.2 / 18.0 / 62.6 | 95,883 |
| P3 period, plan thinking medium | 38/40 | 0/40 | 40/40 | 0 | 2 | 1 / 1 | 4.1 / 8.2 / 63.4 | 96,085 |
| P4 period, plan thinking low | 40/40 | 0/40 | 40/40 | 0 | 0 | 0 / 0 | 2.8 / 7.9 / 25.9 | 89,588 |

The rule written before the run chose the period schema (lone-date rate 0% against 2% allowed, final pass 100% against 90%) and then the lowest thinking level that was within one run of the best and had a plan p90 no higher than the default's: P4, plan `thinking_level: low`. Both are now the defaults. The flat variants lost 4 of 40 requests each to the lone date; their questions below 8/10 were q02, q06, q07, q15 (P0) and q02, q12, q15, q16 (P1); the period variants had none. The first plan was right 100% of the time with period and low thinking, against 52% flat.

Full evaluation on the new defaults (`after-fix2`, 24 questions, 3 runs, 2 workers): 72 of 72, with 0 wrong answers, 0 model errors, 0 re-plans and 0 lone-date first plans, against 68 of 72 for `after-fix1`. q02 and q15, the two questions that were 1/3, are 3/3, and no question got worse. Tokens fell from 160,942 to 127,018 and summed request time from 1,089.6 s to 691.0 s. Two cautions. The prompts were tuned on these 24 questions, so 72/72 is not a measure of generalisation. And Vertex had no 504s in this run or in the probe, which it did in the vocabulary run (lesson 20), so part of the speed-up may be calmer conditions.

Two things in the probe's own numbers. The call times of 60 s or more are a timed-out attempt (the limit was 60 s) followed by a quick retry, so the p99 of all "successful" calls is the timeout itself. The per-task timeouts in `config/model.yaml` are 3 times the p99 of the calls under 60 s (route 22.8 s, plan 25.9 s), and the retries are now 3 attempts. That is longer than the old 60 s for these two tasks, while the data suggest a hung call is usually fine on the retry; a shorter timeout might recover sooner. That was not tried.

## 22. A demo that printed only the summary gave no sign that the data came from BigQuery

`scripts/ask.py` printed the model's two-sentence summary and a footer. The SQL, the rows and the BigQuery job existed in the run and were never shown, so a viewer saw something that looked like a chatbot. The demo now prints the SQL and its bound parameters at approval, the first rows after the run, and the job id with a console link and a `bq show -j` command. The job id is also in the audit line. Run against the real job, `bq show` returned the same query text, the same bytes processed (21,960,635) and the same bytes billed (41,943,040) that the demo printed.

The first real run also showed something the summary had hidden. For "top 5 products by profit in Jeans", Gemini's plan left out `metric` and `category`, so the query ranked all products by revenue. The summary said that no rows were Jeans, which was true of the rows but did not say the plan was wrong. The SQL and the `@category = NULL` line made the cause visible before the approval. Measured afterwards, with no prompt change: the evaluation question q03 ("Top 5 products by profit in Jeans...") gave 9 correct plans in 10 runs, and the one miss dropped `category` and kept `metric`. Through `ask.py`, declining at approval, the same words in lower case kept `category` in 6 of 10 runs and the capitalised words in 8 of 10; in the five tries for the README sample, `category` was dropped four times. The samples are small and the cause is not established (the lower-case wording is one difference, the real date instead of the pinned one is another). The model drops optional parameters the question names, and the defaults then fill them in silently. A required, nullable field for every parameter, as was done for dates (lesson 21), is not built.

## Still open

- The cause of the order-volume ramp since October 14 is not established. A data-generator artifact is the likeliest guess, but nothing here tests that.
- At temperature 0 the real model is not deterministic: the same question gave a different route in three runs (lesson 18), so a single run says little. The evaluation uses 3 runs per question for that reason.
- A failed plan call used to be reported as "the plan is not an object"; it is now reported as the model error and ends with status `error` (fixed alongside lesson 21). The re-plan after a failed call still tells the model its previous plan was rejected, though there was none.
- With `date_schema: period` a lone date cannot be written, so the validator's date-pair rule is only a backstop. With `flat` the model still wrote one in about half of the first plans of the date-heavy questions (lesson 21).
- The return-window figures in lesson 4 were measured from the item's `created_at` and have not been repeated on the order clock. The 30-day window leaves a wide margin either way.
- Because the data is regenerated (lesson 13), eval answers cannot be fixed numbers stored once. Either compute each reference answer from its reference SQL at eval time, or snapshot the tables into a project dataset. Not decided.
