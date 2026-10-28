"""Deterministic argument key normalization for tool schemas."""
from __future__ import annotations


def _key_token(value: str) -> str:
    return value.replace("_", "").replace("-", "").casefold()


def normalize_arguments(args, schema):
    """Normalize unique case/separator variants of declared schema keys.

    Unknown keys are intentionally retained so the caller's schema validator can
    reject them. Values and tool names are never inferred or changed.
    """
    if not isinstance(args, dict) or not isinstance(schema, dict):
        raise ValueError("invalid_tool_arguments")
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        return dict(args)

    schema_keys = [key for key in properties if isinstance(key, str)]
    result = {}
    for input_key, value in args.items():
        if input_key in properties:
            target = input_key
        elif isinstance(input_key, str):
            candidates = [key for key in schema_keys
                          if _key_token(key) == _key_token(input_key)]
            if len(candidates) > 1:
                raise ValueError("ambiguous_tool_argument_alias")
            target = candidates[0] if candidates else input_key
        else:
            target = input_key
        if target in result:
            raise ValueError("ambiguous_tool_argument_alias")
        property_schema = properties.get(target)
        if (target in properties and isinstance(property_schema, dict)
                and isinstance(value, dict)):
            value = normalize_arguments(value, property_schema)
        result[target] = value
    return result
