# Lessons learned

Working notes from building this project, written as things happened. Each entry says what went wrong or surprised me, what was done about it, and what it changed.

## 1. A valid query is not a correct query

A BigQuery dry run proves the SQL parses and reports how many bytes it would scan. It says nothing about whether the numbers are right. A misspelled status such as `'Cancelled'` would parse cleanly and quietly match nothing, so the five status values were checked against the data (Shipped, Complete, Processing, Cancelled, Returned; 180,996 items in total) before any result was trusted.

## 2. The bytes a dry run reports are not the bytes billed

BigQuery bills a minimum of 10 MiB per table a query references, and the dry run does not apply it. A single-table query that scanned 2 MiB was billed 10 MiB. Joins were billed 10 MiB per table referenced: 20 MiB for two tables that scanned 3 to 6 MiB, and 40 MiB for four tables that scanned 14 to 21 MiB. The audit record therefore logs both the estimate and the billed bytes, and the cost check compares the estimate to the cap knowing the bill can be higher for small queries.

## 3. At this scale the byte cap catches mistakes, not budgets

A full scan of `order_items` is about 13 MB, which costs a fraction of a cent. The 250 MiB per-query cap is about 12 times the largest reference query (21 MiB), so a query over it is almost certainly a bug, such as a bad join or a missing filter. The same arithmetic on a warehouse with terabyte tables is where the cap stops being a nicety. A full scan of the `events` table (about 367 MiB) would be refused. The cap logic has been tested with a stub client only; no real over-cap query has been run yet.

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

## Still open

- The cause of the order-volume ramp since October 14 is not established. A data-generator artifact is the likeliest guess, but nothing here tests that.
- The return-window figures in lesson 4 were measured from the item's `created_at` and have not been repeated on the order clock. The 30-day window leaves a wide margin either way.
- `average_order_value` has no reference SQL yet.
- No real query over the cap has been run through the cost check.
