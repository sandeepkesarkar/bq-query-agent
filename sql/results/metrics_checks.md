# Results: metrics_checks.sql

Run 2026-10-26 19:53 UTC. Project bq-query-agent. Per-query cap 262,144,000 bytes (250.00 MiB).

## 1. table_sizes

- Dry-run estimate: 0 bytes (0.00 MiB)
- Bytes processed: 0 bytes (0.00 MiB)
- Bytes billed: 0 bytes (0.00 MiB)
- Rows: 7

| table_id | row_count | size_bytes |
|---|---|---|
| events | 2,425,142 | 385,206,267 |
| inventory_items | 488,003 | 80,987,848 |
| users | 100,000 | 19,811,761 |
| order_items | 180,996 | 13,587,945 |
| orders | 124,732 | 6,744,875 |
| products | 29,120 | 4,285,975 |
| distribution_centers | 10 | 809 |

## 2. status_pairs_orders_vs_items

- Dry-run estimate: 5,577,336 bytes (5.32 MiB)
- Bytes processed: 5,577,336 bytes (5.32 MiB)
- Bytes billed: 20,971,520 bytes (20.00 MiB)
- Rows: 5

| order_status | item_status | items |
|---|---|---|
| Shipped | Shipped | 54,844 |
| Complete | Complete | 45,238 |
| Processing | Processing | 35,945 |
| Cancelled | Cancelled | 26,811 |
| Returned | Returned | 18,158 |

## 3. timestamp_anomalies

- Dry-run estimate: 3,046,320 bytes (2.91 MiB)
- Bytes processed: 3,046,320 bytes (2.91 MiB)
- Bytes billed: 10,485,760 bytes (10.00 MiB)
- Rows: 1

| returned_before_created | returned_before_delivered | shipped_before_created | delivered_before_shipped |
|---|---|---|---|
| 668 | 0 | 35,912 | 0 |

## 4. customers_by_traffic_source

- Dry-run estimate: 4,071,919 bytes (3.88 MiB)
- Bytes processed: 4,071,919 bytes (3.88 MiB)
- Bytes billed: 31,457,280 bytes (30.00 MiB)
- Rows: 5

| traffic_source | registered_users | customers_via_orders | customers_via_items |
|---|---|---|---|
| Search | 69,951 | 55,822 | 79,807 |
| Organic | 14,965 | 11,890 | 79,807 |
| Facebook | 6,022 | 4,812 | 79,807 |
| Email | 4,988 | 3,995 | 79,807 |
| Display | 4,074 | 3,288 | 79,807 |

## 5. date_range

- Dry-run estimate: 1,447,968 bytes (1.38 MiB)
- Bytes processed: 1,447,968 bytes (1.38 MiB)
- Bytes billed: 10,485,760 bytes (10.00 MiB)
- Rows: 1

| first_item | last_item | now_utc | items_dated_in_the_future |
|---|---|---|---|
| 2019-01-15 19:05:56+00:00 | 2026-10-30 00:39:00.487649+00:00 | 2026-10-26 19:53:14.200683+00:00 | 1,291 |
