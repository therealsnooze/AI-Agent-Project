"""Validate model-produced tool arguments against the tool's JSON schema.

Strict tool use already constrains the model, but the executor never trusts
its input: this is the first gate every action passes through."""

from __future__ import annotations

from typing import Any

_TYPES = {
    "object": dict,
    "array": list,
    "string": str,
    "boolean": bool,
    "integer": int,
    "number": (int, float),
}

MAX_STRING_CHARS = 250_000


def validate(schema: dict, value: Any, path: str = "input") -> list[str]:
    errors: list[str] = []
    expected = schema.get("type")
    py_type = _TYPES.get(expected)
    if py_type is not None:
        if expected in ("integer", "number") and isinstance(value, bool):
            return [f"{path}: expected {expected}, got boolean"]
        if not isinstance(value, py_type):
            return [f"{path}: expected {expected}, got {type(value).__name__}"]
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: {value!r} is not one of {schema['enum']}")
    if expected == "string":
        if len(value) > MAX_STRING_CHARS:
            errors.append(f"{path}: string is too long ({len(value):,} chars)")
    elif expected == "object":
        props = schema.get("properties", {})
        for key in schema.get("required", []):
            if key not in value:
                errors.append(f"{path}.{key}: required field missing")
        if schema.get("additionalProperties") is False:
            for key in value:
                if key not in props:
                    errors.append(f"{path}.{key}: unexpected field")
        for key, sub in props.items():
            if key in value:
                errors.extend(validate(sub, value[key], f"{path}.{key}"))
    elif expected == "array" and "items" in schema:
        for i, item in enumerate(value):
            errors.extend(validate(schema["items"], item, f"{path}[{i}]"))
    return errors
