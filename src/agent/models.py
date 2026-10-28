"""The model interface, and a keyword stub that stands in for a real model.

The graph talks to a model through one method, complete_structured(prompt, schema), which
returns a dict shaped like `schema`. Swapping in a real model means writing one class with
that method and a `name`; the graph does not change.

The graph never trusts what comes back. Route output outside the allowed values is treated
as a refusal, and a plan is checked by code in the validate node. The plan schema is built
from the catalog (plan_schema), so a model is shown the allowed names and values.
"""
import html
import json
import re
from typing import Protocol

ROUTE_SCHEMA = {
    "title": "route",
    "type": "object",
    "properties": {
        "route": {"type": "string", "enum": ["answer", "clarify", "refuse"]},
        "reason": {"type": "string"},
    },
    "required": ["route", "reason"],
}

def _param_schema(name, param, existing):
    """The schema for one parameter, widened to cover `existing` (the same name in another function)."""
    if param.type == "enum":
        values = list(existing["enum"]) if existing else []
        values += [v for v in param.values if v not in values]
        schema = {"type": "string", "enum": values}
    elif param.type == "int":
        low = min(param.min, existing["minimum"]) if existing else param.min
        high = max(param.max, existing["maximum"]) if existing else param.max
        schema = {"type": "integer", "minimum": low, "maximum": high}
    else:
        schema = {"type": "string", "format": "date"}
    if existing and existing["type"] != schema["type"]:
        raise ValueError(f"parameter {name!r} has different types in different functions")
    return schema


def plan_schema(catalog):
    """The plan schema, built from the catalog.

    `function` is an enum of the function names. `params` lists every parameter name any
    function uses, all optional, with the union of allowed values and bounds across the
    functions that use it. The schema only narrows what a model writes; the checks in the
    validate node are still the real guard, because they know which parameters belong to
    which function.
    """
    properties = {}
    for fn in catalog.functions.values():
        for name, param in fn.params.items():
            properties[name] = _param_schema(name, param, properties.get(name))
    return {
        "title": "plan",
        "type": "object",
        "properties": {
            "function": {"type": "string", "enum": list(catalog.functions)},
            "params": {"type": "object", "properties": properties, "additionalProperties": False},
        },
        "required": ["function", "params"],
        "additionalProperties": False,
    }


SUMMARY_SCHEMA = {
    "title": "summary",
    "type": "object",
    "properties": {"summary": {"type": "string"}},
    "required": ["summary"],
}


class StructuredModel(Protocol):
    name: str

    def complete_structured(self, prompt: str, schema: dict) -> dict: ...


def escape_text(text):
    """Make text safe to put between prompt tags: '<', '>' and '&' can no longer open or close a tag."""
    return html.escape(str(text), quote=False)


def block(prompt, tag):
    """Return the text inside <tag>...</tag> in a prompt, or an empty string."""
    match = re.search(rf"<{tag}>\n?(.*?)\n?</{tag}>", prompt, re.DOTALL)
    return match.group(1) if match else ""


# --- the keyword stub ---
#
# The stub reads only the question (and, when re-planning, the errors) out of the prompt and
# matches keywords. It knows the function names the way a model would after reading the
# menu in the prompt; the graph itself never names a function.

REFUSE_WORDS = [
    "sql", "select ", "drop ", "delete ", "insert ", "update ", "truncate",
    "ignore previous", "ignore all", "ignore the", "system prompt", "your instructions",
    "email", "address", "phone", "password", "credit card", "personal",
    "who bought", "which customer", "customer named", "user id", "user_id", "names of",
]

# First match wins, so the more specific phrases come first.
FUNCTION_WORDS = [
    ("top_products", ["top ", "best-selling", "best selling", "bestseller", "best products"]),
    ("return_rate_by_category", ["return rate", "returned", "returns"]),
    ("average_order_value", ["average order", "aov", "order value"]),
    ("customers_by_traffic_source", ["traffic source", "traffic", "channel", "acquisition"]),
    ("order_status_breakdown", ["status", "cancelled", "shipped", "processing"]),
    ("revenue_by_period", ["revenue", "sales", "income"]),
]

