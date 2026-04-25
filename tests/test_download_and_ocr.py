import contextlib
import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pypdf import PdfWriter


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "scripts" / "download_and_ocr.py"


def load_module():
    spec = importlib.util.spec_from_file_location("url_pdf_download_ocr", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class DownloadAndOcrTests(unittest.TestCase):
    def test_calculate_chunk_size_scales_with_document_density(self):
        module = load_module()

        self.assertEqual(module.calculate_chunk_size(total_pages=423, file_size_bytes=37 * 1024 * 1024), 20)
        self.assertEqual(module.calculate_chunk_size(total_pages=200, file_size_bytes=8 * 1024 * 1024), 50)
        self.assertEqual(module.calculate_chunk_size(total_pages=80, file_size_bytes=2 * 1024 * 1024), 80)

    def test_initialize_job_state_creates_chunk_plan_and_paths(self):
        module = load_module()

        with tempfile.TemporaryDirectory() as tmpdir:
            pdf_path = Path(tmpdir) / "sample.pdf"
            writer = PdfWriter()
            for _ in range(5):
                writer.add_blank_page(width=72, height=72)
            with pdf_path.open("wb") as fh:
                writer.write(fh)

            job_dir = Path(tmpdir) / "job"
            state = module.initialize_or_load_job_state(
                pdf_path=pdf_path,
                pdf_hash="abc123",
                total_pages=5,
                file_size_bytes=pdf_path.stat().st_size,
                chunk_size=2,
                job_dir=job_dir,
            )

            self.assertEqual(state["pdf_hash"], "abc123")
            self.assertEqual(state["total_pages"], 5)
            self.assertEqual(len(state["chunks"]), 3)
            self.assertEqual(state["chunks"][0]["start_page"], 1)
            self.assertEqual(state["chunks"][0]["end_page"], 2)
            self.assertEqual(state["chunks"][2]["start_page"], 5)
            self.assertEqual(state["chunks"][2]["end_page"], 5)
            self.assertTrue((job_dir / "status.json").exists())

    def test_pending_chunks_skip_completed_entries(self):
        module = load_module()
        state = {
            "chunks": [
                {"index": 1, "status": "completed"},
                {"index": 2, "status": "failed"},
                {"index": 3, "status": "pending"},
            ]
        }

        pending = module.get_pending_chunks(state)

        self.assertEqual([chunk["index"] for chunk in pending], [2, 3])

    def test_pending_chunks_requeue_completed_chunk_if_markdown_missing(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as tmpdir:
            existing_md = Path(tmpdir) / "chunk_001.md"
            existing_md.write_text("ok\n", encoding="utf-8")
            missing_md = Path(tmpdir) / "chunk_002.md"
            state = {
                "chunks": [
                    {"index": 1, "status": "completed", "markdown_path": str(existing_md)},
                    {"index": 2, "status": "completed", "markdown_path": str(missing_md)},
                ]
            }

            pending = module.get_pending_chunks(state)

        self.assertEqual([chunk["index"] for chunk in pending], [2])

    def test_download_failure_returns_structured_json(self):
        module = load_module()
        stdout = io.StringIO()
        stderr = io.StringIO()

        with tempfile.TemporaryDirectory() as tmpdir:
            argv = [
                str(SCRIPT_PATH),
                "https://example.com/report",
                "--output-dir",
                tmpdir,
            ]
            with mock.patch.object(sys, "argv", argv):
                with mock.patch.object(module, "download_pdf", side_effect=RuntimeError("network boom")):
                    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                        exit_code = module.main()

        self.assertEqual(exit_code, 1)
        self.assertEqual(stderr.getvalue(), "")
        payload = json.loads(stdout.getvalue())
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["download_error"], "network boom")
        self.assertEqual(payload["metrics"]["failure_stage"], "download")
        self.assertEqual(payload["metrics"]["failure_reason"], "network")
        self.assertEqual(payload["metrics"]["source_url"], "https://example.com/report")

    def test_download_pdf_rejects_saved_html_from_pdf_url(self):
        module = load_module()

        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir)

            def fake_stream_download(url, destination):
                destination.write_text("<html>login required</html>\n", encoding="utf-8")
                return {"content-type": "application/pdf"}, url

            with mock.patch.object(
                module,
                "probe_url",
                return_value=module.ProbeResult(
                    url="https://example.com/report.pdf",
                    final_url="https://example.com/report.pdf",
                    headers={"content-type": "application/pdf"},
                    body=b"",
                    is_pdf=True,
                    is_html=False,
                ),
            ):
                with mock.patch.object(
                    module,
                    "stream_download_to_path",
                    side_effect=fake_stream_download,
                ):
                    with self.assertRaisesRegex(RuntimeError, "valid PDF"):
                        module.download_pdf("https://example.com/report.pdf", output_dir)

            self.assertFalse((output_dir / "report.pdf").exists())

    def test_classify_failure_reason_maps_common_errors(self):
        module = load_module()

        self.assertEqual(
            module.classify_failure_reason("The page may require login, an extraction code, or interactive JavaScript."),
            "authentication_or_interactive",
        )
        self.assertEqual(
            module.classify_failure_reason("curl timed out after 45s"),
            "timeout",
        )
        self.assertEqual(
            module.classify_failure_reason("PaddleOCR script not found: /tmp/x"),
            "ocr_configuration",
        )

    def test_resolve_paddle_script_prefers_cli_then_env_then_default(self):
        module = load_module()

        with mock.patch.dict(os.environ, {"URL_PDF_DOWNLOAD_OCR_PADDLE_SCRIPT": "/tmp/from-env.py"}, clear=False):
            self.assertEqual(
                module.resolve_paddle_script("/tmp/from-cli.py"),
                Path("/tmp/from-cli.py"),
            )
            self.assertEqual(
                module.resolve_paddle_script(None),
                Path("/tmp/from-env.py"),
            )


if __name__ == "__main__":
    unittest.main()
