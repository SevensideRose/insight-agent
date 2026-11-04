"""Text normalization and small parsing helpers."""
from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path

_WHITESPACE_RE = re.compile(r"[ \t 　]+")
_BLANK_LINES_RE = re.compile(r"\n{3,}")
_CODE_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)

_UNSAFE_FILENAME_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def normalize_whitespace(text: str) -> str:
    if not text:
        return ""
    # NFC preserves Chinese full-width punctuation; NFKC would turn “，” into
    # "," and degrade both citation display and sentence splitting.
    text = unicodedata.normalize("NFC", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _WHITESPACE_RE.sub(" ", text)
    lines = [line.strip() for line in text.split("\n")]
    text = "\n".join(lines)
    return _BLANK_LINES_RE.sub("\n\n", text).strip()


def char_count(text: str) -> int:
    return len(text or "")


def truncate(text: str, limit: int, suffix: str = "…") -> str:
    text = text or ""
    if len(text) <= limit:
        return text
    return text[: max(0, limit - len(suffix))] + suffix


def safe_filename(filename: str) -> str:
    name = Path(filename or "file").name
    name = _UNSAFE_FILENAME_RE.sub("_", name).strip(" .")
    if not name:
        name = "file"
    if len(name) > 180:
        stem = Path(name).stem[:150]
        suffix = Path(name).suffix[:20]
        name = stem + suffix
    return name


def file_type_of(filename: str) -> str:
    suffix = Path(filename or "").suffix.lower().lstrip(".")
    aliases = {"markdown": "md", "htm": "html", "doc": "docx"}
    return aliases.get(suffix, suffix)


def estimate_tokens(text: str) -> int:
    """Rough estimate only; used for budgeting, not billing."""
    text = text or ""
    if not text:
        return 0
    cjk = sum(1 for ch in text if "一" <= ch <= "鿿")
    other = len(text) - cjk
    return max(1, int(cjk * 1.0 + other / 4))


def strip_code_fence(text: str) -> str:
    text = (text or "").strip()
    if text.startswith("```"):
        text = _CODE_FENCE_RE.sub("", text).strip()
    return text


def parse_json_loose(text: str) -> dict:
    """Parse a JSON object from an LLM response, tolerating code fences."""
    cleaned = strip_code_fence(text)
    try:
        value = json.loads(cleaned)
    except json.JSONDecodeError:
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise
        value = json.loads(cleaned[start : end + 1])
    if not isinstance(value, dict):
        raise ValueError("expected a JSON object")
    return value


def format_heading_path(parts: list[str]) -> str:
    return " > ".join(part.strip() for part in parts if part and part.strip())
