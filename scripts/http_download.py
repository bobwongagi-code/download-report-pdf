"""Bounded HTTP fetching with redirect and destination-IP validation."""
from __future__ import annotations

import ipaddress
import os
import random
import re
import socket
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from email.parser import Parser
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin, urlparse

CONNECT_TIMEOUT_SECONDS = 10
REQUEST_TIMEOUT_SECONDS = 45
TOTAL_REQUEST_TIMEOUT_SECONDS = 180
RETRY_ATTEMPTS = 3
RETRY_DELAY_SECONDS = 1
RETRYABLE_STATUS_CODES = {408, 425, 429, 500, 502, 503, 504}
HTML_SNIFF_BYTES = 512 * 1024
MAX_RESPONSE_BYTES = 500 * 1024 * 1024
MAX_HEADER_BYTES = 64 * 1024
MAX_REDIRECTS = 5
MAX_ERROR_TEXT = 4096
DEFAULT_UA = "Mozilla/5.0 (Codex URL OCR)"


class NetworkPolicyError(RuntimeError):
    code = "network_policy"
    retryable = False


class HttpDownloadError(RuntimeError):
    code = "http_error"

    def __init__(self, message: str, *, status: int | None = None, retryable: bool = False):
        super().__init__(message)
        self.status = status
        self.retryable = retryable


@dataclass
class RequestBudget:
    deadline: float = field(
        default_factory=lambda: time.monotonic() + TOTAL_REQUEST_TIMEOUT_SECONDS
    )
    max_bytes: int = MAX_RESPONSE_BYTES
    bytes_read: int = 0

    def remaining_seconds(self) -> float:
        return self.deadline - time.monotonic()

    def consume(self, count: int) -> None:
        self.bytes_read += count
        if self.bytes_read > self.max_bytes:
            raise HttpDownloadError(
                f"Network response byte budget exceeded ({self.max_bytes} bytes)",
                retryable=False,
            )


@dataclass
class ProbeResult:
    url: str
    final_url: str
    headers: dict[str, str]
    body: bytes
    is_pdf: bool
    is_html: bool
    staging_path: Optional[Path] = None
    redirect_count: int = 0
    request_count: int = 1


@dataclass
class _Response:
    status: int
    headers: dict[str, str]
    path: Path
    truncated: bool
    request_url: str


@dataclass(frozen=True)
class NetworkPolicy:
    """Allow public HTTPS destinations and explicitly opted-in public HTTP."""

    allow_http: bool = False
    max_redirects: int = MAX_REDIRECTS

    def validate_url(
        self,
        url: str,
        *,
        parent_url: Optional[str] = None,
        allow_cross_origin: bool = True,
    ) -> str:
        try:
            parsed = urlparse(url)
            parsed.port
        except ValueError as exc:
            raise NetworkPolicyError("URL has an invalid port or host syntax.") from exc
        if parsed.scheme not in {"http", "https"} or (parsed.scheme == "http" and not self.allow_http):
            raise NetworkPolicyError("Only permitted HTTP(S) URLs are supported.")
        if parsed.username is not None or parsed.password is not None:
            raise NetworkPolicyError("URL userinfo is not allowed.")
        if not parsed.hostname:
            raise NetworkPolicyError("URL must contain a hostname.")
        if parent_url and not allow_cross_origin and not same_origin(url, parent_url):
            raise NetworkPolicyError("Cross-origin candidate URLs are not allowed.")
        self._resolve_public_addresses(url)
        return url

    def _resolve_public_addresses(self, url: str) -> list[str]:
        try:
            parsed = urlparse(url)
            parsed.port
        except ValueError as exc:
            raise NetworkPolicyError("URL has an invalid port or host syntax.") from exc
        hostname = parsed.hostname
        if not hostname:
            raise NetworkPolicyError("URL must contain a hostname.")
        try:
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
            addresses = {
                sockaddr[4][0]
                for sockaddr in socket.getaddrinfo(
                    hostname,
                    port,
                    type=socket.SOCK_STREAM,
                )
            }
        except (OSError, socket.gaierror, ValueError) as exc:
            raise NetworkPolicyError(f"Could not resolve host safely: {hostname}") from exc
        if not addresses:
            raise NetworkPolicyError(f"Host resolved to no addresses: {hostname}")
        for address in addresses:
            try:
                parsed_ip = ipaddress.ip_address(address)
            except ValueError as exc:
                raise NetworkPolicyError(f"Host resolved to an invalid IP: {address}") from exc
            if not parsed_ip.is_global:
                raise NetworkPolicyError(
                    f"Refusing non-public destination address for {hostname}: {address}"
                )
        return sorted(addresses)

    def pinned_address(self, url: str) -> str:
        return self._resolve_public_addresses(url)[0]


