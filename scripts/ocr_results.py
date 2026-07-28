"""OCR payload parsing and page-completeness validation."""
from __future__ import annotations

import json
from pathlib import Path

from ocr_cache import write_text_atomic
from ocr_config import MAX_RESULT_BYTES


class OcrValidationError(RuntimeError):
    code = "ocr_incomplete"


def _extract_page_texts(payload: object, expected_pages: int) -> tuple[list[str], list[int], bool]:
    if not isinstance(payload, dict):
        raise OcrValidationError("OCR result payload must be an object")
    raw_result = payload.get("result")
    pages: object = None
    if isinstance(raw_result, dict):
        nested = raw_result.get("result")
        if isinstance(nested, dict) and isinstance(nested.get("layoutParsingResults"), list):
            pages = nested["layoutParsingResults"]
        elif isinstance(raw_result.get("layoutParsingResults"), list):
            pages = raw_result["layoutParsingResults"]
    if pages is None and isinstance(raw_result, list):
        pages = raw_result
    if pages is None:
        raise OcrValidationError(f"OCR result has no page coverage for {expected_pages} expected page(s)")
    if not isinstance(pages, list) or len(pages) != expected_pages:
        actual = len(pages) if isinstance(pages, list) else 0
        raise OcrValidationError(
            f"OCR result page coverage mismatch: expected {expected_pages}, returned {actual}"
        )
    texts: list[str] = []
    page_numbers: list[int] = []
    saw_page_number = False
    for index, page in enumerate(pages):
        if not isinstance(page, dict):
            raise OcrValidationError(f"OCR page {index + 1} is not an object")
        markdown = page.get("markdown")
        if isinstance(markdown, dict):
            text = markdown.get("text")
        elif isinstance(markdown, str):
            text = markdown
        else:
            text = None
        if not isinstance(text, str):
            raise OcrValidationError(f"OCR page {index + 1} has no explicit markdown text")
        texts.append(text)
        page_number = next(
            (page.get(key) for key in ("page_number", "pageNumber", "page_index", "pageIndex") if key in page),
            None,
        )
        if page_number is not None:
            try:
                page_numbers.append(int(page_number))
                saw_page_number = True
            except (TypeError, ValueError) as exc:
                raise OcrValidationError(f"OCR page {index + 1} has invalid page number") from exc
    if saw_page_number and page_numbers != list(range(page_numbers[0], page_numbers[0] + expected_pages)):
        raise OcrValidationError(f"OCR page numbers are not ordered and unique: {page_numbers}")
    return texts, page_numbers, True


def validate_ocr_payload(
    payload: object,
    *,
    expected_pages: int,
    page_start: int = 1,
) -> tuple[str, dict[str, object]]:
    texts, explicit_page_numbers, page_evidence = _extract_page_texts(payload, expected_pages)
    if not page_evidence and expected_pages != 1:
        raise OcrValidationError("OCR result lacks page-level completeness evidence")
    markdown = "\n\n".join(text.rstrip() for text in texts) + "\n"
    blank_pages = [page_start + index for index, text in enumerate(texts) if not text.strip()]
    returned_pages = list(range(page_start, page_start + expected_pages))
    if explicit_page_numbers:
        expected_page_numbers = list(range(page_start, page_start + expected_pages))
        if explicit_page_numbers != expected_page_numbers:
            raise OcrValidationError(
                f"OCR page numbers do not match requested range: expected {expected_page_numbers}, "
                f"returned {explicit_page_numbers}"
            )
        returned_pages = explicit_page_numbers
    validation = {
        "complete": True,
        "expected_pages": expected_pages,
        "returned_pages": len(texts),
        "page_numbers": returned_pages,
        "missing_pages": [],
        "duplicate_pages": [],
        "blank_pages": blank_pages,
        "page_evidence": page_evidence,
    }
    return markdown, validation


def build_markdown_from_result(
    result_path: Path,
    markdown_path: Path | None = None,
    *,
    expected_pages: int | None = None,
    page_start: int = 1,
) -> dict[str, object]:
    if result_path.stat().st_size > MAX_RESULT_BYTES:
        raise OcrValidationError(f"OCR result exceeds {MAX_RESULT_BYTES} bytes")
    try:
        payload = json.loads(result_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise OcrValidationError(f"Invalid OCR result JSON: {exc}") from exc
    if expected_pages is None:
        expected_pages = 1
    markdown, validation = validate_ocr_payload(payload, expected_pages=expected_pages, page_start=page_start)
    if markdown_path is not None:
        write_text_atomic(markdown_path, markdown)
    return {"markdown": markdown, "validation": validation}
