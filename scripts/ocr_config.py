"""Configuration values shared by the OCR subsystems."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from version import TOOL_NAME


DIRECT_OCR_PAGE_LIMIT = 100
MAX_PDF_PAGES = 10_000
MAX_CHUNKS = 500
OCR_CHUNK_RETRY_ATTEMPTS = 3
OCR_CHUNK_RETRY_BASE_DELAY_SECONDS = 1.5
OCR_TIMEOUT_BASE_SECONDS = 60
OCR_TIMEOUT_PER_PAGE_SECONDS = 30
OCR_TIMEOUT_MAX_SECONDS = 1800
OCR_TOTAL_TIMEOUT_SECONDS = 7200
CACHE_TTL_SECONDS = 30 * 24 * 60 * 60
CACHE_MAX_BYTES = 2 * 1024 * 1024 * 1024
MAX_RESULT_BYTES = 100 * 1024 * 1024
MAX_PROCESS_OUTPUT_BYTES = 1 * 1024 * 1024
PADDLE_SCRIPT_ENV_VAR = "URL_PDF_DOWNLOAD_OCR_PADDLE_SCRIPT"
CACHE_DIR_ENV_VAR = "URL_PDF_DOWNLOAD_OCR_CACHE_DIR"

DEFAULT_CACHE_BASE = Path.home() / ".codex" / "cache" / TOOL_NAME
DEFAULT_PADDLE_SCRIPT = (
    Path.home()
    / ".agents"
    / "skills"
    / "paddleocr-doc-parsing"
    / "scripts"
    / "vl_caller.py"
)


@dataclass(frozen=True)
class OcrConfig:
    """Per-invocation OCR paths; callers can use more than one cache safely."""

    cache_root: Path
    job_root: Optional[Path] = None

    def __post_init__(self) -> None:
        cache_root = Path(self.cache_root).expanduser()
        job_root = Path(self.job_root).expanduser() if self.job_root is not None else cache_root / "jobs"
        object.__setattr__(self, "cache_root", cache_root)
        object.__setattr__(self, "job_root", job_root)

    @classmethod
    def from_environment(cls) -> "OcrConfig":
        cache_root = Path(os.environ.get(CACHE_DIR_ENV_VAR, str(DEFAULT_CACHE_BASE))).expanduser()
        return cls(cache_root)


def resolve_paddle_script(configured_path: str | None) -> Path:
    """Resolve the CLI value, environment override, or default script path."""
    if configured_path:
        return Path(os.path.expanduser(configured_path))
    env_path = os.environ.get(PADDLE_SCRIPT_ENV_VAR)
    if env_path:
        return Path(os.path.expanduser(env_path))
    return DEFAULT_PADDLE_SCRIPT
