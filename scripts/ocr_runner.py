"""Public OCR coordinator with compatibility exports for the split subsystems."""
from __future__ import annotations

import contextlib
import hashlib
import tempfile
import time
from pathlib import Path
from typing import Iterator, Optional

from pypdf import PdfReader

from ocr_cache import (
    CacheStore,
    copy_private,
    ensure_private_dir,
    identity_compatible,
    manifest_valid,
    ocr_identity,
    purge_cache as purge_cache_entries,
    script_descriptor,
    sha256_file,
    write_json_atomic,
    write_private,
    write_text_atomic,
)
from ocr_config import (
    CACHE_MAX_BYTES,
    CACHE_TTL_SECONDS,
    DEFAULT_PADDLE_SCRIPT,
    DIRECT_OCR_PAGE_LIMIT,
    MAX_CHUNKS,
    MAX_PDF_PAGES,
    MAX_PROCESS_OUTPUT_BYTES,
    MAX_RESULT_BYTES,
    OCR_CHUNK_RETRY_ATTEMPTS,
    OCR_CHUNK_RETRY_BASE_DELAY_SECONDS,
    OCR_TIMEOUT_BASE_SECONDS,
    OCR_TIMEOUT_MAX_SECONDS,
    OCR_TIMEOUT_PER_PAGE_SECONDS,
    OCR_TOTAL_TIMEOUT_SECONDS,
    OcrConfig,
    PADDLE_SCRIPT_ENV_VAR,
    resolve_paddle_script,
)
from ocr_errors import classify_failure_reason
from ocr_jobs import (
    build_chunk_ranges,
    calculate_chunk_size,
    concatenate_markdown_files,
    get_job_paths,
    get_pending_chunks,
    initialize_or_load_job_state as initialize_job_state,
    run_chunked,
    run_once,
    save_job_state,
    split_pdf_chunk,
    summarize_failed_chunks,
)
from ocr_process import (
    _read_limited,
    _run_process,
    _terminate_process_tree,
    ocr_timeout_for_pages,
    run_paddleocr_subprocess,
    should_retry_chunk,
)
from ocr_results import (
    OcrValidationError,
    _extract_page_texts,
    build_markdown_from_result,
    validate_ocr_payload,
)
from ocr_types import OcrRunResult
from version import (
    CACHE_SCHEMA_VERSION,
    JOB_STATE_SCHEMA_VERSION,
    MERGE_ALGORITHM_VERSION,
    OCR_RESULT_SCHEMA_VERSION,
    TOOL_NAME,
    TOOL_VERSION,
)


def now_ms() -> int:
    return int(time.monotonic() * 1000)


def get_pdf_metadata(pdf_path: Path) -> tuple[int, int]:
    reader = PdfReader(str(pdf_path), strict=False)
    total_pages = len(reader.pages)
    if total_pages <= 0 or total_pages > MAX_PDF_PAGES:
        raise RuntimeError(f"PDF page count outside supported range: {total_pages}")
    file_size = pdf_path.stat().st_size
    if file_size <= 0:
        raise RuntimeError("PDF file is empty")
    return total_pages, file_size


file_sha256 = sha256_file
_ensure_private_dir = ensure_private_dir
_write_private = write_private
_write_text_atomic = write_text_atomic
_write_json_atomic = write_json_atomic
_copy_private = copy_private
_script_descriptor = script_descriptor
_ocr_identity = ocr_identity
_identity_compatible = identity_compatible
_manifest_valid = manifest_valid


def _entry_root(pdf_hash: str, *, config: Optional[OcrConfig] = None) -> Path:
    active_config = config or OcrConfig.from_environment()
    return CacheStore(active_config.cache_root, active_config.job_root).entry_root(pdf_hash)


def get_cache_paths(
    pdf_hash: str,
    identity_hash: Optional[str] = None,
    *,
    config: Optional[OcrConfig] = None,
) -> tuple[Path, Path]:
    active_config = config or OcrConfig.from_environment()
    return CacheStore(active_config.cache_root, active_config.job_root).get_cache_paths(pdf_hash, identity_hash)


def _load_cache_entry(
    pdf_hash: str,
    expected_pages: int,
    descriptor: dict[str, Optional[str]],
    *,
    config: Optional[OcrConfig] = None,
) -> Optional[tuple[Path, dict[str, object]]]:
    active_config = config or OcrConfig.from_environment()
    return CacheStore(active_config.cache_root, active_config.job_root).load_entry(
        pdf_hash,
        expected_pages,
        descriptor,
    )


def _save_cache_entry(
    pdf_hash: str,
    identity_hash: str,
    identity: dict[str, object],
    total_pages: int,
    markdown: Path,
    validation: dict[str, object],
    *,
    raw_result: Optional[Path] = None,
    config: Optional[OcrConfig] = None,
) -> None:
    active_config = config or OcrConfig.from_environment()
    CacheStore(active_config.cache_root, active_config.job_root).save_entry(
        pdf_hash,
        identity_hash,
        identity,
        total_pages,
        markdown,
        validation,
        raw_result=raw_result,
    )


def purge_cache(*, config: Optional[OcrConfig] = None) -> int:
    active_config = config or OcrConfig.from_environment()
    return purge_cache_entries(active_config.cache_root, active_config.job_root)


@contextlib.contextmanager
def _key_lock(
    key: str,
    *,
    persistent: bool = True,
    config: Optional[OcrConfig] = None,
) -> Iterator[None]:
    active_config = config or OcrConfig.from_environment()
    with CacheStore(active_config.cache_root, active_config.job_root).key_lock(key, persistent=persistent):
        yield


