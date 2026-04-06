# url-pdf-download-ocr

Download a PDF from a user-provided URL, save it locally, and generate a same-name Markdown OCR copy beside it.

This repository contains:

- a Codex skill definition for natural-language invocation
- standalone Python scripts for downloading PDFs and generating Markdown OCR copies
- a benchmark runner for KPI tracking, regression checks, and provider-level rule prioritization

## Repository Scope

This is both a skill repository and a lightweight tool repository.

- If you are using Codex skills, start with [SKILL.md](./SKILL.md).
- If you want to run the downloader directly, use `scripts/download_and_ocr.py`.
- If you want to track quality over time, use `scripts/benchmark.py`.

## What It Handles

- direct PDF links
- redirected download links
- HubSpot tracking and email links
- pages that expose PDF download URLs
- common cloud-drive share links when they can be converted into direct downloads

## Requirements

- Python 3.11 or newer recommended
- `curl`
- a PaddleOCR document parsing script compatible with:
  - `--file-path`
  - `--file-type 0`
  - `--output`

By default the downloader looks for PaddleOCR at:

`/Users/wangbo5/.agents/skills/paddleocr-doc-parsing/scripts/vl_caller.py`

You can override that path in either of these ways:

```bash
export URL_PDF_DOWNLOAD_OCR_PADDLE_SCRIPT="/path/to/vl_caller.py"
```

```bash
python3 scripts/download_and_ocr.py "PASTE_LINK_HERE" --paddle-script "/path/to/vl_caller.py"
```

## Layout

- `SKILL.md`: skill instructions and usage notes
- `scripts/download_and_ocr.py`: download plus OCR workflow
- `scripts/benchmark.py`: batch benchmark runner and KPI summary
- `tests/`: unit tests for downloader output and benchmark summaries
- `benchmarks/sample_cases.json`: sample benchmark manifest
- `.github/workflows/ci.yml`: continuous integration
- `.github/workflows/secret-scan.yml`: GitHub Actions secret scanning

## Installation

Clone the repository and enter it:

```bash
git clone https://github.com/bobwongagi-code/download-report-pdf.git
cd download-report-pdf
```

No Python package installation is required for the current scripts.

## Usage

Run the downloader:

```bash
python3 scripts/download_and_ocr.py "PASTE_LINK_HERE"
```

Run the benchmark runner:

```bash
python3 scripts/benchmark.py benchmarks/sample_cases.json
```

Use the included `Makefile` shortcuts if you prefer:

```bash
make test
make check
make benchmark
```

The repository also runs a separate GitHub Actions secret scan on pushes and pull requests.

## Example Output

Successful downloader runs print structured JSON with:

- `pdf_path`
- `md_path`
- `resolved_pdf_url`
- `metrics`

Failed runs still emit structured JSON, including:

- `failure_stage`
- `failure_reason`
- `download_error` or `ocr_error`

## Benchmarking

The benchmark runner reads a JSON case manifest. Each case can include:

- `name`
- `url`
- `input_type`
- `expected_provider`
- `expected_outcome`
- `notes`

Supported normalized expected outcomes:

- `success`
- `download_failed`
- `ocr_failed`
- `crash`

Benchmark outputs are written under `benchmarks/runs/` by default:

- `results.json`
- `summary.json`
- `summary.md`

The summary includes:

- PDF success counts
- end-to-end success counts
- regression counts
- provider-level failure buckets
- OCR cache hit rate
- download efficiency metrics
- provider rule candidates for follow-up work

## Verification

Run the local checks:

```bash
python3 -m unittest discover -s tests
python3 -m py_compile scripts/download_and_ocr.py scripts/benchmark.py
```

Secret scanning is enforced in GitHub Actions with `gitleaks`.

## Contributing

See [CONTRIBUTING.md](./CONTRIBUTING.md) for sample submission guidance, provider-rule work, benchmark expectations, and pull request conventions.

## Legal And Usage Notes

This project is released under the [MIT License](./LICENSE).

You are responsible for using it only on content you are allowed to access and download. Respect the terms of service, copyright rules, access controls, and local laws that apply to the URLs you process.
