"""Small named result types used across the OCR workflow."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Optional


@dataclass
class OcrRunResult:
    """Named OCR result with temporary tuple compatibility for existing callers."""

    markdown_path: Path
    paddle_note: Optional[str]
    cache_hit: bool
    pdf_hash: str
    metadata: dict[str, object]

    def as_tuple(self) -> tuple[Path, Optional[str], bool, str, dict[str, object]]:
        return (
            self.markdown_path,
            self.paddle_note,
            self.cache_hit,
            self.pdf_hash,
            self.metadata,
        )

    def __iter__(self) -> Iterator[object]:
        return iter(self.as_tuple())

    def __getitem__(self, index: int) -> object:
        return self.as_tuple()[index]
