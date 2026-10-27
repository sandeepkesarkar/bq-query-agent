# Results: reference_queries.sql

Run 2026-10-27 02:33 UTC. Project bq-query-agent. Per-query cap 262,144,000 bytes (250.00 MiB).

## 1. revenue_by_period

- Dry-run estimate: 6,745,465 bytes (6.43 MiB)
- Bytes processed: 6,745,465 bytes (6.43 MiB)
- Bytes billed: 20,971,520 bytes (20.00 MiB)
- Rows: 12

| period | final_revenue | pending_revenue |
|---|---|---|
| 2025-11-01 | 167,963.24 | 0.0000 |
| 2025-12-01 | 174,573.97 | 0.0000 |
| 2026-01-01 | 195,953.99 | 0.0000 |
| 2026-02-01 | 199,275.34 | 0.0000 |
| 2026-03-01 | 209,825.96 | 0.0000 |
| 2026-04-01 | 211,759.22 | 0.0000 |
| 2026-05-01 | 260,527.21 | 0.0000 |
| 2026-06-01 | 246,030.39 | 0.0000 |
| 2026-07-01 | 293,357.11 | 0.0000 |
| 2026-08-01 | 317,010.05 | 0.0000 |
| 2026-09-01 | 309,704.34 | 53,313.92 |
| 2026-10-01 | 0.0000 | 589,606.66 |

## 2. top_products

- Dry-run estimate: 21,975,163 bytes (20.96 MiB)
- Bytes processed: 21,975,163 bytes (20.96 MiB)
- Bytes billed: 41,943,040 bytes (40.00 MiB)
- Rows: 10

| product_id | product_name | category | revenue | units | profit |
|---|---|---|---|---|---|
| 23,648 | The North Face Apex Bionic Jacket - Men's | Outerwear & Coats | 7,224.00 | 8 | 4,030.99 |
| 24,283 | The North Face Nuptse 2 Jacket - Noah Green/TNF Black | Outerwear & Coats | 5,418.00 | 6 | 3,196.62 |
| 18,062 | The North Face Apex Bionic Soft Shell Jacket - Men's | Active | 4,515.00 | 5 | 2,555.49 |
| 23,803 | Canada Goose Men's The Chateau Jacket | Outerwear & Coats | 3,260.00 | 4 | 1,747.36 |
| 24,179 | Arc'teryx Men's Diplomat Jacket | Outerwear & Coats | 3,150.00 | 7 | 1,732.50 |
| 8,435 | Arc'teryx Women's Caliber Cardigan | Outerwear & Coats | 2,796.00 | 4 | 1,425.96 |
| 8,409 | Canada Goose Women's Chilliwack Bomber | Outerwear & Coats | 2,780.00 | 4 | 1,629.08 |
| 2,793 | adidas Women's adiFIT Slim Pant | Active | 2,709.00 | 3 | 1,582.06 |
| 2,796 | ASCIS Cushion Low Socks (Pack of 3) | Active | 2,709.00 | 3 | 1,587.47 |
| 18,656 | JORDAN DURASHEEN SHORT MENS 404309-109 | Active | 2,709.00 | 3 | 1,598.31 |

## 3. order_status_breakdown

- Dry-run estimate: 2,275,551 bytes (2.17 MiB)
- Bytes processed: 2,275,551 bytes (2.17 MiB)
- Bytes billed: 10,485,760 bytes (10.00 MiB)
- Rows: 5

| status | orders |
|---|---|
| Shipped | 15,109 |
| Complete | 12,438 |
| Processing | 9,967 |
| Cancelled | 7,430 |
| Returned | 5,012 |

## 4. average_order_value

- Dry-run estimate: 6,745,465 bytes (6.43 MiB)
- Bytes processed: 6,745,465 bytes (6.43 MiB)
- Bytes billed: 20,971,520 bytes (20.00 MiB)
- Rows: 12

| period | orders | revenue | average_order_value |
|---|---|---|---|
| 2025-11-01 | 1,948 | 167,963.24 | 86.22 |
| 2025-12-01 | 2,180 | 174,573.97 | 80.08 |
| 2026-01-01 | 2,267 | 195,953.99 | 86.44 |
| 2026-02-01 | 2,268 | 199,275.34 | 87.86 |
| 2026-03-01 | 2,487 | 209,825.96 | 84.37 |
| 2026-04-01 | 2,522 | 211,759.22 | 83.96 |
| 2026-05-01 | 2,929 | 260,527.21 | 88.95 |
| 2026-06-01 | 2,908 | 246,030.39 | 84.60 |
| 2026-07-01 | 3,322 | 293,357.11 | 88.31 |
| 2026-08-01 | 3,671 | 317,010.05 | 86.36 |
| 2026-09-01 | 4,184 | 363,018.26 | 86.76 |
| 2026-10-01 | 6,828 | 589,606.66 | 86.35 |

## 5. return_rate_by_category

- Dry-run estimate: 15,147,674 bytes (14.45 MiB)
- Bytes processed: 15,147,674 bytes (14.45 MiB)
- Bytes billed: 41,943,040 bytes (40.00 MiB)
- Rows: 25

| category | returned_items | eligible_items | return_rate |
|---|---|---|---|
| Skirts | 81 | 580 | 0.1397 |
| Suits | 38 | 275 | 0.1382 |
| Blazers & Jackets | 105 | 816 | 0.1287 |
| Plus | 143 | 1,119 | 0.1278 |
| Shorts | 395 | 3,104 | 0.1273 |
| Active | 303 | 2,457 | 0.1233 |
| Pants & Capris | 120 | 974 | 0.1232 |
| Underwear | 262 | 2,127 | 0.1232 |
| Outerwear & Coats | 304 | 2,507 | 0.1213 |
| Dresses | 181 | 1,493 | 0.1212 |
| Sleep & Lounge | 358 | 2,983 | 0.1200 |
| Swim | 370 | 3,086 | 0.1199 |
| Maternity | 170 | 1,420 | 0.1197 |
| Intimates | 429 | 3,622 | 0.1184 |
| Suits & Sport Coats | 169 | 1,427 | 0.1184 |
| Jumpsuits & Rompers | 29 | 246 | 0.1179 |
| Socks | 196 | 1,668 | 0.1175 |
| Fashion Hoodies & Sweatshirts | 372 | 3,181 | 0.1169 |
| Leggings | 98 | 855 | 0.1146 |
| Accessories | 308 | 2,700 | 0.1141 |
| Tops & Tees | 367 | 3,227 | 0.1137 |
| Jeans | 383 | 3,454 | 0.1109 |
| Sweaters | 344 | 3,103 | 0.1109 |
| Pants | 205 | 1,943 | 0.1055 |
| Socks & Hosiery | 111 | 1,061 | 0.1046 |

## 6. customers_by_traffic_source

- Dry-run estimate: 3,621,807 bytes (3.45 MiB)
- Bytes processed: 3,621,807 bytes (3.45 MiB)
- Bytes billed: 20,971,520 bytes (20.00 MiB)
- Rows: 5

| traffic_source | customers |
|---|---|
| Search | 26,433 |
| Organic | 5,604 |
| Facebook | 2,287 |
| Email | 1,935 |
| Display | 1,542 |
