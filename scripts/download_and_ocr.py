#!/usr/bin/env python3
import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from email.parser import Parser
from urllib.parse import parse_qsl, quote, unquote, urlencode, urlparse, urlunparse

from pypdf import PdfReader, PdfWriter

DEFAULT_PADDLE_SCRIPT = Path(
    "/Users/wangbo5/.agents/skills/paddleocr-doc-parsing/scripts/vl_caller.py"
)
PADDLE_SCRIPT_ENV_VAR = "URL_PDF_DOWNLOAD_OCR_PADDLE_SCRIPT"
DEFAULT_UA = "Mozilla/5.0 (Codex URL PDF OCR)"
CONNECT_TIMEOUT_SECONDS = 10
REQUEST_TIMEOUT_SECONDS = 45
RETRY_ATTEMPTS = 3
RETRY_DELAY_SECONDS = 1
HTML_SNIFF_BYTES = 512 * 1024
PROBE_RANGE = "0-65535"
CACHE_ROOT = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))) / "cache" / "url-pdf-download-ocr"
JOB_ROOT = CACHE_ROOT / "jobs"
DIRECT_OCR_PAGE_LIMIT = 100
OCR_CHUNK_RETRY_ATTEMPTS = 3
OCR_CHUNK_RETRY_BASE_DELAY_SECONDS = 1.5


@dataclass
class ProbeResult:
    url: str
    final_url: str
    headers: dict[str, str]
    body: bytes
    is_pdf: bool
    is_html: bool


def now_ms() -> int:
    return int(time.monotonic() * 1000)


def classify_failure_reason(message: str) -> str:
    lowered = message.lower()
    if "timed out" in lowered or "timeout" in lowered:
        return "timeout"
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
    if "returned no text or markdown content" in lowered:
        return "ocr_empty_output"
    if "did not produce a valid pdf" in lowered:
        return "invalid_pdf"
    if "could not resolve a downloadable pdf" in lowered:
        return "no_pdf_found"
    if (
        "network" in lowered
        or "curl failed" in lowered
        or "could not resolve host" in lowered
        or "connection" in lowered
        or "ssl" in lowered
        or "empty reply from server" in lowered
    ):
        return "network"
    return "unknown"


def resolve_paddle_script(configured_path: Optional[str]) -> Path:
    if configured_path:
        return Path(os.path.expanduser(configured_path))
    env_path = os.environ.get(PADDLE_SCRIPT_ENV_VAR)
    if env_path:
        return Path(os.path.expanduser(env_path))
    return DEFAULT_PADDLE_SCRIPT


def get_pdf_metadata(pdf_path: Path) -> tuple[int, int]:
    reader = PdfReader(str(pdf_path))
    return len(reader.pages), pdf_path.stat().st_size


def calculate_chunk_size(total_pages: int, file_size_bytes: int) -> int:
    if total_pages <= DIRECT_OCR_PAGE_LIMIT:
        return total_pages
    mb_per_page = (file_size_bytes / (1024 * 1024)) / max(total_pages, 1)
    if mb_per_page >= 0.08:
        return 20
    if mb_per_page >= 0.03:
        return 50
    return min(DIRECT_OCR_PAGE_LIMIT, total_pages)


def build_chunk_ranges(total_pages: int, chunk_size: int) -> list[tuple[int, int]]:
    return [
        (start_page, min(start_page + chunk_size - 1, total_pages))
        for start_page in range(1, total_pages + 1, chunk_size)
    ]


def get_job_paths(job_dir: Path) -> dict[str, Path]:
    return {
        "job_dir": job_dir,
        "status_path": job_dir / "status.json",
        "chunks_dir": job_dir / "chunks",
        "results_dir": job_dir / "results",
        "markdown_dir": job_dir / "markdown",
        "cache_dir": job_dir / "paddle_cache",
    }


