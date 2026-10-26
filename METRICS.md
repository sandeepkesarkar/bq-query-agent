# Metric definitions (thelook_ecommerce)

Every rule here is something the agent's functions must follow. If a function and this file disagree, this file wins and the function is wrong.

## Verified facts (console and script queries, Oct 26, 2026)

- `orders` has 124,732 rows. `order_items` has 124,732 distinct `order_id` values. The two agree.
- `order_items` has 180,996 rows.
- The status of an order equals the status of each of its items, for all 180,996 items: Shipped 54,844, Complete 45,238, Processing 35,945, Cancelled 26,811, Returned 18,158.
- 18,158 items are returned by status, and 18,158 by non-null `returned_at`. Zero rows disagree in either direction.
- `inventory_items.cost` and `products.cost` agree on all 180,996 items. The join order_items -> inventory_items -> products keeps every item.
- `users` has 100,000 rows. 79,807 distinct `user_id` values appear in `order_items`, and the same 79,807 appear in `orders`. The two sources give the same customer count.
- Customers by registration traffic source (customers of registered users): Search 55,822 of 69,951; Organic 11,890 of 14,965; Facebook 4,812 of 6,022; Email 3,995 of 4,988; Display 3,288 of 4,074.
- Items run from 2019-01-15 to 2026-10-30. The check ran at 2026-10-26 19:53 UTC.
- Table sizes in bytes: events 385,206,267 (about 367 MiB); inventory_items 80,987,848; users 19,811,761; order_items 13,587,945; orders 6,744,875; products 4,285,975; distribution_centers 809. A full scan of `events` is over the 250 MiB per-query cap and would be refused. The agent does not use that table.

The data is synthetic and unusually consistent in places. Real data would not be.

## Known anomalies

- 1,291 items are dated after the time of the check (the newest is Oct 30, 2026, four days ahead). No order is: the newest order was created Oct 26, 2026 00:35 UTC.
- An item's `created_at` differs from its order's `created_at` for 180,991 of 180,996 items (all but 5). It is later for 78,499 items, by up to 4 days, and earlier for 102,492 (how much earlier was not measured). The two tables run on different clocks.
- 668 returned items (3.7% of returns) have `returned_at` before `created_at`.
- 35,912 items (19.8% of all items) have `shipped_at` before `created_at`.
- No item is delivered before it ships, and none is returned before delivery.
- Order volume ramps sharply at the end of the data. October 1 to 13 runs at about 200 orders a day, in line with September (about 185). From October 14 it climbs: 271, then 319 on the 20th, 573 on the 22nd, 1,039 on the 24th and 1,771 on the 25th. October 26 holds only 49 orders because the data stops at 00:35 UTC. Over half of October's 9,063 orders (4,856) were created from the 20th to the 25th. Items per order (about 1.45), average sale price (about 59) and the share cancelled or returned (about 25%) are unchanged, so it is more orders, not different orders. The cause is not established.

Because of these, every metric anchors on the order's `created_at` and on `status` only (definition 9). `shipped_at`, `delivered_at` and `returned_at` are not used in any metric, except that a null `returned_at` is the cross-check for definition 4.

## Definitions

1. **Order.** One distinct `order_id`. Count it from `orders`. The `order_items` count matches, so either source gives the same answer.
2. **Item.** One row in `order_items`. Revenue and returns are counted at item level.
3. **Customer.** A distinct `user_id` that appears in `orders`. The same set appears in `order_items` (79,807). A user who has never ordered is a registered user, not a customer: 79,807 customers out of 100,000 registered users.
4. **Return.** An item whose status is Returned. This equals `returned_at IS NOT NULL` on the current data. Use status as the rule and keep the date as a cross-check.
5. **Profit.** Item `sale_price` minus the item's cost from `inventory_items.cost`, reached through the lossless join.
9. **Date of a sale, and the as-of date.** Every sale is dated by its order's `created_at`, including in item-level metrics, which join `orders` to get it. Item `created_at` is never used (see known anomalies). The as-of date is the newest order `created_at`, derived at query time. No order is dated in the future, so nothing needs filtering out. The default window starts on the first day of the month 11 months before the as-of date, so it covers 12 calendar months and the last month may be partial. Eval questions pin literal dates instead, ending no later than Sep 30, 2026, which is before the volume ramp described in the known anomalies.

## Decisions

6. **Revenue.**
   - Rule: sum of `sale_price` for items not Cancelled and not Returned, split into final (order date at least 30 days before the as-of date) and pending.
   - Reason: returns all land within 11 days in the data, so 30 days is a safe documented window.
   - Note: pending is not a forecast. Items inside the window can still be cancelled or returned, so pending revenue overstates what will be kept.
7. **Return rate denominator.**
   - Rule: returned items divided by non-cancelled items whose window has closed.
   - Reason: recent items have not had time to be returned.
8. **customers_by_traffic_source.**
   - Rule: count distinct customers (definition 3) with at least one order created in the date range, using `orders.created_at`, any order status. Traffic source is the user's registration attribute, `users.traffic_source`. The output column is named customers, never users.
   - Reason: not all registered users place an order. A cancelled order is still an order, so it still makes someone a customer. Traffic source belongs to the user, not to the order.
10. **average_order_value.**
   - Rule: revenue as in decision 6 (final and pending together) divided by the number of orders that contribute to it, meaning orders whose items are not Cancelled or Returned. An order's value is the sum of its items' sale prices. Orders are dated by their `created_at` (definition 9) and grouped by month or quarter.
   - Reason: it is the same rule as revenue, so a period's average order value times its order count equals that period's revenue. An order and its items always share a status, so an order is either fully counted or fully left out.
   - Note: pending orders are included and can still be cancelled or returned, so the newest periods carry the same caveat as pending revenue.

## Notes for the README

- Each definition above should appear in the README's data section in one or two lines.
- Any eval question that depends on a definition should reference it by number.
- The anomalies section belongs in the README too. Listing what is wrong with the data is part of the honest write-up.
