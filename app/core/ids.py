"""ID helpers."""
from __future__ import annotations

import uuid


def new_id(prefix: str = "") -> str:
    value = uuid.uuid4().hex
    return f"{prefix}{value}" if prefix else value


def is_hex_id(value: str, length: int = 32) -> bool:
    if not value or len(value) != length:
        return False
    try:
        int(value, 16)
        return True
    except ValueError:
        return False
