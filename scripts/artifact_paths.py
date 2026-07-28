"""Safe filenames and PDF/Markdown output pair allocation."""
from __future__ import annotations

import re
import unicodedata
from email.parser import Parser
from pathlib import Path
from typing import Optional
from urllib.parse import unquote, unquote_to_bytes, urlparse


MAX_FILENAME_BYTES = 180


def _decode_rfc5987(value: str) -> Optional[str]:
    value = value.strip().strip('"')
    parts = value.split("'", 2)
    if len(parts) != 3:
        return None
    charset, _, encoded = parts
    raw = unquote_to_bytes(encoded)
    try:
        return raw.decode(charset or "utf-8", errors="strict")
    except (LookupError, UnicodeDecodeError):
        return raw.decode("utf-8", errors="replace")


def extract_filename(headers: dict[str, str], url: str) -> str:
    disposition = next(
        (value for key, value in headers.items() if key.lower() == "content-disposition"),
        "",
    )
    extended = re.search(r"filename\*\s*=\s*([^;]+)", disposition, re.I)
    name: Optional[str] = None
    if extended:
        name = _decode_rfc5987(extended.group(1))
    if not name:
        regular = re.search(r"filename\s*=\s*(?:\"([^\"]+)\"|([^;]+))", disposition, re.I)
        if regular:
            name = regular.group(1) or regular.group(2).strip()
    if not name:
        parsed = Parser().parsestr(f"Content-Disposition: {disposition}")
        name = parsed.get_filename()
    if not name:
        name = Path(unquote(urlparse(url).path)).name or "download.pdf"
    return sanitize_filename(name)


def sanitize_filename(name: str) -> str:
    normalized = unicodedata.normalize("NFKC", str(name)).replace("/", "-").replace("\\", "-")
    normalized = re.sub(r"[\x00-\x1f\x7f<>:\"|?*]", "-", normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip(" .-")
    if not normalized:
        normalized = "download"
    if not normalized.lower().endswith(".pdf"):
        normalized += ".pdf"
    encoded = normalized.encode("utf-8")
    if len(encoded) > MAX_FILENAME_BYTES:
        suffix = ".pdf"
        budget = MAX_FILENAME_BYTES - len(suffix.encode("utf-8"))
        prefix = encoded[:budget].decode("utf-8", errors="ignore").rstrip(" .-")
        normalized = f"{prefix or 'download'}{suffix}"
    return normalized


def _path_exists(path: Path) -> bool:
    try:
        path.lstat()
        return True
    except FileNotFoundError:
        return False


def output_paths(output_dir: Path, filename: str, *, force: bool = False) -> tuple[Path, Path]:
    pdf_name = sanitize_filename(filename)
    pdf_suffix = Path(pdf_name).suffix or ".pdf"
    stem = Path(pdf_name).stem
    for counter in range(1, 10000):
        suffix = "" if counter == 1 else f"-{counter}"
        pdf_path = output_dir / f"{stem}{suffix}{pdf_suffix}"
        markdown_path = output_dir / f"{stem}{suffix}.ocr.md"
        legacy_markdown_path = output_dir / f"{stem}{suffix}.md"
        if force or not any(_path_exists(path) for path in (pdf_path, markdown_path, legacy_markdown_path)):
            return pdf_path, markdown_path
    raise RuntimeError(f"Could not find a safe output name for {pdf_name}")


def ensure_unique_path(path: Path, max_attempts: int = 9999) -> Path:
    """Backward-compatible path selection that still protects the output pair."""
    pdf_name = sanitize_filename(path.name)
    pdf_suffix = Path(pdf_name).suffix or ".pdf"
    stem = Path(pdf_name).stem
    for counter in range(1, max_attempts + 2):
        suffix = "" if counter == 1 else f"-{counter}"
        candidate = path.parent / f"{stem}{suffix}{pdf_suffix}"
        markdown = path.parent / f"{stem}{suffix}.ocr.md"
        legacy_markdown = path.parent / f"{stem}{suffix}.md"
        if not any(_path_exists(item) for item in (candidate, markdown, legacy_markdown)):
            return candidate
    raise RuntimeError(f"Could not find unique filename after {max_attempts} attempts: {path}")
