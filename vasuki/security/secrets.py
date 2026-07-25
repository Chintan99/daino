"""Secret reference resolution and output redaction."""

from __future__ import annotations

import os
import re
from importlib import import_module
from pathlib import Path

from vasuki.exceptions import ConfigurationError

SECRET_PATTERNS = [
    re.compile(r"(?i)(api[_-]?key|token|password|secret)\s*[:=]\s*['\"]?([^\s,'\"]+)"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S),
]


def resolve_secret(reference: str) -> str:
    """Resolve a secret outside any model context or persistent log."""
    if not reference:
        return ""
    if reference.startswith("env://"):
        name = reference.removeprefix("env://")
        value = os.getenv(name)
        if value is None:
            raise ConfigurationError(f"Required environment variable {name} is not set")
        return value
    if reference.startswith("file://"):
        path = Path(reference.removeprefix("file://")).expanduser()
        try:
            return path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise ConfigurationError(f"Cannot read secret file {path}: {exc}") from exc
    if reference.startswith("keyring://"):
        try:
            keyring = import_module("keyring")
        except ImportError as exc:
            raise ConfigurationError("Install vasuki[keyring] to use keyring secrets") from exc
        target = reference.removeprefix("keyring://")
        service, _, username = target.partition("/")
        value = keyring.get_password(service, username)
        if not isinstance(value, str):
            raise ConfigurationError(f"No keyring entry for {service}/{username}")
        return value
    raise ConfigurationError("Only env://, file://, and keyring:// secret references are allowed")


def redact(text: str, additional_values: list[str] | None = None) -> str:
    """Remove likely credentials while preserving actionable diagnostics."""
    result = text
    for pattern in SECRET_PATTERNS:
        if pattern.groups >= 2:
            result = pattern.sub(lambda match: f"{match.group(1)}=[REDACTED]", result)
        else:
            result = pattern.sub("[REDACTED]", result)
    for value in additional_values or []:
        if value and len(value) >= 4:
            result = result.replace(value, "[REDACTED]")
    return result
