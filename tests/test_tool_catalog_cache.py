"""Stable tool schema serialization and progressive authorization checks."""
from copy import deepcopy
import json

from warmagent.tool_catalog import ToolCatalog


def _entry(name, *, reverse=False, description="fixture"):
    properties = {"zeta": {"type": "string", "description": "last"},
                  "alpha": {"enum": ["second", "first"], "type": "string"}}
    parameters = {"required": ["zeta", "alpha"], "properties": properties,
                  "additionalProperties": False, "type": "object"}
    function = {"parameters": parameters, "description": description, "name": name}
    if reverse:
        properties = dict(reversed(list(properties.items())))
        parameters = {key: parameters[key] for key in reversed(parameters)}
        function = {key: function[key] for key in reversed(function)}
    return ({"function": function, "type": "function"}, lambda **_kwargs: None)


def _serialized(catalog):
    return json.dumps(catalog.schemas(), ensure_ascii=False, separators=(",", ":"))


def test_registration_and_nested_mapping_order_produce_same_schema_bytes():
    names = ["harness__zeta", "harness__alpha", "recall"]
    forward = {name: _entry(name) for name in names}
    reverse = {name: _entry(name, reverse=True) for name in reversed(names)}
    forward_before, reverse_before = deepcopy(forward), deepcopy(reverse)

    left, right = ToolCatalog(forward), ToolCatalog(reverse)

    assert _serialized(left) == _serialized(right)
    output = left.schemas()
    assert [row["function"]["name"] for row in output] == ["harness__alpha", "harness__zeta", "recall"]
    parameters = output[0]["function"]["parameters"]
    assert parameters["required"] == ["zeta", "alpha"]
    assert parameters["properties"]["alpha"]["enum"] == ["second", "first"]
    assert forward == forward_before and reverse == reverse_before
    assert list(forward["harness__zeta"][0]["function"]) == list(
        forward_before["harness__zeta"][0]["function"])


def test_progressive_discovery_is_stable_and_only_expands_registered_tools():
    handlers = {f"harness__tool_{index:02d}": _entry(f"harness__tool_{index:02d}")
                for index in reversed(range(10))}
    catalog = ToolCatalog(handlers)
    assert [row["function"]["name"] for row in catalog.schemas()] == ["harness_discover_tools"]

    result = catalog.discover()
    assert [row["function"]["name"] for row in result["tools"]] == [
        f"harness__tool_{index:02d}" for index in range(8)]
    assert result["remaining"] == ["harness__tool_08", "harness__tool_09"]
    assert catalog.expanded == {row["function"]["name"] for row in result["tools"]}

    sibling = ToolCatalog(dict(reversed(list(handlers.items()))))
    categories = catalog.discover(query="files android")["categories"]
    assert categories == sibling.discover(query="files android")["categories"]
    assert list(catalog.category_view()) == sorted(catalog.category_view())


def test_new_turn_uses_current_permissions_and_schema_without_preexpanding_new_tools():
    handlers = {f"harness__tool_{index:02d}": _entry(f"harness__tool_{index:02d}")
                for index in range(10)}
    catalog = ToolCatalog(handlers)
    catalog.discover(names=["harness__tool_00"])
    assert "harness__tool_00" in {row["function"]["name"] for row in catalog.schemas()}
    expanded = catalog.state()["expanded"]

    del handlers["harness__tool_00"]
    handlers.pop("harness__tool_09")
    handlers["harness__new"] = _entry("harness__new", description="newly granted")
    current_catalog = ToolCatalog(handlers, expanded)
    current = {row["function"]["name"] for row in current_catalog.schemas()}
    assert "harness__tool_00" not in current
    assert "harness__tool_09" not in current
    assert "harness__new" not in current
    assert current_catalog.discover(names=["harness__new"])["tools"][0]["function"]["name"] == "harness__new"
    assert "harness__new" in {row["function"]["name"] for row in current_catalog.schemas()}

    handlers["harness__new"] = _entry("harness__new", description="updated authorized description")
    updated_turn = ToolCatalog(handlers, current_catalog.state()["expanded"])
    schema = next(row for row in updated_turn.schemas() if row["function"]["name"] == "harness__new")
    assert schema["function"]["description"] == "updated authorized description"