IN_DOMAIN_WORDS = ["order", "product", "customer", "store", "shop", "business", "doing", "numbers"]

ISO_DATE = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")
YEAR = re.compile(r"\bin (\d{4})\b")


class KeywordModel:
    """A deterministic stand-in for a model. Good enough to drive the graph; not clever."""

    name = "keyword-stub"

    def __init__(self, catalog):
        self.params = {name: set(fn.params) for name, fn in catalog.functions.items()}
        categories = []
        for fn in catalog.functions.values():
            if "category" in fn.params:
                categories = list(fn.params["category"].values)
        # Longest first, so "Pants & Capris" wins over "Pants".
        self.categories = sorted(categories, key=len, reverse=True)

    def complete_structured(self, prompt, schema):
        task = schema.get("title")
        if task == "route":
            return self._route(self._question(prompt).lower())
        if task == "plan":
            return self._plan(self._question(prompt), block(prompt, "errors"))
        if task == "summary":
            return self._summary(prompt)
        raise ValueError(f"the keyword stub has no answer for schema {task!r}")

    @staticmethod
    def _question(prompt):
        return html.unescape(block(prompt, "question"))  # the graph escapes it (escape_text)

    def _function(self, text):
        for name, words in FUNCTION_WORDS:
            if any(w in text for w in words):
                return name
        return None

    def _route(self, text):
        if any(w in text for w in REFUSE_WORDS):
            return {"route": "refuse",
                    "reason": "I can only report aggregate store metrics, not SQL or personal data."}
        if self._function(text):
            return {"route": "answer", "reason": "matches a function on the menu"}
        if any(w in text for w in IN_DOMAIN_WORDS):
            return {"route": "clarify",
                    "reason": "Which measure do you mean: revenue, top products, order status, "
                              "average order value, return rate, or customers by traffic source?"}
        return {"route": "refuse", "reason": "That question is outside what this store data covers."}

    def _plan(self, question, errors):
        text = question.lower()
        function = self._function(text) or ""
        params = {}
        if "quarter" in text:
            params["group_by"] = "quarter"
        elif "year" in text or "annual" in text:
            params["group_by"] = "year"
        elif "month" in text:
            params["group_by"] = "month"
        if "profit" in text:
            params["metric"] = "profit"
        elif "units" in text or "quantity" in text:
            params["metric"] = "units"
        if m := re.search(r"\btop (\d+)\b", text):
            params["n"] = int(m.group(1))
        if m := re.search(r"\b(\d+) orders\b", text):
            params["min_orders"] = int(m.group(1))
        for category in self.categories:
            if re.search(rf"\b{re.escape(category.lower())}\b", text):
                params["category"] = category
                break
        dates = ISO_DATE.findall(question)
        if len(dates) >= 2:
            params["start_date"], params["end_date"] = dates[0], dates[1]
        elif len(dates) == 1:
            params["start_date"] = dates[0]
        elif m := YEAR.search(text):
            params["start_date"], params["end_date"] = f"{m.group(1)}-01-01", f"{m.group(1)}-12-31"

        # Keep only what the function takes, and on a re-plan drop every parameter that an
        # error named, so it falls back to its default.
        allowed = self.params.get(function, set())
        rejected = set(re.findall(r"^(\w+):", errors, re.MULTILINE))
        if "is after end_date" in errors or "given without" in errors:
            rejected |= {"start_date", "end_date"}
        params = {k: v for k, v in params.items() if k in allowed and k not in rejected}
        return {"function": function, "params": params}

    def _summary(self, prompt):
        rows = json.loads(block(prompt, "rows") or "[]")
        columns = json.loads(block(prompt, "columns") or "[]")
        if not rows:
            return {"summary": "The query returned no rows."}
        first = ", ".join(f"{c} {v}" for c, v in zip(columns, rows[0]))
        return {"summary": f"{len(rows)} rows. The first is: {first}."}