def same_origin(left: str, right: str) -> bool:
    try:
        left_parsed = urlparse(left)
        right_parsed = urlparse(right)
        left_port = left_parsed.port or (443 if left_parsed.scheme == "https" else 80)
        right_port = right_parsed.port or (443 if right_parsed.scheme == "https" else 80)
    except ValueError:
        return False
    return (
        left_parsed.scheme.lower() == right_parsed.scheme.lower()
        and (left_parsed.hostname or "").lower() == (right_parsed.hostname or "").lower()
        and left_port == right_port
    )


def parse_header_file(header_path: Path) -> dict[str, str]:
    _, headers = parse_response_headers(header_path)
    return headers


def parse_response_headers(header_path: Path) -> tuple[int, dict[str, str]]:
    with header_path.open("rb") as header_file:
        raw_headers = header_file.read(MAX_HEADER_BYTES + 1)
    if len(raw_headers) > MAX_HEADER_BYTES:
        raise HttpDownloadError("HTTP response headers exceeded the size limit", retryable=False)
    header_text = raw_headers.decode("iso-8859-1", errors="replace")
    blocks = [block for block in re.split(r"\r?\n\r?\n", header_text) if block.strip()]
    block = blocks[-1] if blocks else ""
    status_match = re.search(r"^HTTP/\S+\s+(\d{3})", block, re.MULTILINE)
    status = int(status_match.group(1)) if status_match else 0
    parsed = Parser().parsestr(block)
    return status, {key.lower(): value for key, value in parsed.items()}


def _safe_timeout(budget: RequestBudget) -> int:
    remaining = budget.remaining_seconds()
    if remaining <= 0:
        raise HttpDownloadError("Network task deadline exceeded", retryable=False)
    return max(1, min(REQUEST_TIMEOUT_SECONDS, int(remaining)))


