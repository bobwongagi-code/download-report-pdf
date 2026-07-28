"""Stable error classification for OCR and download reporting."""
from __future__ import annotations

from typing import Optional


def classify_failure_reason(
    message: str,
    status: Optional[int] = None,
    error_code: Optional[str] = None,
) -> str:
    lowered = message.lower()
    if error_code == "network_policy":
        return "network_policy"
    if error_code == "invalid_url":
        return "invalid_url"
    if error_code == "ocr_incomplete":
        return "ocr_incomplete"
    if status in {401, 403}:
        return "authentication_or_interactive"
    if status == 404:
        return "no_pdf_found"
    if status in {408, 425, 429, 500, 502, 503, 504}:
        return "network"
    if "timed out" in lowered or "timeout" in lowered:
        return "timeout"
    if (
        "network" in lowered
        or "network_policy" in lowered
        or "curl" in lowered
        or "could not resolve host" in lowered
        or "connection" in lowered
        or "ssl" in lowered
    ):
        return "network"
    if (
        "login" in lowered
        or "extraction code" in lowered
        or "interactive javascript" in lowered
        or "manual button click" in lowered
        or "captcha" in lowered
        or "anti-bot" in lowered
    ):
        return "authentication_or_interactive"
    if "paddleocr script not found" in lowered or "not configured" in lowered:
        return "ocr_configuration"
    if "returned no text" in lowered or "ocr_empty_output" in lowered:
        return "ocr_empty_output"
    if "ocr_incomplete" in lowered or "missing page" in lowered or "page coverage" in lowered:
        return "ocr_incomplete"
    if "pdf validation failed" in lowered or "did not produce a valid pdf" in lowered:
        return "invalid_pdf"
    if "could not resolve a downloadable pdf" in lowered:
        return "no_pdf_found"
    return "unknown"
