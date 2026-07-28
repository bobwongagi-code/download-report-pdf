#!/usr/bin/env python3
"""Download a PDF from a URL and create a validated Markdown OCR artifact."""
from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path

from pypdf import PdfReader

from http_download import (
    CONNECT_TIMEOUT_SECONDS,
    MAX_REDIRECTS,
    MAX_RESPONSE_BYTES,
    NetworkPolicy,
    RequestBudget,
    RETRY_ATTEMPTS,
    RETRY_DELAY_SECONDS,
    REQUEST_TIMEOUT_SECONDS,
    TOTAL_REQUEST_TIMEOUT_SECONDS,
    probe_url,
)
from ocr_runner import (
    DEFAULT_PADDLE_SCRIPT,
    PADDLE_SCRIPT_ENV_VAR,
    classify_failure_reason,
    now_ms,
    purge_cache,
    run_paddleocr,
)
from artifact_paths import extract_filename, output_paths
from providers import (
    detect_provider_label,
    extract_hubspot_second_hop,
    extract_pdf_candidates,
    is_hubspot_tracking_url,
    known_provider_candidates,
)
from url_utils import normalize_url, redact_text, redact_url
from version import TOOL_NAME, TOOL_VERSION


MAX_CANDIDATES = 20


def validate_pdf_file(path: Path) -> int:
    """Validate the file signature and parse its page tree before publication."""
    try:
        with path.open("rb") as fh:
            if fh.read(5) != b"%PDF-":
                raise RuntimeError("file signature is not %PDF-")
        reader = PdfReader(str(path), strict=False)
        page_count = len(reader.pages)
    except Exception as exc:
        raise RuntimeError(f"PDF validation failed: {exc}") from exc
    if page_count <= 0:
        raise RuntimeError("PDF validation failed: document has no pages")
    return page_count


def publish_staged_file(staging_path: Path, destination: Path, *, force: bool = False) -> None:
    """Publish a same-directory staging file without following destination symlinks."""
    try:
        with staging_path.open("rb") as fh:
            os.fsync(fh.fileno())
        destination.parent.mkdir(parents=True, exist_ok=True)
        if force:
            os.replace(staging_path, destination)
            return
        try:
            os.link(staging_path, destination)
        except FileExistsError as exc:
            raise RuntimeError(f"Output already exists: {destination}") from exc
        staging_path.unlink(missing_ok=True)
    except BaseException:
        staging_path.unlink(missing_ok=True)
        raise


def _new_markdown_staging(output_dir: Path, markdown_path: Path) -> Path:
    handle = tempfile.NamedTemporaryFile(
        mode="wb",
        delete=False,
        dir=output_dir,
        prefix=f".{markdown_path.name}.",
        suffix=".part",
    )
    path = Path(handle.name)
    handle.close()
    path.unlink(missing_ok=True)
    return path


def _cleanup_probe(probe) -> None:
    staging_path = getattr(probe, "staging_path", None)
    if staging_path:
        try:
            staging_path.unlink(missing_ok=True)
        except OSError:
            pass


def _error_code(exc: BaseException) -> str:
    return str(getattr(exc, "code", "unknown"))


def _error_details(exc: BaseException) -> dict[str, object]:
    details: dict[str, object] = {"error_code": _error_code(exc)}
    for name in ("status", "retryable"):
        value = getattr(exc, name, None)
        if value is not None:
            details[name] = value
    return details


