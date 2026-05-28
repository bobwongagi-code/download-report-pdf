#!/usr/bin/env python3
import argparse
import json
import math
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Optional


SKILL_ROOT = Path(__file__).resolve().parent.parent
DOWNLOAD_SCRIPT = SKILL_ROOT / "scripts" / "download_and_ocr.py"


def percentile_nearest_rank(values: list[int], percent: int) -> Optional[int]:
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil((percent / 100) * len(ordered)))
    return ordered[rank - 1]


def average(values: list[int]) -> Optional[float]:
    if not values:
        return None
    return round(sum(values) / len(values), 4)


def normalize_expected_outcome(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    normalized = value.strip().lower().replace("-", "_")
    aliases = {
        "ok": "success",
        "pdf_and_md": "success",
        "full_success": "success",
        "download_failed": "download_failed",
        "ocr_failed": "ocr_failed",
        "pdf_only": "ocr_failed",
        "crash": "crash",
    }
    return aliases.get(normalized, normalized)


def outcome_for_result(result: dict[str, Any]) -> str:
    status = str(result.get("status") or "").strip().lower()
    if status == "ok":
        return "success"
    if status in {"download_failed", "ocr_failed", "crash"}:
        return status
    return "unknown"


def evaluate_case_expectations(case: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    expected_provider = case.get("expected_provider")
    expected_outcome = normalize_expected_outcome(case.get("expected_outcome"))
    notes = case.get("notes")
    actual_provider = result.get("metrics", {}).get("provider")
    actual_outcome = outcome_for_result(result)
    regression_reasons: list[str] = []

    if expected_provider and actual_provider != expected_provider:
        regression_reasons.append("provider_mismatch")
    if expected_outcome and actual_outcome != expected_outcome:
        regression_reasons.append("outcome_mismatch")

    enriched = dict(result)
    enriched["expected_provider"] = expected_provider
    enriched["expected_outcome"] = expected_outcome
    enriched["notes"] = notes
    enriched["actual_provider"] = actual_provider
    enriched["actual_outcome"] = actual_outcome
    enriched["is_regression"] = bool(regression_reasons)
    enriched["regression_reasons"] = regression_reasons
    return enriched


def reason_priority_weight(reason: str) -> int:
    weights = {
        "no_pdf_found": 5,
        "invalid_pdf": 4,
        "timeout": 3,
        "network": 2,
        "unknown": 2,
        "authentication_or_interactive": 1,
        "ocr_empty_output": 0,
        "ocr_configuration": 0,
    }
    return weights.get(reason, 0)


def recommendation_for_reason(reason: str) -> tuple[str, str]:
    recommendations = {
        "no_pdf_found": (
            "high",
            "Expand provider-specific direct-download rewrites and HTML/PDF candidate extraction.",
        ),
        "invalid_pdf": (
            "high",
            "Tighten provider-specific URL selection and final PDF validation before saving files.",
        ),
        "timeout": (
            "medium",
            "Add provider-specific lighter probes, shorter retry paths, or timeout-aware fallbacks.",
        ),
        "network": (
            "medium",
            "Review provider-specific redirect handling, retry behavior, and network compatibility.",
        ),
        "unknown": (
            "medium",
            "Collect a few failing samples and inspect provider-specific response patterns before adding rules.",
        ),
        "authentication_or_interactive": (
            "low",
            "Prioritize clearer blocker detection before deeper automation; this likely needs login, code, or button-driven flow.",
        ),
        "ocr_empty_output": (
            "low",
            "Improve OCR post-processing or document parsing quality rather than provider download rules.",
        ),
        "ocr_configuration": (
            "low",
            "Fix PaddleOCR environment or configuration first; provider-specific download rules will not help.",
        ),
    }
    return recommendations.get(
        reason,
        ("low", "Inspect a few failures manually before adding provider-specific rules."),
    )


def build_provider_rule_candidates(by_provider: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for provider, bucket in by_provider.items():
        failure_reasons = bucket.get("failure_reason_counts", {})
        if not failure_reasons:
            continue

        ranked_reasons = sorted(
            failure_reasons.items(),
            key=lambda item: (item[1], reason_priority_weight(item[0]), item[0]),
            reverse=True,
        )
        top_failure_reason, top_failure_count = ranked_reasons[0]
        priority_tier, recommendation = recommendation_for_reason(top_failure_reason)
        count = int(bucket.get("count", 0))
        failure_count = int(bucket.get("download_failure_count", 0)) + int(bucket.get("ocr_failure_count", 0))
        priority_score = failure_count * max(1, reason_priority_weight(top_failure_reason))
        failure_rate = round(failure_count / count, 4) if count else None

        candidates.append(
            {
                "provider": provider,
                "count": count,
                "failure_count": failure_count,
                "failure_rate": failure_rate,
                "top_failure_reason": top_failure_reason,
                "top_failure_reason_count": top_failure_count,
                "priority_tier": priority_tier,
                "priority_score": priority_score,
                "recommendation": recommendation,
            }
        )

    candidates.sort(
        key=lambda item: (
            item["priority_score"],
            item["failure_count"],
            item["failure_rate"] or 0,
            item["provider"],
        ),
        reverse=True,
    )
    return candidates


def _collect_metric_int_list(results: list[dict[str, Any]], key: str) -> list[int]:
    """Extract a list of int metric values for *key*, skipping items without the metric."""
    return [
        int(item["metrics"][key])
        for item in results
        if isinstance(item.get("metrics"), dict) and item["metrics"].get(key) is not None
    ]


def _count_string_field(results: list[dict[str, Any]], field_path: str) -> dict[str, int]:
    """Count occurrences of a string-valued metric across all results."""
    counts: dict[str, int] = {}
    for item in results:
        value = item.get("metrics", {}).get(field_path)
        if value is not None:
            key = str(value)
            counts[key] = counts.get(key, 0) + 1
    return counts


def _count_regression_reasons(results: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in results:
        for reason in item.get("regression_reasons", []):
            key = str(reason)
            counts[key] = counts.get(key, 0) + 1
    return counts


def _extract_item_facts(item: dict[str, Any]) -> dict[str, Any]:
    """Derive common booleans and metric values from a single result item."""
    metrics = item.get("metrics", {})
    return {
        "pdf_created": bool(item.get("pdf_created")),
        "md_created": bool(item.get("md_created")),
        "download_failed": (
            item.get("status") == "download_failed"
            or metrics.get("failure_stage") == "download"
        ),
        "ocr_failed": (
            item.get("status") == "ocr_failed"
            or metrics.get("failure_stage") == "ocr"
        ),
        "crashed": item.get("status") == "crash",
        "is_regression": bool(item.get("is_regression")),
        "cache_eligible": metrics.get("ocr_cache_hit") is not None,
        "cache_hit": bool(metrics.get("ocr_cache_hit")),
        "candidate_count": metrics.get("candidate_count"),
        "candidate_probe_count": metrics.get("candidate_probe_count"),
        "resolved_via": metrics.get("resolved_via"),
        "failure_reason": metrics.get("failure_reason"),
        "regression_reasons": item.get("regression_reasons", []),
    }


def _new_bucket(include_provider_fields: bool = False) -> dict[str, Any]:
    """Create a fresh accumulation bucket for by_input_type or by_provider."""
    bucket: dict[str, Any] = {
        "count": 0,
        "pdf_success_count": 0,
        "full_success_count": 0,
        "download_failure_count": 0,
        "ocr_failure_count": 0,
        "crash_count": 0,
        "regression_count": 0,
        "regression_reason_counts": {},
        "candidate_count_total": 0,
        "candidate_count_cases": 0,
        "candidate_probe_count_total": 0,
        "candidate_probe_count_cases": 0,
        "failure_reason_counts": {},
    }
    if include_provider_fields:
        bucket.update({
            "ocr_cache_hit_count": 0,
            "ocr_cache_eligible_count": 0,
            "resolved_via_counts": {},
        })
    return bucket


def _accumulate_item(bucket: dict[str, Any], facts: dict[str, Any]) -> None:
    """Add one result item's facts into *bucket* (mutates in place)."""
    bucket["count"] += 1
    bucket["pdf_success_count"] += int(facts["pdf_created"])
    bucket["full_success_count"] += int(facts["pdf_created"] and facts["md_created"])
    bucket["download_failure_count"] += int(facts["download_failed"])
    bucket["ocr_failure_count"] += int(facts["ocr_failed"])
    bucket["crash_count"] += int(facts["crashed"])
    bucket["regression_count"] += int(facts["is_regression"])

    if facts["candidate_count"] is not None:
        bucket["candidate_count_total"] += int(facts["candidate_count"])
        bucket["candidate_count_cases"] += 1
    if facts["candidate_probe_count"] is not None:
        bucket["candidate_probe_count_total"] += int(facts["candidate_probe_count"])
        bucket["candidate_probe_count_cases"] += 1

    failure_reason = facts["failure_reason"]
    if failure_reason is not None:
        key = str(failure_reason)
        bucket["failure_reason_counts"][key] = bucket["failure_reason_counts"].get(key, 0) + 1
    for reason in facts["regression_reasons"]:
        key = str(reason)
        bucket["regression_reason_counts"][key] = bucket["regression_reason_counts"].get(key, 0) + 1

    # Provider-only fields (no-op when absent from bucket).
    if "ocr_cache_hit_count" in bucket:
        bucket["ocr_cache_hit_count"] += int(facts["cache_hit"])
        bucket["ocr_cache_eligible_count"] += int(facts["cache_eligible"])
    if "resolved_via_counts" in bucket and facts["resolved_via"] is not None:
        key = str(facts["resolved_via"])
        bucket["resolved_via_counts"][key] = bucket["resolved_via_counts"].get(key, 0) + 1


def _finalize_candidate_averages(bucket: dict[str, Any]) -> None:
    """Replace raw totals/cases with computed averages (mutates in place)."""
    cc_cases = bucket.pop("candidate_count_cases")
    cpc_cases = bucket.pop("candidate_probe_count_cases")
    cc_total = bucket.pop("candidate_count_total")
    cpc_total = bucket.pop("candidate_probe_count_total")
    bucket["avg_candidate_count"] = round(cc_total / cc_cases, 4) if cc_cases else None
    bucket["avg_candidate_probe_count"] = round(cpc_total / cpc_cases, 4) if cpc_cases else None

    if "ocr_cache_eligible_count" in bucket:
        eligible = bucket["ocr_cache_eligible_count"]
        bucket["ocr_cache_hit_rate"] = (
            round(bucket["ocr_cache_hit_count"] / eligible, 4) if eligible else None
        )


def summarize_results(results: list[dict[str, Any]]) -> dict[str, Any]:
    total_ms_values = _collect_metric_int_list(results, "total_ms")
    candidate_counts = _collect_metric_int_list(results, "candidate_count")
    candidate_probe_counts = _collect_metric_int_list(results, "candidate_probe_count")
    download_total_ms_values = _collect_metric_int_list(results, "download_total_ms")
    final_download_ms_values = _collect_metric_int_list(results, "final_download_ms")

    ocr_cache_eligible_count = sum(
        1 for item in results if item.get("metrics", {}).get("ocr_cache_hit") is not None
    )
    ocr_cache_hit_count = sum(
        1 for item in results if bool(item.get("metrics", {}).get("ocr_cache_hit"))
    )
    resolved_via_counts = _count_string_field(results, "resolved_via")
    failure_reason_counts = _count_string_field(results, "failure_reason")
    regression_reason_counts = _count_regression_reasons(results)

    all_facts = [_extract_item_facts(item) for item in results]

    summary: dict[str, Any] = {
        "total_cases": len(results),
        "pdf_success_count": sum(1 for f in all_facts if f["pdf_created"]),
        "full_success_count": sum(1 for f in all_facts if f["pdf_created"] and f["md_created"]),
        "download_failure_count": sum(1 for f in all_facts if f["download_failed"]),
        "ocr_failure_count": sum(1 for f in all_facts if f["ocr_failed"]),
        "crash_count": sum(1 for f in all_facts if f["crashed"]),
        "regression_count": sum(1 for f in all_facts if f["is_regression"]),
        "regression_reason_counts": regression_reason_counts,
        "ocr_cache_hit_count": ocr_cache_hit_count,
        "ocr_cache_eligible_count": ocr_cache_eligible_count,
        "ocr_cache_hit_rate": (
            round(ocr_cache_hit_count / ocr_cache_eligible_count, 4)
            if ocr_cache_eligible_count
            else None
        ),
        "timing_ms": {
            "p50_total_ms": percentile_nearest_rank(total_ms_values, 50),
            "p90_total_ms": percentile_nearest_rank(total_ms_values, 90),
        },
        "failure_reason_counts": failure_reason_counts,
        "download_efficiency": {
            "avg_candidate_count": average(candidate_counts),
            "avg_candidate_probe_count": average(candidate_probe_counts),
            "resolved_via_counts": resolved_via_counts,
            "timing_ms": {
                "p50_download_total_ms": percentile_nearest_rank(download_total_ms_values, 50),
                "p90_download_total_ms": percentile_nearest_rank(download_total_ms_values, 90),
                "p50_final_download_ms": percentile_nearest_rank(final_download_ms_values, 50),
                "p90_final_download_ms": percentile_nearest_rank(final_download_ms_values, 90),
            },
        },
        "by_input_type": {},
        "by_provider": {},
    }

    for item, facts in zip(results, all_facts):
        input_type = item.get("input_type") or "unknown"
        provider = item.get("metrics", {}).get("provider") or "unknown"

        bucket = summary["by_input_type"].setdefault(input_type, _new_bucket())
        _accumulate_item(bucket, facts)

        provider_bucket = summary["by_provider"].setdefault(
            provider, _new_bucket(include_provider_fields=True)
        )
        _accumulate_item(provider_bucket, facts)

    for bucket in summary["by_input_type"].values():
        _finalize_candidate_averages(bucket)
    for bucket in summary["by_provider"].values():
        _finalize_candidate_averages(bucket)

    summary["provider_rule_candidates"] = build_provider_rule_candidates(summary["by_provider"])

    return summary


def load_cases(manifest_path: Path) -> list[dict[str, Any]]:
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise RuntimeError("Benchmark manifest must be a JSON array.")
    cases: list[dict[str, Any]] = []
    for index, item in enumerate(payload, start=1):
        if not isinstance(item, dict):
            raise RuntimeError(f"Case #{index} must be a JSON object.")
        if "url" not in item:
            raise RuntimeError(f"Case #{index} is missing required field: url")
        case = dict(item)
        case.setdefault("name", f"case-{index}")
        case.setdefault("input_type", "unknown")
        case.setdefault("expected_provider", None)
        case.setdefault("expected_outcome", None)
        case.setdefault("notes", None)
        cases.append(case)
    return cases


def run_case(case: dict[str, Any], output_dir: Path) -> dict[str, Any]:
    case_output_dir = output_dir / str(case["name"])
    case_output_dir.mkdir(parents=True, exist_ok=True)

    cmd = [
        sys.executable,
        str(DOWNLOAD_SCRIPT),
        str(case["url"]),
        "--output-dir",
        str(case_output_dir),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    stdout = proc.stdout.strip()
    stderr = proc.stderr.strip()

    result: dict[str, Any] = {
        "name": case["name"],
        "url": case["url"],
        "input_type": case.get("input_type", "unknown"),
        "status": "crash",
        "pdf_created": False,
        "md_created": False,
        "exit_code": proc.returncode,
        "stdout": stdout,
        "stderr": stderr,
        "metrics": {},
    }

    if stdout:
        try:
            payload = json.loads(stdout)
        except json.JSONDecodeError:
            payload = None
        if isinstance(payload, dict):
            result["metrics"] = payload.get("metrics") or {}
            result["pdf_path"] = payload.get("pdf_path")
            result["md_path"] = payload.get("md_path")
            result["resolved_pdf_url"] = payload.get("resolved_pdf_url")
            result["download_error"] = payload.get("download_error")
            result["ocr_error"] = payload.get("ocr_error")
            result["pdf_created"] = bool(payload.get("pdf_path"))
            result["md_created"] = bool(payload.get("md_path"))
            if proc.returncode == 0 and payload.get("ok"):
                result["status"] = "ok"
            elif result["metrics"].get("failure_stage") == "download":
                result["status"] = "download_failed"
            elif result["metrics"].get("failure_stage") == "ocr":
                result["status"] = "ocr_failed"

    return evaluate_case_expectations(case, result)


def write_summary_markdown(summary: dict[str, Any], results: list[dict[str, Any]], path: Path) -> None:
    lines = [
        "# URL PDF Download OCR Benchmark",
        "",
        f"- Total cases: {summary['total_cases']}",
        f"- PDF success: {summary['pdf_success_count']}",
        f"- Full success: {summary['full_success_count']}",
        f"- Download failures: {summary['download_failure_count']}",
        f"- OCR failures: {summary['ocr_failure_count']}",
        f"- Crashes: {summary['crash_count']}",
        f"- Regressions: {summary['regression_count']}",
        f"- Regression reasons: {summary['regression_reason_counts']}",
        f"- OCR cache hits: {summary['ocr_cache_hit_count']}/{summary['ocr_cache_eligible_count']}",
        f"- OCR cache hit rate: {summary['ocr_cache_hit_rate']}",
        f"- p50 total ms: {summary['timing_ms']['p50_total_ms']}",
        f"- p90 total ms: {summary['timing_ms']['p90_total_ms']}",
        f"- Failure reasons: {summary['failure_reason_counts']}",
        "",
        "## Download Efficiency",
        "",
        f"- Avg candidate count: {summary['download_efficiency']['avg_candidate_count']}",
        f"- Avg candidate probe count: {summary['download_efficiency']['avg_candidate_probe_count']}",
        f"- Resolved via: {summary['download_efficiency']['resolved_via_counts']}",
        f"- p50 download total ms: {summary['download_efficiency']['timing_ms']['p50_download_total_ms']}",
        f"- p90 download total ms: {summary['download_efficiency']['timing_ms']['p90_download_total_ms']}",
        f"- p50 final download ms: {summary['download_efficiency']['timing_ms']['p50_final_download_ms']}",
        f"- p90 final download ms: {summary['download_efficiency']['timing_ms']['p90_final_download_ms']}",
        "",
        "## By Provider",
        "",
    ]
    for provider, bucket in sorted(summary["by_provider"].items()):
        lines.append(
            f"- {provider}: count={bucket['count']}, pdf_success={bucket['pdf_success_count']}, "
            f"full_success={bucket['full_success_count']}, regressions={bucket['regression_count']}, "
            f"cache_hit_rate={bucket['ocr_cache_hit_rate']}, "
            f"avg_candidate_probe_count={bucket['avg_candidate_probe_count']}, "
            f"resolved_via={bucket['resolved_via_counts']}, failure_reasons={bucket['failure_reason_counts']}, "
            f"regression_reasons={bucket['regression_reason_counts']}"
        )
    if summary["provider_rule_candidates"]:
        lines.extend(
            [
                "",
                "## Recommended Work",
                "",
            ]
        )
        for item in summary["provider_rule_candidates"]:
            lines.append(
                f"- {item['provider']}: tier={item['priority_tier']}, failure_rate={item['failure_rate']}, "
                f"top_reason={item['top_failure_reason']}, recommendation={item['recommendation']}"
            )
    lines.extend(
        [
            "",
        "## Cases",
        "",
        ]
    )
    for item in results:
        lines.append(
            f"- {item['name']}: {item['status']} "
            f"(pdf={item['pdf_created']}, md={item['md_created']}, total_ms={item.get('metrics', {}).get('total_ms')}, "
            f"regression={item.get('is_regression')}, expected_provider={item.get('expected_provider')}, "
            f"expected_outcome={item.get('expected_outcome')})"
        )
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Run benchmark cases for url-pdf-download-ocr.")
    parser.add_argument("manifest", help="Path to a JSON manifest file describing benchmark cases.")
    parser.add_argument(
        "--report-dir",
        default=str(SKILL_ROOT / "benchmarks" / "runs"),
        help="Directory to store benchmark outputs.",
    )
    args = parser.parse_args()

    manifest_path = Path(os.path.expanduser(args.manifest)).resolve()
    report_dir = Path(os.path.expanduser(args.report_dir)).resolve()
    report_dir.mkdir(parents=True, exist_ok=True)

    try:
        cases = load_cases(manifest_path)
    except Exception as exc:
        print(f"BENCHMARK_ERROR: {exc}", file=sys.stderr)
        return 2

    results = [run_case(case, report_dir / "outputs") for case in cases]
    summary = summarize_results(results)

    (report_dir / "results.json").write_text(
        json.dumps(results, ensure_ascii=True, indent=2) + "\n",
        encoding="utf-8",
    )
    (report_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=True, indent=2) + "\n",
        encoding="utf-8",
    )
    write_summary_markdown(summary, results, report_dir / "summary.md")

    print(
        json.dumps(
            {
                "ok": True,
                "manifest": str(manifest_path),
                "report_dir": str(report_dir),
                "summary": summary,
            },
            ensure_ascii=True,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
