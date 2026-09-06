# Tencent is pleased to support the open source community by making tRPC-Agent-Python available.
#
# Copyright (C) 2026 Tencent. All rights reserved.
#
# tRPC-Agent-Python is licensed under Apache-2.0.
"""Sensitive-data masking utilities for logs, traces, and audit records."""

from __future__ import annotations

import logging
import re

DEFAULT_PATTERNS: dict[str, str] = {
    "api_key": r"(?i)(api[_-]?key|token|secret|password)\s*[:=]\s*[^\s,;]+",
    "phone": r"\b1[3-9]\d{9}\b",
    "id_card": r"\b\d{17}[\dXx]\b",
    "bearer": r"(?i)bearer\s+[a-z0-9._-]+",
}

DEFAULT_REPLACEMENT = "[REDACTED]"


class Masker:
    """Regex-based sensitive-data redactor."""

    def __init__(self, extra_patterns: dict[str, str] | None = None):
        self._rules: dict[str, tuple[re.Pattern, str]] = {}
        for name, pattern in DEFAULT_PATTERNS.items():
            self._rules[name] = (re.compile(pattern), DEFAULT_REPLACEMENT)
        for name, pattern in (extra_patterns or {}).items():
            self._rules[name] = (re.compile(pattern), DEFAULT_REPLACEMENT)

    def register(self, name: str, pattern: str, replacement: str = DEFAULT_REPLACEMENT) -> None:
        """Register or override a named pattern with a custom replacement."""
        self._rules[name] = (re.compile(pattern), replacement)

    def mask(self, text: str) -> str:
        """Return the text with all registered patterns redacted."""
        result = text
        for compiled, replacement in self._rules.values():
            result = compiled.sub(replacement, result)
        return result


class SensitiveDataFilter(logging.Filter):
    """A logging.Filter that redacts sensitive data from log records."""

    def __init__(self, masker: Masker):
        super().__init__()
        self._masker = masker

    def filter(self, record: logging.LogRecord) -> bool:
        formatted = record.getMessage()
        record.msg = self._masker.mask(formatted)
        record.args = ()
        return True
