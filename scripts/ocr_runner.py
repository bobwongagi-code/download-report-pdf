"""PaddleOCR subprocess invocation, chunking, job state management, and caching."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Optional

from pypdf import PdfReader, PdfWriter

CACHE_ROOT = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))) / "cache" / "url-pdf-download-ocr"
JOB_ROOT = CACHE_ROOT / "jobs"
DIRECT_OCR_PAGE_LIMIT = 100
OCR_CHUNK_RETRY_ATTEMPTS = 3
OCR_CHUNK_RETRY_BASE_DELAY_SECONDS = 1.5
OCR_TIMEOUT_BASE_SECONDS = 60
OCR_TIMEOUT_PER_PAGE_SECONDS = 30
OCR_TIMEOUT_MAX_SECONDS = 1800
PADDLE_SCRIPT_ENV_VAR = "URL_PDF_DOWNLOAD_OCR_PADDLE_SCRIPT"
DEFAULT_PADDLE_SCRIPT = Path.home() / ".agents" / "skills" / "paddleocr-doc-parsing" / "scripts" / "vl_caller.py"


def ocr_timeout_for_pages(page_count: int) -> int:
    """Calculate OCR subprocess timeout: 60s base + 30s per page, capped at 1800s."""
    return min(OCR_TIMEOUT_BASE_SECONDS + OCR_TIMEOUT_PER_PAGE_SECONDS * page_count, OCR_TIMEOUT_MAX_SECONDS)


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
    page_count: int = DIRECT_OCR_PAGE_LIMIT,
) -> subprocess.CompletedProcess[str]:
    timeout_seconds = ocr_timeout_for_pages(page_count)
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
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        raise RuntimeError(
            f"PaddleOCR timed out after {timeout_seconds}s "
            f"({page_count} pages) on {input_pdf_path}"
        )


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
            chunk_page_count = int(chunk["end_page"]) - int(chunk["start_page"]) + 1
            proc = run_paddleocr_subprocess(
                input_pdf_path=chunk_pdf_path,
                paddle_script=paddle_script,
                result_path=chunk_result_path,
                cache_dir=paths["cache_dir"],
                page_count=chunk_page_count,
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

    fd, result_json = tempfile.mkstemp(prefix="url_pdf_ocr_", suffix=".json")
    os.close(fd)
    result_path = Path(result_json)
    timeout_seconds = ocr_timeout_for_pages(total_pages)

    try:
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

        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_seconds)
        if proc.returncode != 0:
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
    except subprocess.TimeoutExpired:
        raise RuntimeError(
            f"PaddleOCR timed out after {timeout_seconds}s "
            f"({total_pages} pages) on {pdf_path}"
        )
    finally:
        result_path.unlink(missing_ok=True)
