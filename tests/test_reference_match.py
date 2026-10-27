"""The function library against real BigQuery. Skipped without credentials.

1. Each function, called with default parameters, returns what its reference query in
   sql/reference_queries.sql returns. Those queries were run and checked by hand (METRICS.md,
   LESSONS.md 11), so this shows the config templates, the loader and the runner reproduce them.
2. Calls with set parameters (other groupings and metrics, pinned dates, a category, a
   different min_orders) return what the same reference query returns after a plain text
   edit: literal dates and values written into the SQL. That path never touches the loader,
   the templates or parameter binding, so it is an independent check of all three.
3. Both caps work on real jobs: the dry-run check refuses a query over the cap, and
   maximum_bytes_billed stops a job whose bill would pass it.

Every query here reads under 25 MiB. A full run costs about 20 queries.
"""
from dataclasses import replace
from datetime import date

import pytest
from google.cloud import bigquery

from dry_run_queries import SQL_FILE, split_queries
from functions.catalog import load
from functions.queries import BilledLimitError, OverCapError, run

pytestmark = pytest.mark.integration

# return_rate_by_category adds window_end, which the reference query does not have.
COMPARE_COLUMNS = {"return_rate_by_category": 4}

REFERENCE = {name: sql for _n, name, _s, _e, sql in split_queries(SQL_FILE.read_text())}


@pytest.fixture(scope="module")
def client():
    try:
        import google.auth
        from google.auth.exceptions import DefaultCredentialsError

        google.auth.default()
        return bigquery.Client()
    except (DefaultCredentialsError, OSError) as exc:
        pytest.skip(f"no BigQuery credentials: {exc}")


@pytest.fixture(scope="module")
def catalog():
    return load()


def same(a, b):
    if isinstance(a, float) or isinstance(b, float):
        return a == pytest.approx(b, rel=1e-9, abs=1e-6)  # float sums can differ in the last bits
    return a == b


def run_reference(client, catalog, sql):
    job = client.query(
        sql,
        job_config=bigquery.QueryJobConfig(
            use_query_cache=False, maximum_bytes_billed=catalog.limits["max_bytes_per_query"]
        ),
        location=catalog.dataset["location"],
    )
    rows = job.result()
    return [f.name for f in rows.schema], [tuple(r.values()) for r in rows]


def assert_matches(catalog, name, result, ref_columns, expected):
    width = COMPARE_COLUMNS.get(name, len(ref_columns))
    assert ref_columns == list(catalog.functions[name].output_columns[:width])
    assert result.columns[:width] == ref_columns
    assert len(result.rows) == len(expected) > 0
    for got, want in zip(result.rows, expected):
        assert all(same(g, w) for g, w in zip(got[:width], want)), (got, want)


def edited(sql, edits):
    """Apply text edits to a reference query. Each old text must appear exactly once."""
    for old, new in edits:
        assert sql.count(old) == 1, f"expected one {old!r} in the reference query"
        sql = sql.replace(old, new)
    return sql


@pytest.mark.parametrize("name", list(REFERENCE))
def test_defaults_match_reference_query(client, catalog, name):
    ref_columns, expected = run_reference(client, catalog, REFERENCE[name])
    result = run(name, {}, catalog=catalog, client=client)
    assert_matches(catalog, name, result, ref_columns, expected)
    assert 0 < result.estimated_bytes <= catalog.limits["max_bytes_per_query"]
    assert result.billed_bytes >= result.processed_bytes


# Reference edits shared by the order-dated queries.
ORDER_START = "AND DATE(o.created_at) >= d.start_date"
ORDER_END = "AND DATE(o.created_at) <= d.as_of_date"


def dates(start, end):
    return [(ORDER_START, f"AND DATE(o.created_at) >= DATE '{start}'"),
            (ORDER_END, f"AND DATE(o.created_at) <= DATE '{end}'")]


