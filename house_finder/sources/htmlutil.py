"""Small HTML/JSON extraction helpers shared by adapters.

These deliberately avoid heavy dependencies (no BeautifulSoup) so the app
installs quickly. Adapters that need richer parsing can add their own.
"""

from __future__ import annotations

import html
import json
import re
from typing import Any

_SCRIPT_JSON_RE = re.compile(
    r'<script[^>]*type=["\']application/(?:ld\+)?json["\'][^>]*>(.*?)</script>',
    re.IGNORECASE | re.DOTALL,
)
_NEXT_DATA_RE = re.compile(
    r'<script[^>]*id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>',
    re.IGNORECASE | re.DOTALL,
)
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def strip_tags(text: str) -> str:
    return _WS_RE.sub(" ", html.unescape(_TAG_RE.sub(" ", text or ""))).strip()


def extract_json_ld(html_text: str) -> list[Any]:
    """Return all JSON-LD / application-json blobs found in the page."""
    out: list[Any] = []
    for m in _SCRIPT_JSON_RE.finditer(html_text):
        try:
            out.append(json.loads(m.group(1).strip()))
        except (ValueError, TypeError):
            continue
    return out


def extract_next_data(html_text: str) -> dict | None:
    m = _NEXT_DATA_RE.search(html_text)
    if not m:
        return None
    try:
        return json.loads(m.group(1).strip())
    except (ValueError, TypeError):
        return None


def find_all(obj: Any, key: str) -> list[Any]:
    """Recursively collect every value stored under ``key`` in nested data."""
    found: list[Any] = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == key:
                found.append(v)
            found.extend(find_all(v, key))
    elif isinstance(obj, list):
        for item in obj:
            found.extend(find_all(item, key))
    return found


def first_attr(html_text: str, pattern: str, group: int = 1) -> str:
    m = re.search(pattern, html_text, re.IGNORECASE | re.DOTALL)
    return m.group(group).strip() if m else ""
