import contextlib
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SCRIPT_PATH = Path("/Users/wangbo5/.codex/skills/url-pdf-download-ocr/scripts/download_and_ocr.py")


def load_module():
    spec = importlib.util.spec_from_file_location("url_pdf_download_ocr", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class DownloadAndOcrTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
