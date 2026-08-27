"""Small helpers shared by the public tracking-by-number API clients.

The consumer tracking JSON endpoints (DPD, Hermes, ...) aren't officially
documented, so parsing leans on these forgiving accessors: unknown or
renamed keys degrade to ``None``/``""`` instead of raising.
"""
from __future__ import annotations

from typing import Any


def pick(obj: Any, *keys: str) -> Any:
    """Return obj[key] for the first key present (case-insensitive-ish)."""
    if not isinstance(obj, dict):
        return None
    lowered = {k.lower(): v for k, v in obj.items()}
    for key in keys:
        if key in obj:
            return obj[key]
        if key.lower() in lowered:
            return lowered[key.lower()]
    return None


def text(value: Any) -> str:
    """Flatten the {'content': [...]} / plain-string label shapes seen."""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        content = value.get("content") or value.get("Content")
        if isinstance(content, list):
            return " ".join(str(p) for p in content if p).strip()
        if isinstance(content, str):
            return content.strip()
        for key in ("longText", "shortText", "label", "text", "name", "value"):
            if key in value:
                return text(value[key])
        return ""
    if isinstance(value, list):
        return " ".join(text(v) for v in value if v).strip()
    return ""


def first(value: Any) -> Any:
    if isinstance(value, list) and value:
        return value[0]
    return value


def as_bool(value: Any) -> bool:
    return value is True or str(value).strip().lower() in ("true", "1", "yes")
