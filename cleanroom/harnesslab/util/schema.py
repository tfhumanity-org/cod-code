"""Minimal JSON-Schema validator (FINAL - architect).

Tool inputs MUST be validated against their schema after coercion (03 §2.1,
03 R3/R4). We support the subset tool schemas actually use: ``type`` (incl.
lists of types), ``properties``, ``required``, ``additionalProperties``,
``enum``, ``const``, ``items``, ``minItems``/``maxItems``,
``minLength``/``maxLength``, ``minimum``/``maximum``, ``pattern``,
``anyOf``/``oneOf``.

``validate`` returns a list of human-readable error strings (empty = valid);
the tool pipeline turns them into an ``InputValidationError`` result.
"""

from __future__ import annotations

import re
from typing import Any

_TYPES: dict[str, tuple[type, ...]] = {
    "object": (dict,),
    "array": (list,),
    "string": (str,),
    "integer": (int,),
    "number": (int, float),
    "boolean": (bool,),
    "null": (type(None),),
}


def _type_ok(value: Any, t: str) -> bool:
    if t in ("integer", "number") and isinstance(value, bool):
        return False
    return isinstance(value, _TYPES.get(t, (object,)))


def validate(value: Any, schema: dict[str, Any], path: str = "$") -> list[str]:
    errs: list[str] = []
    if not schema:
        return errs
    if "anyOf" in schema or "oneOf" in schema:
        options = schema.get("anyOf") or schema.get("oneOf") or []
        if not any(not validate(value, s, path) for s in options):
            errs.append(f"{path}: does not match any allowed shape")
        return errs
    t = schema.get("type")
    if t is not None:
        types = t if isinstance(t, list) else [t]
        if not any(_type_ok(value, x) for x in types):
            return [f"{path}: expected {' or '.join(types)}, got {type(value).__name__}"]
    if "const" in schema and value != schema["const"]:
        errs.append(f"{path}: must equal {schema['const']!r}")
    if "enum" in schema and value not in schema["enum"]:
        errs.append(f"{path}: must be one of {schema['enum']!r}")
    if isinstance(value, str):
        if "minLength" in schema and len(value) < schema["minLength"]:
            errs.append(f"{path}: shorter than {schema['minLength']}")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            errs.append(f"{path}: longer than {schema['maxLength']}")
        if "pattern" in schema and not re.search(schema["pattern"], value):
            errs.append(f"{path}: does not match pattern {schema['pattern']!r}")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            errs.append(f"{path}: below minimum {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            errs.append(f"{path}: above maximum {schema['maximum']}")
    if isinstance(value, list):
        if "minItems" in schema and len(value) < schema["minItems"]:
            errs.append(f"{path}: fewer than {schema['minItems']} items")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            errs.append(f"{path}: more than {schema['maxItems']} items")
        if isinstance(schema.get("items"), dict):
            for i, item in enumerate(value):
                errs += validate(item, schema["items"], f"{path}[{i}]")
    if isinstance(value, dict):
        props: dict[str, Any] = schema.get("properties", {})
        for req in schema.get("required", []):
            if req not in value:
                errs.append(f"{path}: missing required property {req!r}")
        for k, v in value.items():
            if k in props:
                errs += validate(v, props[k], f"{path}.{k}")
            elif schema.get("additionalProperties") is False:
                errs.append(f"{path}: unexpected property {k!r}")
    return errs
