"""Low-level HTTP operations: curl wrappers, probing, and streaming downloads."""
from __future__ import annotations

import subprocess
import tempfile
import time
from dataclasses import dataclass
from email.parser import Parser
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

CONNECT_TIMEOUT_SECONDS = 10
REQUEST_TIMEOUT_SECONDS = 45
RETRY_ATTEMPTS = 3
RETRY_DELAY_SECONDS = 1
HTML_SNIFF_BYTES = 512 * 1024
PROBE_RANGE = "0-65535"
DEFAULT_UA = "Mozilla/5.0 (Codex URL PDF OCR)"


@dataclass
class ProbeResult:
    url: str
    final_url: str
    headers: dict[str, str]
    body: bytes
    is_pdf: bool
    is_html: bool


def parse_header_file(header_path: Path) -> dict[str, str]:
    header_text = header_path.read_text(encoding="utf-8", errors="replace")
    last_header_block = [block for block in header_text.split("\r\n\r\n") if block.strip()]
    parsed = Parser().parsestr(last_header_block[-1] if last_header_block else "")
    return {k.lower(): v for k, v in parsed.items()}


def run_curl(args: list[str], *, timeout_seconds: int = REQUEST_TIMEOUT_SECONDS) -> subprocess.CompletedProcess[str]:
    last_error: Optional[str] = None
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        try:
            proc = subprocess.run(
                args,
                capture_output=True,
                text=True,
                timeout=timeout_seconds + 5,
            )
        except subprocess.TimeoutExpired:
            last_error = f"curl timed out after {timeout_seconds}s"
            proc = None
        else:
            if proc.returncode == 0:
                return proc
            last_error = proc.stderr.strip() or proc.stdout.strip() or f"curl failed (exit {proc.returncode})"

        if attempt < RETRY_ATTEMPTS:
            time.sleep(RETRY_DELAY_SECONDS)

    raise RuntimeError(last_error or "curl failed")


def curl_head(url: str) -> tuple[dict[str, str], str]:
    with tempfile.NamedTemporaryFile(delete=False) as header_tmp:
        header_path = Path(header_tmp.name)
    try:
        cmd = [
            "curl",
            "-fsSIL",
            "-A",
            DEFAULT_UA,
            "--connect-timeout",
            str(CONNECT_TIMEOUT_SECONDS),
            "--max-time",
            str(REQUEST_TIMEOUT_SECONDS),
            "-D",
            str(header_path),
            "-o",
            "/dev/null",
            "-w",
            "%{url_effective}",
            "--",
            url,
        ]
        proc = run_curl(cmd)
        headers = parse_header_file(header_path)
        final_url = proc.stdout.strip()
        return headers, final_url
    finally:
        header_path.unlink(missing_ok=True)


def curl_fetch_range(url: str, byte_range: str = PROBE_RANGE) -> tuple[bytes, dict[str, str], str]:
    with tempfile.NamedTemporaryFile(delete=False) as body_tmp, tempfile.NamedTemporaryFile(
        delete=False
    ) as header_tmp:
        body_path = Path(body_tmp.name)
        header_path = Path(header_tmp.name)
    try:
        cmd = [
            "curl",
            "-fsSL",
            "-A",
            DEFAULT_UA,
            "--connect-timeout",
            str(CONNECT_TIMEOUT_SECONDS),
            "--max-time",
            str(REQUEST_TIMEOUT_SECONDS),
            "-r",
            byte_range,
            "-D",
            str(header_path),
            "-o",
            str(body_path),
            "-w",
            "%{url_effective}",
            "--",
            url,
        ]
        proc = run_curl(cmd)
        headers = parse_header_file(header_path)
        final_url = proc.stdout.strip()
        data = body_path.read_bytes()
        return data, headers, final_url
    finally:
        body_path.unlink(missing_ok=True)
        header_path.unlink(missing_ok=True)


def looks_like_pdf(headers: dict[str, str], url: str, data: bytes) -> bool:
    content_type = headers.get("content-type", "").lower()
    if "application/pdf" in content_type:
        return True
    if urlparse(url).path.lower().endswith(".pdf"):
        return True
    return data.startswith(b"%PDF-")


def has_pdf_file_signature(path: Path) -> bool:
    try:
        with path.open("rb") as fh:
            return fh.read(5) == b"%PDF-"
    except OSError:
        return False


def probe_url(url: str) -> ProbeResult:
    headers: dict[str, str] = {}
    final_url = url
    try:
        headers, final_url = curl_head(url)
    except Exception:
        pass

    if looks_like_pdf(headers, final_url, b""):
        return ProbeResult(
            url=url,
            final_url=final_url,
            headers=headers,
            body=b"",
            is_pdf=True,
            is_html=False,
        )

    body, range_headers, range_final_url = curl_fetch_range(url)
    merged_headers = dict(headers)
    merged_headers.update(range_headers)
    content_type = merged_headers.get("content-type", "").lower()
    is_pdf = looks_like_pdf(merged_headers, range_final_url, body)
    is_html = ("text/html" in content_type) or body.lstrip().startswith((b"<!DOCTYPE html", b"<html", b"<HTML"))
    return ProbeResult(
        url=url,
        final_url=range_final_url,
        headers=merged_headers,
        body=body[:HTML_SNIFF_BYTES],
        is_pdf=is_pdf,
        is_html=is_html,
    )


def stream_download_to_path(url: str, destination: Path) -> tuple[dict[str, str], str]:
    with tempfile.NamedTemporaryFile(delete=False) as header_tmp:
        header_path = Path(header_tmp.name)
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        cmd = [
            "curl",
            "-fsSL",
            "-A",
            DEFAULT_UA,
            "--connect-timeout",
            str(CONNECT_TIMEOUT_SECONDS),
            "--max-time",
            str(REQUEST_TIMEOUT_SECONDS),
            "-o",
            str(destination),
            "-D",
            str(header_path),
            "-w",
            "%{url_effective}",
            "--max-filesize",
            "524288000",
            "--",
            url,
        ]
        proc = run_curl(cmd, timeout_seconds=REQUEST_TIMEOUT_SECONDS)
        headers = parse_header_file(header_path)
        final_url = proc.stdout.strip()
        return headers, final_url
    finally:
        header_path.unlink(missing_ok=True)
