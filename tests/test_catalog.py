"""The loader: placeholders are filled only from known keys, and a bad config fails at load."""
import copy
from pathlib import Path

import pytest
import yaml

from dry_run_queries import SQL_FILE, split_queries
from functions.catalog import DEFAULT_CONFIG, ConfigError, _slot_choices, load

SRC = Path(__file__).resolve().parents[1] / "src" / "functions"
FUNCTIONS = [
    "revenue_by_period",
    "top_products",
    "order_status_breakdown",
    "average_order_value",
    "return_rate_by_category",
    "customers_by_traffic_source",
]


@pytest.fixture(scope="module")
def catalog():
    return load()


@pytest.fixture
def raw():
    return yaml.safe_load(DEFAULT_CONFIG.read_text())


def load_raw(raw, tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(raw))
    return load(path)


def test_loads_the_six_functions(catalog):
    assert list(catalog.functions) == FUNCTIONS


def test_every_function_has_a_reference_query():
    names = [name for _n, name, _s, _e, _sql in split_queries(SQL_FILE.read_text())]
    assert names == FUNCTIONS


@pytest.mark.parametrize("name", FUNCTIONS)
def test_every_slot_choice_renders_without_braces(catalog, name):
    fn = catalog.functions[name]
    for choices in _slot_choices(fn):
        sql = catalog.render(name, choices)
        assert "{" not in sql and "}" not in sql
        assert "`bigquery-public-data.thelook_ecommerce.orders`" in sql
        assert "INTERVAL 11 MONTH" in sql  # the window snippet, with its own placeholder filled


def test_slot_choice_changes_only_the_slot(catalog):
    by_units = catalog.render("top_products", {"metric": "units"})
    by_profit = catalog.render("top_products", {"metric": "profit"})
    assert "ORDER BY units DESC, product_id" in by_units
    assert "ORDER BY profit DESC, product_id" in by_profit
    assert by_units.replace("units DESC", "profit DESC") == by_profit


def test_render_rejects_a_choice_outside_the_slot(catalog):
    with pytest.raises(ConfigError):
        catalog.render("top_products", {"metric": "revenue; DROP TABLE x"})


def test_unknown_placeholder_fails_at_load(raw, tmp_path):
    raw["functions"]["order_status_breakdown"]["sql"] += "\n-- {no_such_key}"
    with pytest.raises(ConfigError, match="no_such_key"):
        load_raw(raw, tmp_path)


def test_a_key_defined_twice_fails(raw, tmp_path):
    raw["constants"]["orders"] = 1
    with pytest.raises(ConfigError, match="defined twice"):
        load_raw(raw, tmp_path)


def test_a_slot_cannot_shadow_a_shared_key(raw, tmp_path):
    fn = raw["functions"]["top_products"]
    fn["slots"]["orders"] = fn["slots"].pop("order_by")
    with pytest.raises(ConfigError, match="shadows"):
        load_raw(raw, tmp_path)


def test_slot_values_must_match_the_enum(raw, tmp_path):
    del raw["functions"]["top_products"]["slots"]["order_by"]["values"]["profit"]
    with pytest.raises(ConfigError, match="must match"):
        load_raw(raw, tmp_path)


def test_undeclared_query_parameter_fails(raw, tmp_path):
    raw["functions"]["order_status_breakdown"]["sql"] += "\n-- @status"
    with pytest.raises(ConfigError, match="undeclared"):
        load_raw(raw, tmp_path)


def test_unused_parameter_fails(raw, tmp_path):
    raw["functions"]["order_status_breakdown"]["params"]["n"] = {
        "type": "int", "min": 1, "max": 5, "default": 1,
    }
    with pytest.raises(ConfigError, match="unused"):
        load_raw(raw, tmp_path)


@pytest.mark.parametrize(
    "spec, message",
    [
        ({"type": "text", "default": "x"}, "unknown type"),
        ({"type": "enum", "values": [], "default": "x"}, "no values"),
        ({"type": "enum", "values_from": "colour", "optional": True}, "no enum list"),
        ({"type": "enum", "values": ["a", "b"], "default": "c"}, "is not allowed"),
        ({"type": "enum", "values": ["a", "b"]}, "needs a default"),
        ({"type": "int", "max": 5, "default": 1}, "needs min and max"),
        ({"type": "int", "min": 1, "max": 5, "default": 6}, "outside its bounds"),
        ({"type": "int", "min": 1, "max": 5}, "needs a default"),
        ({"type": "date", "default": "yesterday"}, "window_start or as_of"),
    ],
)
def test_bad_parameter_specs_fail_at_load(raw, tmp_path, spec, message):
    fn = raw["functions"]["order_status_breakdown"]
    fn["params"]["extra"] = spec
    fn["sql"] += "\n-- @extra"  # so the parameter counts as used
    with pytest.raises(ConfigError, match=message):
        load_raw(raw, tmp_path)


def test_category_list_comes_from_enums(catalog, raw):
    assert catalog.functions["top_products"].params["category"].values == tuple(
        raw["enums"]["category"]
    )


def test_snippet_nesting_is_bounded(raw, tmp_path):
    raw = copy.deepcopy(raw)
    raw["snippets"]["loop"] = "{loop}"
    raw["functions"]["order_status_breakdown"]["sql"] += "\n-- {loop}"
    with pytest.raises(ConfigError, match="deeper"):
        load_raw(raw, tmp_path)


def test_library_never_uses_str_format():
    for path in SRC.glob("*.py"):
        text = path.read_text()
        assert ".format(" not in text and "format_map(" not in text, path.name
