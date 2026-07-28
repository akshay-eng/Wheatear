"""Small, deterministic log redactor for credentials supplied by the GUI."""

from __future__ import annotations

import re

_LABELED_SECRET = re.compile(
    r"(?i)\b(api[_ -]?key|access[_ -]?token|authorization|cookie|password|secret)"
    r"(\s*[:=]\s*)((?:Bearer\s+)?[^\s,;]+)"
)
_BEARER = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")


class SecretRedactor:
    def __init__(self, values: list[str] | None = None) -> None:
        self._values = sorted(
            {value for value in (values or []) if value and len(value) >= 4},
            key=len,
            reverse=True,
        )

    def __call__(self, value: object) -> str:
        text = str(value)
        for secret in self._values:
            text = text.replace(secret, "[redacted]")
        text = _BEARER.sub("Bearer [redacted]", text)
        return _LABELED_SECRET.sub(r"\1\2[redacted]", text)

    def clear(self) -> None:
        self._values.clear()
