import json
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock
from urllib.parse import urlparse

from pypdf import PdfWriter


SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import benchmark
import download_and_ocr
import http_download
import providers
import url_utils


class SecurityBoundaryTests(unittest.TestCase):
    def test_network_policy_rejects_loopback_and_private_addresses(self):
        policy = http_download.NetworkPolicy()
        with self.assertRaises(http_download.NetworkPolicyError):
            policy.validate_url("http://example.com/report.pdf")
        with self.assertRaises(http_download.NetworkPolicyError):
            policy.validate_url("https://127.0.0.1/secret")
        with mock.patch.object(
            http_download.socket,
            "getaddrinfo",
            return_value=[(2, 1, 6, "", ("10.0.0.7", 80))],
        ):
            with self.assertRaises(http_download.NetworkPolicyError):
                policy.validate_url("https://example.test/secret")

    def test_cross_origin_candidates_are_rejected_by_default(self):
        policy = http_download.NetworkPolicy()
        with self.assertRaises(http_download.NetworkPolicyError):
            policy.validate_url(
                "https://cdn.example.test/report.pdf",
                parent_url="https://reports.example.test/page",
                allow_cross_origin=False,
            )

    def test_redirect_destination_is_revalidated(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            response_path = Path(tmpdir) / "redirect.body"
            response_path.write_bytes(b"redirect")

            def resolve(_self, url):
                if urlparse(url).hostname == "private.test":
                    raise http_download.NetworkPolicyError("private redirect")
                return ["127.0.0.1"]

            response = http_download._Response(
                status=302,
                headers={"location": "https://private.test/secret"},
                path=response_path,
                truncated=False,
                request_url="https://public.test/start",
            )
            policy = http_download.NetworkPolicy()
            with mock.patch.object(
                http_download.NetworkPolicy,
                "_resolve_public_addresses",
                new=resolve,
            ):
                with mock.patch.object(http_download, "_request_once", return_value=response) as request:
                    with self.assertRaises(http_download.NetworkPolicyError):
                        http_download.fetch_url(
                            "https://public.test/start",
                            policy=policy,
                            staging_dir=Path(tmpdir),
                        )
            request.assert_called_once()

    def test_local_direct_pdf_is_fetched_once_before_publication(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            source_pdf = Path(tmpdir) / "source.pdf"
            writer = PdfWriter()
            writer.add_blank_page(width=72, height=72)
            with source_pdf.open("wb") as fh:
                writer.write(fh)
            body = source_pdf.read_bytes()
            requests: list[str] = []

            class Handler(BaseHTTPRequestHandler):
                def do_GET(self):
                    requests.append(self.path)
                    self.send_response(200)
                    self.send_header("Content-Type", "application/pdf")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)

                def log_message(self, *_args):
                    pass

            server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                source_url = f"http://public.test:{server.server_port}/report.pdf"
                with mock.patch.object(
                    http_download.NetworkPolicy,
                    "_resolve_public_addresses",
                    return_value=["127.0.0.1"],
                ):
                    pdf_path, _, metrics = download_and_ocr.download_pdf(
                        source_url,
                        Path(tmpdir) / "downloads",
                        allow_http=True,
                    )
                self.assertEqual(requests, ["/report.pdf"])
                self.assertEqual(metrics["resolved_pdf_pages"], 1)
                self.assertEqual(pdf_path.name, "report.pdf")
            finally:
                server.shutdown()
                server.server_close()

    def test_provider_matching_and_query_rewrite_preserve_unknown_pairs(self):
        self.assertIsNone(providers.detect_provider_label("https://evil-dropbox.com/file"))
        self.assertIsNone(providers.detect_provider_label("https://dropbox.com.evil.test/file"))
        rewritten = providers.replace_query(
            "https://www.dropbox.com/s/x/report.pdf?x=1&x=2&sig=a%2Bb&dl=0",
            {"dl": "1"},
        )
        self.assertIn("x=1&x=2&sig=a%2Bb&dl=1", rewritten)

    def test_url_redaction_keeps_origin_but_not_path_or_query_tokens(self):
        self.assertIs(providers.UrlValidationError, url_utils.UrlValidationError)
        redacted = providers.redact_url(
            "https://example.test/customer-secret/report.pdf?signature=secret-token"
        )
        self.assertIn("https://example.test/", redacted)
        self.assertNotIn("customer-secret", redacted)
        self.assertNotIn("secret-token", redacted)
        generic_redacted = providers.redact_text("blocked ftp://example.test/private?token=secret")
        self.assertNotIn("private", generic_redacted)
        self.assertNotIn("token=secret", generic_redacted)

    def test_filename_parsing_keeps_rfc5987_and_unicode_names(self):
        self.assertEqual(
            providers.extract_filename(
                {"content-disposition": "attachment; filename*=UTF-8''report%20x.pdf"},
                "https://example.test/y.pdf",
            ),
            "report x.pdf",
        )
        self.assertEqual(
            providers.extract_filename(
                {"content-disposition": 'attachment; filename="报告.pdf"'},
                "https://example.test/y.pdf",
            ),
            "报告.pdf",
        )

    def test_output_pair_avoids_existing_markdown_and_symlinks(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "report.md").write_text("keep", encoding="utf-8")
            target = root / "target.md"
            target.write_text("keep target", encoding="utf-8")
            (root / "report.ocr.md").symlink_to(target)
            pdf_path, markdown_path = providers.output_paths(root, "report.pdf")
            self.assertEqual(pdf_path.name, "report-2.pdf")
            self.assertEqual(markdown_path.name, "report-2.ocr.md")
            self.assertEqual(target.read_text(encoding="utf-8"), "keep target")

    def test_legacy_unique_path_helper_protects_markdown_pair(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "report.md").write_text("keep", encoding="utf-8")

            unique = providers.ensure_unique_path(root / "report.pdf")

            self.assertEqual(unique.name, "report-2.pdf")

    def test_publish_no_clobber_does_not_follow_output_symlink(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = root / "target.txt"
            target.write_text("keep", encoding="utf-8")
            destination = root / "report.pdf"
            destination.symlink_to(target)
            staging = root / ".report.pdf.part"
            staging.write_bytes(b"new")

            with self.assertRaisesRegex(RuntimeError, "Output already exists"):
                download_and_ocr.publish_staged_file(staging, destination)

            self.assertTrue(destination.is_symlink())
            self.assertEqual(target.read_text(encoding="utf-8"), "keep")
            self.assertFalse(staging.exists())

    def test_benchmark_rejects_path_like_case_names(self):
        module = benchmark
        with tempfile.TemporaryDirectory() as tmpdir:
            manifest = Path(tmpdir) / "cases.json"
            manifest.write_text(
                json.dumps([{"name": "../../outside", "url": "https://example.test/report.pdf"}]),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(RuntimeError, "display-only"):
                module.load_cases(manifest)

    def test_benchmark_marks_missing_artifacts_as_failure(self):
        case = {
            "name": "case",
            "url": "https://example.test/report.pdf",
            "input_type": "direct_pdf",
            "expected_provider": None,
            "expected_outcome": "success",
            "notes": None,
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            with mock.patch.object(
                benchmark,
                "_run_benchmark_process",
                return_value=benchmark.subprocess.CompletedProcess(
                    [],
                    0,
                    json.dumps({"ok": True, "pdf_path": "/outside/report.pdf", "md_path": "/outside/report.ocr.md", "metrics": {}}),
                    "",
                ),
            ):
                result = benchmark.run_case(case, Path(tmpdir))
        self.assertEqual(result["status"], "artifact_failed")
        self.assertTrue(result["artifact_errors"])


if __name__ == "__main__":
    unittest.main()
