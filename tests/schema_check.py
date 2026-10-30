"""A tiny JSON Schema checker covering exactly the keywords schemas/finding.v1.schema.json uses.

Test-only: the tool itself validates events by hand (stdlib only, no jsonschema dependency), and this lets the tests
check that hand-written validation and the schema file agree.
"""
from __future__ import annotations

import re
from typing import Any

_TYPES = {
    "string": lambda v: isinstance(v, str),
    "integer": lambda v: type(v) is int,
    "boolean": lambda v: type(v) is bool,
    "array": lambda v: isinstance(v, list),
    "object": lambda v: isinstance(v, dict),
}


def validate(instance: Any, schema: dict) -> list[str]:
    return validate_sub(instance, schema, schema)


def _matches_pattern(pattern: str, text: str) -> bool:
    """A JSON Schema pattern is ECMA-262, where `$` holds only at the very end of the text. Python's also holds before a final
    newline, which would let "<40 hex>\\n" through here while any other validator of the schema file rejects it: `\\Z` is the end."""
    assert "$" not in pattern[:-1], f"{pattern}: a `$` other than the last character needs its own translation"
    return re.search(pattern[:-1] + r"\Z" if pattern.endswith("$") else pattern, text) is not None


def _resolve(ref: str, root: dict) -> dict:
    assert ref.startswith("#/"), f"unsupported $ref {ref}"
    node: Any = root
    for part in ref[2:].split("/"):
        node = node[part]
    return node


def _check(instance: Any, schema: dict, root: dict, path: str, errors: list[str]) -> None:
    if "$ref" in schema:
        _check(instance, _resolve(schema["$ref"], root), root, path, errors)
    if "oneOf" in schema:
        matching = [sub for sub in schema["oneOf"] if not validate_sub(instance, sub, root)]
        if len(matching) != 1:
            errors.append(f"{path}: matches {len(matching)} oneOf branches, expected 1")
    for sub in schema.get("allOf", []):
        _check(instance, sub, root, path, errors)
    if "if" in schema:
        branch = "then" if not validate_sub(instance, schema["if"], root) else "else"
        if branch in schema:
            _check(instance, schema[branch], root, path, errors)
    if "const" in schema and (instance != schema["const"] or type(instance) is not type(schema["const"])):
        errors.append(f"{path}: must equal {schema['const']!r}")
    if "enum" in schema and instance not in schema["enum"]:
        errors.append(f"{path}: {instance!r} not in {schema['enum']}")
    if "type" in schema and not _TYPES[schema["type"]](instance):
        errors.append(f"{path}: expected {schema['type']}")
        return
    if isinstance(instance, str):
        if len(instance) < schema.get("minLength", 0):
            errors.append(f"{path}: shorter than minLength")
        if "pattern" in schema and not _matches_pattern(schema["pattern"], instance):
            errors.append(f"{path}: {instance!r} does not match {schema['pattern']}")
    if type(instance) is int and "minimum" in schema and instance < schema["minimum"]:
        errors.append(f"{path}: below minimum {schema['minimum']}")
    if isinstance(instance, list):
        if len(instance) < schema.get("minItems", 0):
            errors.append(f"{path}: fewer than minItems")
        for index, item in enumerate(instance):
            if "items" in schema:
                _check(item, schema["items"], root, f"{path}[{index}]", errors)
    if isinstance(instance, dict):
        for key in schema.get("required", []):
            if key not in instance:
                errors.append(f"{path}: missing required {key!r}")
        properties = schema.get("properties", {})
        for key, value in instance.items():
            if key in properties:
                _check(value, properties[key], root, f"{path}.{key}", errors)
            elif schema.get("additionalProperties") is False:
                errors.append(f"{path}: unexpected property {key!r}")


def validate_sub(instance: Any, schema: dict, root: dict) -> list[str]:
    errors: list[str] = []
    _check(instance, schema, root, "$", errors)
    return errors
