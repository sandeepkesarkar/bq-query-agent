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

## Still open

- The cause of the order-volume ramp since October 14 is not established. A data-generator artifact is the likeliest guess, but nothing here tests that.
- The return-window figures in lesson 4 were measured from the item's `created_at` and have not been repeated on the order clock. The 30-day window leaves a wide margin either way.
- Because the data is regenerated (lesson 13), eval answers cannot be fixed numbers stored once. Either compute each reference answer from its reference SQL at eval time, or snapshot the tables into a project dataset. Not decided.
