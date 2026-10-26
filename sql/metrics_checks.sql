-- Metric checks for thelook_ecommerce. Each query answers one open question in METRICS.md.
-- Run all of them with:   uv run python scripts/run_queries.py
-- Results land in sql/results/metrics_checks.md (estimate, bytes processed and bytes billed
-- are recorded for every query).

-- 1. table_sizes
SELECT table_id, row_count, size_bytes
FROM `bigquery-public-data.thelook_ecommerce.__TABLES__`
ORDER BY size_bytes DESC;

-- 2. status_pairs_orders_vs_items
SELECT o.status AS order_status, oi.status AS item_status, COUNT(*) AS items
FROM `bigquery-public-data.thelook_ecommerce.order_items` AS oi
JOIN `bigquery-public-data.thelook_ecommerce.orders` AS o USING (order_id)
GROUP BY 1, 2
ORDER BY items DESC;

-- 3. timestamp_anomalies
SELECT
  COUNTIF(returned_at  < created_at)   AS returned_before_created,
  COUNTIF(returned_at  < delivered_at) AS returned_before_delivered,
  COUNTIF(shipped_at   < created_at)   AS shipped_before_created,
  COUNTIF(delivered_at < shipped_at)   AS delivered_before_shipped
FROM `bigquery-public-data.thelook_ecommerce.order_items`;

-- 4. customers_by_traffic_source
WITH buyers AS (
  SELECT DISTINCT user_id
  FROM `bigquery-public-data.thelook_ecommerce.orders`
)
SELECT
  u.traffic_source,
  COUNT(*)         AS registered_users,
  COUNT(b.user_id) AS customers_via_orders,
  (SELECT COUNT(DISTINCT user_id)
   FROM `bigquery-public-data.thelook_ecommerce.order_items`) AS customers_via_items
FROM `bigquery-public-data.thelook_ecommerce.users` AS u
LEFT JOIN buyers AS b ON b.user_id = u.id
GROUP BY u.traffic_source
ORDER BY registered_users DESC;

-- 5. date_range
SELECT
  MIN(created_at)     AS first_item,
  MAX(created_at)     AS last_item,
  CURRENT_TIMESTAMP() AS now_utc,
  COUNTIF(created_at > CURRENT_TIMESTAMP()) AS items_dated_in_the_future
FROM `bigquery-public-data.thelook_ecommerce.order_items`;

-- 6. order_vs_item_created_at
SELECT
  COUNT(*)                                          AS items,
  COUNTIF(oi.created_at = o.created_at)             AS same_timestamp,
  COUNTIF(oi.created_at > o.created_at)             AS item_after_order,
  COUNTIF(oi.created_at < o.created_at)             AS item_before_order,
  MAX(TIMESTAMP_DIFF(oi.created_at, o.created_at, DAY)) AS max_days_item_after_order,
  MAX(o.created_at)                                 AS last_order_created,
  COUNTIF(o.created_at > CURRENT_TIMESTAMP())       AS orders_dated_in_the_future
FROM `bigquery-public-data.thelook_ecommerce.order_items` AS oi
JOIN `bigquery-public-data.thelook_ecommerce.orders` AS o USING (order_id);

-- 7. monthly_volume_and_size
SELECT
  DATE_TRUNC(DATE(o.created_at), MONTH) AS month,
  COUNT(DISTINCT o.order_id)            AS orders,
  COUNT(*)                              AS items,
  ROUND(COUNT(*) / COUNT(DISTINCT o.order_id), 3) AS items_per_order,
  ROUND(AVG(oi.sale_price), 2)          AS avg_sale_price,
  ROUND(COUNTIF(oi.status IN ('Cancelled', 'Returned')) / COUNT(*), 4) AS share_cancelled_or_returned
FROM `bigquery-public-data.thelook_ecommerce.order_items` AS oi
JOIN `bigquery-public-data.thelook_ecommerce.orders` AS o ON oi.order_id = o.order_id
WHERE DATE(o.created_at) >= DATE '2025-09-01'
GROUP BY month
ORDER BY month;

-- 8. daily_volume_recent
SELECT
  DATE(o.created_at)             AS day,
  COUNT(DISTINCT o.order_id)     AS orders,
  COUNT(*)                       AS items,
  ROUND(SUM(oi.sale_price), 0)   AS gross_item_value
FROM `bigquery-public-data.thelook_ecommerce.order_items` AS oi
JOIN `bigquery-public-data.thelook_ecommerce.orders` AS o ON oi.order_id = o.order_id
WHERE DATE(o.created_at) >= DATE '2026-09-15'
GROUP BY day
ORDER BY day;