def save_job_state(job_dir: Path, state: dict[str, object]) -> None:
    paths = get_job_paths(job_dir)
    for path in paths.values():
        if path.suffix:
            continue
        path.mkdir(parents=True, exist_ok=True)
    paths["status_path"].write_text(json.dumps(state, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")


def initialize_or_load_job_state(
    pdf_path: Path,
    pdf_hash: str,
    total_pages: int,
    file_size_bytes: int,
    chunk_size: int,
    job_dir: Optional[Path] = None,
) -> dict[str, object]:
    resolved_job_dir = job_dir or (JOB_ROOT / pdf_hash)
    paths = get_job_paths(resolved_job_dir)
    status_path = paths["status_path"]
    if status_path.exists():
        state = json.loads(status_path.read_text(encoding="utf-8"))
        if state.get("pdf_hash") == pdf_hash:
            return state

    chunks = []
    for index, (start_page, end_page) in enumerate(build_chunk_ranges(total_pages, chunk_size), start=1):
        chunks.append(
            {
                "index": index,
                "start_page": start_page,
                "end_page": end_page,
                "status": "pending",
                "attempts": 0,
                "error": None,
                "pdf_path": str(paths["chunks_dir"] / f"chunk_{index:03d}_{start_page:04d}_{end_page:04d}.pdf"),
                "result_path": str(paths["results_dir"] / f"chunk_{index:03d}.json"),
                "markdown_path": str(paths["markdown_dir"] / f"chunk_{index:03d}.md"),
            }
        )

    state: dict[str, object] = {
        "version": 1,
        "pdf_hash": pdf_hash,
        "source_pdf_path": str(pdf_path),
        "total_pages": total_pages,
        "file_size_bytes": file_size_bytes,
        "chunk_size": chunk_size,
        "status": "pending",
        "chunks": chunks,
    }
    save_job_state(resolved_job_dir, state)
    return state


def get_pending_chunks(state: dict[str, object]) -> list[dict[str, object]]:
    pending = []
    for chunk in state.get("chunks", []):
        chunk_markdown_path = Path(str(chunk["markdown_path"])) if chunk.get("markdown_path") else None
        if chunk.get("status") != "completed" or (chunk_markdown_path and not chunk_markdown_path.exists()):
            pending.append(chunk)
    return pending


def split_pdf_chunk(source_pdf: Path, destination_pdf: Path, start_page: int, end_page: int) -> None:
    destination_pdf.parent.mkdir(parents=True, exist_ok=True)
    reader = PdfReader(str(source_pdf))
    writer = PdfWriter()
    for page_index in range(start_page - 1, end_page):
        writer.add_page(reader.pages[page_index])
    with destination_pdf.open("wb") as fh:
        writer.write(fh)


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while True:
            chunk = fh.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def get_cache_paths(pdf_hash: str) -> tuple[Path, Path]:
    CACHE_ROOT.mkdir(parents=True, exist_ok=True)
    return CACHE_ROOT / f"{pdf_hash}.md", CACHE_ROOT / f"{pdf_hash}.json"


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
            last_error = proc.stderr.strip() or proc.stdout.strip() or "curl failed"

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
            url,
        ]
        proc = run_curl(cmd, timeout_seconds=REQUEST_TIMEOUT_SECONDS)
        headers = parse_header_file(header_path)
        final_url = proc.stdout.strip()
        return headers, final_url
    finally:
        header_path.unlink(missing_ok=True)


def looks_like_pdf(headers: dict[str, str], url: str, data: bytes) -> bool:
    content_type = headers.get("content-type", "").lower()
    if "application/pdf" in content_type:
        return True
    if urlparse(url).path.lower().endswith(".pdf"):
        return True
    return data.startswith(b"%PDF-")


def extract_filename(headers: dict[str, str], url: str) -> str:
    disposition = headers.get("content-disposition", "")
    match = re.search(r"filename\\*=UTF-8''([^;]+)", disposition, re.I)
    if match:
        return sanitize_filename(unquote(match.group(1)))
    match = re.search(r'filename="?([^";]+)"?', disposition, re.I)
    if match:
        return sanitize_filename(match.group(1))

    path_name = Path(unquote(urlparse(url).path)).name
    if path_name:
        return sanitize_filename(path_name)
    return "download.pdf"


def sanitize_filename(name: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip(".-")
    if not cleaned:
        cleaned = "download.pdf"
    if not cleaned.lower().endswith(".pdf"):
        cleaned += ".pdf"
    return cleaned


def ensure_unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    stem = path.stem
    suffix = path.suffix
    counter = 2
    while True:
        candidate = path.with_name(f"{stem}-{counter}{suffix}")
        if not candidate.exists():
            return candidate
        counter += 1


def extract_hubspot_second_hop(html: str) -> Optional[str]:
    anchor_match = re.search(r'<a href="(https://[^"]+/events/public/v1/encoded/track/[^"]+)"', html)
    if anchor_match:
        return anchor_match.group(1)

    var_match = re.search(r'var targetURL = "(https://[^"]+_jss=-2)";', html)
    if var_match:
        return var_match.group(1).replace("_jss=-2", "_jss=0")
    return None


def is_hubspot_tracking_url(url: str) -> bool:
    host = urlparse(url).netloc.lower()
    return "hubspotlinks.com" in host or "/events/public/v1/encoded/track/" in url


def detect_provider_label(url: str) -> Optional[str]:
    host = urlparse(url).netloc.lower()
    if "hubspotlinks.com" in host or "hubspotemail.net" in host:
        return "HubSpot"
    if "drive.google.com" == host:
        return "Google Drive"
    if "dropbox.com" in host:
        return "Dropbox"
    if "sharepoint.com" in host or "onedrive.live.com" in host or host == "1drv.ms":
        return "SharePoint/OneDrive"
    if "pan.baidu.com" in host or "yun.baidu.com" in host:
        return "Baidu Netdisk"
    if "aliyundrive.com" in host or "alipan.com" in host:
        return "Aliyun Drive"
    if "123pan.com" in host or "123684.com" in host:
        return "123Pan"
    if "lanzou" in host or "lanzn.com" in host:
        return "Lanzou"
    if "pan.quark.cn" in host:
        return "Quark Drive"
    if "weiyun.com" in host:
        return "Weiyun"
    if "feishu.cn" in host:
        return "Feishu"
    return None


def replace_query(url: str, updates: dict[str, str]) -> str:
    parsed = urlparse(url)
    pairs = dict(parse_qsl(parsed.query, keep_blank_values=True))
    pairs.update(updates)
    return urlunparse(parsed._replace(query=urlencode(pairs)))


def known_provider_candidates(url: str) -> list[str]:
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    candidates: list[str] = []
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))

    if host == "drive.google.com":
        match = re.search(r"/file/d/([^/]+)", parsed.path)
        if match:
            file_id = match.group(1)
            candidates.append(f"https://drive.google.com/uc?export=download&id={quote(file_id)}")
        file_id = query.get("id")
        if file_id:
            candidates.append(f"https://drive.google.com/uc?export=download&id={quote(file_id)}")

    if "dropbox.com" in host:
        candidates.append(replace_query(url, {"dl": "1"}))
        candidates.append(replace_query(url, {"raw": "1"}))

    if "sharepoint.com" in host or "onedrive.live.com" in host or host == "1drv.ms":
        candidates.append(replace_query(url, {"download": "1"}))

    if "aliyundrive.com" in host or "alipan.com" in host:
        candidates.append(replace_query(url, {"download": "1"}))

    if "123pan.com" in host or "123684.com" in host:
        candidates.append(replace_query(url, {"download": "1"}))

    if "pan.quark.cn" in host:
        candidates.append(replace_query(url, {"download": "1"}))

    for key in [
        "url",
        "target",
        "download",
        "download_url",
        "downloadurl",
        "redirect",
        "redirect_url",
        "jump",
        "dest",
        "destination",
        "src",
        "file",
        "dlink",
    ]:
        value = query.get(key)
        if value and value.startswith(("http://", "https://")):
            candidates.append(value)

    deduped: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        if candidate not in seen:
            seen.add(candidate)
            deduped.append(candidate)
    return deduped


