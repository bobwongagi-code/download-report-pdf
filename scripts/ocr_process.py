"""PaddleOCR process execution, timeout enforcement, and retry policy."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Optional

from ocr_config import (
    MAX_PROCESS_OUTPUT_BYTES,
    OCR_TIMEOUT_BASE_SECONDS,
    OCR_TIMEOUT_MAX_SECONDS,
    OCR_TIMEOUT_PER_PAGE_SECONDS,
)
from ocr_errors import classify_failure_reason
from process_supervisor import kill_process_tree, read_capture, run_captured_process


def ocr_timeout_for_pages(page_count: int) -> int:
    return min(OCR_TIMEOUT_BASE_SECONDS + OCR_TIMEOUT_PER_PAGE_SECONDS * page_count, OCR_TIMEOUT_MAX_SECONDS)


def _read_limited(path: Path, limit: int = MAX_PROCESS_OUTPUT_BYTES) -> str:
    return read_capture(path, limit).strip()


_terminate_process_tree = kill_process_tree


def _run_process(
    command: list[str],
    *,
    timeout_seconds: int,
    work_dir: Path,
) -> subprocess.CompletedProcess[str]:
    result = run_captured_process(
        command,
        work_dir=work_dir,
        timeout_seconds=timeout_seconds,
        max_output_bytes=MAX_PROCESS_OUTPUT_BYTES,
        timeout_label="PaddleOCR",
    )
    return subprocess.CompletedProcess(
        command,
        result.returncode,
        stdout=result.stdout.strip(),
        stderr=result.stderr.strip(),
    )


def run_paddleocr_subprocess(
    input_pdf_path: Path,
    paddle_script: Path,
    result_path: Path,
    cache_dir: Path,
    page_count: int = 100,
    timeout_seconds: Optional[int] = None,
) -> subprocess.CompletedProcess[str]:
    command = [
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
    return _run_process(
        command,
        timeout_seconds=timeout_seconds or ocr_timeout_for_pages(page_count),
        work_dir=result_path.parent,
    )


def should_retry_chunk(error_message: str) -> bool:
    reason = classify_failure_reason(error_message)
    return reason in {"timeout", "network", "unknown", "ocr_empty_output", "ocr_incomplete"}