CASES = [
    pytest.param(
        "revenue_by_period",
        {"group_by": "quarter", "start_date": "2025-10-01", "end_date": "2026-09-30"},
        [("DATE_TRUNC(DATE(o.created_at), MONTH)", "DATE_TRUNC(DATE(o.created_at), QUARTER)")]
        + dates("2025-10-01", "2026-09-30"),
        id="revenue-quarter-pinned",
    ),
    pytest.param(
        "revenue_by_period",
        {"group_by": "year", "start_date": "2024-01-01", "end_date": "2026-09-30"},
        [("DATE_TRUNC(DATE(o.created_at), MONTH)", "DATE_TRUNC(DATE(o.created_at), YEAR)")]
        + dates("2024-01-01", "2026-09-30"),
        id="revenue-year-pinned",
    ),
    pytest.param(
        "top_products",
        {"metric": "profit", "n": 5, "category": "Jeans",
         "start_date": "2026-01-01", "end_date": "2026-09-30"},
        [("ORDER BY revenue DESC, product_id", "ORDER BY profit DESC, product_id"),
         ("LIMIT 10", "LIMIT 5"),
         (ORDER_END, "AND DATE(o.created_at) <= DATE '2026-09-30'\n  AND p.category = 'Jeans'"),
         (ORDER_START, "AND DATE(o.created_at) >= DATE '2026-01-01'")],
        id="top-products-profit-jeans",
    ),
    pytest.param(
        "top_products",
        {"metric": "units", "n": 20},
        [("ORDER BY revenue DESC, product_id", "ORDER BY units DESC, product_id"),
         ("LIMIT 10", "LIMIT 20")],
        id="top-products-units-default-window",
    ),
    pytest.param(
        "average_order_value",
        {"group_by": "quarter", "start_date": "2025-10-01", "end_date": "2026-09-30"},
        [("DATE_TRUNC(t.order_date, MONTH)", "DATE_TRUNC(t.order_date, QUARTER)"),
         ("WHERE t.order_date >= d.start_date", "WHERE t.order_date >= DATE '2025-10-01'"),
         ("AND t.order_date <= d.as_of_date", "AND t.order_date <= DATE '2026-09-30'")],
        id="aov-quarter-pinned",
    ),
    pytest.param(
        "return_rate_by_category",
        {"start_date": "2026-01-01", "end_date": "2026-06-30", "min_orders": 300},
        [(ORDER_START, "AND DATE(o.created_at) >= DATE '2026-01-01'"),
         ("AND DATE(o.created_at) <= DATE_SUB(d.as_of_date, INTERVAL 30 DAY)",
          "AND DATE(o.created_at) <= DATE '2026-06-30'"),
         ("HAVING COUNT(DISTINCT oi.order_id) >= 100", "HAVING COUNT(DISTINCT oi.order_id) >= 300")],
        id="return-rate-pinned-min-orders",
    ),
    pytest.param(
        "order_status_breakdown",
        {"start_date": "2026-09-01"},  # end_date left out: a typed NULL, so it ends at the as-of date
        [("WHERE DATE(o.created_at) >= d.start_date", "WHERE DATE(o.created_at) >= DATE '2026-09-01'")],
        id="status-start-only",
    ),
    pytest.param(
        "customers_by_traffic_source",
        {"start_date": "2026-01-01", "end_date": "2026-03-31"},
        [("WHERE DATE(o.created_at) >= d.start_date", "WHERE DATE(o.created_at) >= DATE '2026-01-01'"),
         (ORDER_END, "AND DATE(o.created_at) <= DATE '2026-03-31'")],
        id="customers-pinned",
    ),
]


@pytest.mark.parametrize("name, params, edits", CASES)
def test_set_parameters_match_edited_reference(client, catalog, name, params, edits):
    ref_columns, expected = run_reference(client, catalog, edited(REFERENCE[name], edits))
    result = run(name, params, catalog=catalog, client=client)
    assert_matches(catalog, name, result, ref_columns, expected)


def test_return_rate_window_end(client, catalog):
    """window_end is the end date, or 30 days before the as-of date if that is earlier."""
    pinned = run("return_rate_by_category", {"end_date": "2026-06-30"}, catalog=catalog, client=client)
    assert {row[4] for row in pinned.rows} == {date(2026, 6, 30)}

    _cols, [(as_of,)] = run_reference(
        client, catalog, "SELECT MAX(DATE(created_at)) FROM `bigquery-public-data.thelook_ecommerce.orders`"
    )
    default = run("return_rate_by_category", {}, catalog=catalog, client=client)
    assert {row[4] for row in default.rows} == {date.fromordinal(as_of.toordinal() - 30)}


class Recorder:
    """Wraps a real client and records whether each call was a dry run."""

    def __init__(self, client):
        self.client = client
        self.dry_runs = []

    def query(self, sql, job_config, location=None):
        self.dry_runs.append(bool(job_config.dry_run))
        return self.client.query(sql, job_config=job_config, location=location)


def with_cap(catalog, cap):
    return replace(catalog, limits={**catalog.limits, "max_bytes_per_query": cap})


def test_real_dry_run_over_the_cap_is_refused(client, catalog):
    recorder = Recorder(client)
    with pytest.raises(OverCapError) as info:
        run("top_products", catalog=with_cap(catalog, 1024 ** 2), client=recorder)
    assert info.value.estimated_bytes > 1024 ** 2
    assert recorder.dry_runs == [True]  # refused after the dry run; nothing executed


def test_maximum_bytes_billed_stops_a_real_job(client, catalog):
    # The dry run estimates about 2 MiB, under a 5 MiB cap, but BigQuery bills at least
    # 10 MiB per table, so the job's own limit has to stop it.
    recorder = Recorder(client)
    with pytest.raises(BilledLimitError) as info:
        run("order_status_breakdown", catalog=with_cap(catalog, 5 * 1024 ** 2), client=recorder)
    assert recorder.dry_runs == [True, False]
    job = client.get_job(info.value.job_id, location=catalog.dataset["location"])
    assert job.error_result["reason"] == "bytesBilledLimitExceeded"
    assert not job.total_bytes_billed
