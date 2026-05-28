#!/usr/bin/env python3
"""Download a PDF from a URL and create a Markdown OCR copy.

Orchestration layer -- delegates HTTP work to http_download, provider
detection to providers, and OCR to ocr_runner.
"""
import argparse
import json
import os
from pathlib import Path
from typing import Optional

from http_download import (
    CONNECT_TIMEOUT_SECONDS,
    REQUEST_TIMEOUT_SECONDS,
    RETRY_ATTEMPTS,
    RETRY_DELAY_SECONDS,
    curl_head,
    has_pdf_file_signature,
    looks_like_pdf,
    probe_url,
    stream_download_to_path,
)
from ocr_runner import (
    DEFAULT_PADDLE_SCRIPT,
    PADDLE_SCRIPT_ENV_VAR,
    classify_failure_reason,
    now_ms,
    run_paddleocr,
)
from providers import (
    detect_provider_label,
    ensure_unique_path,
    extract_filename,
    extract_hubspot_second_hop,
    extract_pdf_candidates,
    is_hubspot_tracking_url,
    known_provider_candidates,
    normalize_url,
)


def download_pdf(source_url: str, output_dir: Path) -> tuple[Path, str, dict[str, object]]:
    """Resolve *source_url* to a PDF, download it, and return (path, final_url, metrics)."""
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
        candidate_urls = list(dict.fromkeys(candidate_urls))[:20]
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
    if not has_pdf_file_signature(pdf_path):
        pdf_path.unlink(missing_ok=True)
        raise RuntimeError("Resolved URL did not produce a valid PDF during final download.")
    if not looks_like_pdf(download_headers, final_pdf_url, b"%PDF-"):
        head, head_final_url = curl_head(final_pdf_url)
        if looks_like_pdf(head, head_final_url, b"%PDF-"):
            final_pdf_url = head_final_url
    metrics["final_download_ms"] = now_ms() - final_download_started_ms
    metrics["download_total_ms"] = now_ms() - download_started_ms
    return pdf_path, final_pdf_url, metrics


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
