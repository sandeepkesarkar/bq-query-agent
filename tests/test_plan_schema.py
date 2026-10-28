"""The plan schema built from the catalog. No cloud access."""
from datetime import date

import jsonschema
import pytest

from agent.graph import _plan_valid
from agent.models import plan_schema
from functions.catalog import load

TODAY = date(2026, 10, 27)


@pytest.fixture(scope="module")
def catalog():
    return load()


@pytest.fixture(scope="module")
def schema(catalog):
    return plan_schema(catalog, "flat")   # the flat schema; test_period_schema.py has the period one


def objects(node):
    if isinstance(node, dict):
        if node.get("type") == "object":
            yield node
        for value in node.values():
            yield from objects(value)


def full_plan(fn):
    """A plan that sets every parameter of one function to a valid value."""
    params = {}
    for p in fn.params.values():
        params[p.name] = (p.values[0] if p.type == "enum" else p.max if p.type == "int"
                          else "2026-01-01" if p.name == "start_date" else "2026-06-30")
    return {"function": fn.name, "params": params}


def test_title_and_required_fields(schema):
    assert schema["title"] == "plan"
    assert schema["required"] == ["function", "params"]


def test_every_function_is_in_the_enum(catalog, schema):
    assert schema["properties"]["function"]["enum"] == list(catalog.functions)


def test_every_object_forbids_extra_properties(schema):
    found = list(objects(schema))
    assert len(found) == 2  # the plan and its params
    assert all(o["additionalProperties"] is False for o in found)


def test_every_catalog_parameter_appears_and_none_is_required(catalog, schema):
    params = schema["properties"]["params"]
    used = {name for fn in catalog.functions.values() for name in fn.params}
    assert set(params["properties"]) == used
    assert "required" not in params


def test_bounds_enums_and_dates(catalog, schema):
    props = schema["properties"]["params"]["properties"]
    assert props["n"] == {"type": "integer", "minimum": 1, "maximum": 20}
    assert props["min_orders"] == {"type": "integer", "minimum": 1, "maximum": 1000}
    assert props["start_date"] == props["end_date"] == {"type": "string", "format": "date"}
    # group_by is month|quarter|year in one function and month|quarter in another: the union.
    assert props["group_by"]["enum"] == ["month", "quarter", "year"]
    assert props["metric"]["enum"] == ["revenue", "units", "profit"]
    categories = catalog.functions["top_products"].params["category"].values
    assert props["category"]["enum"] == list(categories)


@pytest.mark.parametrize("name", list(load().functions))
def test_a_valid_plan_for_each_function_passes(catalog, schema, name):
    plan = full_plan(catalog.functions[name])
    jsonschema.validate(plan, schema, format_checker=jsonschema.FormatChecker())
    assert _plan_valid(catalog, plan, TODAY) == []  # and the code check agrees


@pytest.mark.parametrize("name", list(load().functions))
def test_a_plan_with_no_params_passes(schema, name):
    jsonschema.validate({"function": name, "params": {}}, schema)


@pytest.mark.parametrize("plan", [
    {"function": "export_all_users", "params": {}},
    {"function": "top_products", "params": {"n": 99}},
    {"function": "top_products", "params": {"category": "Shoes"}},
    {"function": "top_products", "params": {"start_date": "yesterday"}},
    {"function": "top_products", "params": {"sql": "SELECT 1"}},
    {"function": "top_products", "params": {}, "sql": "SELECT 1"},
    {"function": "top_products"},
])
def test_plans_outside_the_schema_fail(schema, plan):
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(plan, schema, format_checker=jsonschema.FormatChecker())


def test_the_schema_does_not_replace_the_code_check(catalog, schema):
    # group_by "year" is in the schema (union) but average_order_value does not take it.
    plan = {"function": "average_order_value", "params": {"group_by": "year"}}
    jsonschema.validate(plan, schema)
    assert _plan_valid(catalog, plan, TODAY)
