import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


SCRIPT_PATH = Path("/Users/wangbo5/.codex/skills/url-pdf-download-ocr/scripts/benchmark.py")


def load_module():
    spec = importlib.util.spec_from_file_location("url_pdf_benchmark", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class BenchmarkTests(unittest.TestCase):
    def test_load_cases_preserves_expectations_and_notes(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as tmpdir:
            manifest_path = Path(tmpdir) / "cases.json"
            manifest_path.write_text(
                json.dumps(
                    [
                        {
                            "name": "sample-1",
                            "url": "https://example.com/a.pdf",
                            "input_type": "direct_pdf",
                            "expected_provider": "HubSpot",
                            "expected_outcome": "success",
                            "notes": "known good sample",
                        }
                    ]
                ),
                encoding="utf-8",
            )

            cases = module.load_cases(manifest_path)

        self.assertEqual(cases[0]["expected_provider"], "HubSpot")
        self.assertEqual(cases[0]["expected_outcome"], "success")
        self.assertEqual(cases[0]["notes"], "known good sample")

    def test_evaluate_case_expectations_flags_regressions(self):
        module = load_module()
        case = {
            "name": "sample-1",
            "expected_provider": "HubSpot",
            "expected_outcome": "success",
            "notes": "baseline should stay green",
        }
        result = {
            "status": "download_failed",
            "metrics": {"provider": "unknown"},
        }

        evaluated = module.evaluate_case_expectations(case, result)

        self.assertTrue(evaluated["is_regression"])
        self.assertEqual(
            evaluated["regression_reasons"],
            ["provider_mismatch", "outcome_mismatch"],
        )
        self.assertEqual(evaluated["expected_provider"], "HubSpot")
        self.assertEqual(evaluated["expected_outcome"], "success")
        self.assertEqual(evaluated["notes"], "baseline should stay green")

    def test_summarize_results_counts_success_and_latency(self):
        module = load_module()
        results = [
            {
                "name": "ok-1",
                "status": "ok",
                "pdf_created": True,
                "md_created": True,
                "metrics": {
                    "total_ms": 1200,
                    "provider": "HubSpot",
                    "ocr_cache_hit": True,
                    "candidate_count": 1,
                    "candidate_probe_count": 1,
                    "resolved_via": "candidate",
                    "download_total_ms": 1100,
                    "final_download_ms": 200,
                },
                "input_type": "hubspot",
                "expected_provider": "HubSpot",
                "expected_outcome": "success",
                "is_regression": False,
                "regression_reasons": [],
            },
            {
                "name": "pdf-only",
                "status": "ocr_failed",
                "pdf_created": True,
                "md_created": False,
                "metrics": {
                    "total_ms": 2400,
                    "failure_stage": "ocr",
                    "failure_reason": "ocr_configuration",
                    "provider": "Dropbox",
                    "ocr_cache_hit": False,
                    "candidate_count": 3,
                    "candidate_probe_count": 2,
                    "resolved_via": "candidate",
                    "download_total_ms": 2100,
                    "final_download_ms": 500,
                },
                "input_type": "share",
                "expected_provider": "Dropbox",
                "expected_outcome": "success",
                "is_regression": True,
                "regression_reasons": ["outcome_mismatch"],
            },
            {
                "name": "download-failed",
                "status": "download_failed",
                "pdf_created": False,
                "md_created": False,
                "metrics": {
                    "total_ms": 600,
                    "failure_stage": "download",
                    "failure_reason": "authentication_or_interactive",
                    "provider": "Dropbox",
                    "candidate_count": 4,
                    "candidate_probe_count": 4,
                    "resolved_via": "unknown",
                },
                "input_type": "share",
                "expected_provider": "Dropbox",
                "expected_outcome": "download_failed",
                "is_regression": False,
                "regression_reasons": [],
            },
        ]

        summary = module.summarize_results(results)

        self.assertEqual(summary["total_cases"], 3)
        self.assertEqual(summary["pdf_success_count"], 2)
        self.assertEqual(summary["full_success_count"], 1)
        self.assertEqual(summary["download_failure_count"], 1)
        self.assertEqual(summary["ocr_failure_count"], 1)
        self.assertEqual(summary["crash_count"], 0)
        self.assertEqual(summary["regression_count"], 1)
        self.assertEqual(summary["regression_reason_counts"]["outcome_mismatch"], 1)
        self.assertEqual(summary["failure_reason_counts"]["authentication_or_interactive"], 1)
        self.assertEqual(summary["failure_reason_counts"]["ocr_configuration"], 1)
        self.assertEqual(summary["timing_ms"]["p50_total_ms"], 1200)
        self.assertEqual(summary["timing_ms"]["p90_total_ms"], 2400)
        self.assertEqual(summary["ocr_cache_hit_count"], 1)
        self.assertEqual(summary["ocr_cache_eligible_count"], 2)
        self.assertEqual(summary["ocr_cache_hit_rate"], 0.5)
        self.assertEqual(summary["download_efficiency"]["avg_candidate_count"], 2.6667)
        self.assertEqual(summary["download_efficiency"]["avg_candidate_probe_count"], 2.3333)
        self.assertEqual(summary["download_efficiency"]["resolved_via_counts"]["candidate"], 2)
        self.assertEqual(summary["download_efficiency"]["resolved_via_counts"]["unknown"], 1)
        self.assertEqual(summary["download_efficiency"]["timing_ms"]["p50_download_total_ms"], 1100)
        self.assertEqual(summary["download_efficiency"]["timing_ms"]["p90_download_total_ms"], 2100)
        self.assertEqual(summary["by_input_type"]["share"]["count"], 2)
        self.assertEqual(summary["by_provider"]["Dropbox"]["count"], 2)
        self.assertEqual(summary["by_provider"]["Dropbox"]["download_failure_count"], 1)
        self.assertEqual(summary["by_provider"]["Dropbox"]["regression_count"], 1)
        self.assertEqual(summary["by_provider"]["Dropbox"]["failure_reason_counts"]["authentication_or_interactive"], 1)
        self.assertEqual(summary["by_provider"]["Dropbox"]["failure_reason_counts"]["ocr_configuration"], 1)
        self.assertEqual(summary["by_provider"]["Dropbox"]["avg_candidate_probe_count"], 3.0)
        self.assertEqual(summary["by_provider"]["Dropbox"]["resolved_via_counts"]["unknown"], 1)
        self.assertEqual(summary["by_provider"]["HubSpot"]["ocr_cache_hit_count"], 1)
        self.assertEqual(summary["provider_rule_candidates"][0]["provider"], "Dropbox")
        self.assertEqual(summary["provider_rule_candidates"][0]["top_failure_reason"], "authentication_or_interactive")
        self.assertEqual(summary["provider_rule_candidates"][0]["priority_tier"], "low")
        self.assertIn("blocker detection", summary["provider_rule_candidates"][0]["recommendation"])


if __name__ == "__main__":
    unittest.main()
