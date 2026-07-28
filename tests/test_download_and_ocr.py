import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pypdf import PdfWriter


SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import download_and_ocr as main_mod
import http_download
import ocr_cache
import ocr_runner
import providers


class DownloadAndOcrTests(unittest.TestCase):
    def test_calculate_chunk_size_scales_with_document_density(self):
        self.assertEqual(ocr_runner.calculate_chunk_size(total_pages=423, file_size_bytes=37 * 1024 * 1024), 20)
        self.assertEqual(ocr_runner.calculate_chunk_size(total_pages=200, file_size_bytes=8 * 1024 * 1024), 50)
        self.assertEqual(ocr_runner.calculate_chunk_size(total_pages=80, file_size_bytes=2 * 1024 * 1024), 80)

    def test_initialize_job_state_creates_chunk_plan_and_paths(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            pdf_path = Path(tmpdir) / "sample.pdf"
            writer = PdfWriter()
            for _ in range(5):
                writer.add_blank_page(width=72, height=72)
            with pdf_path.open("wb") as fh:
                writer.write(fh)

            job_dir = Path(tmpdir) / "job"
            state = ocr_runner.initialize_or_load_job_state(
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

    def test_invalid_persisted_job_state_is_rebuilt(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            job_dir = root / "job"
            job_dir.mkdir()
            (job_dir / "status.json").write_text("[]", encoding="utf-8")

            state = ocr_runner.initialize_or_load_job_state(
                pdf_path=root / "missing.pdf",
                pdf_hash="abc123",
                total_pages=2,
                file_size_bytes=10,
                chunk_size=1,
                job_dir=job_dir,
            )

            self.assertIsInstance(state, dict)
            self.assertEqual(len(state["chunks"]), 2)
            self.assertEqual(state["chunks"][0]["status"], "pending")

    def test_cache_purge_preserves_active_job_files(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config = ocr_runner.OcrConfig(cache_root=root / "cache")
            active_job = config.job_root / "pdf-hash" / "identity-hash"
            active_job.mkdir(parents=True)
            (active_job / "status.json").write_text(
                json.dumps({"status": "running"}),
                encoding="utf-8",
            )
            active_file = active_job / "chunks" / "chunk_001.md"
            active_file.parent.mkdir()
            active_file.write_bytes(b"active")

            completed_entry = config.cache_root / "entries" / "old-pdf" / "old-identity"
            completed_entry.mkdir(parents=True)
            (completed_entry / "artifact.md").write_bytes(b"completed")

            with mock.patch.object(ocr_cache, "CACHE_MAX_BYTES", 1):
                ocr_cache.purge_cache(config.cache_root, config.job_root)

            self.assertTrue(active_file.exists())
            self.assertFalse((completed_entry / "artifact.md").exists())

    def test_pending_chunks_skip_completed_entries(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            markdown_path = Path(tmpdir) / "chunk_001.md"
            markdown_path.write_text("page 1\n", encoding="utf-8")
            state = {
                "chunks": [
                    {
                        "index": 1,
                        "start_page": 1,
                        "end_page": 1,
                        "status": "completed",
                        "markdown_path": str(markdown_path),
                        "validation": {
                            "complete": True,
                            "expected_pages": 1,
                            "returned_pages": 1,
                            "page_numbers": [1],
                            "missing_pages": [],
                            "duplicate_pages": [],
                            "blank_pages": [],
                            "page_evidence": True,
                        },
                        "markdown_sha256": ocr_runner.file_sha256(markdown_path),
                    },
                    {"index": 2, "status": "failed"},
                    {"index": 3, "status": "pending"},
                ]
            }

            pending = ocr_runner.get_pending_chunks(state)

        self.assertEqual([chunk["index"] for chunk in pending], [2, 3])

    def test_pending_chunks_requeue_completed_chunk_if_markdown_missing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            existing_md = Path(tmpdir) / "chunk_001.md"
            existing_md.write_text("ok\n", encoding="utf-8")
            missing_md = Path(tmpdir) / "chunk_002.md"
            state = {
                "chunks": [
                    {
                        "index": 1,
                        "start_page": 1,
                        "end_page": 1,
                        "status": "completed",
                        "markdown_path": str(existing_md),
                        "validation": {
                            "complete": True,
                            "expected_pages": 1,
                            "returned_pages": 1,
                            "page_numbers": [1],
                            "missing_pages": [],
                            "duplicate_pages": [],
                            "blank_pages": [],
                            "page_evidence": True,
                        },
                        "markdown_sha256": ocr_runner.file_sha256(existing_md),
                    },
                    {"index": 2, "status": "completed", "markdown_path": str(missing_md)},
                ]
            }

            pending = ocr_runner.get_pending_chunks(state)

        self.assertEqual([chunk["index"] for chunk in pending], [2])

    def test_download_failure_returns_structured_json(self):
        stdout = io.StringIO()
        stderr = io.StringIO()

        with tempfile.TemporaryDirectory() as tmpdir:
            script_path = SCRIPTS_DIR / "download_and_ocr.py"
            argv = [
                str(script_path),
                "https://example.com/report",
                "--output-dir",
                tmpdir,
            ]
            with mock.patch.object(sys, "argv", argv):
                with mock.patch.object(main_mod, "download_pdf", side_effect=RuntimeError("network boom")):
                    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                        exit_code = main_mod.main()

        self.assertEqual(exit_code, 1)
        self.assertEqual(stderr.getvalue(), "")
        payload = json.loads(stdout.getvalue())
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["download_error"], "network boom")
        self.assertEqual(payload["metrics"]["failure_stage"], "download")
        self.assertEqual(payload["metrics"]["failure_reason"], "network")
        self.assertEqual(payload["metrics"]["source_url"], main_mod.redact_url("https://example.com/report"))

    def test_download_failure_preserves_resolution_metrics(self):
        stdout = io.StringIO()
        stderr = io.StringIO()
        error = http_download.NetworkPolicyError("Refusing non-public destination")
        error.metrics = {
            "candidate_count": 1,
            "candidate_probe_count": 1,
            "candidate_attempts": [
                {
                    "index": 1,
                    "url": "https://example.test/<redacted-path:abc>",
                    "outcome": "error",
                    "error_code": "network_policy",
                }
            ],
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            with mock.patch.object(main_mod, "download_pdf", side_effect=error):
                with mock.patch.object(sys, "argv", ["download_and_ocr.py", "https://example.test/page", "--output-dir", tmpdir]):
                    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                        exit_code = main_mod.main()

        self.assertEqual(exit_code, 1)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["metrics"]["candidate_probe_count"], 1)
        self.assertEqual(payload["metrics"]["failure_reason"], "network_policy")

    def test_download_pdf_attaches_candidate_attempts_to_resolution_error(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir)
            html_path = output_dir / "html.part"
            html_path.write_text("<a href='https://example.test/report.pdf'>download</a>", encoding="utf-8")
            initial = http_download.ProbeResult(
                url="https://example.test/page",
                final_url="https://example.test/page",
                headers={"content-type": "text/html"},
                body=html_path.read_bytes(),
                is_pdf=False,
                is_html=True,
                staging_path=html_path,
            )
            with mock.patch.object(
                main_mod,
                "probe_url",
                side_effect=[initial, http_download.NetworkPolicyError("blocked candidate")],
            ):
                with self.assertRaises(RuntimeError) as raised:
                    main_mod.download_pdf("https://example.test/page", output_dir)

            self.assertEqual(raised.exception.metrics["candidate_probe_count"], 1)
            self.assertEqual(
                raised.exception.metrics["candidate_attempts"][0]["error_code"],
                "network_policy",
            )

    def test_success_output_redacts_paddle_stderr_note(self):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            pdf_path = root / "report.pdf"
            md_path = root / "report.ocr.md"
            md_stage = root / ".report.ocr.md.part"
            pdf_path.write_bytes(b"%PDF-")
            md_stage.write_text("page\n", encoding="utf-8")
            with mock.patch.object(
                main_mod,
                "download_pdf",
                return_value=(pdf_path, "https://example.test/<redacted-path:abc>", {}),
            ):
                with mock.patch.object(
                    main_mod,
                    "run_paddleocr",
                    return_value=ocr_runner.OcrRunResult(
                        md_stage,
                        "download https://example.test/private?token=secret",
                        False,
                        "hash",
                        {},
                    ),
                ):
                    with mock.patch.object(sys, "argv", ["download_and_ocr.py", "https://example.test/page", "--output-dir", tmpdir]):
                        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                            exit_code = main_mod.main()

        self.assertEqual(exit_code, 0)
        payload = json.loads(stdout.getvalue())
        self.assertNotIn("token=secret", payload["paddle_note"])

    def test_download_pdf_rejects_saved_html_from_pdf_url(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir)
            fake_pdf_path = output_dir / ".http-result.part"
            fake_pdf_path.write_text("<html>login required</html>\n", encoding="utf-8")

            with mock.patch.object(
                main_mod,
                "probe_url",
                return_value=http_download.ProbeResult(
                    url="https://example.com/report.pdf",
                    final_url="https://example.com/report.pdf",
                    headers={"content-type": "application/pdf"},
                    body=b"",
                    is_pdf=True,
                    is_html=False,
                    staging_path=fake_pdf_path,
                ),
            ):
                with self.assertRaisesRegex(RuntimeError, "PDF validation failed"):
                    main_mod.download_pdf("https://example.com/report.pdf", output_dir)

            self.assertFalse((output_dir / "report.pdf").exists())

    def test_whitespace_only_ocr_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            result_path = Path(tmpdir) / "result.json"
            result_path.write_text(json.dumps({"text": "   "}), encoding="utf-8")
            with self.assertRaises(ocr_runner.OcrValidationError):
                ocr_runner.build_markdown_from_result(result_path, expected_pages=1)

    def test_ocr_requires_explicit_page_coverage(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            result_path = Path(tmpdir) / "result.json"
            result_path.write_text(
                json.dumps({"result": {"result": {"layoutParsingResults": [
                    {"markdown": {"text": "page 1"}}
                ]}}}),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ocr_runner.OcrValidationError, "coverage mismatch"):
                ocr_runner.build_markdown_from_result(result_path, expected_pages=2)

    def test_explicit_blank_page_is_validated_not_dropped(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            result_path = Path(tmpdir) / "result.json"
            markdown_path = Path(tmpdir) / "out.md"
            result_path.write_text(
                json.dumps({"result": {"result": {"layoutParsingResults": [
                    {"markdown": {"text": ""}},
                    {"markdown": {"text": "page 2"}},
                ]}}}),
                encoding="utf-8",
            )
            validation = ocr_runner.build_markdown_from_result(
                result_path,
                markdown_path,
                expected_pages=2,
            )
            self.assertEqual(validation["validation"]["blank_pages"], [1])
            self.assertTrue(markdown_path.exists())

    def test_explicit_page_numbers_must_match_requested_range(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            result_path = Path(tmpdir) / "result.json"
            result_path.write_text(
                json.dumps({"result": {"result": {"layoutParsingResults": [
                    {"page_number": 1, "markdown": {"text": "page 1"}},
                    {"page_number": 2, "markdown": {"text": "page 2"}},
                ]}}}),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ocr_runner.OcrValidationError, "requested range"):
                ocr_runner.build_markdown_from_result(
                    result_path,
                    expected_pages=2,
                    page_start=5,
                )

    def test_duplicate_page_numbers_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            result_path = Path(tmpdir) / "result.json"
            result_path.write_text(
                json.dumps({"result": {"result": {"layoutParsingResults": [
                    {"page_number": 1, "markdown": {"text": "page 1"}},
                    {"page_number": 1, "markdown": {"text": "duplicate"}},
                ]}}}),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ocr_runner.OcrValidationError, "ordered and unique"):
                ocr_runner.build_markdown_from_result(
                    result_path,
                    expected_pages=2,
                )

    def test_invalid_whitespace_cache_manifest_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            markdown_path = Path(tmpdir) / "artifact.md"
            markdown_path.write_text("\n", encoding="utf-8")
            validation = {
                "complete": True,
                "expected_pages": 1,
                "returned_pages": 1,
                "page_numbers": [1],
                "missing_pages": [],
                "duplicate_pages": [],
                "blank_pages": [],
                "page_evidence": False,
            }
            manifest = {
                "schema_id": "url-pdf-download-ocr.artifact-manifest",
                "schema_version": ocr_runner.CACHE_SCHEMA_VERSION,
                "status": "completed",
                "pdf_sha256": "abc",
                "total_pages": 1,
                "expires_at": 0,
                "markdown_sha256": ocr_runner.file_sha256(markdown_path),
                "validation": validation,
            }
            self.assertFalse(
                ocr_runner._manifest_valid(
                    manifest,
                    pdf_hash="abc",
                    expected_pages=1,
                    markdown_path=markdown_path,
                )
            )

    def test_cache_identity_separates_scripts_and_survives_script_removal(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            pdf_path = root / "sample.pdf"
            writer = PdfWriter()
            writer.add_blank_page(width=72, height=72)
            with pdf_path.open("wb") as fh:
                writer.write(fh)

            script_a = root / "ocr_a.py"
            script_b = root / "ocr_b.py"
            script_template = """import json, sys\nargs = sys.argv\nout = args[args.index('--output') + 1]\ntext = {text!r}\npayload = {{'ok': True, 'text': text, 'result': {{'result': {{'layoutParsingResults': [{{'markdown': {{'text': text}}}}]}}}}}}\nwith open(out, 'w', encoding='utf-8') as fh: json.dump(payload, fh)\n"""
            script_a.write_text(script_template.format(text="MODEL A"), encoding="utf-8")
            script_b.write_text(script_template.format(text="MODEL B"), encoding="utf-8")

            config = ocr_runner.OcrConfig(cache_root=root / "cache")
            first_md = root / "first.md"
            _, _, first_hit, _, _ = ocr_runner.run_paddleocr(
                pdf_path, str(script_a), markdown_path=first_md, config=config
            )
            self.assertFalse(first_hit)
            self.assertEqual(first_md.read_text(encoding="utf-8").strip(), "MODEL A")

            script_a.unlink()
            cached_md = root / "cached.md"
            _, _, cached_hit, _, _ = ocr_runner.run_paddleocr(
                pdf_path, str(script_a), markdown_path=cached_md, config=config
            )
            self.assertTrue(cached_hit)
            self.assertEqual(cached_md.read_text(encoding="utf-8").strip(), "MODEL A")

            fresh_md = root / "fresh.md"
            _, _, fresh_hit, _, _ = ocr_runner.run_paddleocr(
                pdf_path, str(script_b), markdown_path=fresh_md, config=config
            )
            self.assertFalse(fresh_hit)
            self.assertEqual(fresh_md.read_text(encoding="utf-8").strip(), "MODEL B")

    def test_chunk_timeout_is_retried(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            pdf_path = root / "large.pdf"
            writer = PdfWriter()
            for _ in range(101):
                writer.add_blank_page(width=72, height=72)
            with pdf_path.open("wb") as fh:
                writer.write(fh)
            fake_script = root / "fake.py"
            fake_script.write_text("# patched subprocess\n", encoding="utf-8")

            calls = {"count": 0}

            def fake_subprocess(**kwargs):
                calls["count"] += 1
                if calls["count"] == 1:
                    raise RuntimeError("PaddleOCR timed out after 1s")
                input_pages = len(__import__("pypdf").PdfReader(str(kwargs["input_pdf_path"])).pages)
                payload = {
                    "ok": True,
                    "result": {
                        "result": {
                            "layoutParsingResults": [
                                {"markdown": {"text": f"page {index}"}}
                                for index in range(input_pages)
                            ]
                        }
                    },
                }
                kwargs["result_path"].write_text(json.dumps(payload), encoding="utf-8")
                return subprocess.CompletedProcess([], 0, "", "")

            config = ocr_runner.OcrConfig(cache_root=root / "cache")
            with mock.patch.object(ocr_runner, "run_paddleocr_subprocess", side_effect=fake_subprocess):
                result = ocr_runner.run_paddleocr(
                    pdf_path,
                    str(fake_script),
                    markdown_path=root / "large.md",
                    use_cache=False,
                    config=config,
                )
            self.assertEqual(calls["count"], 3)
            self.assertEqual(result[4]["ocr_total_pages"], 101)
            self.assertFalse((root / "cache" / "jobs").exists())

    def test_classify_failure_reason_maps_common_errors(self):
        self.assertEqual(
            ocr_runner.classify_failure_reason("The page may require login, an extraction code, or interactive JavaScript."),
            "authentication_or_interactive",
        )
        self.assertEqual(
            ocr_runner.classify_failure_reason("curl timed out after 45s"),
            "timeout",
        )
        self.assertEqual(
            ocr_runner.classify_failure_reason("PaddleOCR script not found: /tmp/x"),
            "ocr_configuration",
        )
        self.assertEqual(
            ocr_runner.classify_failure_reason(
                "Refusing non-public destination",
                error_code="network_policy",
            ),
            "network_policy",
        )
        self.assertEqual(
            ocr_runner.classify_failure_reason(
                "OCR page has no explicit markdown",
                error_code="ocr_incomplete",
            ),
            "ocr_incomplete",
        )

    def test_resolve_paddle_script_prefers_cli_then_env_then_default(self):
        with mock.patch.dict(os.environ, {"URL_PDF_DOWNLOAD_OCR_PADDLE_SCRIPT": "/tmp/from-env.py"}, clear=False):
            self.assertEqual(
                ocr_runner.resolve_paddle_script("/tmp/from-cli.py"),
                Path("/tmp/from-cli.py"),
            )
            self.assertEqual(
                ocr_runner.resolve_paddle_script(None),
                Path("/tmp/from-env.py"),
            )

    def test_no_cache_does_not_create_persistent_cache_metadata(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            pdf_path = root / "sample.pdf"
            writer = PdfWriter()
            writer.add_blank_page(width=72, height=72)
            with pdf_path.open("wb") as fh:
                writer.write(fh)
            script = root / "ocr.py"
            script.write_text(
                """import json, sys
args = sys.argv
out = args[args.index('--output') + 1]
payload = {'result': {'result': {'layoutParsingResults': [{'markdown': {'text': 'ok'}}]}}}
with open(out, 'w', encoding='utf-8') as fh: json.dump(payload, fh)
""",
                encoding="utf-8",
            )

            config = ocr_runner.OcrConfig(cache_root=root / "cache")
            ocr_runner.run_paddleocr(
                pdf_path,
                str(script),
                markdown_path=root / "out.md",
                use_cache=False,
                config=config,
            )
            self.assertFalse(config.cache_root.exists())


if __name__ == "__main__":
    unittest.main()
