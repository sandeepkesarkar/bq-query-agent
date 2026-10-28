"""Load the dataset config and render each function's SQL template.

A template holds two kinds of name:
- {name} is replaced here, by looking the name up in the config's tables, constants and
  snippets, or in the function's own slots. Nothing else can fill a placeholder, and an
  unknown name is an error. str.format is never used, so braces in a value cannot reach
  any other lookup.
- @name is a BigQuery query parameter. It is left in the SQL and bound by the runner.

Values from a question never enter a template. The only way a parameter changes the SQL
text is by choosing one of a slot's fixed values, and the choice is checked against the
parameter's allowed list first.
"""
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = ROOT / "config" / "thelook.yaml"

PLACEHOLDER = re.compile(r"\{([a-z_][a-z0-9_]*)\}")
QUERY_PARAM = re.compile(r"@([a-z_][a-z0-9_]*)")
MAX_DEPTH = 5  # snippets may hold placeholders; this bounds how deep that can go
PARAM_TYPES = {"enum", "int", "date"}


class ConfigError(Exception):
    """The config file is malformed or inconsistent."""


@dataclass(frozen=True)
class Param:
    name: str
    type: str                      # enum, int or date
    default: object = None         # for dates: window_start or as_of, bound as NULL
    values: tuple = ()             # allowed values for an enum
    min: int | None = None
    max: int | None = None
    optional: bool = False


@dataclass(frozen=True)
class Function:
    name: str
    description: str
    not_for: str
    params: dict                   # name -> Param
    slots: dict                    # slot name -> {"by": param name, "values": {choice: text}}
    output_columns: tuple
    sql: str                       # the raw template
    bound: frozenset = frozenset()  # parameters that appear in the SQL as @name


@dataclass(frozen=True)
class Catalog:
    dataset: dict
    limits: dict
    functions: dict                # name -> Function
    lookups: dict = field(repr=False)  # tables, constants and snippets, by placeholder name
    vocabulary: tuple = ()         # short sentences defining the period words a question may use
    vocabulary_in_prompts: bool = False  # whether the route and plan prompts carry them

    def render(self, function_name, choices):
        """Return the SQL for one function, with every {placeholder} filled.

        `choices` maps each slot-selecting parameter to its already-validated value.
        """
        fn = self.functions[function_name]
        known = dict(self.lookups)
        for slot_name, slot in fn.slots.items():
            choice = choices[slot["by"]]
            if choice not in slot["values"]:
                raise ConfigError(f"{function_name}: no {slot_name} for {slot['by']}={choice!r}")
            known[slot_name] = slot["values"][choice]
        return _fill(fn.sql, known, function_name)


def _fill(text, known, where, depth=0):
    if depth > MAX_DEPTH:
        raise ConfigError(f"{where}: placeholders nest deeper than {MAX_DEPTH}")

    def replace(match):
        name = match.group(1)
        if name not in known:
            raise ConfigError(f"{where}: unknown placeholder {{{name}}}")
        return _fill(str(known[name]), known, where, depth + 1)

    out = PLACEHOLDER.sub(replace, text)
    if "{" in out or "}" in out:
        raise ConfigError(f"{where}: a brace is left in the rendered SQL")
    return out


