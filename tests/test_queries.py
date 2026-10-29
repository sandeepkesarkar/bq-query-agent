"""The runner without BigQuery: validation, typed binding, and the byte cap, using a stub client."""
from dataclasses import replace
from datetime import date, datetime

import pytest

from functions.catalog import load
from functions.fake_client import StubClient
from functions.queries import (
    OverCapError, ValidationError, estimate, execute, prepare, run, validate,
)

TODAY = date(2026, 10, 27)
CAP = 250 * 1024 ** 2


@pytest.fixture(scope="module")
def catalog():
    return load()


def errors_for(catalog, function, params):
    with pytest.raises(ValidationError) as info:
        validate(catalog, function, params, today=TODAY)
    return info.value.errors


def test_cap_matches_the_project_rule(catalog):
    assert catalog.limits["max_bytes_per_query"] == CAP


# --- validation ---

def test_unknown_function(catalog):
    assert errors_for(catalog, "drop_tables", {}) == ["unknown function 'drop_tables'"]


def test_unknown_parameter(catalog):
    assert errors_for(catalog, "order_status_breakdown", {"status": "Shipped"}) == [
        "unknown parameter 'status'"
    ]


@pytest.mark.parametrize("value", ["week", "Month", "", 1])
def test_enum_outside_its_list(catalog, value):
    assert "group_by" in errors_for(catalog, "revenue_by_period", {"group_by": value})[0]


def test_year_is_allowed_for_revenue_but_not_average_order_value(catalog):
    assert validate(catalog, "revenue_by_period", {"group_by": "year"}, TODAY)["group_by"] == "year"
    errors_for(catalog, "average_order_value", {"group_by": "year"})


@pytest.mark.parametrize("value", ["Jeans; DROP TABLE x", "jeans", "{orders}", "Shoes"])
def test_category_must_be_in_the_list(catalog, value):
    assert "category" in errors_for(catalog, "top_products", {"category": value})[0]


def test_category_in_the_list_is_accepted(catalog):
    assert validate(catalog, "top_products", {"category": "Jeans"}, TODAY)["category"] == "Jeans"


@pytest.mark.parametrize("value", [0, 21, -1, True, 5.0, "5"])
def test_int_bounds_and_type(catalog, value):
    assert "n:" in errors_for(catalog, "top_products", {"n": value})[0]


@pytest.mark.parametrize("value", [1, 20])
def test_int_bounds_are_inclusive(catalog, value):
    assert validate(catalog, "top_products", {"n": value}, TODAY)["n"] == value


@pytest.mark.parametrize(
    "value", ["2026/01/01", "January 2026", "2026-13-01", 20260101, datetime(2026, 1, 1, 12)]
)
def test_date_must_be_iso(catalog, value):
    assert "start_date" in errors_for(catalog, "order_status_breakdown", {"start_date": value})[0]


def test_date_before_the_data(catalog):
    errors = errors_for(catalog, "order_status_breakdown", {"start_date": "2018-12-31"})
    assert "outside 2019-01-01" in errors[0]
    assert validate(catalog, "order_status_breakdown", {"start_date": "2019-01-01"}, TODAY)


def test_date_after_today(catalog):
    errors = errors_for(catalog, "order_status_breakdown", {"end_date": "2026-10-28"})
    assert "outside" in errors[0]
    assert validate(catalog, "order_status_breakdown", {"end_date": "2026-10-27"}, TODAY)


def test_date_max_can_be_a_fixed_date(catalog):
    fixed = replace(catalog, limits={**catalog.limits, "date_max": date(2026, 9, 30)})
    assert validate(fixed, "order_status_breakdown", {"end_date": "2026-09-30"}, TODAY)
    errors = errors_for(fixed, "order_status_breakdown", {"end_date": "2026-10-01"})
    assert "outside 2019-01-01 to 2026-09-30" in errors[0]


def test_date_max_must_be_a_date_or_today_utc(catalog):
    broken = replace(catalog, limits={**catalog.limits, "date_max": "tomorrow"})
    with pytest.raises(ValueError, match="date_max"):
        validate(broken, "order_status_breakdown", {}, TODAY)


def test_start_after_end(catalog):
    errors = errors_for(
        catalog, "order_status_breakdown", {"start_date": "2026-09-30", "end_date": "2026-09-01"}
    )
    assert errors == ["start_date 2026-09-30 is after end_date 2026-09-01"]


def test_start_equal_to_end_is_allowed(catalog):
    validate(catalog, "order_status_breakdown", {"start_date": "2026-09-01", "end_date": "2026-09-01"}, TODAY)


def test_every_problem_is_reported(catalog):
    errors = errors_for(
        catalog, "top_products", {"metric": "clicks", "n": 99, "category": "Shoes", "x": 1}
    )
    assert len(errors) == 4


def test_defaults_fill_in(catalog):
    assert validate(catalog, "top_products", {}, TODAY) == {
        "metric": "revenue", "n": 10, "start_date": None, "end_date": None, "category": None,
    }


# --- binding ---

def bound(prepared):
    return {p.name: (p.type_, p.value) for p in prepared.query_parameters}


def test_omitted_dates_and_category_are_typed_nulls(catalog):
    p = prepare(catalog, "top_products", {}, TODAY)
    assert bound(p) == {
        "start_date": ("DATE", None),
        "end_date": ("DATE", None),
        "category": ("STRING", None),
        "n": ("INT64", 10),
    }


