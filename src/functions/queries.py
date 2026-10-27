"""Check a function call against the catalog, then dry-run, cap and execute it.

    result = run("top_products", {"metric": "units", "n": 5})

or, with a pause between the cost check and the run (the agent's approval step):

    prepared = prepare(catalog, "top_products", {"metric": "units", "n": 5})
    estimated = estimate(prepared)          # raises OverCapError over the cap
    result = execute(prepared, estimated_bytes=estimated)

Every step that decides whether a query runs is here, in code:
1. validate: the function is on the menu, every parameter is known, enums are in their
   lists, ints are within bounds, dates are within the limits and start is not after end.
2. bind: every value becomes a typed query parameter (DATE, INT64 or STRING). A date or
   category left out is still bound, as a typed NULL, and the SQL falls back to its default.
3. dry run: BigQuery estimates the bytes. Anything over limits.max_bytes_per_query is
   refused and never runs.
4. execute: the same cap is set as maximum_bytes_billed, so BigQuery itself stops a job
   whose bill would go over it.
"""
from dataclasses import dataclass
from datetime import date, datetime, timezone

from google.api_core.exceptions import GoogleAPICallError
from google.cloud import bigquery

from functions.catalog import load

BQ_TYPES = {"date": "DATE", "int": "INT64", "enum": "STRING"}


class ValidationError(ValueError):
    """The call is not allowed. `errors` lists every problem found, not just the first."""

    def __init__(self, errors):
        self.errors = list(errors)
        super().__init__("; ".join(self.errors))


class OverCapError(Exception):
    """The dry-run estimate is over the per-query cap. Nothing was run."""

    def __init__(self, estimated_bytes, cap):
        self.estimated_bytes = estimated_bytes
        self.cap = cap
        if estimated_bytes is None:
            super().__init__("the dry run gave no byte estimate, so the query was refused")
        else:
            super().__init__(f"estimated {estimated_bytes:,} bytes is over the cap of {cap:,}")


class BilledLimitError(Exception):
    """BigQuery stopped the job because its bill would pass maximum_bytes_billed.

    BigQuery reports this as HTTP 500, which normally means "retry". Retrying cannot help,
    so it gets its own error.
    """

    def __init__(self, cap, job_id, message):
        self.cap = cap
        self.job_id = job_id
        super().__init__(f"BigQuery stopped job {job_id}: bill would pass {cap:,} bytes ({message})")


def _billed_limit_exceeded(exc):
    reasons = {e.get("reason") for e in getattr(exc, "errors", None) or [] if isinstance(e, dict)}
    return "bytesBilledLimitExceeded" in reasons or "bytesBilledLimitExceeded" in str(exc)


@dataclass(frozen=True)
class Prepared:
    """A validated call: the rendered SQL and its typed parameters, ready to dry-run."""
    function: str
    values: dict          # every parameter, with defaults filled in; None means bound as NULL
    sql: str
    query_parameters: list
    cap: int              # limits.max_bytes_per_query, for the dry-run check and the job
    location: str | None


@dataclass(frozen=True)
class Result:
    function: str
    values: dict
    columns: list
    rows: list            # one tuple per row, in column order
    estimated_bytes: int | None  # None if execute() was called without an estimate
    processed_bytes: int
    billed_bytes: int


def _date_max(limits, today):
    value = limits["date_max"]
    if value == "today_utc":
        return today
    if isinstance(value, date):
        return value
    raise ValueError(f"limits.date_max must be a date or today_utc, not {value!r}")


def _as_date(value):
    if isinstance(value, datetime):  # a datetime is a date subclass; a timestamp is not a date
        return None
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError:
            return None
    return None


