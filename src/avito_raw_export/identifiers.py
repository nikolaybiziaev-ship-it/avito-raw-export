"""Opaque identifiers are validated, URL-encoded and mapped independently to disk."""

import hashlib
import re
from urllib.parse import quote


def valid_chat_id(value: str) -> bool:
    return (
        isinstance(value, str)
        and 0 < len(value) <= 256
        and value.isascii()
        and all(c.isalnum() or c in "_-~" for c in value)
    )


def chat_segment(value: str) -> str:
    if not valid_chat_id(value):
        raise ValueError("Unsafe chat identifier")
    return quote(value, safe="")


def file_id(value: str) -> str:
    # No remote value is ever treated as a path. Keep portable v1 names compatible.
    reserved = {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *[f"COM{i}" for i in range(10)],
        *[f"LPT{i}" for i in range(10)],
    }
    if re.fullmatch(r"[A-Za-z0-9_-]{1,80}", value) and value.upper() not in reserved:
        return value
    return "id-" + hashlib.sha256(value.encode("utf-8")).hexdigest()
