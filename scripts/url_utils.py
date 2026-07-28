"""Generic URL validation and redaction helpers."""
from __future__ import annotations

import hashlib
import re
from urllib.parse import urlparse


SUPPORTED_SCHEMES = {"http", "https"}


class UrlValidationError(ValueError):
    code = "invalid_url"


def normalize_url(url: str) -> str:
    if not isinstance(url, str):
        raise ValueError("URL must be a string.")
    value = url.strip()
    try:
        parsed = urlparse(value)
        parsed.port
    except ValueError as exc:
        raise UrlValidationError("URL has invalid host or port syntax.") from exc
    if parsed.scheme not in SUPPORTED_SCHEMES:
        raise UrlValidationError("Only http and https URLs are supported.")
    if parsed.username is not None or parsed.password is not None or not parsed.hostname:
        raise UrlValidationError("URL must contain a hostname and no userinfo.")
    return value


def redact_url(url: str) -> str:
    """Keep the origin plus stable path/query digests without persisting URL tokens."""
    try:
        parsed = urlparse(url)
        host = parsed.hostname or ""
        port = f":{parsed.port}" if parsed.port else ""
        path = parsed.path or "/"
        path_digest = hashlib.sha256(path.encode("utf-8", errors="replace")).hexdigest()[:12]
        base = f"{parsed.scheme}://{host}{port}/<redacted-path:{path_digest}>"
        if parsed.query:
            query_digest = hashlib.sha256(url.encode("utf-8", errors="replace")).hexdigest()[:12]
            return f"{base}?redacted={query_digest}"
        return base
    except (TypeError, ValueError):
        digest = hashlib.sha256(str(url).encode("utf-8", errors="replace")).hexdigest()[:12]
        return f"<invalid-url:{digest}>"


def redact_text(text: str) -> str:
    return re.sub(
        r"\b[a-zA-Z][a-zA-Z0-9+.-]*://[^\s\"']+",
        lambda match: redact_url(match.group(0)),
        text or "",
    )