def extract_pdf_candidates(html: str, base_url: str) -> list[str]:
    candidates: list[str] = []

    for match in re.finditer(r'https://[^"\']+?\.pdf(?:\?[^"\']*)?', html, re.I):
        candidates.append(match.group(0))

    for match in re.finditer(r'https:\\/\\/[^"\']+?\.pdf(?:\\\/?[^"\']*)?', html, re.I):
        candidates.append(match.group(0).replace("\\/", "/"))

    for match in re.finditer(r'href="([^"]+?\.pdf(?:\?[^"]*)?)"', html, re.I):
        candidate = match.group(1)
        if candidate.startswith("http://") or candidate.startswith("https://"):
            candidates.append(candidate)
        elif candidate.startswith("/"):
            parsed = urlparse(base_url)
            candidates.append(urlunparse((parsed.scheme, parsed.netloc, candidate, "", "", "")))

    for match in re.finditer(r'(?:window\.location(?:\.replace)?|document\.location)\s*=\s*"([^"]+?\.pdf(?:\?[^"]*)?)"', html, re.I):
        candidate = match.group(1)
        if candidate.startswith("http://") or candidate.startswith("https://"):
            candidates.append(candidate)
        elif candidate.startswith("/"):
            parsed = urlparse(base_url)
            candidates.append(urlunparse((parsed.scheme, parsed.netloc, candidate, "", "", "")))

    for match in re.finditer(
        r'<meta[^>]+http-equiv="refresh"[^>]+content="[^"]*url=([^"]+)"',
        html,
        re.I,
    ):
        candidate = match.group(1).strip()
        if candidate.startswith("http://") or candidate.startswith("https://"):
            candidates.append(candidate)
        elif candidate.startswith("/"):
            parsed = urlparse(base_url)
            candidates.append(urlunparse((parsed.scheme, parsed.netloc, candidate, "", "", "")))

    for match in re.finditer(r'(?:href|src)="([^"]+)"', html, re.I):
        candidate = match.group(1)
        if ".pdf" not in candidate.lower():
            continue
        if candidate.startswith("http://") or candidate.startswith("https://"):
            candidates.append(candidate)
        elif candidate.startswith("/"):
            parsed = urlparse(base_url)
            candidates.append(urlunparse((parsed.scheme, parsed.netloc, candidate, "", "", "")))

    for key in [
        "url",
        "target",
        "download",
        "download_url",
        "downloadurl",
        "redirect",
        "redirect_url",
        "jump",
        "dest",
        "destination",
        "src",
        "file",
        "dlink",
    ]:
        pattern = rf'"{key}"\s*:\s*"([^"]+)"'
        for match in re.finditer(pattern, html, re.I):
            candidate = match.group(1).replace("\\/", "/")
            if ".pdf" in candidate.lower() or candidate.startswith(("http://", "https://")):
                candidates.append(candidate)

    deduped: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        if candidate not in seen:
            seen.add(candidate)
            deduped.append(candidate)
    return deduped


