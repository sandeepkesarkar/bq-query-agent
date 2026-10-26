-- Reference queries for the thelook_ecommerce functions.
-- STATUS (Oct 26, 2026): rewritten so every sale is dated by its order's created_at (METRICS.md,
-- definition 9), then run clean. Queries 3 and 6 matched their earlier results exactly, as
-- they should. Bytes under each query are dry-run estimates; the billed figure is 10 MiB per
-- table referenced here (see sql/results/reference_queries.md).
-- Note: order volume ramps sharply from Oct 14, 2026, so the newest month is not comparable
-- with earlier ones (METRICS.md, known anomalies). Eval windows end no later than Sep 30.
-- Function 4 (average_order_value) has no reference SQL yet.
-- Definitions come from METRICS.md. If a query and METRICS.md disagree, METRICS.md wins.
--
-- Dates are derived from the data, nothing is hard-coded:
--   as_of_date = newest created_at in the orders table
--   start_date = first day of the month 11 months before that, so the window is 12 calendar
--                months (the last month may be partial)
-- Every sale is dated by its order's created_at, including in item-level queries, which join
-- orders for it. Item created_at is never used: it differs from the order's for almost every
-- item, and 1,291 items are dated in the future (METRICS.md, known anomalies).
-- For the eval question set, pin literal dates instead, so reference answers cannot drift
-- if the dataset changes.

-- 1. revenue_by_period (group_by = month)
WITH d AS (
  SELECT
    MAX(DATE(created_at)) AS as_of_date,
    DATE_TRUNC(DATE_SUB(MAX(DATE(created_at)), INTERVAL 11 MONTH), MONTH) AS start_date
  FROM `bigquery-public-data.thelook_ecommerce.orders`
)
SELECT
  DATE_TRUNC(DATE(o.created_at), MONTH) AS period,
  SUM(IF(DATE(o.created_at) <= DATE_SUB(d.as_of_date, INTERVAL 30 DAY), oi.sale_price, 0)) AS final_revenue,
  SUM(IF(DATE(o.created_at) >  DATE_SUB(d.as_of_date, INTERVAL 30 DAY), oi.sale_price, 0)) AS pending_revenue
FROM `bigquery-public-data.thelook_ecommerce.order_items` AS oi
JOIN `bigquery-public-data.thelook_ecommerce.orders` AS o ON oi.order_id = o.order_id
CROSS JOIN d
WHERE oi.status NOT IN ('Cancelled', 'Returned')
  AND DATE(o.created_at) >= d.start_date
  AND DATE(o.created_at) <= d.as_of_date
GROUP BY period
ORDER BY period;
-- bytes estimate (dry run, derived dates): 6,745,465 bytes (6.43 MiB)

-- 2. top_products (metric = revenue; for units or profit, change only the ORDER BY column)
WITH d AS (
  SELECT
    MAX(DATE(created_at)) AS as_of_date,
    DATE_TRUNC(DATE_SUB(MAX(DATE(created_at)), INTERVAL 11 MONTH), MONTH) AS start_date
  FROM `bigquery-public-data.thelook_ecommerce.orders`
)
SELECT
  p.id AS product_id,
  p.name AS product_name,
  p.category,
  SUM(oi.sale_price) AS revenue,
  COUNT(*) AS units,
  SUM(oi.sale_price - ii.cost) AS profit
FROM `bigquery-public-data.thelook_ecommerce.order_items` AS oi
JOIN `bigquery-public-data.thelook_ecommerce.orders` AS o ON oi.order_id = o.order_id
JOIN `bigquery-public-data.thelook_ecommerce.inventory_items` AS ii ON oi.inventory_item_id = ii.id
JOIN `bigquery-public-data.thelook_ecommerce.products` AS p ON ii.product_id = p.id
CROSS JOIN d
WHERE oi.status NOT IN ('Cancelled', 'Returned')
  AND DATE(o.created_at) >= d.start_date
  AND DATE(o.created_at) <= d.as_of_date
