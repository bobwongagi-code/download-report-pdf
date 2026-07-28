#!/usr/bin/env python3
import argparse
import hashlib
import json
import math
import os
import platform
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Optional

from pypdf import PdfReader
from process_supervisor import kill_process_tree, read_capture, run_captured_process

SKILL_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = SKILL_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from url_utils import normalize_url, redact_text, redact_url
from version import TOOL_NAME, TOOL_VERSION


DOWNLOAD_SCRIPT = SKILL_ROOT / "scripts" / "download_and_ocr.py"
VALID_EXPECTED_OUTCOMES = {"success", "download_failed", "ocr_failed", "crash"}
CASE_TIMEOUT_SECONDS = 900
MAX_CAPTURE_BYTES = 16 * 1024


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
    if not isinstance(value, str):
        raise ValueError(f"Unsupported expected_outcome: {value!r}")
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
    normalized = aliases.get(normalized, normalized)
    if normalized not in VALID_EXPECTED_OUTCOMES:
        raise ValueError(f"Unsupported expected_outcome: {value!r}")
    return normalized


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
    metrics = result.get("metrics") if isinstance(result.get("metrics"), dict) else {}
    actual_provider = metrics.get("resolved_provider") or metrics.get("provider")
    actual_outcome = outcome_for_result(result)
    regression_reasons: list[str] = []

    if expected_provider and actual_provider != expected_provider:
        regression_reasons.append("provider_mismatch")
    if expected_outcome and actual_outcome != expected_outcome:
        regression_reasons.append("outcome_mismatch")

    enriched = dict(result)
    enriched["expected_provider"] = expected_provider
    enriched["expected_outcome"] = expected_outcome
    enriched["notes"] = redact_text(str(notes)) if notes is not None else None
    enriched["actual_provider"] = actual_provider
    enriched["actual_outcome"] = actual_outcome
    enriched["is_regression"] = bool(regression_reasons)
    enriched["regression_reasons"] = regression_reasons
    return enriched


