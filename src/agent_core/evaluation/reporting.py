"""带显式分母和覆盖数的内存报告；不持久化或输出底层异常文本。"""
from __future__ import annotations

import math
from collections import Counter
from collections.abc import Mapping
from datetime import date, datetime
from decimal import Decimal

from .contracts import EvalReport
from .scoring import score_case

_RESPONSE_KEYS = {
    "status", "route_to", "stage", "error_code", "generation_mode", "prompt_version",
    "source_id", "metric_definition_id", "sql", "parameters", "columns", "rows",
    "row_count", "truncated", "tables", "execution_attempted", "validation_status",
    "mapping_status", "elapsed_ms", "execution_elapsed_ms",
    "session_id", "task_id", "request_id", "step_id",
}
_METADATA_KEYS = {"mode", "schema_fingerprint_sha256", "captured_at", "permissions_verified",
                  "live_permissions_verified", "same_transaction_revalidation"}
_CANDIDATE_KEYS = {"sql", "source_id", "metric_definition_id", "parameter_names",
                   "schema_fingerprint_sha256", "dialect", "placeholder_style", "validated"}
_SECRET_KEYS = {"password", "passwd", "secret", "token", "api_key", "authorization",
                "connection_string", "dsn", "error_message", "exception", "traceback"}
_PRECONDITION_KEYS = {"verified", "origin", "snapshot_sha256", "semantic_metadata_sha256",
                      "schema_fingerprint_sha256", "error_code", "permissions_verified",
                      "database_name", "reader_role", "projection_row_counts"}


def _safe(value, depth=0):
    if depth > 12:
        return None
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, Decimal):
        return str(value) if value.is_finite() else None
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {key: _safe(item, depth + 1) for key, item in value.items()
                if isinstance(key, str) and key.lower() not in _SECRET_KEYS}
    if isinstance(value, (list, tuple)):
        return [_safe(item, depth + 1) for item in value]
    # 禁止 str(exception) / repr(未知对象)。
    return None


def _observation_response(response):
    if not isinstance(response, Mapping):
        return {}
    result = {key: _safe(response[key]) for key in _RESPONSE_KEYS if key in response}
    timings = response.get("timings")
    if isinstance(timings, Mapping):
        result["timings"] = {key: value for key, value in timings.items()
                             if key in {"metadata_initial", "mapping", "generation", "validation", "execution"}
                             and type(value) in (int, float) and math.isfinite(value) and value >= 0}
    limits = response.get("execution_limits")
    if isinstance(limits, Mapping):
        result["execution_limits"] = {key: value for key, value in limits.items()
                                       if key in {"max_rows", "db_timeout_ms", "lock_timeout_ms",
                                                  "connect_timeout_ms", "execution_timeout_ms"}
                                       and type(value) is int}
    for key in ("metadata", "execution_metadata"):
        value = response.get(key)
        if isinstance(value, Mapping):
            result[key] = {name: _safe(value[name]) for name in _METADATA_KEYS if name in value}
    candidate = response.get("candidate")
    if isinstance(candidate, Mapping):
        result["candidate"] = {name: _safe(candidate[name]) for name in _CANDIDATE_KEYS if name in candidate}
    validation = response.get("validation")
    if isinstance(validation, Mapping):
        result["validation"] = {name: _safe(validation[name]) for name in ("status", "passed", "error_code")
                                if name in validation}
    issues = response.get("issues")
    if isinstance(issues, list):
        result["issues"] = [{name: _safe(issue[name])
                             for name in ("code", "field_id", "missing_members", "filter_index", "value_index", "candidates")
                             if name in issue}
                            for issue in issues if isinstance(issue, Mapping)]
    return result


def _ratio(numerator, denominator, reason=None):
    if reason is None and denominator == 0:
        reason = "zero_denominator"
    return {"numerator": numerator, "denominator": denominator,
            "value": None if reason else numerator / denominator,
            "measurement_status": "not_measurable" if reason else "scored", "reason": reason}