GROUP BY product_id, product_name, p.category
ORDER BY revenue DESC
LIMIT 10;
-- bytes estimate (dry run, derived dates): 21,975,163 bytes (20.96 MiB)

-- 3. order_status_breakdown (counts orders, from the orders table)
WITH d AS (
  SELECT
    MAX(DATE(created_at)) AS as_of_date,
    DATE_TRUNC(DATE_SUB(MAX(DATE(created_at)), INTERVAL 11 MONTH), MONTH) AS start_date
  FROM `bigquery-public-data.thelook_ecommerce.orders`
)
SELECT o.status, COUNT(*) AS orders
FROM `bigquery-public-data.thelook_ecommerce.orders` AS o
CROSS JOIN d
WHERE DATE(o.created_at) >= d.start_date
  AND DATE(o.created_at) <= d.as_of_date
GROUP BY o.status
ORDER BY orders DESC;
-- bytes estimate (dry run, derived dates): 2,275,551 bytes (2.17 MiB)

-- 5. return_rate_by_category (items whose 30-day window has closed, cancelled excluded)
-- The window ends at as_of_date minus 30 days, so every item counted has had its full
-- chance to be returned. min_orders is applied to distinct orders, so the name matches
-- what it counts.
WITH d AS (
  SELECT
    MAX(DATE(created_at)) AS as_of_date,
    DATE_TRUNC(DATE_SUB(MAX(DATE(created_at)), INTERVAL 11 MONTH), MONTH) AS start_date
  FROM `bigquery-public-data.thelook_ecommerce.orders`
)
SELECT
  p.category,
  COUNTIF(oi.status = 'Returned') AS returned_items,
  COUNT(*) AS eligible_items,
  SAFE_DIVIDE(COUNTIF(oi.status = 'Returned'), COUNT(*)) AS return_rate
FROM `bigquery-public-data.thelook_ecommerce.order_items` AS oi
JOIN `bigquery-public-data.thelook_ecommerce.orders` AS o ON oi.order_id = o.order_id
JOIN `bigquery-public-data.thelook_ecommerce.inventory_items` AS ii ON oi.inventory_item_id = ii.id
JOIN `bigquery-public-data.thelook_ecommerce.products` AS p ON ii.product_id = p.id
CROSS JOIN d
WHERE oi.status != 'Cancelled'
  AND DATE(o.created_at) >= d.start_date
  AND DATE(o.created_at) <= DATE_SUB(d.as_of_date, INTERVAL 30 DAY)
GROUP BY p.category
HAVING COUNT(DISTINCT oi.order_id) >= 100
ORDER BY return_rate DESC;
-- bytes estimate (dry run, derived dates): 15,147,674 bytes (14.45 MiB)

-- 6. customers_by_traffic_source (customers who placed at least one order in the window)
-- Customer and traffic-source rules are METRICS.md definition 3 and decision 8. A cancelled
-- order still counts. Traffic source is the user's registration attribute.
WITH d AS (
  SELECT
    MAX(DATE(created_at)) AS as_of_date,
    DATE_TRUNC(DATE_SUB(MAX(DATE(created_at)), INTERVAL 11 MONTH), MONTH) AS start_date
  FROM `bigquery-public-data.thelook_ecommerce.orders`
)
SELECT
  u.traffic_source,
  COUNT(DISTINCT o.user_id) AS customers
FROM `bigquery-public-data.thelook_ecommerce.orders` AS o
JOIN `bigquery-public-data.thelook_ecommerce.users` AS u ON u.id = o.user_id
CROSS JOIN d
WHERE DATE(o.created_at) >= d.start_date
  AND DATE(o.created_at) <= d.as_of_date
GROUP BY u.traffic_source
ORDER BY customers DESC;
-- bytes estimate (dry run, derived dates): 3,621,807 bytes (3.45 MiB)