def normalize_url(url: str) -> str:
    return url.strip()


def download_pdf(source_url: str, output_dir: Path) -> tuple[Path, str, dict[str, object]]:
    metrics: dict[str, object] = {
        "provider": detect_provider_label(source_url) or "unknown",
        "candidate_count": 0,
        "candidate_probe_count": 0,
        "resolved_via": "unknown",
    }
    download_started_ms = now_ms()
    source_url = normalize_url(source_url)
    provider_label = detect_provider_label(source_url)
    probe_started_ms = now_ms()
    probe = probe_url(source_url)
    metrics["initial_probe_ms"] = now_ms() - probe_started_ms
    metrics["initial_final_url"] = probe.final_url

    if probe.is_pdf:
        pdf_url = probe.final_url
        filename = extract_filename(probe.headers, probe.final_url)
        metrics["resolved_via"] = "direct"
    else:
        html = probe.body.decode("utf-8", errors="replace")
        candidate_urls: list[str] = []
        candidate_urls.extend(known_provider_candidates(source_url))
        candidate_urls.extend(known_provider_candidates(probe.final_url))
        if is_hubspot_tracking_url(source_url):
            second_hop = extract_hubspot_second_hop(html)
            if second_hop:
                candidate_urls.append(second_hop)
        candidate_urls.extend(extract_pdf_candidates(html, probe.final_url))
        candidate_urls = list(dict.fromkeys(candidate_urls))
        metrics["candidate_count"] = len(candidate_urls)

        resolved = False
        pdf_url = ""
        filename = ""
        candidate_probe_started_ms = now_ms()
        for candidate in candidate_urls:
            metrics["candidate_probe_count"] = int(metrics["candidate_probe_count"]) + 1
            try:
                candidate_probe = probe_url(candidate)
            except Exception:
                continue
            if not candidate_probe.is_pdf:
                continue
            pdf_url = candidate_probe.final_url
            filename = extract_filename(candidate_probe.headers, candidate_probe.final_url)
            resolved = True
            metrics["resolved_via"] = "candidate"
            break
        metrics["candidate_probe_ms"] = now_ms() - candidate_probe_started_ms

        if not resolved:
            provider_text = f" Detected provider: {provider_label}." if provider_label else ""
            raise RuntimeError(
                "Could not resolve a downloadable PDF from the provided URL."
                + provider_text
                + " The page may require login, an extraction code, or interactive JavaScript."
            )

    output_dir.mkdir(parents=True, exist_ok=True)
    pdf_path = ensure_unique_path(output_dir / filename)
    final_download_started_ms = now_ms()
    download_headers, final_pdf_url = stream_download_to_path(pdf_url, pdf_path)
    if not looks_like_pdf(download_headers, final_pdf_url, b""):
        head, head_final_url = curl_head(final_pdf_url)
        if not looks_like_pdf(head, head_final_url, b""):
            pdf_path.unlink(missing_ok=True)
            raise RuntimeError("Resolved URL did not produce a valid PDF during final download.")
        final_pdf_url = head_final_url
    metrics["final_download_ms"] = now_ms() - final_download_started_ms
    metrics["download_total_ms"] = now_ms() - download_started_ms
    return pdf_path, final_pdf_url, metrics