def validate(catalog, function, params, today=None):
    """Return every parameter's value, with defaults filled in, or raise ValidationError."""
    if function not in catalog.functions:
        raise ValidationError([f"unknown function {function!r}"])
    if not isinstance(params, dict):
        raise ValidationError(["params must be a mapping of name to value"])
    fn = catalog.functions[function]
    today = today or datetime.now(timezone.utc).date()
    date_min = catalog.limits["date_min"]
    date_max = _date_max(catalog.limits, today)

    errors = [f"unknown parameter {name!r}" for name in params if name not in fn.params]
    values = {}
    for name, p in fn.params.items():
        value = params.get(name)
        if value is None:
            if p.type == "date" or p.optional:
                values[name] = None  # bound as NULL; the SQL falls back to the default
            else:
                values[name] = p.default
            continue
        if p.type == "enum":
            if not isinstance(value, str) or value not in p.values:
                errors.append(f"{name}: {value!r} is not one of the allowed values")
                continue
        elif p.type == "int":
            if not isinstance(value, int) or isinstance(value, bool):
                errors.append(f"{name}: {value!r} is not a whole number")
                continue
            if not p.min <= value <= p.max:
                errors.append(f"{name}: {value} is outside {p.min} to {p.max}")
                continue
        elif p.type == "date":
            parsed = _as_date(value)
            if parsed is None:
                errors.append(f"{name}: {value!r} is not a date in YYYY-MM-DD form")
                continue
            if not date_min <= parsed <= date_max:
                errors.append(f"{name}: {parsed} is outside {date_min} to {date_max}")
                continue
            value = parsed
        values[name] = value

    start, end = values.get("start_date"), values.get("end_date")
    if isinstance(start, date) and isinstance(end, date) and start > end:
        errors.append(f"start_date {start} is after end_date {end}")
    if errors:
        raise ValidationError(errors)
    return values


def prepare(catalog, function, params, today=None):
    """Validate a call, render its SQL and build its typed query parameters."""
    values = validate(catalog, function, params, today)
    fn = catalog.functions[function]
    sql = catalog.render(function, values)
    query_parameters = [
        bigquery.ScalarQueryParameter(name, BQ_TYPES[fn.params[name].type], values[name])
        for name in sorted(fn.bound)
    ]
    return Prepared(
        function=function,
        values=values,
        sql=sql,
        query_parameters=query_parameters,
        cap=catalog.limits["max_bytes_per_query"],
        location=catalog.dataset.get("location"),
    )


def estimate(prepared, *, client=None):
    """Dry-run the query and return its estimated bytes. Raises OverCapError over the cap.

    Nothing is executed and nothing is billed.
    """
    client = client or bigquery.Client()
    dry = client.query(
        prepared.sql,
        job_config=bigquery.QueryJobConfig(
            dry_run=True,
            use_query_cache=False,  # a cached result would report 0 bytes
            query_parameters=prepared.query_parameters,
        ),
        location=prepared.location,
    )
    estimated = dry.total_bytes_processed
    if estimated is None or estimated > prepared.cap:
        raise OverCapError(estimated, prepared.cap)
    return estimated


def execute(prepared, *, client=None, estimated_bytes=None):
    """Run the query with the cap as maximum_bytes_billed and return the rows and bytes.

    Call estimate() first: this is the second cap, not the first. Raises BilledLimitError if
    BigQuery stops the job at maximum_bytes_billed (the stopped job records no bytes
    processed or billed).
    """
    client = client or bigquery.Client()
    job = client.query(
        prepared.sql,
        job_config=bigquery.QueryJobConfig(
            use_query_cache=False,
            maximum_bytes_billed=prepared.cap,
            query_parameters=prepared.query_parameters,
        ),
        location=prepared.location,
    )
    try:
        rows = job.result()
    except GoogleAPICallError as exc:
        if _billed_limit_exceeded(exc):
            raise BilledLimitError(prepared.cap, job.job_id, exc.message) from exc
        raise
    return Result(
        function=prepared.function,
        values=prepared.values,
        columns=[f.name for f in rows.schema],
        rows=[tuple(r.values()) for r in rows],
        estimated_bytes=estimated_bytes,
        processed_bytes=job.total_bytes_processed,
        billed_bytes=job.total_bytes_billed,
    )


def run(function, params=None, *, catalog=None, client=None, today=None):
    """Validate, dry-run, refuse over the cap, execute, and return the rows and bytes.

    Raises ValidationError if the call is not allowed and OverCapError if the estimate is
    over the cap; in both cases nothing was executed. Raises BilledLimitError if BigQuery
    stops the job at maximum_bytes_billed.
    """
    catalog = catalog or load()
    prepared = prepare(catalog, function, params or {}, today)
    client = client or bigquery.Client()
    estimated = estimate(prepared, client=client)
    return execute(prepared, client=client, estimated_bytes=estimated)