def _read_error(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()[-MAX_ERROR_TEXT:]
    except OSError:
        return "curl failed"


def _request_once(
    url: str,
    *,
    policy: NetworkPolicy,
    budget: RequestBudget,
    staging_dir: Path,
) -> _Response:
    policy.validate_url(url)
    pinned_address = policy.pinned_address(url)
    parsed = urlparse(url)
    hostname = parsed.hostname or ""
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    body_tmp = tempfile.NamedTemporaryFile(
        mode="wb", delete=False, dir=staging_dir, prefix=".http-", suffix=".part"
    )
    body_path = Path(body_tmp.name)
    body_tmp.close()
    header_tmp = tempfile.NamedTemporaryFile(
        mode="wb", delete=False, dir=staging_dir, prefix=".headers-", suffix=".part"
    )
    header_path = Path(header_tmp.name)
    header_tmp.close()
    stderr_tmp = tempfile.NamedTemporaryFile(
        mode="wb", delete=False, dir=staging_dir, prefix=".curl-", suffix=".stderr"
    )
    stderr_path = Path(stderr_tmp.name)
    stderr_tmp.close()

    command = [
        "curl",
        "-sS",
        "-A",
        DEFAULT_UA,
        "--connect-timeout",
        str(min(CONNECT_TIMEOUT_SECONDS, _safe_timeout(budget))),
        "--max-time",
        str(_safe_timeout(budget)),
        "--max-redirs",
        "0",
        "--proto",
        "=http,https",
        "--noproxy",
        "*",
        "--resolve",
        f"{hostname}:{port}:{f'[{pinned_address}]' if ':' in pinned_address else pinned_address}",
        "-D",
        str(header_path),
        "-o",
        "-",
        "--",
        url,
    ]
    truncated = False
    process: Optional[subprocess.Popen[bytes]] = None
    try:
        with body_path.open("wb") as output, stderr_path.open("wb") as error_output:
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=error_output,
            )
            assert process.stdout is not None
            signature = bytearray()
            total = 0
            while True:
                chunk = process.stdout.read(64 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                budget.consume(len(chunk))
                if len(signature) < 5:
                    signature.extend(chunk[: 5 - len(signature)])
                is_pdf_body = bytes(signature) == b"%PDF-"
                if not is_pdf_body and total > HTML_SNIFF_BYTES:
                    truncated = True
                    process.terminate()
                    break
                if total > MAX_RESPONSE_BYTES:
                    process.kill()
                    raise HttpDownloadError(
                        f"Response exceeded {MAX_RESPONSE_BYTES} bytes", retryable=False
                    )
                output.write(chunk)
            if truncated:
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            else:
                try:
                    process.wait(timeout=max(1, _safe_timeout(budget) + 5))
                except subprocess.TimeoutExpired as exc:
                    process.kill()
                    process.wait()
                    raise HttpDownloadError("curl timed out", retryable=True) from exc
            return_code = process.returncode
            process.stdout.close()
        status, headers = parse_response_headers(header_path)
        if not status:
            raise HttpDownloadError(
                _read_error(stderr_path) or "curl returned no HTTP response", retryable=True
            )
        if return_code != 0 and not truncated:
            message = _read_error(stderr_path) or f"curl failed (exit {return_code})"
            raise HttpDownloadError(message, retryable=True)
        return _Response(status, headers, body_path, truncated, url)
    except BaseException:
        if process is not None and process.poll() is None:
            try:
                process.kill()
                process.wait()
            except OSError:
                pass
        if process is not None and process.stdout is not None:
            process.stdout.close()
        body_path.unlink(missing_ok=True)
        raise
    finally:
        header_path.unlink(missing_ok=True)
        stderr_path.unlink(missing_ok=True)


def _retry_after(headers: dict[str, str]) -> float:
    value = headers.get("retry-after", "").strip()
    try:
        return min(30.0, max(0.0, float(value)))
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(value)
            now = time.time()
            return min(30.0, max(0.0, retry_at.timestamp() - now))
        except (TypeError, ValueError, OverflowError):
            return 0.0


def _request_with_retries(
    url: str,
    *,
    policy: NetworkPolicy,
    budget: RequestBudget,
    staging_dir: Path,
) -> _Response:
    last_error: Optional[Exception] = None
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        response: Optional[_Response] = None
        retry_headers: dict[str, str] = {}
        try:
            response = _request_once(
                url,
                policy=policy,
                budget=budget,
                staging_dir=staging_dir,
            )
            if response.status not in RETRYABLE_STATUS_CODES:
                return response
            last_error = HttpDownloadError(
                f"HTTP {response.status} for {url}",
                status=response.status,
                retryable=True,
            )
            retry_headers = response.headers
            response.path.unlink(missing_ok=True)
            response = None
        except HttpDownloadError as exc:
            last_error = exc
            if not exc.retryable:
                raise
        if attempt < RETRY_ATTEMPTS:
            delay = _retry_after(retry_headers)
            if not delay:
                delay = min(
                    30.0,
                    RETRY_DELAY_SECONDS * (2 ** (attempt - 1))
                    + random.uniform(0.0, RETRY_DELAY_SECONDS),
                )
            if budget.remaining_seconds() <= delay:
                break
            time.sleep(delay)
    if last_error:
        raise last_error
    raise HttpDownloadError("HTTP request failed", retryable=False)


def fetch_url(
    url: str,
    *,
    policy: Optional[NetworkPolicy] = None,
    parent_url: Optional[str] = None,
    allow_cross_origin: bool = True,
    budget: Optional[RequestBudget] = None,
    staging_dir: Optional[Path] = None,
) -> ProbeResult:
    policy = policy or NetworkPolicy()
    budget = budget or RequestBudget()
    staging_dir = (staging_dir or Path(tempfile.gettempdir())).resolve()
    staging_dir.mkdir(parents=True, exist_ok=True)
    current_url = url
    redirect_count = 0
    request_count = 0
    while True:
        policy.validate_url(
            current_url,
            parent_url=parent_url if redirect_count == 0 else None,
            allow_cross_origin=allow_cross_origin if redirect_count == 0 else True,
        )
        response = _request_with_retries(
            current_url,
            policy=policy,
            budget=budget,
            staging_dir=staging_dir,
        )
        request_count += 1
        if 300 <= response.status < 400:
            location = response.headers.get("location")
            response.path.unlink(missing_ok=True)
            if not location:
                raise HttpDownloadError(
                    f"HTTP {response.status} redirect missing Location", status=response.status
                )
            redirect_count += 1
            if redirect_count > policy.max_redirects:
                raise HttpDownloadError("Maximum redirect count exceeded", retryable=False)
            current_url = urljoin(current_url, location)
            continue
        if response.status < 200 or response.status >= 300:
            response.path.unlink(missing_ok=True)
            raise HttpDownloadError(
                f"HTTP {response.status} for {current_url}",
                status=response.status,
                retryable=False,
            )
        with response.path.open("rb") as body_file:
            body = body_file.read(HTML_SNIFF_BYTES)
        headers = response.headers
        content_type = headers.get("content-type", "").lower()
        is_pdf = looks_like_pdf(headers, current_url, body)
        is_html = (
            "text/html" in content_type
            or "application/json" in content_type
            or body.lstrip().lower().startswith((b"<!doctype html", b"<html", b"{", b"["))
        )
        return ProbeResult(
            url=url,
            final_url=current_url,
            headers=headers,
            body=body,
            is_pdf=is_pdf,
            is_html=is_html,
            staging_path=response.path,
            redirect_count=redirect_count,
            request_count=request_count,
        )


def probe_url(
    url: str,
    *,
    policy: Optional[NetworkPolicy] = None,
    parent_url: Optional[str] = None,
    allow_cross_origin: bool = True,
    budget: Optional[RequestBudget] = None,
    staging_dir: Optional[Path] = None,
) -> ProbeResult:
    return fetch_url(
        url,
        policy=policy,
        parent_url=parent_url,
        allow_cross_origin=allow_cross_origin,
        budget=budget,
        staging_dir=staging_dir,
    )


def curl_head(url: str) -> tuple[dict[str, str], str]:
    """Compatibility wrapper; it performs one bounded GET, never a HEAD probe."""
    result = fetch_url(url)
    try:
        return result.headers, result.final_url
    finally:
        if result.staging_path:
            result.staging_path.unlink(missing_ok=True)


def curl_fetch_range(url: str, byte_range: str = "0-65535") -> tuple[bytes, dict[str, str], str]:
    """Compatibility wrapper with a hard sniff limit; Range is intentionally not used."""
    del byte_range
    result = fetch_url(url)
    try:
        return result.body, result.headers, result.final_url
    finally:
        if result.staging_path:
            result.staging_path.unlink(missing_ok=True)


def looks_like_pdf(headers: dict[str, str], url: str, data: bytes) -> bool:
    if data:
        return data.startswith(b"%PDF-")
    content_type = headers.get("content-type", "").lower()
    return "application/pdf" in content_type or urlparse(url).path.lower().endswith(".pdf")


def has_pdf_file_signature(path: Path) -> bool:
    try:
        with path.open("rb") as fh:
            return fh.read(5) == b"%PDF-"
    except OSError:
        return False


def stream_download_to_path(
    url: str,
    destination: Path,
    *,
    force: bool = False,
) -> tuple[dict[str, str], str]:
    """Compatibility wrapper that publishes a staging file without clobbering by default."""
    result = fetch_url(url, staging_dir=destination.parent)
    if not result.staging_path:
        raise HttpDownloadError("Download did not produce a staging file", retryable=False)
    published = False
    try:
        if force:
            os.replace(result.staging_path, destination)
        else:
            try:
                os.link(result.staging_path, destination)
            except FileExistsError as exc:
                raise HttpDownloadError(
                    f"Output already exists: {destination}",
                    retryable=False,
                ) from exc
            result.staging_path.unlink(missing_ok=True)
        published = True
        return result.headers, result.final_url
    except BaseException:
        if not published:
            result.staging_path.unlink(missing_ok=True)
        raise
