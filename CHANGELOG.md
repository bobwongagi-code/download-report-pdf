# Changelog

All notable changes to this project will be documented in this file.

## [Unreleased]

### Added

- benchmark summaries for success rate, latency, OCR cache reuse, failure reasons, and provider-level follow-up recommendations
- manifest expectations for provider and outcome regression checks
- open-source project files including README, contributing guide, templates, CI, and code of conduct
- configurable PaddleOCR script path via CLI flag and environment variable

### Changed

- downloader emits structured JSON for both download and OCR failures
- download path uses bounded retries, probe-first checks, and streaming final downloads
