# Results: metrics_checks.sql

Run 2026-10-26 20:00 UTC. Project bq-query-agent. Per-query cap 262,144,000 bytes (250.00 MiB).

## 6. order_vs_item_created_at

- Dry-run estimate: 4,891,648 bytes (4.67 MiB)
- Bytes processed: 4,891,648 bytes (4.67 MiB)
- Bytes billed: 20,971,520 bytes (20.00 MiB)
- Rows: 1

| items | same_timestamp | item_after_order | item_before_order | max_days_item_after_order | last_order_created | orders_dated_in_the_future |
|---|---|---|---|---|---|---|
| 180,996 | 5 | 78,499 | 102,492 | 4 | 2026-10-26 00:35:13+00:00 | 0 |
