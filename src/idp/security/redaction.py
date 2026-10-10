"""Mask common PII/financial identifiers before anything is written to logs.

Document contents never go to CloudWatch in full; this is a second line of defense for error messages
and snippets that might slip through.
"""

from __future__ import annotations

import re

_PATTERNS = [
    (re.compile(r"\b\d{3}-\d{2}-\d{4}\b"), "[SSN]"),
    (re.compile(r"\b(?:\d[ -]?){13,19}\b"), "[CARD]"),
    (re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{11,30}\b"), "[IBAN]"),
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"), "[EMAIL]"),
    (re.compile(r"\+?\d{1,2}[ .-]?\(?\d{3}\)?[ .-]?\d{3}[ .-]?\d{4}\b"), "[PHONE]"),
]


def redact(text: str, max_len: int = 200) -> str:
    for pattern, token in _PATTERNS:
        text = pattern.sub(token, text)
    return text if len(text) <= max_len else text[:max_len] + "..."
