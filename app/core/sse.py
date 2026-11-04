"""Server-Sent Events encoding."""
from __future__ import annotations

import json
from typing import Any


def sse_event(event: str, data: Any) -> str:
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"), default=str)
    return f"event: {event}\ndata: {payload}\n\n"


def sse_comment(text: str = "keep-alive") -> str:
    return f": {text}\n\n"
