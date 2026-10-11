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


_RETURN_WORDS = ("rücksendung", "ruecksendung", "retour")


def is_return(item: Any) -> bool:
    """True for a return shipment ("Retoure" / "Rücksendung").

    Carriers flag returns differently: the DPD account marks them with
    ``direction == "return"``, others only say so in the status text.
    Returns are not parcels that arrive at your door, so the "in delivery"
    count and the notifications leave them out.
    """
    if not isinstance(item, dict):
        return False
    if str(item.get("direction") or "").lower() == "return":
        return True
    status = str(item.get("status") or "").lower()
    return any(word in status for word in _RETURN_WORDS)
