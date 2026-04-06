# url-pdf-download-ocr

Download a PDF from a user-provided URL, save it locally, and generate a same-name Markdown OCR copy beside it.

## What It Handles

- direct PDF links
- redirected download links
- HubSpot tracking and email links
- pages that expose PDF download URLs
- common cloud-drive share links when they can be converted into direct downloads

## Layout

- `SKILL.md`: skill instructions and usage notes
- `scripts/download_and_ocr.py`: download plus OCR workflow
- `scripts/benchmark.py`: batch benchmark runner and KPI summary
- `tests/`: unit tests for downloader output and benchmark summaries
- `benchmarks/sample_cases.json`: sample benchmark manifest

## Usage

Run the downloader:

```bash
python3 scripts/download_and_ocr.py "PASTE_LINK_HERE"
```

Run the benchmark runner:

```bash
python3 scripts/benchmark.py benchmarks/sample_cases.json
```

## Requirements

- Python 3
- `curl`
- PaddleOCR document parsing script at:
  `/Users/wangbo5/.agents/skills/paddleocr-doc-parsing/scripts/vl_caller.py`

## Verification

```bash
python3 -m unittest discover -s tests
python3 -m py_compile scripts/download_and_ocr.py scripts/benchmark.py
```
