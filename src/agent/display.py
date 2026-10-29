"""Text for the command-line demo: the rows, the bound parameters and the BigQuery job.

Pure functions that return strings; nothing here prints or touches the cloud.
"""
from datetime import date, datetime
from decimal import Decimal

CELL_WIDTH = 40
CONSOLE_URL = "https://console.cloud.google.com/bigquery?project={project}&j=bq:{location}:{job_id}&page=queryresults"


def _is_number(value):
    return isinstance(value, (int, float, Decimal)) and not isinstance(value, bool)


def _is_id_column(name):
    """A column named id or ending in _id holds identifiers, which are not quantities."""
    return str(name) == "id" or str(name).endswith("_id")


def _cell(value, plain=False):
    """The text of one cell: integers with thousands separators (plain digits in an id
    column), floats and Decimals with two decimals, dates as ISO text, None as empty, and
    anything longer than 40 characters cut."""
    if value is None:
        text = ""
    elif isinstance(value, bool):
        text = str(value)
    elif isinstance(value, int):
        text = str(value) if plain else f"{value:,}"
    elif isinstance(value, (float, Decimal)):
        text = f"{value:,.2f}"
    elif isinstance(value, (date, datetime)):
        text = value.isoformat()
    else:
        text = str(value)
    text = " ".join(text.split())  # a newline would break the table
    return text if len(text) <= CELL_WIDTH else text[:CELL_WIDTH - 1] + "…"


def format_table(columns, rows, limit=10):
    """An aligned text table: a header, a rule, up to `limit` rows, and "... N more rows".

    A column is right-aligned when every non-empty value in the shown rows is a number.
    """
    shown = [list(r) for r in rows[:limit]]
    plain = [_is_id_column(c) for c in columns]
    cells = [[_cell(v, plain[i]) for i, v in enumerate(row)] for row in shown]
    widths = [max([len(str(c))] + [len(row[i]) for row in cells]) for i, c in enumerate(columns)]
    right = [
        any(row[i] is not None for row in shown)
        and all(_is_number(row[i]) for row in shown if row[i] is not None)
        for i in range(len(columns))
    ]

    def line(values):
        parts = [v.rjust(w) if r else v.ljust(w) for v, w, r in zip(values, widths, right)]
        return "  ".join(parts).rstrip()

    out = [line([str(c) for c in columns]), "  ".join("-" * w for w in widths)]
    out += [line(row) for row in cells]
    if len(rows) > limit:
        out.append(f"... {len(rows) - limit:,} more rows")
    return "\n".join(out)


def format_parameters(parameters):
    """One line per bound parameter: "@start_date = 2026-01-01 (DATE)"."""
    return "\n".join(f"@{p['name']} = {p['value']} ({p['type']})" for p in parameters)


def console_link(project, location, job_id):
    """The BigQuery console page for a job."""
    return CONSOLE_URL.format(project=project, location=location, job_id=job_id)


def bq_show_command(location, job_id):
    """The command that prints the same job from a terminal."""
    return f"bq show -j --location={location} {job_id}"