def _unavailable(reason):
    return {"numerator": None, "denominator": None, "value": None,
            "measurement_status": "not_measurable", "reason": reason}


def _percentile(values, percentile):
    if not values:
        return None
    position = (len(values) - 1) * percentile
    lower, upper = math.floor(position), math.ceil(position)
    return round(values[lower] + (values[upper] - values[lower]) * (position - lower), 3)


def _performance(observations):
    values = sorted(observation.elapsed_ms for observation in observations
                    if type(observation.elapsed_ms) in (int, float)
                    and math.isfinite(observation.elapsed_ms) and observation.elapsed_ms >= 0)
    return {"count": len(values), "missing_timing_count": len(observations) - len(values),
            "p50_ms": _percentile(values, .5), "p95_ms": _percentile(values, .95),
            "min_ms": values[0] if values else None, "max_ms": values[-1] if values else None}


def summarize(run) -> EvalReport:
    """完整率只适用于快照已验证、观察完整且可评分的 complete 运行。"""
    cases = {case.case_id: case for case in run.cases}
    supported = [case for case in run.cases if case.kind != "roadmap"]
    roadmap = [case for case in run.cases if case.kind == "roadmap"]
    known = [observation for observation in run.observations if observation.case_id in cases]
    scores = [score_case(cases[observation.case_id], observation) for observation in known]
    scored_pairs = [(score, observation) for score, observation in zip(scores, known)
                    if score.measurement_status == "scored"]
    scored = [score for score, _ in scored_pairs]
    answer = [score for score in scored if score.kind == "answer"]
    clarification = [score for score in scored if score.kind == "clarification"]
    safety = [score for score in scored if score.kind == "safety"]
    pairs = [(observation.case_id, observation.attempt_id) for observation in run.observations]
    unique_attempts = len(set(pairs))
    valid_attempt_ids = all(type(observation.attempt_id) is int and observation.attempt_id > 0
                            for observation in run.observations)
    expected_keys = set()
    valid_plan = type(run.planned_attempts) is int and run.planned_attempts >= 0
    if supported:
        valid_plan = (valid_plan and run.planned_attempts % len(supported) == 0
                      and 1 <= run.planned_attempts // len(supported) <= 10)
        if valid_plan:
            repeats = run.planned_attempts // len(supported)
            expected_keys = {(case.case_id, attempt_id) for case in supported
                             for attempt_id in range(1, repeats + 1)}
    else:
        valid_plan = valid_plan and run.planned_attempts == 0
    observed_case_ids = {observation.case_id for observation in known}
    missing_cases = [case.case_id for case in supported if case.case_id not in observed_case_ids]
    reason = None
    if run.status != "complete":
        reason = "run_interrupted" if run.status == "interrupted" else "run_invalid"
    elif run.preconditions.get("verified") is not True:
        reason = "snapshot_not_verified"
    elif not supported:
        reason = "no_supported_cases"
    elif len(known) != len(run.observations):
        reason = "unknown_case_observation"
    elif len(set(cases)) != len(run.cases):
        reason = "duplicate_case_id"
    elif unique_attempts != len(pairs):
        reason = "duplicate_attempt"
    elif not valid_plan or not valid_attempt_ids:
        reason = "invalid_attempt_plan"
    elif set(pairs) != expected_keys:
        reason = "incomplete_observations"
    elif missing_cases or len(scored) != run.planned_attempts:
        reason = "incomplete_observations"
    elif any(observation.provider_origin != run.manifest.provider_origin
             or observation.execution_origin != run.manifest.execution_origin for observation in known):
        reason = "origin_mismatch"

    metrics = {
        "supported_scope_accuracy": _ratio(sum(score.passed is True for score in scored), len(scored), reason),
        "answer_accuracy": _ratio(sum(score.passed is True for score in answer), len(answer), reason),
        "clarification_accuracy": _ratio(sum(score.passed is True for score in clarification), len(clarification), reason),
        "safety_block_rate": _ratio(sum(score.passed is True for score in safety), len(safety), reason),
        "clarification_recall": _ratio(sum(score.actual_status == "needs_clarification" for score in clarification),
                                       len(clarification), reason),
        "clarification_precision": _ratio(sum(score.actual_status == "needs_clarification" for score in clarification),
                                          sum(score.actual_status == "needs_clarification" for score in scored), reason),
        "unnecessary_clarification_rate": _ratio(sum(score.actual_status == "needs_clarification" for score in answer),
                                                len(answer), reason),
        "timeout_rate": _ratio(sum(score.failure_category == "timeout" for score in scored), len(scored), reason),
        "error_response_rate": _ratio(sum(score.actual_status == "error" for score in scored), len(scored), reason),
        "failure_rate": _ratio(sum(score.passed is False for score in scored), len(scored), reason),
        "answer_error_rate": _ratio(sum(score.actual_status == "error" for score in answer), len(answer), reason),
    }
    metrics["error_response_rate"]["definition"] = "error_responses_including_expected_safety_rejections"
    answer_pairs = [(score, observation) for score, observation in scored_pairs if score.kind == "answer"]
    validation_rejections = sum(
        observation.response.get("validation_status") == "rejected"
        or isinstance(observation.response.get("validation"), Mapping)
        and observation.response["validation"].get("status") == "rejected"
        for _, observation in answer_pairs if isinstance(observation.response, Mapping))
    mapping_rejections = sum(observation.response.get("mapping_status") in {"error", "needs_clarification"}
                             for _, observation in answer_pairs if isinstance(observation.response, Mapping)
                             and isinstance(observation.response.get("mapping_status"), str))
    false_rejections = sum(
        observation.response.get("validation_status") == "rejected"
        or isinstance(observation.response.get("validation"), Mapping)
        and observation.response["validation"].get("status") == "rejected"
        or observation.response.get("mapping_status") in ("error", "needs_clarification")
        for _, observation in answer_pairs if isinstance(observation.response, Mapping))
    metrics.update(
        answer_validation_rejection_rate=_ratio(validation_rejections, len(answer), reason),
        answer_mapping_refusal_rate=_ratio(mapping_rejections, len(answer), reason),
        answer_false_rejection_rate=_ratio(false_rejections, len(answer), reason),
    )
    candidates = [(score, observation) for score, observation in scored_pairs
                  if isinstance(observation.response, Mapping)
                  and isinstance(observation.response.get("candidate"), Mapping)]
    metrics["candidate_validation_rate"] = _ratio(
        sum(observation.response.get("validation_status") == "passed" for _, observation in candidates),
        len(candidates), reason)
    # 当前接口未提供模型实际调用次数，不能用题数伪造生成调用的分母。
    metrics["generation_output_rate"] = _unavailable("generation_call_trace_unavailable")
    metrics["token_usage"] = _unavailable("provider_usage_unavailable")
    metrics["cost"] = _unavailable("provider_usage_and_pricing_unavailable")
    metrics["stage_timings"] = _unavailable("stage_timing_trace_unavailable")

    golds = [case.expected.get("gold") for case in supported if case.kind == "answer"]
    gold_human_reviewed = bool(golds) and all(
        isinstance(gold, Mapping) and gold.get("human_reviewed") is True
        and isinstance(gold.get("reviewed_by"), str) and bool(gold["reviewed_by"])
        and gold["reviewed_by"] != "independent_python_oracle" for gold in golds)
    standard_basis = ("independent_python_oracle" if golds and all(
        isinstance(gold, Mapping) and gold.get("reviewed_by") == "independent_python_oracle" for gold in golds)
                      else "declared_standard")

    actual_model_quality = (reason is None and bool(answer)
                            and run.manifest.provider_origin == "real"
                            and run.manifest.execution_origin == "live")
    quality_label = ("structured_sql_real_model_on_" + standard_basis if actual_model_quality
                     else "structured_sql_demo_or_simulated" if
                     run.manifest.provider_origin != "real" or run.manifest.execution_origin != "live"
                     else "structured_sql_not_fully_measured")
    records = [{**_safe(score.as_dict()), "observation": _observation_response(observation.response),
                "provider_origin": observation.provider_origin, "execution_origin": observation.execution_origin}
               for score, observation in zip(scores, known)]
    records.extend({"case_id": case.case_id, "kind": "roadmap", "passed": None,
                    "measurement_status": "not_measurable", "reason": "module_not_implemented",
                    "observation": None} for case in roadmap if case.case_id not in observed_case_ids)
    records.extend({"case_id": case_id, "passed": None, "measurement_status": "not_run",
                    "reason": "attempt_missing", "observation": None} for case_id in missing_cases)
    successful = [observation for score, observation in scored_pairs
                  if score.kind == "answer" and score.passed is True]
    safety_violations = sum(score.safety_violation for score in scores)
    preconditions = {"verified": run.preconditions.get("verified") is True}
    for phase in ("initial", "final"):
        evidence = run.preconditions.get(phase)
        if isinstance(evidence, Mapping):
            preconditions[phase] = {key: _safe(evidence[key]) for key in _PRECONDITION_KEYS if key in evidence}
    stage_errors = Counter(score.stage or "unknown" for score in scored if score.actual_status == "error")
    unexpected_stage_errors = Counter(score.stage or "unknown" for score in scored
                                      if score.actual_status == "error" and score.passed is False)
    stage_failures = Counter(score.stage or "unknown" for score in scored if score.passed is False)
    return EvalReport({
        "run_id": run.manifest.run_id, "status": run.status, "error_code": run.error_code,
        "suite_version": run.manifest.suite_version, "data_snapshot_id": run.manifest.data_snapshot_id,
        "manifest": _safe(run.manifest.as_dict()),
        "preconditions": preconditions,
        "measurement_status": "scored" if reason is None else "not_measurable", "reason": reason,
        "quality_claim": {"real_model_quality": actual_model_quality, "evaluation_level": "structured_sql_service",
                          "label": quality_label, "gold_human_reviewed": gold_human_reviewed,
                          "standard_basis": standard_basis},
        "coverage": {"planned_cases": len(run.cases), "supported_cases": len(supported), "roadmap_cases": len(roadmap),
                     "planned_attempts": run.planned_attempts, "observed_attempts": len(run.observations),
                     "scored_attempts": len(scored), "not_measurable_attempts": len(scores) - len(scored),
                     "missing_attempts": len(expected_keys - set(pairs)) if valid_plan else None,
                     "unknown_observations": len(run.observations) - len(known)},
        "metrics": metrics,
        "stage_error_response_rates": {stage: {**_ratio(count, len(scored), reason),
                                       "denominator_scope": "all_scored_attempts",
                                       "definition": "error_responses_including_expected_safety_rejections"}
                              for stage, count in sorted(stage_errors.items())},
        "stage_error_rates": {stage: {**_ratio(count, len(scored), reason),
                                       "denominator_scope": "all_scored_attempts"}
                              for stage, count in sorted(unexpected_stage_errors.items())},
        "stage_failure_rates": {stage: {**_ratio(count, len(scored), reason),
                                         "denominator_scope": "all_scored_attempts"}
                                for stage, count in sorted(stage_failures.items())},
        "performance": {"scope": "runner_wall_time_including_failure_and_cleanup",
                        "all": _performance(run.observations), "successful_answers": _performance(successful)},
        "failure_counts": dict(sorted(Counter(score.failure_category for score in scored if score.passed is False).items())),
        "status_counts": dict(sorted(Counter(score.actual_status or "missing" for score in scores).items())),
        "safety_violation_count": safety_violations,
        "expected_rejection_count": sum(score.actual_status == "error" and score.passed is True for score in scored),
        "acceptance": {"blocked": safety_violations > 0 or reason is not None,
                       "reason": "unsafe_execution" if safety_violations else reason}, "records": records,
    })
