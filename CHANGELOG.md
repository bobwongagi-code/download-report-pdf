# Changelog

All notable changes to this project will be documented in this file.

## [0.3.0] - 2026-07-28

### Added

- benchmark summaries for success rate, latency, OCR cache reuse, failure reasons, and provider-level follow-up recommendations
- manifest expectations for provider and outcome regression checks
- configurable PaddleOCR script path via CLI flag and environment variable

### Changed

- added public-destination network policy, manual redirect validation, bounded one-request downloads, and atomic staging publication
- added no-clobber PDF/Markdown output pairs, full PDF parsing validation, page-complete OCR validation, versioned cache manifests, and cross-process cache locks
- hardened Benchmark case paths, artifact checks, timeouts, redacted provenance, and configurable failure gates
- fixed provider hostname matching, query rewrites, and RFC 5987/Unicode filename handling
- disabled implicit agent invocation and pinned CI actions and Python dependencies
- downloader emits structured JSON for both download and OCR failures
- download path uses bounded retries, one bounded GET per URL hop, and streaming staging publication

## [Unreleased]