def build_markdown_from_result(result_path: Path, markdown_path: Path) -> None:
    payload = json.loads(result_path.read_text(encoding="utf-8"))
    text = payload.get("text")
    if text:
        markdown_path.write_text(text.rstrip() + "\n", encoding="utf-8")
        return

    result = payload.get("result")
    chunks: list[str] = []
    if isinstance(result, list):
        for item in result:
            if isinstance(item, dict) and item.get("markdown"):
                chunks.append(str(item["markdown"]))
    elif isinstance(result, dict) and result.get("markdown"):
        chunks.append(str(result["markdown"]))

    if not chunks:
        raise RuntimeError("PaddleOCR returned no text or markdown content.")
    markdown_path.write_text("\n\n".join(chunks).rstrip() + "\n", encoding="utf-8")


def run_paddleocr_subprocess(
    input_pdf_path: Path,
    paddle_script: Path,
    result_path: Path,
    cache_dir: Path,
) -> subprocess.CompletedProcess[str]:
    cmd = [
        sys.executable,
        str(paddle_script),
        "--file-path",
        str(input_pdf_path),
        "--file-type",
        "0",
        "--output",
        str(result_path),
        "--cache-dir",
        str(cache_dir),
        "--pretty",
    ]
    return subprocess.run(cmd, capture_output=True, text=True)


def should_retry_chunk(error_message: str) -> bool:
    return classify_failure_reason(error_message) in {"timeout", "network", "unknown"}


def concatenate_markdown_files(markdown_paths: list[Path], destination: Path) -> None:
    parts = []
    for markdown_path in markdown_paths:
        parts.append(markdown_path.read_text(encoding="utf-8").rstrip())
    destination.write_text("\n\n".join(part for part in parts if part).rstrip() + "\n", encoding="utf-8")


def summarize_failed_chunks(chunks: list[dict[str, object]]) -> str:
    details = ", ".join(
        f"{chunk['index']}({chunk['start_page']}-{chunk['end_page']}: {chunk.get('error')})"
        for chunk in chunks
    )
    return f"Chunk OCR failed for {len(chunks)} chunk(s): {details}. Re-run will resume from unfinished chunks."