def download_pdf(
    source_url: str,
    output_dir: Path,
    *,
    force: bool = False,
    allow_http: bool = False,
) -> tuple[Path, str, dict[str, object]]:
    """Resolve and publish a PDF using one bounded GET per URL hop."""
    source_url = normalize_url(source_url)
    output_dir.mkdir(parents=True, exist_ok=True)
    network_policy = NetworkPolicy(allow_http=allow_http)
    budget = RequestBudget()
    source_provider = detect_provider_label(source_url) or "unknown"
    metrics: dict[str, object] = {
        "source_provider": source_provider,
        "resolved_provider": "unknown",
        "provider": source_provider,
        "candidate_count": 0,
        "candidate_probe_count": 0,
        "resolved_via": "unknown",
        "candidate_attempts": [],
        "network_budget": {
            "deadline_seconds": TOTAL_REQUEST_TIMEOUT_SECONDS,
            "max_response_bytes": MAX_RESPONSE_BYTES,
            "max_redirects": MAX_REDIRECTS,
            "allow_http": allow_http,
        },
    }
    download_started_ms = now_ms()
    initial_probe = None
    selected_probe = None
    candidate_urls: list[str] = []

    try:
        probe_started_ms = now_ms()
        initial_probe = probe_url(
            source_url,
            policy=network_policy,
            budget=budget,
            staging_dir=output_dir,
        )
        metrics["initial_probe_ms"] = now_ms() - probe_started_ms
        metrics["initial_final_url"] = redact_url(initial_probe.final_url)
        metrics["redirect_count"] = initial_probe.redirect_count
        metrics["request_count"] = initial_probe.request_count

        if initial_probe.is_pdf:
            selected_probe = initial_probe
            metrics["resolved_via"] = "direct"
        elif initial_probe.is_html:
            html = initial_probe.body.decode("utf-8", errors="replace")
            candidate_urls.extend(known_provider_candidates(source_url))
            candidate_urls.extend(known_provider_candidates(initial_probe.final_url))
            if is_hubspot_tracking_url(source_url):
                second_hop = extract_hubspot_second_hop(html)
                if second_hop:
                    candidate_urls.append(second_hop)
            candidate_urls.extend(extract_pdf_candidates(html, initial_probe.final_url))
            candidate_urls = list(dict.fromkeys(candidate_urls))[:MAX_CANDIDATES]
            metrics["candidate_count"] = len(candidate_urls)
            candidate_probe_started_ms = now_ms()
            for candidate_index, candidate in enumerate(candidate_urls, start=1):
                metrics["candidate_probe_count"] = candidate_index
                try:
                    candidate_probe = probe_url(
                        candidate,
                        policy=network_policy,
                        parent_url=initial_probe.final_url,
                        allow_cross_origin=False,
                        budget=budget,
                        staging_dir=output_dir,
                    )
                    if not candidate_probe.is_pdf:
                        _cleanup_probe(candidate_probe)
                        metrics["candidate_attempts"].append({
                            "index": candidate_index,
                            "url": redact_url(candidate),
                            "outcome": "not_pdf",
                        })
                        continue
                    selected_probe = candidate_probe
                    metrics["resolved_via"] = "candidate"
                    metrics["candidate_attempts"].append({
                        "index": candidate_index,
                        "url": redact_url(candidate),
                        "outcome": "pdf",
                    })
                    break
                except Exception as exc:
                    metrics["candidate_attempts"].append({
                        "index": candidate_index,
                        "url": redact_url(candidate),
                        "outcome": "error",
                        **_error_details(exc),
                        "error": redact_text(str(exc))[:300],
                    })
            metrics["candidate_probe_ms"] = now_ms() - candidate_probe_started_ms
            _cleanup_probe(initial_probe)
            initial_probe = None
        else:
            raise RuntimeError("The source response was neither a PDF nor an HTML page.")

        if selected_probe is None or not selected_probe.staging_path:
            raise RuntimeError(
                "Could not resolve a downloadable PDF from the provided URL."
                f" Detected provider: {source_provider}."
                " The page may require login, an extraction code, or interactive JavaScript."
            )

        staging_path = selected_probe.staging_path
        page_count = validate_pdf_file(staging_path)
        filename = extract_filename(selected_probe.headers, selected_probe.final_url)
        pdf_path, _ = output_paths(output_dir, filename, force=force)
        publish_staged_file(staging_path, pdf_path, force=force)
        selected_probe.staging_path = None
        metrics["resolved_provider"] = detect_provider_label(selected_probe.final_url) or "unknown"
        metrics["provider"] = metrics["resolved_provider"]
        metrics["resolved_pdf_pages"] = page_count
        metrics["resolved_pdf_url"] = redact_url(selected_probe.final_url)
        metrics["final_download_ms"] = now_ms() - download_started_ms
        metrics["download_total_ms"] = now_ms() - download_started_ms
        return pdf_path, redact_url(selected_probe.final_url), metrics
    except Exception as exc:
        try:
            setattr(exc, "metrics", metrics)
        except (AttributeError, TypeError):
            pass
        raise
    finally:
        _cleanup_probe(initial_probe)
        if selected_probe is not None:
            _cleanup_probe(selected_probe)


