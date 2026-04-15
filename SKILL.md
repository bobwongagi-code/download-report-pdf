---
name: url-pdf-download-ocr
description: Download PDFs from user-provided URLs into the default Downloads folder, then create a same-name Markdown OCR copy beside the PDF. Use when a user gives a URL that may be a direct PDF link, a HubSpot email or tracking link, a redirected download link, a Google Drive or Dropbox share link, a SharePoint or OneDrive file link, a Chinese cloud-drive share link, or a webpage that exposes a PDF download. Natural-language triggers include short requests like “下载pdf”, “下载这个pdf”, “把这个链接下载成pdf”, “把这个链接转成md”, or “download this URL as PDF”, as long as a URL or obvious download-link context is present.
---

# URL PDF Download OCR

Use this skill when the user gives a URL and wants a local PDF plus a Markdown version.

## Workflow

1. Save outputs to `~/Downloads` by default unless the user explicitly requests another folder.
2. Run `scripts/download_and_ocr.py` with the provided link.
3. Return both output paths: the saved PDF and the generated Markdown file.

## Command

```bash
python3 scripts/download_and_ocr.py "PASTE_LINK_HERE"
```

Optional output directory:

```bash
python3 scripts/download_and_ocr.py "PASTE_LINK_HERE" --output-dir "/custom/path"
```

Optional PaddleOCR override:

```bash
python3 scripts/download_and_ocr.py "PASTE_LINK_HERE" --paddle-script "/path/to/vl_caller.py"
```

## Behavior

- Accept direct PDF URLs, redirected download URLs, HubSpot email links, HubSpot tracking links, and HTML pages that expose a PDF link.
- Accept common cloud-share URLs such as Google Drive, Dropbox, SharePoint, OneDrive, Baidu Netdisk, Aliyun Drive, 123Pan, Lanzou, Quark Drive, Weiyun, and Feishu when they can be converted into a direct download URL without interactive login.
- First try the URL as-is.
- Try known provider-specific direct-download rewrites before giving up.
- If the response is HTML instead of PDF, extract likely PDF candidates from the page and retry.
- Handle HubSpot tracking pages by resolving the second-hop tracking URL before downloading the real PDF.
- Preserve the server-provided filename when available; otherwise derive a stable name from the final URL.
- Create a same-name Markdown file beside the PDF, for example:
  - `~/Downloads/report.pdf`
  - `~/Downloads/report.md`

## Limits

- If the URL requires an authenticated browser session, captcha, extraction code, anti-bot token, or manual button click with no direct downloadable PDF URL in the HTML, surface the blocker clearly.
- Do not fake success when the page does not expose a retrievable PDF.

## PaddleOCR Requirement

After download, always invoke the PaddleOCR document parsing script at:

`/Users/wangbo5/.agents/skills/paddleocr-doc-parsing/scripts/vl_caller.py`

To make the repository portable, prefer either:

- `--paddle-script "/path/to/vl_caller.py"`
- `URL_PDF_DOWNLOAD_OCR_PADDLE_SCRIPT=/path/to/vl_caller.py`

Use local-file mode with `--file-path` and `--file-type 0`.

If PaddleOCR is not configured or returns an error:

- Show the exact error.
- Do not pretend the Markdown conversion succeeded.
- Keep the downloaded PDF if it was already saved.

## Output Rules

- Report the absolute PDF path.
- Report the absolute Markdown path.
- Include the structured `metrics` block from the script output when debugging, benchmarking, or reviewing performance.
- Download failures and OCR failures both return structured JSON, so downstream tooling can parse `failure_stage` consistently.
- Failed runs also include `failure_reason`, using coarse categories such as `network`, `timeout`, `authentication_or_interactive`, `invalid_pdf`, `ocr_configuration`, and `ocr_empty_output`.
- If OCR succeeds, mention that both files were created.
- If download succeeds but OCR fails, say that only the PDF was created and include the OCR error.

## Performance Notes

- The downloader uses bounded timeouts and retries.
- Candidate links are probed before full download to reduce wasted bandwidth.
- OCR results are cached by PDF content hash under the local Codex cache directory, so repeated runs on the same PDF can reuse the existing Markdown output.
- Large PDFs are OCRed through a resumable chunked workflow with persisted job state under the local Codex cache.
- The skill does not treat partial OCR as success; it only emits the final Markdown file after every chunk succeeds.

## Benchmarking

Use the benchmark runner when you want KPI-style measurements across a case list:

```bash
python3 scripts/benchmark.py benchmarks/sample_cases.json
```

Outputs are written under `benchmarks/runs/` by default:

- `results.json`
- `summary.json`
- `summary.md`

The benchmark summary includes:

- overall PDF success and full success counts
- download and OCR failure counts
- p50 and p90 total latency
- OCR cache hit count and hit rate
- rollups by `input_type`
- rollups by resolved `provider`
- failure-reason buckets overall and by provider

Each manifest case can also include:

- `expected_provider`
- `expected_outcome`
- `notes`

Expected outcomes currently normalize to:

- `success`
- `download_failed`
- `ocr_failed`
- `crash`

When these expectations are present, the benchmark also reports regression counts and mismatch reasons such as `provider_mismatch` and `outcome_mismatch`.