def run_paddleocr_chunked(
    pdf_path: Path,
    paddle_script: Path,
    markdown_path: Path,
    pdf_hash: str,
    total_pages: int,
    file_size_bytes: int,
) -> tuple[Path, Optional[str], bool, str, dict[str, object]]:
    chunk_size = calculate_chunk_size(total_pages, file_size_bytes)
    job_dir = JOB_ROOT / pdf_hash
    state = initialize_or_load_job_state(
        pdf_path=pdf_path,
        pdf_hash=pdf_hash,
        total_pages=total_pages,
        file_size_bytes=file_size_bytes,
        chunk_size=chunk_size,
        job_dir=job_dir,
    )
    paths = get_job_paths(job_dir)
    paths["cache_dir"].mkdir(parents=True, exist_ok=True)
    cached_md_path, cached_json_path = get_cache_paths(pdf_hash)

    if cached_md_path.exists() and cached_md_path.stat().st_size > 0:
        shutil.copy2(cached_md_path, markdown_path)
        return markdown_path, None, True, pdf_hash, {
            "ocr_strategy": "chunked_resume",
            "ocr_total_pages": total_pages,
            "ocr_chunk_size": chunk_size,
            "ocr_chunk_count": len(state["chunks"]),
            "ocr_job_dir": str(job_dir),
        }

    for chunk in get_pending_chunks(state):
        chunk_pdf_path = Path(str(chunk["pdf_path"]))
        chunk_result_path = Path(str(chunk["result_path"]))
        chunk_markdown_path = Path(str(chunk["markdown_path"]))
        if chunk.get("status") == "completed" and chunk_markdown_path.exists():
            continue
        if not chunk_pdf_path.exists():
            split_pdf_chunk(pdf_path, chunk_pdf_path, int(chunk["start_page"]), int(chunk["end_page"]))

        last_error = None
        for attempt in range(OCR_CHUNK_RETRY_ATTEMPTS):
            chunk["attempts"] = int(chunk.get("attempts", 0)) + 1
            save_job_state(job_dir, state)
            proc = run_paddleocr_subprocess(
                input_pdf_path=chunk_pdf_path,
                paddle_script=paddle_script,
                result_path=chunk_result_path,
                cache_dir=paths["cache_dir"],
            )
            if proc.returncode == 0 and chunk_result_path.exists():
                build_markdown_from_result(chunk_result_path, chunk_markdown_path)
                chunk["status"] = "completed"
                chunk["error"] = None
                save_job_state(job_dir, state)
                last_error = None
                break

            last_error = (proc.stderr or proc.stdout or "Unknown PaddleOCR error").strip()
            chunk["status"] = "failed"
            chunk["error"] = last_error
            save_job_state(job_dir, state)
            if attempt < OCR_CHUNK_RETRY_ATTEMPTS - 1 and should_retry_chunk(last_error):
                time.sleep(OCR_CHUNK_RETRY_BASE_DELAY_SECONDS * (2 ** attempt))
                continue
            break

        if last_error is not None:
            continue

    failed_chunks = [chunk for chunk in state["chunks"] if chunk.get("status") != "completed"]
    if failed_chunks:
        state["status"] = "failed"
        save_job_state(job_dir, state)
        raise RuntimeError(summarize_failed_chunks(failed_chunks))

    ordered_markdown_paths = [Path(str(chunk["markdown_path"])) for chunk in state["chunks"]]
    concatenate_markdown_files(ordered_markdown_paths, markdown_path)
    shutil.copy2(markdown_path, cached_md_path)
    if paths["status_path"].exists():
        shutil.copy2(paths["status_path"], cached_json_path)
    state["status"] = "completed"
    save_job_state(job_dir, state)
    return markdown_path, f"Completed {len(state['chunks'])} chunk(s) via resumable OCR job.", False, pdf_hash, {
        "ocr_strategy": "chunked_resume",
        "ocr_total_pages": total_pages,
        "ocr_chunk_size": chunk_size,
        "ocr_chunk_count": len(state["chunks"]),
        "ocr_job_dir": str(job_dir),
    }


def run_paddleocr(pdf_path: Path, configured_paddle_script: Optional[str]) -> tuple[Path, Optional[str], bool, str, dict[str, object]]:
    paddle_script = resolve_paddle_script(configured_paddle_script)
    if not paddle_script.exists():
        raise RuntimeError(f"PaddleOCR script not found: {paddle_script}")

    markdown_path = pdf_path.with_suffix(".md")
    pdf_hash = file_sha256(pdf_path)
    cached_md_path, cached_json_path = get_cache_paths(pdf_hash)
    total_pages, file_size_bytes = get_pdf_metadata(pdf_path)

    if cached_md_path.exists() and cached_md_path.stat().st_size > 0:
        shutil.copy2(cached_md_path, markdown_path)
        return markdown_path, None, True, pdf_hash, {
            "ocr_strategy": "full_cache",
            "ocr_total_pages": total_pages,
        }

    if total_pages > DIRECT_OCR_PAGE_LIMIT:
        return run_paddleocr_chunked(
            pdf_path=pdf_path,
            paddle_script=paddle_script,
            markdown_path=markdown_path,
            pdf_hash=pdf_hash,
            total_pages=total_pages,
            file_size_bytes=file_size_bytes,
        )

    fd, result_json = tempfile.mkstemp(prefix="hubspot_pdf_ocr_", suffix=".json")
    os.close(fd)
    result_path = Path(result_json)

    cmd = [
        sys.executable,
        str(paddle_script),
        "--file-path",
        str(pdf_path),
        "--file-type",
        "0",
        "--output",
        str(result_path),
        "--pretty",
        ]

    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        try:
            result_path.unlink(missing_ok=True)
        except OSError:
            pass
        error = (proc.stderr or proc.stdout or "Unknown PaddleOCR error").strip()
        raise RuntimeError(error)

    build_markdown_from_result(result_path, markdown_path)
    shutil.copy2(markdown_path, cached_md_path)
    try:
        shutil.copy2(result_path, cached_json_path)
    except OSError:
        pass
    return markdown_path, proc.stderr.strip() or None, False, pdf_hash, {
        "ocr_strategy": "single_pass",
        "ocr_total_pages": total_pages,
    }


