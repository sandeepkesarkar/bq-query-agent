"""A stand-in for google.cloud.bigquery.Client, so tests and the evaluation runner need no cloud access."""
from types import SimpleNamespace

BILLED = 10 * 1024 ** 2  # BigQuery's minimum bill for one table


class StubRows:
    """Stands in for a RowIterator: a schema, and rows that have .values()."""

    def __init__(self, columns=("status", "orders"), rows=(("Shipped", 3),)):
        self.schema = [SimpleNamespace(name=c) for c in columns]
        self.rows = list(rows)

    def __iter__(self):
        return iter([SimpleNamespace(values=lambda r=r: r) for r in self.rows])


class StubClient:
    """Records every query. A dry run reports `estimate` bytes; a real run returns `rows`."""

    def __init__(self, estimate, rows=None):
        self.estimate = estimate
        self.rows = rows or StubRows()
        self.calls = []

    @property
    def executed(self):
        return [c for c in self.calls if not c.config.dry_run]

    def query(self, sql, job_config, location=None):
        self.calls.append(SimpleNamespace(sql=sql, config=job_config, location=location))
        if job_config.dry_run:
            return SimpleNamespace(total_bytes_processed=self.estimate)
        return SimpleNamespace(
            result=lambda: self.rows,
            total_bytes_processed=self.estimate,
            total_bytes_billed=BILLED,
            job_id="stub-job",
        )
