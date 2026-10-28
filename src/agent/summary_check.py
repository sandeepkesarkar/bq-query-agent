"""Check that every number in a model's summary can be found in the rows it summarized.

The check is deliberately strict. A summary that fails is replaced by a plain sentence built
here, so a wrong or invented figure never reaches the person. A true figure the check does not
recognise (for example "1.2 million" for 1234567) also fails; that costs a less fluent answer,
not a wrong one.
"""
import re
from decimal import Decimal

NUMBER = re.compile(r"\d+(?:,\d{3})*(?:\.\d+)?")


def _tokens(text):
    return [Decimal(t.replace(",", "")) for t in NUMBER.findall(text)]


def _row_values(rows):
    """Every number a summary could be quoting: numeric cells, and digits inside text cells
    such as the month label '2026-03'."""
    values = []
    for row in rows:
        for cell in row:
            if cell is None or isinstance(cell, bool):
                continue
            if isinstance(cell, (int, float, Decimal)):
                values.append(abs(Decimal(str(cell))))
            else:
                values.extend(_tokens(str(cell)))
    return values


def unmatched_numbers(summary, rows):
    """Numbers in `summary` that appear in no row (and are not the row count). Empty means ok.

    A number matches a row value if it equals it at the precision it is written with: "9.5"
    matches 9.46, "1,234" matches 1234.2. A number followed by % also matches the value times 100.
    """
    values = _row_values(rows) + [Decimal(len(rows))]
    missing = []
    for match in NUMBER.finditer(summary):
        text = match.group()
        shown = Decimal(text.replace(",", ""))
        half_unit = Decimal(1).scaleb(shown.as_tuple().exponent) / 2
        candidates = list(values)
        if summary[match.end():match.end() + 1] == "%":
            candidates += [v * 100 for v in values]
        if not any(abs(v - shown) <= half_unit for v in candidates):
            missing.append(text)
    return missing


def fallback_summary(columns, rows):
    """A plain answer built by code: the row count and the first row."""
    if not rows:
        return "The query returned no rows."
    first = ", ".join(f"{c} {v}" for c, v in zip(columns, rows[0]))
    return f"{len(rows)} rows. The first is: {first}."