def test_given_values_are_typed(catalog):
    p = prepare(
        catalog, "top_products",
        {"start_date": "2026-01-01", "end_date": date(2026, 9, 30), "category": "Jeans", "n": 3},
        TODAY,
    )
    assert bound(p) == {
        "start_date": ("DATE", date(2026, 1, 1)),
        "end_date": ("DATE", date(2026, 9, 30)),
        "category": ("STRING", "Jeans"),
        "n": ("INT64", 3),
    }


def test_slot_selectors_are_not_bound(catalog):
    p = prepare(catalog, "revenue_by_period", {"group_by": "quarter"}, TODAY)
    assert set(bound(p)) == {"start_date", "end_date"}
    assert "DATE_TRUNC(DATE(o.created_at), QUARTER) AS period" in p.sql


def test_values_never_reach_the_sql_text(catalog):
    p = prepare(catalog, "top_products", {"category": "Blazers & Jackets", "n": 7}, TODAY)
    assert "Blazers" not in p.sql and "LIMIT @n" in p.sql


# --- dry run, cap and execution, with a stub client ---

def test_over_the_cap_is_refused_and_never_executed(catalog):
    client = StubClient(CAP + 1)
    with pytest.raises(OverCapError):
        run("order_status_breakdown", catalog=catalog, client=client, today=TODAY)
    assert len(client.calls) == 1 and client.calls[0].config.dry_run


def test_missing_estimate_is_refused(catalog):
    client = StubClient(None)
    with pytest.raises(OverCapError):
        run("order_status_breakdown", catalog=catalog, client=client, today=TODAY)
    assert len(client.calls) == 1


def test_invalid_call_never_reaches_bigquery(catalog):
    client = StubClient(1)
    with pytest.raises(ValidationError):
        run("order_status_breakdown", {"start_date": "yesterday"}, catalog=catalog, client=client)
    assert client.calls == []


def test_at_the_cap_runs_with_the_cap_as_maximum_bytes_billed(catalog):
    client = StubClient(CAP)
    result = run("order_status_breakdown", catalog=catalog, client=client, today=TODAY)
    dry, real = client.calls
    assert dry.config.dry_run and not real.config.dry_run
    assert real.config.maximum_bytes_billed == CAP
    assert dry.sql == real.sql
    assert [p.to_api_repr() for p in dry.config.query_parameters] == [
        p.to_api_repr() for p in real.config.query_parameters
    ]
    assert real.location == "US"
    assert result.columns == ["status", "orders"]
    assert result.rows == [("Shipped", 3)]
    assert (result.estimated_bytes, result.processed_bytes, result.billed_bytes) == (
        CAP, CAP, 10 * 1024 ** 2
    )


def test_billed_limit_from_bigquery_gets_its_own_error(catalog):
    from google.api_core.exceptions import InternalServerError

    from functions.queries import BilledLimitError

    class StoppedJob:
        job_id = "job-1"

        def result(self):
            raise InternalServerError(
                "Query exceeded limit for bytes billed",
                errors=[{"reason": "bytesBilledLimitExceeded"}],
            )

    class Client(StubClient):
        def query(self, sql, job_config, location=None):
            if job_config.dry_run:
                return super().query(sql, job_config, location)
            return StoppedJob()

    with pytest.raises(BilledLimitError) as info:
        run("order_status_breakdown", catalog=catalog, client=Client(1), today=TODAY)
    assert info.value.job_id == "job-1" and info.value.cap == CAP


def test_other_bigquery_errors_pass_through(catalog):
    from google.api_core.exceptions import InternalServerError

    class BrokenJob:
        job_id = "job-2"

        def result(self):
            raise InternalServerError("backend error", errors=[{"reason": "backendError"}])

    class Client(StubClient):
        def query(self, sql, job_config, location=None):
            if job_config.dry_run:
                return super().query(sql, job_config, location)
            return BrokenJob()

    with pytest.raises(InternalServerError):
        run("order_status_breakdown", catalog=catalog, client=Client(1), today=TODAY)


def test_dry_run_does_not_use_the_query_cache(catalog):
    client = StubClient(1)
    estimate(prepare(catalog, "order_status_breakdown", {}, TODAY), client=client)
    (dry,) = client.calls
    assert dry.config.dry_run is True
    assert dry.config.use_query_cache is False


def test_estimate_and_execute_separately(catalog):
    prepared = prepare(catalog, "order_status_breakdown", {}, TODAY)
    client = StubClient(123)
    estimated = estimate(prepared, client=client)
    result = execute(prepared, client=client, estimated_bytes=estimated)
    assert estimated == 123 and result.estimated_bytes == 123
    assert [bool(c.config.dry_run) for c in client.calls] == [True, False]
    assert client.calls[1].config.maximum_bytes_billed == CAP


def test_estimate_refuses_over_the_cap_without_executing(catalog):
    client = StubClient(CAP + 1)
    with pytest.raises(OverCapError):
        estimate(prepare(catalog, "order_status_breakdown", {}, TODAY), client=client)
    assert len(client.calls) == 1


# --- the job, so a run can be found in the console ---

def test_execute_returns_the_job_id_project_and_location(catalog):
    client = StubClient(1000)
    result = execute(prepare(catalog, "revenue_by_period", {}, TODAY), client=client)
    assert (result.job_id, result.project, result.location) == ("stub-job", "stub-project", "US")