def initialize_or_load_job_state(
    pdf_path: Path,
    pdf_hash: str,
    total_pages: int,
    file_size_bytes: int,
    chunk_size: int,
    job_dir: Optional[Path] = None,
    identity_hash: str = "legacy",
    *,
    config: Optional[OcrConfig] = None,
) -> dict[str, object]:
    active_config = config or OcrConfig.from_environment()
    return initialize_job_state(
        pdf_path=pdf_path,
        pdf_hash=pdf_hash,
        total_pages=total_pages,
        file_size_bytes=file_size_bytes,
        chunk_size=chunk_size,
        job_dir=job_dir,
        identity_hash=identity_hash,
        job_root=active_config.job_root,
    )


def _run_chunked_locked(
    pdf_path: Path,
    paddle_script: Path,
    markdown_path: Path,
    pdf_hash: str,
    total_pages: int,
    file_size_bytes: int,
    identity_hash: str,
    identity: dict[str, object],
    *,
    keep_raw: bool,
    write_cache: bool,
    deadline: float,
    job_root: Optional[Path] = None,
    config: Optional[OcrConfig] = None,
) -> OcrRunResult:
    active_config = config or OcrConfig.from_environment()
    return run_chunked(
        pdf_path,
        paddle_script,
        markdown_path,
        pdf_hash,
        total_pages,
        file_size_bytes,
        identity_hash,
        identity,
        store=CacheStore(active_config.cache_root, job_root or active_config.job_root),
        process_runner=run_paddleocr_subprocess,
        keep_raw=keep_raw,
        write_cache=write_cache,
        deadline=deadline,
    )


def _run_once_locked(
    pdf_path: Path,
    paddle_script: Path,
    markdown_path: Path,
    pdf_hash: str,
    total_pages: int,
    file_size_bytes: int,
    identity_hash: str,
    identity: dict[str, object],
    *,
    keep_raw: bool,
    write_cache: bool,
    deadline: float,
    job_root: Optional[Path] = None,
    config: Optional[OcrConfig] = None,
) -> OcrRunResult:
    active_config = config or OcrConfig.from_environment()
    return run_once(
        pdf_path,
        paddle_script,
        markdown_path,
        pdf_hash,
        total_pages,
        file_size_bytes,
        identity_hash,
        identity,
        store=CacheStore(active_config.cache_root, job_root or active_config.job_root),
        process_runner=run_paddleocr_subprocess,
        keep_raw=keep_raw,
        write_cache=write_cache,
        deadline=deadline,
    )


def run_paddleocr(
    pdf_path: Path,
    configured_paddle_script: Optional[str],
    *,
    markdown_path: Optional[Path] = None,
    use_cache: bool = True,
    keep_raw: bool = False,
    config: Optional[OcrConfig] = None,
) -> OcrRunResult:
    active_config = config or OcrConfig.from_environment()
    markdown_path = markdown_path or pdf_path.with_name(f"{pdf_path.stem}.ocr.md")
    pdf_hash = file_sha256(pdf_path)
    total_pages, file_size_bytes = get_pdf_metadata(pdf_path)
    descriptor = _script_descriptor(configured_paddle_script)
    identity_hash, identity = _ocr_identity(pdf_hash, descriptor)
    store = CacheStore(active_config.cache_root, active_config.job_root)
    if use_cache:
        purge_cache(config=active_config)
    lock_key = f"{pdf_hash}-{hashlib.sha256(descriptor['script_ref'].encode()).hexdigest()[:16]}"
    with store.key_lock(lock_key, persistent=use_cache):
        if use_cache:
            with store.maintenance_lock():
                cached = store.load_entry(pdf_hash, total_pages, descriptor)
                if cached:
                    cached_markdown, manifest = cached
                    store.copy_private(cached_markdown, markdown_path)
                    return OcrRunResult(
                        markdown_path,
                        None,
                        True,
                        pdf_hash,
                        {
                            "ocr_strategy": "validated_cache",
                            "ocr_total_pages": total_pages,
                            "ocr_validation": manifest["validation"],
                            "ocr_cache_identity": identity_hash,
                        },
                    )
        paddle_script = resolve_paddle_script(configured_paddle_script)
        if not paddle_script.is_file():
            raise RuntimeError(f"PaddleOCR script not found: {paddle_script}")
        deadline = time.monotonic() + OCR_TOTAL_TIMEOUT_SECONDS
        if use_cache:
            result = _run_once_locked(
                pdf_path,
                paddle_script,
                markdown_path,
                pdf_hash,
                total_pages,
                file_size_bytes,
                identity_hash,
                identity,
                keep_raw=keep_raw,
                write_cache=True,
                deadline=deadline,
                job_root=active_config.job_root,
                config=active_config,
            )
        else:
            with tempfile.TemporaryDirectory(prefix="url-pdf-ocr-job-") as temporary_job:
                result = _run_once_locked(
                    pdf_path,
                    paddle_script,
                    markdown_path,
                    pdf_hash,
                    total_pages,
                    file_size_bytes,
                    identity_hash,
                    identity,
                    keep_raw=False,
                    write_cache=False,
                    deadline=deadline,
                    job_root=Path(temporary_job),
                    config=active_config,
                )
        if use_cache:
            result.metadata["ocr_cache_identity"] = identity_hash
        return result
