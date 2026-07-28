"""Private OCR cache entries, identities, locks, and atomic file storage."""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import platform
import shutil
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Optional

from ocr_config import CACHE_MAX_BYTES, CACHE_TTL_SECONDS, resolve_paddle_script
from version import CACHE_SCHEMA_VERSION, MERGE_ALGORITHM_VERSION, OCR_RESULT_SCHEMA_VERSION, TOOL_NAME, TOOL_VERSION

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows fallback
    fcntl = None


def ensure_private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        path.chmod(0o700)
    except OSError:
        pass


def write_private(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = tempfile.NamedTemporaryFile(
        mode="wb",
        delete=False,
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".part",
    )
    temp_path = Path(temp.name)
    try:
        with temp:
            temp.write(data)
            temp.flush()
            os.fsync(temp.fileno())
        os.chmod(temp_path, 0o600)
        os.replace(temp_path, path)
        try:
            path.chmod(0o600)
        except OSError:
            pass
    finally:
        temp_path.unlink(missing_ok=True)


def write_text_atomic(path: Path, text: str) -> None:
    write_private(path, text.encode("utf-8"))


def write_json_atomic(path: Path, payload: object) -> None:
    write_text_atomic(path, json.dumps(payload, ensure_ascii=True, indent=2) + "\n")


def copy_private(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp = tempfile.NamedTemporaryFile(
        mode="wb",
        delete=False,
        dir=destination.parent,
        prefix=f".{destination.name}.",
        suffix=".part",
    )
    temp_path = Path(temp.name)
    try:
        with source.open("rb") as source_fh, temp:
            shutil.copyfileobj(source_fh, temp, length=1024 * 1024)
            temp.flush()
            os.fsync(temp.fileno())
        os.chmod(temp_path, 0o600)
        os.replace(temp_path, destination)
    finally:
        temp_path.unlink(missing_ok=True)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while True:
            chunk = fh.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def script_descriptor(configured_path: Optional[str]) -> dict[str, Optional[str]]:
    script_path = resolve_paddle_script(configured_path).expanduser().resolve()
    return {
        "script_ref": str(script_path),
        "script_sha256": sha256_file(script_path) if script_path.is_file() else None,
    }


def ocr_identity(pdf_hash: str, descriptor: dict[str, Optional[str]]) -> tuple[str, dict[str, object]]:
    payload: dict[str, object] = {
        "pdf_sha256": pdf_hash,
        "pdf_hash": pdf_hash,
        "engine": "paddleocr-document-parsing",
        "engine_script": descriptor,
        "tool_name": TOOL_NAME,
        "tool_version": TOOL_VERSION,
        "result_schema_version": OCR_RESULT_SCHEMA_VERSION,
        "cache_schema_version": CACHE_SCHEMA_VERSION,
        "merge_algorithm_version": MERGE_ALGORITHM_VERSION,
        "options": {"file_type": 0, "pretty": True},
    }
    identity_hash = hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()
    return identity_hash, payload


def identity_compatible(actual: object, expected: dict[str, object]) -> bool:
    if not isinstance(actual, dict):
        return False
    for key, expected_value in expected.items():
        actual_value = actual.get(key)
        if key == "engine_script" and isinstance(expected_value, dict) and isinstance(actual_value, dict):
            if actual_value.get("script_ref") != expected_value.get("script_ref"):
                return False
            expected_hash = expected_value.get("script_sha256")
            if expected_hash is not None and actual_value.get("script_sha256") != expected_hash:
                return False
            continue
        if actual_value != expected_value:
            return False
    return True


def manifest_valid(
    manifest: object,
    *,
    pdf_hash: str,
    expected_pages: int,
    markdown_path: Path,
) -> bool:
    if not isinstance(manifest, dict):
        return False
    if manifest.get("schema_id") != "url-pdf-download-ocr.artifact-manifest":
        return False
    if manifest.get("schema_version") != CACHE_SCHEMA_VERSION:
        return False
    try:
        if manifest.get("status") != "completed" or manifest.get("pdf_sha256") != pdf_hash:
            return False
        if int(manifest.get("total_pages", -1)) != expected_pages:
            return False
        expires_at = float(manifest.get("expires_at", 0))
        if expires_at <= time.time():
            return False
        if markdown_path.is_symlink() or not markdown_path.is_file() or markdown_path.stat().st_size <= 0:
            return False
        if sha256_file(markdown_path) != manifest.get("markdown_sha256"):
            return False
        validation = manifest.get("validation", {})
        expected_page_numbers = list(range(1, expected_pages + 1))
        if not isinstance(validation, dict):
            return False
        if (
            validation.get("complete") is not True
            or int(validation.get("expected_pages", -1)) != expected_pages
            or int(validation.get("returned_pages", -1)) != expected_pages
            or validation.get("page_numbers") != expected_page_numbers
            or validation.get("missing_pages") != []
            or validation.get("duplicate_pages") != []
        ):
            return False
        blank_pages = validation.get("blank_pages", [])
        if not isinstance(blank_pages, list) or len(set(blank_pages)) != len(blank_pages):
            return False
        if validation.get("page_evidence") is not True:
            return False
        with markdown_path.open("r", encoding="utf-8") as fh:
            has_non_whitespace = any(chunk.strip() for chunk in iter(lambda: fh.read(1024 * 1024), ""))
        if not has_non_whitespace:
            return blank_pages == expected_page_numbers
        return True
    except (OSError, TypeError, ValueError):
        return False


@dataclass(frozen=True)
class CacheStore:
    """Own the filesystem roots and policies for one OCR invocation."""

    cache_root: Path
    job_root: Path

    def entry_root(self, pdf_hash: str) -> Path:
        return self.cache_root / "entries" / pdf_hash

    def get_cache_paths(self, pdf_hash: str, identity_hash: Optional[str] = None) -> tuple[Path, Path]:
        identity_hash = identity_hash or "legacy"
        root = self.entry_root(pdf_hash) / identity_hash
        return root / "artifact.md", root / "manifest.json"

    def copy_private(self, source: Path, destination: Path) -> None:
        copy_private(source, destination)

    def load_entry(
        self,
        pdf_hash: str,
        expected_pages: int,
        descriptor: dict[str, Optional[str]],
    ) -> Optional[tuple[Path, dict[str, object]]]:
        root = self.entry_root(pdf_hash)
        if not root.is_dir():
            return None
        exact_identity, expected_identity = ocr_identity(pdf_hash, descriptor)
        candidates = [root / exact_identity] + [
            path for path in root.iterdir() if path.is_dir() and path.name != exact_identity
        ]
        for entry_dir in candidates:
            manifest_path = entry_dir / "manifest.json"
            markdown_path = entry_dir / "artifact.md"
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            identity = manifest.get("ocr_identity", {}) if isinstance(manifest, dict) else {}
            if identity_compatible(identity, expected_identity) and manifest_valid(
                manifest,
                pdf_hash=pdf_hash,
                expected_pages=expected_pages,
                markdown_path=markdown_path,
            ):
                return markdown_path, manifest
        return None

    def save_entry(
        self,
        pdf_hash: str,
        identity_hash: str,
        identity: dict[str, object],
        total_pages: int,
        markdown: Path,
        validation: dict[str, object],
        *,
        raw_result: Optional[Path] = None,
    ) -> None:
        with self.maintenance_lock():
            save_cache_entry(
                self,
                pdf_hash,
                identity_hash,
                identity,
                total_pages,
                markdown,
                validation,
                raw_result=raw_result,
            )

    def purge(self) -> int:
        return purge_cache(self.cache_root, self.job_root)

    @contextlib.contextmanager
    def key_lock(self, key: str, *, persistent: bool = True) -> Iterator[None]:
        with key_lock(self.cache_root, key, persistent=persistent):
            yield

    @contextlib.contextmanager
    def maintenance_lock(self) -> Iterator[None]:
        with key_lock(self.cache_root, "maintenance", persistent=True):
            yield


def save_cache_entry(
    store: CacheStore,
    pdf_hash: str,
    identity_hash: str,
    identity: dict[str, object],
    total_pages: int,
    markdown: Path,
    validation: dict[str, object],
    *,
    raw_result: Optional[Path] = None,
) -> None:
    ensure_private_dir(store.cache_root)
    ensure_private_dir(store.cache_root / "entries")
    ensure_private_dir(store.entry_root(pdf_hash))
    entry_dir = store.entry_root(pdf_hash) / identity_hash
    ensure_private_dir(entry_dir)
    artifact_path = entry_dir / "artifact.md"
    copy_private(markdown, artifact_path)
    manifest: dict[str, object] = {
        "schema_id": "url-pdf-download-ocr.artifact-manifest",
        "schema_version": CACHE_SCHEMA_VERSION,
        "tool_name": TOOL_NAME,
        "tool_version": TOOL_VERSION,
        "status": "completed",
        "pdf_sha256": pdf_hash,
        "total_pages": total_pages,
        "ocr_identity": identity,
        "identity_hash": identity_hash,
        "validation": validation,
        "markdown_sha256": sha256_file(artifact_path),
        "markdown_bytes": artifact_path.stat().st_size,
        "created_at": time.time(),
        "expires_at": time.time() + CACHE_TTL_SECONDS,
        "platform": platform.system(),
    }
    write_json_atomic(entry_dir / "manifest.json", manifest)
    if raw_result is not None:
        copy_private(raw_result, entry_dir / "raw.json")


def purge_cache(cache_root: Path, job_root: Path) -> int:
    """Remove expired or over-capacity private cache and job files."""
    if not any(root.exists() for root in (cache_root / "entries", job_root)):
        return 0
    with key_lock(cache_root, "maintenance", persistent=True):
        return _purge_cache_unlocked(cache_root, job_root)


def _active_job_dir(job_root: Path, path: Path, now: float) -> bool:
    try:
        relative = path.relative_to(job_root)
    except ValueError:
        return False
    if len(relative.parts) < 3:
        return False
    job_dir = job_root / relative.parts[0] / relative.parts[1]
    status_path = job_dir / "status.json"
    try:
        state = json.loads(status_path.read_text(encoding="utf-8"))
        status_age = now - status_path.stat().st_mtime
    except (OSError, TypeError, ValueError):
        return False
    return isinstance(state, dict) and state.get("status") in {"pending", "running"} and status_age <= CACHE_TTL_SECONDS


def _purge_cache_unlocked(cache_root: Path, job_root: Path) -> int:
    removed = 0
    roots = [cache_root / "entries", job_root]
    if not any(root.exists() for root in roots):
        return 0
    now = time.time()
    files: list[tuple[float, int, Path]] = []
    for root in roots:
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if path.is_symlink():
                try:
                    path.unlink()
                    removed += 1
                except OSError:
                    pass
                continue
            if not path.is_file():
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            if root == job_root and _active_job_dir(job_root, path, now):
                continue
            if now - stat.st_mtime > CACHE_TTL_SECONDS:
                try:
                    path.unlink()
                    removed += 1
                except OSError:
                    pass
            else:
                files.append((stat.st_mtime, stat.st_size, path))
    total = sum(size for _, size, _ in files)
    for _, size, path in sorted(files):
        if total <= CACHE_MAX_BYTES:
            break
        try:
            path.unlink()
            total -= size
            removed += 1
        except OSError:
            pass
    for root in roots:
        if not root.exists():
            continue
        for path in sorted(root.rglob("*"), reverse=True):
            if path.is_dir():
                try:
                    path.rmdir()
                except OSError:
                    pass
    return removed


@contextlib.contextmanager
def key_lock(cache_root: Path, key: str, *, persistent: bool = True) -> Iterator[None]:
    temporary_root = None
    if persistent:
        lock_path = cache_root / "locks" / f"{key}.lock"
        ensure_private_dir(cache_root)
        ensure_private_dir(lock_path.parent)
    else:
        temporary_root = tempfile.TemporaryDirectory(prefix=f"{TOOL_NAME}-lock-")
        lock_path = Path(temporary_root.name) / f"{key}.lock"
    try:
        with lock_path.open("a+") as lock_file:
            try:
                lock_path.chmod(0o600)
            except OSError:
                pass
            if fcntl is not None:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                if fcntl is not None:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
    finally:
        if temporary_root is not None:
            temporary_root.cleanup()
