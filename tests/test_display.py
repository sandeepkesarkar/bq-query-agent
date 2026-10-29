"""The demo's text formatting: tables, bound parameters and the BigQuery job links."""
from datetime import date, datetime
from decimal import Decimal

from agent.display import bq_show_command, console_link, format_parameters, format_table


def test_header_rule_and_aligned_rows():
    out = format_table(["name", "units"], [("Jeans", 1234567), ("Tee", 5)]).splitlines()
    assert out[0] == "name       units"
    assert out[1] == "-----  ---------"
    assert out[2] == "Jeans  1,234,567"
    assert out[3] == "Tee            5"


def test_empty_rows_show_only_the_header_and_rule():
    out = format_table(["a", "b"], []).splitlines()
    assert out == ["a  b", "-  -"]


def test_one_row():
    out = format_table(["status", "orders"], [("Shipped", 3)]).splitlines()
    assert len(out) == 3 and out[2] == "Shipped       3"


def test_more_rows_than_the_limit_end_with_a_count():
    rows = [(i,) for i in range(1, 14)]
    out = format_table(["n"], rows, limit=10).splitlines()
    assert out[-1] == "... 3 more rows" and len(out) == 2 + 10 + 1
    assert format_table(["n"], rows, limit=13).splitlines()[-1] == "13"


def test_none_is_an_empty_cell():
    out = format_table(["category", "rate"], [("Jeans", None), ("Tops", 0.5)]).splitlines()
    assert out[2].rstrip() == "Jeans" and out[3].endswith("0.50")


def test_a_long_string_is_cut_with_an_ellipsis():
    out = format_table(["name"], [("x" * 60,)]).splitlines()
    assert out[2] == "x" * 39 + "…" and len(out[2]) == 40


def test_floats_and_decimals_have_two_decimals_and_integers_separators():
    out = format_table(["f", "d", "i"], [(1234.5, Decimal("9876.543"), 1000)]).splitlines()
    assert out[2].split("  ") == ["1,234.50", "9,876.54", "1,000"]


def test_dates_are_iso_text_and_left_aligned():
    out = format_table(["day", "n"], [(date(2026, 1, 2), 1), (datetime(2026, 1, 3, 4, 5), 22)]).splitlines()
    assert out[2].startswith("2026-01-02") and out[3].startswith("2026-01-03T04:05:00")


def test_format_parameters_one_line_each():
    text = format_parameters([{"name": "start_date", "type": "DATE", "value": "2026-01-01"},
                              {"name": "category", "type": "STRING", "value": "NULL"}])
    assert text == "@start_date = 2026-01-01 (DATE)\n@category = NULL (STRING)"
    assert format_parameters([]) == ""


def test_console_link_and_bq_show_command():
    assert console_link("my-proj", "US", "job_123") == (
        "https://console.cloud.google.com/bigquery?project=my-proj&j=bq:US:job_123&page=queryresults")
    assert bq_show_command("US", "job_123") == "bq show -j --location=US job_123"
