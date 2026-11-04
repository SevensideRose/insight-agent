"""Logging setup with secret redaction."""
from __future__ import annotations

import re
import sys
from pathlib import Path

from loguru import logger

SECRET_PATTERNS = [
    re.compile(r"(sk-[A-Za-z0-9_\-]{8,})"),
    re.compile(r"(m0-[A-Za-z0-9_\-]{8,})"),
    re.compile(r"(?i)(api[_-]?key\s*[=:]\s*)([^\s,;'\"]+)"),
    re.compile(r"(?i)(authorization:\s*bearer\s+)(\S+)"),
]


def redact(text: str) -> str:
    value = str(text)
    for pattern in SECRET_PATTERNS:
        if pattern.groups >= 2:
            value = pattern.sub(lambda m: m.group(1) + "***", value)
        else:
            value = pattern.sub("***", value)
    return value


def _sink(message) -> None:
    sys.stderr.write(redact(str(message)))


def setup_logging(log_dir: Path, level: str = "INFO") -> None:
    logger.remove()
    # Patch the record once so every sink (console and file) is redacted.
    logger.configure(patcher=lambda record: record.update(message=redact(record["message"])))
    logger.add(_sink, level=level.upper(), colorize=False)
    log_dir.mkdir(parents=True, exist_ok=True)
    logger.add(
        log_dir / "app.log",
        level=level.upper(),
        rotation="00:00",
        retention="14 days",
        encoding="utf-8",
        enqueue=True,
        format="{time:YYYY-MM-DD HH:mm:ss} | {level:<8} | {name}:{line} | {message}",
    )