def main() -> int:
    total_started_ms = now_ms()
    parser = argparse.ArgumentParser(
        description="Download a PDF from a URL and create a validated Markdown OCR copy."
    )
    parser.add_argument("url", nargs="?", help="A URL that resolves to or exposes a PDF download")
    parser.add_argument(
        "--output-dir",
        default=str(Path.home() / "Downloads"),
        help="Directory for output artifacts (default: ~/Downloads)",
    )
    parser.add_argument(
        "--paddle-script",
        default=None,
        help=f"PaddleOCR script; defaults to ${PADDLE_SCRIPT_ENV_VAR} or {DEFAULT_PADDLE_SCRIPT}",
    )
    parser.add_argument("--force", action="store_true", help="Explicitly replace matching regular output files")
    parser.add_argument("--allow-http", action="store_true", help="Allow public plaintext HTTP URLs explicitly")
    parser.add_argument("--no-cache", action="store_true", help="Do not read or write OCR cache entries")
    parser.add_argument("--keep-raw", action="store_true", help="Keep validated raw OCR JSON in the private cache")
    parser.add_argument("--purge-cache", action="store_true", help="Purge expired and over-capacity OCR cache entries, then exit")
    args = parser.parse_args()

    if args.purge_cache:
        removed = purge_cache()
        print(json.dumps({"ok": True, "purged_cache_entries": removed, "tool_version": TOOL_VERSION}))
        return 0
    if not args.url:
        parser.error("url is required unless --purge-cache is used")

    output_dir = Path(os.path.expanduser(args.output_dir)).resolve()
    source_provider = detect_provider_label(args.url) or "unknown"
    metrics: dict[str, object] = {
        "tool": TOOL_NAME,
        "tool_version": TOOL_VERSION,
        "source_url": redact_url(args.url),
        "source_provider": source_provider,
        "resolved_provider": "unknown",
        "provider": source_provider,
        "output_dir": str(output_dir),
        "timeouts": {
            "connect_timeout_seconds": CONNECT_TIMEOUT_SECONDS,
            "request_timeout_seconds": REQUEST_TIMEOUT_SECONDS,
            "total_network_timeout_seconds": TOTAL_REQUEST_TIMEOUT_SECONDS,
        },
        "retry_policy": {
            "attempts": RETRY_ATTEMPTS,
            "delay_seconds": RETRY_DELAY_SECONDS,
        },
        "network_policy": {"allow_http": args.allow_http},
    }

    try:
        pdf_path, resolved_url, download_metrics = download_pdf(
            args.url,
            output_dir,
            force=args.force,
            allow_http=args.allow_http,
        )
        metrics.update(download_metrics)
    except Exception as exc:
        download_metrics = getattr(exc, "metrics", None)
        if isinstance(download_metrics, dict):
            metrics.update(download_metrics)
        print(json.dumps(
            {
                "ok": False,
                "pdf_path": None,
                "md_path": None,
                "resolved_pdf_url": None,
                "download_error": redact_text(str(exc)),
                **_error_details(exc),
                "metrics": {
                    **metrics,
                    "total_ms": now_ms() - total_started_ms,
                    "failure_stage": "download",
                    "failure_reason": classify_failure_reason(
                        str(exc),
                        getattr(exc, "status", None),
                        _error_code(exc),
                    ),
                },
            },
            ensure_ascii=True,
            indent=2,
        ))
        return 1

    _, markdown_path = output_paths(output_dir, pdf_path.name, force=True)
    markdown_staging_path = _new_markdown_staging(output_dir, markdown_path)
    try:
        ocr_started_ms = now_ms()
        ocr_result = run_paddleocr(
            pdf_path,
            args.paddle_script,
            markdown_path=markdown_staging_path,
            use_cache=not args.no_cache,
            keep_raw=args.keep_raw,
        )
        publish_staged_file(ocr_result.markdown_path, markdown_path, force=args.force)
        metrics["ocr_ms"] = now_ms() - ocr_started_ms
        metrics["ocr_cache_hit"] = ocr_result.cache_hit
        metrics["pdf_sha256"] = ocr_result.pdf_hash
        metrics.update(ocr_result.metadata)
    except Exception as exc:
        markdown_staging_path.unlink(missing_ok=True)
        print(json.dumps(
            {
                "ok": False,
                "pdf_path": str(pdf_path),
                "md_path": None,
                "resolved_pdf_url": resolved_url,
                "ocr_error": redact_text(str(exc)),
                "error_code": _error_code(exc),
                "metrics": {
                    **metrics,
                    "total_ms": now_ms() - total_started_ms,
                    "failure_stage": "ocr",
                    "failure_reason": classify_failure_reason(str(exc), error_code=_error_code(exc)),
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
            "md_path": str(markdown_path),
            "resolved_pdf_url": resolved_url,
            "paddle_note": redact_text(ocr_result.paddle_note) if ocr_result.paddle_note else None,
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
