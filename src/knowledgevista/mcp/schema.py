"""Checking a tool call's arguments against the small JSON-Schema subset our tools declare.

Only what the tools use: object properties, `required`, `additionalProperties: false`, string / integer / array-of-string types,
`enum`, `minimum` / `maximum`, `minLength` / `maxLength`, `maxItems`. A wrong argument becomes a tool error that says which one and
why, so a model can correct itself in the next call (a protocol error would end the exchange without telling it what was wrong).
"""

from __future__ import annotations

from typing import Any


def validate(schema: dict[str, Any], arguments: Any) -> list[str]:
    """The reasons `arguments` do not satisfy `schema`; empty when they do."""
    if not isinstance(arguments, dict):
        return ["arguments must be an object"]
    problems: list[str] = []
    properties = schema.get("properties", {})
    for name in schema.get("required", []):
        if name not in arguments:
            problems.append(f"missing required argument {name!r}")
    if schema.get("additionalProperties") is False:
        problems += [f"unknown argument {name!r} (allowed: {', '.join(sorted(properties))})" for name in arguments if name not in properties]
    for name, value in arguments.items():
        if name in properties:
            problems += [f"{name}: {why}" for why in _check(properties[name], value)]
    return problems


def _check(spec: dict[str, Any], value: Any) -> list[str]:
    kind = spec.get("type")
    if kind == "string":
        if not isinstance(value, str):
            return ["must be a string"]
        out = []
        if "minLength" in spec and len(value) < spec["minLength"]:
            out.append(f"must have at least {spec['minLength']} character(s)")
        if "maxLength" in spec and len(value) > spec["maxLength"]:
            out.append(f"must have at most {spec['maxLength']} characters")
        if "enum" in spec and value not in spec["enum"]:
            out.append(f"must be one of {', '.join(map(repr, spec['enum']))}")
        return out
    if kind == "integer":
        if isinstance(value, bool) or not isinstance(value, int):
            return ["must be an integer"]
        out = []
        if "minimum" in spec and value < spec["minimum"]:
            out.append(f"must be at least {spec['minimum']}")
        if "maximum" in spec and value > spec["maximum"]:
            out.append(f"must be at most {spec['maximum']}")
        return out
    if kind == "array":
        if not isinstance(value, list):
            return ["must be an array"]
        out = []
        if "maxItems" in spec and len(value) > spec["maxItems"]:
            out.append(f"must have at most {spec['maxItems']} item(s)")
        item = spec.get("items")
        if item:
            for i, element in enumerate(value):
                out += [f"item {i} {why}" for why in _check(item, element)]
        return out
    return []