def _param(fn_name, name, spec, enums):
    kind = spec.get("type")
    if kind not in PARAM_TYPES:
        raise ConfigError(f"{fn_name}.{name}: unknown type {kind!r}")
    values = ()
    if kind == "enum":
        if "values_from" in spec:
            if spec["values_from"] not in enums:
                raise ConfigError(f"{fn_name}.{name}: no enum list {spec['values_from']!r}")
            values = tuple(enums[spec["values_from"]])
        else:
            values = tuple(spec.get("values") or ())
        if not values:
            raise ConfigError(f"{fn_name}.{name}: enum has no values")
    param = Param(
        name=name,
        type=kind,
        default=spec.get("default"),
        values=values,
        min=spec.get("min"),
        max=spec.get("max"),
        optional=bool(spec.get("optional", False)),
    )
    if kind == "int" and (param.min is None or param.max is None):
        raise ConfigError(f"{fn_name}.{name}: int needs min and max")
    if kind == "int" and param.default is not None and not param.min <= param.default <= param.max:
        raise ConfigError(f"{fn_name}.{name}: default {param.default} is outside its bounds")
    if kind != "date" and not param.optional and param.default is None:
        raise ConfigError(f"{fn_name}.{name}: needs a default or optional: true")
    if kind == "enum" and param.default is not None and param.default not in values:
        raise ConfigError(f"{fn_name}.{name}: default {param.default!r} is not allowed")
    if kind == "date" and param.default not in ("window_start", "as_of"):
        raise ConfigError(f"{fn_name}.{name}: date default must be window_start or as_of")
    return param


def load(path=DEFAULT_CONFIG):
    """Read the config, check it is consistent, and return a Catalog."""
    raw = yaml.safe_load(Path(path).read_text())
    for section in ("dataset", "tables", "constants", "limits", "enums", "snippets", "functions"):
        if section not in raw:
            raise ConfigError(f"missing section {section!r}")

    lookups = {}
    for section in ("tables", "constants", "snippets"):
        for key, value in raw[section].items():
            if key in lookups:
                raise ConfigError(f"placeholder {{{key}}} is defined twice")
            lookups[key] = value

    if not isinstance(raw.get("vocabulary_in_prompts", False), bool):
        raise ConfigError("vocabulary_in_prompts must be true or false")

    functions = {}
    for fn_name, spec in raw["functions"].items():
        params = {
            name: _param(fn_name, name, p, raw["enums"])
            for name, p in (spec.get("params") or {}).items()
        }
        slots = spec.get("slots") or {}
        for slot_name, slot in slots.items():
            if slot_name in lookups:
                raise ConfigError(f"{fn_name}: slot {{{slot_name}}} shadows a shared key")
            by = params.get(slot.get("by"))
            if by is None or by.type != "enum":
                raise ConfigError(f"{fn_name}.{slot_name}: must be selected by an enum param")
            if set(slot["values"]) != set(by.values):
                raise ConfigError(f"{fn_name}.{slot_name}: values must match {by.name}'s list")

        bound = frozenset(QUERY_PARAM.findall(spec["sql"]))
        fn = Function(
            name=fn_name,
            description=" ".join(spec["description"].split()),
            not_for=" ".join(spec.get("not_for", "").split()),
            params=params,
            slots=slots,
            output_columns=tuple(spec["output_columns"]),
            sql=spec["sql"],
            bound=bound,
        )
        # Every parameter must reach the query, either bound or by choosing a slot, and
        # every bound name in the SQL must be a declared parameter.
        selectors = {s["by"] for s in slots.values()}
        if bound - set(params):
            raise ConfigError(f"{fn_name}: undeclared query parameters {sorted(bound - set(params))}")
        if set(params) - bound - selectors:
            raise ConfigError(f"{fn_name}: unused parameters {sorted(set(params) - bound - selectors)}")
        if bound & selectors:
            raise ConfigError(f"{fn_name}: {sorted(bound & selectors)} both bound and slot-selecting")
        functions[fn_name] = fn

    catalog = Catalog(
        dataset=raw["dataset"], limits=raw["limits"], functions=functions, lookups=lookups,
        vocabulary=tuple(raw.get("vocabulary") or ()),
        vocabulary_in_prompts=bool(raw.get("vocabulary_in_prompts", False)),
    )

    # Render every slot combination once, so a bad template fails at load and not mid-question.
    for fn in functions.values():
        for choices in _slot_choices(fn):
            catalog.render(fn.name, choices)
    return catalog


def _slot_choices(fn):
    selectors = sorted({s["by"] for s in fn.slots.values()})
    combos = [{}]
    for name in selectors:
        combos = [{**c, name: v} for c in combos for v in fn.params[name].values]
    return combos