def main() -> int:
    total_started_ms = now_ms()
    parser = argparse.ArgumentParser(
        description="Download a PDF from a URL and create a Markdown OCR copy."
    )
    parser.add_argument("url", help="A URL that resolves to or exposes a PDF download")
    parser.add_argument(
        "--output-dir",
        default=str(Path.home() / "Downloads"),
        help="Directory to store the PDF and Markdown files (default: ~/Downloads)",
    )
    parser.add_argument(
        "--paddle-script",
        default=None,
        help=(
            "Path to the PaddleOCR document parsing script. "
            f"Defaults to ${PADDLE_SCRIPT_ENV_VAR} or {DEFAULT_PADDLE_SCRIPT}"
        ),
    )
    args = parser.parse_args()

    output_dir = Path(os.path.expanduser(args.output_dir)).resolve()
    metrics: dict[str, object] = {
        "source_url": args.url,
        "output_dir": str(output_dir),
        "timeouts": {
            "connect_timeout_seconds": CONNECT_TIMEOUT_SECONDS,
            "request_timeout_seconds": REQUEST_TIMEOUT_SECONDS,
        },
        "retry_policy": {
            "attempts": RETRY_ATTEMPTS,
            "delay_seconds": RETRY_DELAY_SECONDS,
        },
    }

    try:
        pdf_path, resolved_url, download_metrics = download_pdf(args.url, output_dir)
        metrics.update(download_metrics)
    except Exception as exc:
        print(json.dumps(
            {
                "ok": False,
                "pdf_path": None,
                "resolved_pdf_url": None,
                "download_error": str(exc),
                "metrics": {
                    **metrics,
                    "total_ms": now_ms() - total_started_ms,
                    "failure_stage": "download",
                    "failure_reason": classify_failure_reason(str(exc)),
                },
            },
            ensure_ascii=True,
            indent=2,
        ))
        return 1

    try:
        ocr_started_ms = now_ms()
        md_path, paddle_note, cache_hit, pdf_hash, ocr_meta = run_paddleocr(pdf_path, args.paddle_script)
        metrics["ocr_ms"] = now_ms() - ocr_started_ms
        metrics["ocr_cache_hit"] = cache_hit
        metrics["pdf_sha256"] = pdf_hash
        metrics.update(ocr_meta)
    except Exception as exc:
        print(json.dumps(
            {
                "ok": False,
                "pdf_path": str(pdf_path),
                "resolved_pdf_url": resolved_url,
                "ocr_error": str(exc),
                "metrics": {
                    **metrics,
                    "total_ms": now_ms() - total_started_ms,
                    "failure_stage": "ocr",
                    "failure_reason": classify_failure_reason(str(exc)),
                },
            },
            ensure_ascii=True,
            indent=2,
        ))
        return 2

    print(json.dumps(
        {
            "ok": True,
            "pdf_path": str(pdf_path),
            "md_path": str(md_path),
            "resolved_pdf_url": resolved_url,
            "paddle_note": paddle_note,
            "metrics": {
                **metrics,
                "total_ms": now_ms() - total_started_ms,
                "failure_stage": None,
            },
        },
        ensure_ascii=True,
        indent=2,
    ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
