"""Resumable OCR job state, PDF chunking, and execution workflows."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Callable, Optional

from pypdf import PdfReader, PdfWriter

from ocr_cache import CacheStore, ensure_private_dir, sha256_file, write_json_atomic, write_text_atomic
from ocr_config import (
    DIRECT_OCR_PAGE_LIMIT,
    MAX_CHUNKS,
    OCR_CHUNK_RETRY_ATTEMPTS,
    OCR_CHUNK_RETRY_BASE_DELAY_SECONDS,
    OcrConfig,
)
from ocr_process import ocr_timeout_for_pages, should_retry_chunk
from ocr_results import OcrValidationError, build_markdown_from_result
from ocr_types import OcrRunResult
from version import JOB_STATE_SCHEMA_VERSION, TOOL_VERSION


ProcessRunner = Callable[..., subprocess.CompletedProcess[str]]


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
    ranges = [
        (start_page, min(start_page + chunk_size - 1, total_pages))
        for start_page in range(1, total_pages + 1, chunk_size)
    ]
    if len(ranges) > MAX_CHUNKS:
        raise RuntimeError(f"PDF requires too many OCR chunks: {len(ranges)}")
    return ranges


def get_job_paths(job_dir: Path) -> dict[str, Path]:
    return {
        "job_dir": job_dir,
        "status_path": job_dir / "status.json",
        "chunks_dir": job_dir / "chunks",
        "work_dir": job_dir / "work",
        "cache_dir": job_dir / "engine_cache",
    }


def save_job_state(job_dir: Path, state: dict[str, object]) -> None:
    ensure_private_dir(job_dir)
    write_json_atomic(get_job_paths(job_dir)["status_path"], state)


def initialize_or_load_job_state(
    pdf_path: Path,
    pdf_hash: str,
    total_pages: int,
    file_size_bytes: int,
    chunk_size: int,
    job_dir: Optional[Path] = None,
    identity_hash: str = "legacy",
    job_root: Optional[Path] = None,
) -> dict[str, object]:
    resolved_job_dir = job_dir or ((job_root or OcrConfig.from_environment().job_root) / pdf_hash / identity_hash)
    status_path = get_job_paths(resolved_job_dir)["status_path"]
    if status_path.exists():
        try:
            state = json.loads(status_path.read_text(encoding="utf-8"))
            if _valid_job_state(
                state,
                pdf_hash=pdf_hash,
                identity_hash=identity_hash,
                total_pages=total_pages,
                file_size_bytes=file_size_bytes,
                chunk_size=chunk_size,
                job_dir=resolved_job_dir,
            ):
                return state
        except (OSError, ValueError, TypeError):
            pass

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
                "markdown_path": str(get_job_paths(resolved_job_dir)["chunks_dir"] / f"chunk_{index:03d}.md"),
            }
        )
    state: dict[str, object] = {
        "schema_id": "url-pdf-download-ocr.job-state",
        "schema_version": JOB_STATE_SCHEMA_VERSION,
        "tool_version": TOOL_VERSION,
        "pdf_sha256": pdf_hash,
        "pdf_hash": pdf_hash,
        "identity_hash": identity_hash,
        "total_pages": total_pages,
        "file_size_bytes": file_size_bytes,
        "chunk_size": chunk_size,
        "status": "pending",
        "chunks": chunks,
    }
    save_job_state(resolved_job_dir, state)
    return state


def _valid_job_state(
    state: object,
    *,
    pdf_hash: str,
    identity_hash: str,
    total_pages: int,
    file_size_bytes: int,
    chunk_size: int,
    job_dir: Path,
) -> bool:
    if not isinstance(state, dict):
        return False
    if (
        state.get("schema_id") != "url-pdf-download-ocr.job-state"
        or state.get("schema_version") != JOB_STATE_SCHEMA_VERSION
        or state.get("pdf_sha256") != pdf_hash
        or state.get("identity_hash") != identity_hash
        or state.get("total_pages") != total_pages
        or state.get("file_size_bytes") != file_size_bytes
        or state.get("chunk_size") != chunk_size
        or state.get("status") not in {"pending", "running", "failed", "completed"}
    ):
        return False
    chunks = state.get("chunks")
    expected_ranges = build_chunk_ranges(total_pages, chunk_size)
    if not isinstance(chunks, list) or len(chunks) != len(expected_ranges):
        return False
    chunks_dir = get_job_paths(job_dir)["chunks_dir"]
    for index, (chunk, (start_page, end_page)) in enumerate(zip(chunks, expected_ranges), start=1):
        if not isinstance(chunk, dict):
            return False
        if (
            chunk.get("index") != index
            or chunk.get("start_page") != start_page
            or chunk.get("end_page") != end_page
            or chunk.get("status") not in {"pending", "running", "failed", "completed"}
            or not isinstance(chunk.get("attempts"), int)
            or chunk.get("attempts") < 0
            or not isinstance(chunk.get("markdown_path"), str)
            or chunk.get("markdown_path") != str(chunks_dir / f"chunk_{index:03d}.md")
            or (chunk.get("error") is not None and not isinstance(chunk.get("error"), str))
        ):
            return False
        if chunk.get("status") == "completed":
            if not isinstance(chunk.get("validation"), dict) or not isinstance(chunk.get("markdown_sha256"), str):
                return False
    return True


def get_pending_chunks(state: dict[str, object]) -> list[dict[str, object]]:
    pending = []
    for chunk in state.get("chunks", []):
        if chunk.get("status") != "completed":
            pending.append(chunk)
            continue
        markdown_value = chunk.get("markdown_path")
        if not markdown_value or "validation" not in chunk:
            pending.append(chunk)
            continue
        validation = chunk.get("validation", {})
        expected_pages = int(chunk.get("end_page", 0)) - int(chunk.get("start_page", 0)) + 1
        expected_page_numbers = list(range(int(chunk.get("start_page", 0)), int(chunk.get("end_page", 0)) + 1))
        markdown_path = Path(str(markdown_value)) if markdown_value else None
        try:
            markdown_digest = sha256_file(markdown_path) if markdown_path and markdown_path.is_file() else None
        except OSError:
            markdown_digest = None
        valid_file = bool(
            markdown_path
            and markdown_path.is_file()
            and not markdown_path.is_symlink()
            and markdown_path.stat().st_size > 0
            and isinstance(validation, dict)
            and validation.get("complete") is True
            and int(validation.get("expected_pages", -1)) == expected_pages
            and int(validation.get("returned_pages", -1)) == expected_pages
            and validation.get("page_numbers") == expected_page_numbers
            and validation.get("missing_pages") == []
            and validation.get("duplicate_pages") == []
            and validation.get("page_evidence") is True
            and chunk.get("markdown_sha256") == markdown_digest
        )
        if not valid_file:
            pending.append(chunk)
    return pending


def split_pdf_chunk(
    source_pdf: Path,
    destination_pdf: Path,
    start_page: int,
    end_page: int,
    *,
    reader: Optional[PdfReader] = None,
) -> None:
    destination_pdf.parent.mkdir(parents=True, exist_ok=True)
    owned_reader = reader is None
    reader = reader or PdfReader(str(source_pdf), strict=False)
    writer = PdfWriter()
    total_pages = len(reader.pages)
    if start_page < 1 or end_page > total_pages or start_page > end_page:
        raise RuntimeError(f"Invalid chunk page range: {start_page}-{end_page}/{total_pages}")
    for page_index in range(start_page - 1, end_page):
        writer.add_page(reader.pages[page_index])
    temp = tempfile.NamedTemporaryFile(
        mode="wb",
        delete=False,
        dir=destination_pdf.parent,
        prefix=".chunk-",
        suffix=".part",
    )
    temp_path = Path(temp.name)
    try:
        with temp:
            writer.write(temp)
            temp.flush()
            os.fsync(temp.fileno())
        os.replace(temp_path, destination_pdf)
    finally:
        temp_path.unlink(missing_ok=True)
    del owned_reader


def concatenate_markdown_files(markdown_paths: list[Path], destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        delete=False,
        dir=destination.parent,
        prefix=f".{destination.name}.",
        suffix=".part",
    )
    temp_path = Path(temp.name)
    try:
        with temp:
            first = True
            for markdown_path in markdown_paths:
                if not first:
                    temp.write("\n")
                first = False
                with markdown_path.open("r", encoding="utf-8") as source:
                    shutil.copyfileobj(source, temp, length=1024 * 1024)
            temp.write("\n")
            temp.flush()
            os.fsync(temp.fileno())
        os.chmod(temp_path, 0o600)
        os.replace(temp_path, destination)
    finally:
        temp_path.unlink(missing_ok=True)


def summarize_failed_chunks(chunks: list[dict[str, object]]) -> str:
    details = ", ".join(
        f"{chunk['index']}({chunk['start_page']}-{chunk['end_page']}: {chunk.get('error')})"
        for chunk in chunks
    )
    return f"Chunk OCR failed for {len(chunks)} chunk(s): {details}. Re-run will resume from unfinished chunks."


def run_chunked(
    pdf_path: Path,
    paddle_script: Path,
    markdown_path: Path,
    pdf_hash: str,
    total_pages: int,
    file_size_bytes: int,
    identity_hash: str,
    identity: dict[str, object],
    *,
    store: CacheStore,
    process_runner: ProcessRunner,
    keep_raw: bool,
    write_cache: bool,
    deadline: float,
) -> OcrRunResult:
    chunk_size = calculate_chunk_size(total_pages, file_size_bytes)
    active_job_root = store.job_root
    ensure_private_dir(active_job_root)
    ensure_private_dir(active_job_root / pdf_hash)
    job_dir = active_job_root / pdf_hash / identity_hash
    ensure_private_dir(job_dir)
    paths = get_job_paths(job_dir)
    ensure_private_dir(paths["chunks_dir"])
    ensure_private_dir(paths["work_dir"])
    state = initialize_or_load_job_state(
        pdf_path=pdf_path,
        pdf_hash=pdf_hash,
        total_pages=total_pages,
        file_size_bytes=file_size_bytes,
        chunk_size=chunk_size,
        job_dir=job_dir,
        identity_hash=identity_hash,
    )
    source_reader = PdfReader(str(pdf_path), strict=False)
    for chunk in get_pending_chunks(state):
        if time.monotonic() >= deadline:
            raise RuntimeError("OCR task deadline exceeded")
        start_page = int(chunk["start_page"])
        end_page = int(chunk["end_page"])
        chunk_page_count = end_page - start_page + 1
        chunk_markdown_path = Path(str(chunk["markdown_path"]))
        last_error: Optional[str] = None
        for attempt in range(1, OCR_CHUNK_RETRY_ATTEMPTS + 1):
            chunk["attempts"] = int(chunk.get("attempts", 0)) + 1
            chunk["status"] = "running"
            save_job_state(job_dir, state)
            attempt_dir = Path(
                tempfile.mkdtemp(prefix=f"attempt-{int(chunk['index']):03d}-", dir=paths["work_dir"])
            )
            chunk_pdf_path = attempt_dir / "input.pdf"
            result_path = attempt_dir / "result.json"
            try:
                split_pdf_chunk(pdf_path, chunk_pdf_path, start_page, end_page, reader=source_reader)
                remaining_seconds = int(deadline - time.monotonic())
                if remaining_seconds <= 0:
                    raise RuntimeError("OCR task deadline exceeded")
                proc = process_runner(
                    input_pdf_path=chunk_pdf_path,
                    paddle_script=paddle_script,
                    result_path=result_path,
                    cache_dir=attempt_dir / "engine_cache",
                    page_count=chunk_page_count,
                    timeout_seconds=max(1, min(ocr_timeout_for_pages(chunk_page_count), remaining_seconds)),
                )
                if proc.returncode != 0:
                    raise RuntimeError((proc.stderr or proc.stdout or "PaddleOCR returned a non-zero exit code").strip())
                if not result_path.is_file():
                    raise OcrValidationError("PaddleOCR returned success without a result file")
                validated = build_markdown_from_result(
                    result_path,
                    expected_pages=chunk_page_count,
                    page_start=start_page,
                )
                write_text_atomic(chunk_markdown_path, str(validated["markdown"]))
                chunk["status"] = "completed"
                chunk["error"] = None
                chunk["validation"] = validated["validation"]
                chunk["markdown_sha256"] = sha256_file(chunk_markdown_path)
                save_job_state(job_dir, state)
                if keep_raw and write_cache:
                    kept_raw = (
                        paths["chunks_dir"] / f"chunk_{int(chunk['index']):03d}.raw.json"
                    )
                    ensure_private_dir(kept_raw.parent)
                    store.copy_private(result_path, kept_raw)
                last_error = None
                break
            except Exception as exc:
                last_error = str(exc) or exc.__class__.__name__
                chunk["status"] = "failed"
                chunk["error"] = last_error[:1000]
                save_job_state(job_dir, state)
                if attempt < OCR_CHUNK_RETRY_ATTEMPTS and should_retry_chunk(last_error):
                    delay = min(8.0, OCR_CHUNK_RETRY_BASE_DELAY_SECONDS * (2 ** (attempt - 1)))
                    time.sleep(min(delay, max(0.0, deadline - time.monotonic())))
            finally:
                shutil.rmtree(attempt_dir, ignore_errors=True)
        if last_error is not None:
            continue

    failed_chunks = [chunk for chunk in state["chunks"] if chunk.get("status") != "completed"]
    if failed_chunks:
        state["status"] = "failed"
        save_job_state(job_dir, state)
        raise RuntimeError(summarize_failed_chunks(failed_chunks))

    ordered_paths = [Path(str(chunk["markdown_path"])) for chunk in state["chunks"]]
    if any(not path.is_file() for path in ordered_paths):
        raise OcrValidationError("OCR chunk manifest references a missing page result")
    concatenate_markdown_files(ordered_paths, markdown_path)
    validation = {
        "complete": True,
        "expected_pages": total_pages,
        "returned_pages": sum(int(chunk["validation"]["returned_pages"]) for chunk in state["chunks"]),
        "page_numbers": list(range(1, total_pages + 1)),
        "missing_pages": [],
        "duplicate_pages": [],
        "blank_pages": [
            page
            for chunk in state["chunks"]
            for page in chunk["validation"].get("blank_pages", [])
        ],
        "page_evidence": True,
    }
    if write_cache:
        store.save_entry(
            pdf_hash,
            identity_hash,
            identity,
            total_pages,
            markdown_path,
            validation,
        )
        if keep_raw:
            raw_dir = store.entry_root(pdf_hash) / identity_hash / "raw"
            ensure_private_dir(raw_dir)
            with store.maintenance_lock():
                for raw_path in paths["chunks_dir"].glob("*.raw.json"):
                    store.copy_private(raw_path, raw_dir / raw_path.name.replace(".raw", ""))
    state["status"] = "completed"
    state["artifacts_cleaned"] = True
    save_job_state(job_dir, state)
    for chunk in state["chunks"]:
        Path(str(chunk["markdown_path"])).unlink(missing_ok=True)
    for raw_path in paths["chunks_dir"].glob("*.raw.json"):
        raw_path.unlink(missing_ok=True)
    return OcrRunResult(
        markdown_path,
        f"Completed {len(state['chunks'])} validated chunk(s).",
        False,
        pdf_hash,
        {
            "ocr_strategy": "chunked_resume",
            "ocr_total_pages": total_pages,
            "ocr_chunk_size": chunk_size,
            "ocr_chunk_count": len(state["chunks"]),
            "ocr_job_dir": str(job_dir),
            "ocr_validation": validation,
        },
    )


def run_once(
    pdf_path: Path,
    paddle_script: Path,
    markdown_path: Path,
    pdf_hash: str,
    total_pages: int,
    file_size_bytes: int,
    identity_hash: str,
    identity: dict[str, object],
    *,
    store: CacheStore,
    process_runner: ProcessRunner,
    keep_raw: bool,
    write_cache: bool,
    deadline: float,
) -> OcrRunResult:
    if total_pages > DIRECT_OCR_PAGE_LIMIT:
        return run_chunked(
            pdf_path,
            paddle_script,
            markdown_path,
            pdf_hash,
            total_pages,
            file_size_bytes,
            identity_hash,
            identity,
            store=store,
            process_runner=process_runner,
            keep_raw=keep_raw,
            write_cache=write_cache,
            deadline=deadline,
        )
    with tempfile.TemporaryDirectory(prefix="url-pdf-ocr-") as temp_dir:
        temp_dir_path = Path(temp_dir)
        result_path = temp_dir_path / "result.json"
        remaining_seconds = int(deadline - time.monotonic())
        if remaining_seconds <= 0:
            raise RuntimeError("OCR task deadline exceeded")
        proc = process_runner(
            input_pdf_path=pdf_path,
            paddle_script=paddle_script,
            result_path=result_path,
            cache_dir=temp_dir_path / "engine_cache",
            page_count=total_pages,
            timeout_seconds=max(1, min(ocr_timeout_for_pages(total_pages), remaining_seconds)),
        )
        if proc.returncode != 0:
            raise RuntimeError((proc.stderr or proc.stdout or "PaddleOCR returned a non-zero exit code").strip())
        if not result_path.is_file():
            raise OcrValidationError("PaddleOCR returned success without a result file")
        validated = build_markdown_from_result(result_path, expected_pages=total_pages)
        write_text_atomic(markdown_path, str(validated["markdown"]))
        raw_result = result_path if keep_raw and write_cache else None
        if write_cache:
            store.save_entry(
                pdf_hash,
                identity_hash,
                identity,
                total_pages,
                markdown_path,
                validated["validation"],
                raw_result=raw_result,
            )
        return OcrRunResult(
            markdown_path,
            proc.stderr or None,
            False,
            pdf_hash,
            {
                "ocr_strategy": "single_pass",
                "ocr_total_pages": total_pages,
                "ocr_validation": validated["validation"],
            },
        )