def reason_priority_weight(reason: str) -> int:
    weights = {
        "no_pdf_found": 5,
        "invalid_pdf": 4,
        "network_policy": 4,
        "invalid_url": 4,
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
        "network_policy": (
            "low",
            "Check the case URL and provider redirect policy; non-public destinations are intentionally rejected.",
        ),
        "invalid_url": (
            "low",
            "Correct the manifest URL before changing provider-specific download rules.",
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
    metrics = item.get("metrics") if isinstance(item.get("metrics"), dict) else {}
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
        "artifact_failed": item.get("status") == "artifact_failed",
        "is_regression": bool(item.get("is_regression")),
        "cache_eligible": metrics.get("ocr_cache_hit") is not None,
        "cache_hit": bool(metrics.get("ocr_cache_hit")),
        "candidate_count": metrics.get("candidate_count"),
        "candidate_probe_count": metrics.get("candidate_probe_count"),
        "resolved_via": metrics.get("resolved_via"),
        "failure_reason": metrics.get("failure_reason"),
        "regression_reasons": item.get("regression_reasons", []),
    }


def _case_id(index: int, case: dict[str, Any]) -> str:
    digest = hashlib.sha256(
        json.dumps(
            {"index": index, "name": case["name"], "url": case["url"]},
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()[:12]
    return f"case-{index:03d}-{digest}"


def _path_is_inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _verify_artifact(value: object, case_output_dir: Path, *, suffix: str) -> tuple[bool, Optional[str]]:
    if not isinstance(value, str) or not value:
        return False, "missing_path"
    path = Path(value).expanduser()
    if path.is_symlink():
        return False, "symlink_artifact"
    if not _path_is_inside(path, case_output_dir):
        return False, "artifact_outside_case_directory"
    try:
        if not path.is_file() or path.stat().st_size <= 0:
            return False, "artifact_missing_or_empty"
    except OSError:
        return False, "artifact_unreadable"
    if suffix and not path.name.lower().endswith(suffix.lower()):
        return False, "unexpected_artifact_suffix"
    if suffix == ".pdf":
        try:
            with path.open("rb") as fh:
                if fh.read(5) != b"%PDF-":
                    return False, "invalid_pdf_signature"
            if len(PdfReader(str(path), strict=False).pages) <= 0:
                return False, "invalid_pdf_page_tree"
        except Exception:
            return False, "invalid_pdf_structure"
    return True, None


def _redact_output(text: str) -> str:
    redacted = re.sub(
        r"\b[a-zA-Z][a-zA-Z0-9+.-]*://[^\s\"']+",
        lambda match: redact_url(match.group(0)),
        text or "",
    )
    return redacted[-MAX_CAPTURE_BYTES:]


def _redact_value(value: Any) -> Any:
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        return {str(key): _redact_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_value(item) for item in value]
    return value


_kill_process_tree = kill_process_tree


def _read_capture(path: Path) -> str:
    return read_capture(path, MAX_CAPTURE_BYTES)


def _run_benchmark_process(
    command: list[str],
    *,
    case_output_dir: Path,
    timeout_seconds: int,
) -> subprocess.CompletedProcess[str]:
    return run_captured_process(
        command,
        work_dir=case_output_dir,
        timeout_seconds=timeout_seconds,
        max_output_bytes=MAX_CAPTURE_BYTES,
        timeout_label="benchmark subprocess",
        timeout_error_factory=lambda cmd, seconds: subprocess.TimeoutExpired(cmd, seconds),
    )


def _new_bucket(include_provider_fields: bool = False) -> dict[str, Any]:
    """Create a fresh accumulation bucket for by_input_type or by_provider."""
    bucket: dict[str, Any] = {
        "count": 0,
        "pdf_success_count": 0,
        "full_success_count": 0,
        "download_failure_count": 0,
        "ocr_failure_count": 0,
        "crash_count": 0,
        "artifact_failure_count": 0,
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
    bucket["artifact_failure_count"] += int(facts["artifact_failed"])
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
        "artifact_failure_count": sum(1 for item in results if item.get("status") == "artifact_failed"),
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
    names: set[str] = set()
    for index, item in enumerate(payload, start=1):
        if not isinstance(item, dict):
            raise RuntimeError(f"Case #{index} must be a JSON object.")
        if "url" not in item or not isinstance(item["url"], str):
            raise RuntimeError(f"Case #{index} is missing required field: url")
        try:
            normalize_url(item["url"])
        except ValueError as exc:
            raise RuntimeError(f"Case #{index} has invalid url: {exc}") from exc
        case = dict(item)
        case.setdefault("name", f"case-{index}")
        if not isinstance(case["name"], str) or not case["name"].strip() or case["name"] in {".", ".."}:
            raise RuntimeError(f"Case #{index} has invalid name")
        if "/" in case["name"] or "\\" in case["name"] or case["name"].startswith("."):
            raise RuntimeError(f"Case #{index} name must be a display-only value, not a path")
        if case["name"] in names:
            raise RuntimeError(f"Duplicate case name: {case['name']}")
        names.add(case["name"])
        case.setdefault("input_type", "unknown")
        case.setdefault("expected_provider", None)
        case.setdefault("expected_outcome", None)
        case.setdefault("notes", None)
        if case["expected_provider"] is not None and not isinstance(case["expected_provider"], str):
            raise RuntimeError(f"Case #{index} expected_provider must be a string or null")
        if not isinstance(case["input_type"], str):
            raise RuntimeError(f"Case #{index} input_type must be a string")
        if case["notes"] is not None and not isinstance(case["notes"], str):
            raise RuntimeError(f"Case #{index} notes must be a string or null")
        case["expected_outcome"] = normalize_expected_outcome(case["expected_outcome"])
        cases.append(case)
    return cases


def run_case(
    case: dict[str, Any],
    output_dir: Path,
    *,
    case_index: int = 1,
    timeout_seconds: int = CASE_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    case_output_dir = (output_dir / _case_id(case_index, case)).resolve()
    if not _path_is_inside(case_output_dir, output_dir):
        raise RuntimeError("Benchmark case output escaped report root")
    case_output_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        case_output_dir.chmod(0o700)
    except OSError:
        pass

    cmd = [
        sys.executable,
        str(DOWNLOAD_SCRIPT),
        str(case["url"]),
        "--output-dir",
        str(case_output_dir),
    ]
    try:
        proc = _run_benchmark_process(
            cmd,
            case_output_dir=case_output_dir,
            timeout_seconds=timeout_seconds,
        )
    except subprocess.TimeoutExpired:
        return evaluate_case_expectations(case, {
            "name": case["name"],
            "url": redact_url(case["url"]),
            "input_type": case.get("input_type", "unknown"),
            "status": "crash",
            "pdf_created": False,
            "md_created": False,
            "exit_code": None,
            "stdout": "",
            "stderr": f"benchmark case timed out after {timeout_seconds}s",
            "metrics": {"failure_stage": "benchmark", "failure_reason": "timeout"},
            "artifact_errors": ["case_timeout"],
        })
    except (OSError, RuntimeError) as exc:
        return evaluate_case_expectations(case, {
            "name": case["name"],
            "url": redact_url(case["url"]),
            "input_type": case.get("input_type", "unknown"),
            "status": "crash",
            "pdf_created": False,
            "md_created": False,
            "exit_code": None,
            "stdout": "",
            "stderr": _redact_output(str(exc)),
            "metrics": {"failure_stage": "benchmark", "failure_reason": "process_error"},
            "artifact_errors": ["benchmark_process_error"],
        })
    stdout = proc.stdout.strip()
    stderr = proc.stderr.strip()

    result: dict[str, Any] = {
        "name": case["name"],
        "url": redact_url(case["url"]),
        "input_type": case.get("input_type", "unknown"),
        "status": "crash",
        "pdf_created": False,
        "md_created": False,
        "exit_code": proc.returncode,
        "stdout": _redact_output(stdout),
        "stderr": _redact_output(stderr),
        "metrics": {},
        "artifact_errors": [],
    }

    if stdout:
        try:
            payload = json.loads(stdout)
        except json.JSONDecodeError:
            payload = None
        if isinstance(payload, dict):
            raw_metrics = payload.get("metrics")
            result["metrics"] = _redact_value(raw_metrics) if isinstance(raw_metrics, dict) else {}
            result["pdf_path"] = payload.get("pdf_path")
            result["md_path"] = payload.get("md_path")
            result["resolved_pdf_url"] = (
                redact_url(str(payload.get("resolved_pdf_url")))
                if payload.get("resolved_pdf_url")
                else None
            )
            result["download_error"] = redact_text(str(payload.get("download_error") or "")) or None
            result["ocr_error"] = redact_text(str(payload.get("ocr_error") or "")) or None
            pdf_ok, pdf_error = _verify_artifact(payload.get("pdf_path"), case_output_dir, suffix=".pdf")
            md_ok, md_error = _verify_artifact(payload.get("md_path"), case_output_dir, suffix=".md")
            result["pdf_created"] = pdf_ok
            result["md_created"] = md_ok
            result["artifact_errors"] = [error for error in (pdf_error, md_error) if error]
            if proc.returncode == 0 and payload.get("ok"):
                result["status"] = "ok" if pdf_ok and md_ok else "artifact_failed"
            elif result["metrics"].get("failure_stage") == "download":
                result["status"] = "download_failed"
            elif result["metrics"].get("failure_stage") == "ocr":
                result["status"] = "ocr_failed"
        else:
            result["artifact_errors"] = ["invalid_json_output"]

    if not stdout and proc.returncode != 0:
        result["stderr"] = _redact_output(stderr) or "benchmark subprocess returned no JSON output"
    elif not stdout:
        result["artifact_errors"] = ["missing_json_output"]

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
        f"- Artifact failures: {summary['artifact_failure_count']}",
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
    parser.add_argument(
        "--case-timeout",
        type=int,
        default=CASE_TIMEOUT_SECONDS,
        help=f"Maximum seconds per case (default: {CASE_TIMEOUT_SECONDS}).",
    )
    parser.add_argument(
        "--fail-on-regression",
        action="store_true",
        help="Return non-zero when provider or outcome expectations regress.",
    )
    parser.add_argument(
        "--fail-on-any-error",
        action="store_true",
        help="Return non-zero when any case fails, crashes, or has invalid artifacts.",
    )
    parser.add_argument(
        "--run-id",
        default=None,
        help="Stable run directory suffix; defaults to a timestamp plus random id.",
    )
    args = parser.parse_args()

    if args.case_timeout <= 0:
        print("BENCHMARK_ERROR: --case-timeout must be positive", file=sys.stderr)
        return 2

    manifest_path = Path(os.path.expanduser(args.manifest)).resolve()
    report_root = Path(os.path.expanduser(args.report_dir)).resolve()
    run_id = args.run_id or f"{time.strftime('%Y%m%d-%H%M%S', time.gmtime())}-{uuid.uuid4().hex[:8]}"
    if not re.fullmatch(r"[A-Za-z0-9._-]+", run_id):
        print("BENCHMARK_ERROR: --run-id contains unsafe path characters", file=sys.stderr)
        return 2
    report_dir = report_root / f"run-{run_id}"
    if report_dir.is_symlink() or (report_dir.exists() and any(report_dir.iterdir())):
        print("BENCHMARK_ERROR: run directory already exists; choose a new --run-id", file=sys.stderr)
        return 2
    report_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        report_dir.chmod(0o700)
    except OSError:
        pass

    try:
        cases = load_cases(manifest_path)
    except Exception as exc:
        print(f"BENCHMARK_ERROR: {exc}", file=sys.stderr)
        return 2

    results = [
        run_case(case, report_dir / "outputs", case_index=index, timeout_seconds=args.case_timeout)
        for index, case in enumerate(cases, start=1)
    ]
    summary = summarize_results(results)

    git_revision = "unknown"
    try:
        git_revision = subprocess.run(
            ["git", "-C", str(SKILL_ROOT), "rev-parse", "HEAD"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            check=True,
            timeout=5,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    def _write_json(path: Path, payload: object) -> None:
        path.write_text(json.dumps(payload, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
        try:
            path.chmod(0o600)
        except OSError:
            pass

    _write_json(report_dir / "provenance.json", {
        "tool": TOOL_NAME,
        "tool_version": TOOL_VERSION,
        "run_id": run_id,
        "git_revision": git_revision,
        "manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
        "python": sys.version,
        "platform": platform.platform(),
        "case_timeout_seconds": args.case_timeout,
    })

    _write_json(report_dir / "results.json", results)
    _write_json(report_dir / "summary.json", summary)
    write_summary_markdown(summary, results, report_dir / "summary.md")
    try:
        (report_dir / "summary.md").chmod(0o600)
    except OSError:
        pass

    has_error = bool(
        summary["download_failure_count"]
        or summary["ocr_failure_count"]
        or summary["crash_count"]
        or summary["artifact_failure_count"]
    )
    gate_failed = bool((args.fail_on_any_error and has_error) or (args.fail_on_regression and summary["regression_count"]))
    print(
        json.dumps(
            {
                "ok": not has_error and not bool(summary["regression_count"]),
                "gate_failed": gate_failed,
                "manifest": str(manifest_path),
                "report_dir": str(report_dir),
                "summary": summary,
            },
            ensure_ascii=True,
            indent=2,
        )
    )
    return 1 if gate_failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
