"""报告分母、覆盖数、性能和来源声明的纯离线回归。"""
from __future__ import annotations

import copy
import json
import unittest
from dataclasses import replace

from agent_core.evaluation.contracts import EvalCase, EvalRun, Observation, RunManifest
from agent_core.evaluation.reporting import summarize


def case(case_id="A", kind="answer"):
    if kind == "answer":
        expected = {"status": "ok", "columns": [{"name": "sales_amount", "data_type": "decimal", "unit": "金额"}],
                    "rows": [{"sales_amount": "1.00"}], "source_id": "chinook_db", "metric_definition_id": "gross_v1"}
    elif kind == "clarification":
        expected = {"issue_codes": ["MISSING_TIME_RANGE"], "clarification_fields": ["time_range"]}
    elif kind == "safety":
        expected = {"error_codes": ["SQL_NOT_ALLOWED"]}
    else:
        expected = {}
    return EvalCase(case_id, kind, {}, {}, expected, scope="roadmap" if kind == "roadmap" else "sql_v1",
                    data_snapshot_id="data-v1")


def answer():
    return {"status": "ok", "stage": "complete",
            "columns": [{"name": "sales_amount", "data_type": "decimal", "unit": "金额"}],
            "rows": [{"sales_amount": "1.00"}], "row_count": 1, "truncated": False,
            "source_id": "chinook_db", "metric_definition_id": "gross_v1",
            "validation_status": "passed", "execution_attempted": True,
            "metadata": {"mode": "live", "live_permissions_verified": True},
            "execution_metadata": {"mode": "live", "permissions_verified": True,
                                   "same_transaction_revalidation": True}}


def run(cases, observations, *, provider="demo", execution="mock", planned=None, status="complete", verified=True):
    manifest = RunManifest("run-1", "suite-v1", "data-v1", provider, execution,
                           model_id="actual-model" if provider == "real" else None)
    values = [Observation(case_id, attempt, response, elapsed, provider, execution)
              for case_id, attempt, response, elapsed in observations]
    return EvalRun(manifest, tuple(cases), values, status,
                   sum(item.kind != "roadmap" for item in cases) if planned is None else planned,
                   {"verified": verified})


class EvaluationReportingTests(unittest.TestCase):
    def test_timeout_and_failure_remain_in_denominator_and_latency(self):
        cases = [case("A"), case("B"), case("C")]
        observed = [("A", 1, answer(), 10),
                    ("B", 1, {"status": "error", "stage": "execution", "error_code": "QUERY_TIMEOUT"}, 100),
                    ("C", 1, {"status": "error", "stage": "generation", "error_code": "LLM_RESPONSE_INVALID"}, 30)]
        report = summarize(run(cases, observed)).as_dict()
        metric = report["metrics"]["answer_accuracy"]
        self.assertEqual((metric["numerator"], metric["denominator"], metric["value"]), (1, 3, 1 / 3))
        self.assertEqual(report["metrics"]["timeout_rate"]["value"], 1 / 3)
        self.assertEqual(report["performance"]["all"]["count"], 3)
        self.assertEqual(report["performance"]["all"]["p50_ms"], 30)
        self.assertEqual(report["performance"]["all"]["p95_ms"], 93)
        self.assertEqual(report["performance"]["successful_answers"]["count"], 1)
        self.assertEqual(report["failure_counts"], {"llm_error": 1, "timeout": 1})

    def test_roadmap_coverage_is_explicit_without_fabricated_attempt(self):
        report = summarize(run([case("A"), case("R", "roadmap")], [("A", 1, answer(), 10)])).as_dict()
        self.assertEqual(report["coverage"]["planned_cases"], 2)
        self.assertEqual(report["coverage"]["supported_cases"], 1)
        self.assertEqual(report["coverage"]["roadmap_cases"], 1)
        self.assertEqual(report["coverage"]["planned_attempts"], 1)
        record = next(item for item in report["records"] if item["case_id"] == "R")
        self.assertIsNone(record["passed"])
        self.assertIsNone(record["observation"])
        self.assertEqual(record["measurement_status"], "not_measurable")
        self.assertEqual(report["metrics"]["supported_scope_accuracy"]["value"], 1)

    def test_zero_denominators_are_null(self):
        report = summarize(run([case("R", "roadmap")], [], planned=0)).as_dict()
        for metric in report["metrics"].values():
            self.assertIsNone(metric["value"])
        self.assertEqual(report["metrics"]["answer_accuracy"]["reason"], "no_supported_cases")
        self.assertEqual(report["reason"], "no_supported_cases")
        self.assertIsNone(report["performance"]["all"]["p95_ms"])

    def test_invalid_interrupted_and_unverified_runs_publish_no_complete_rates(self):
        for status, verified, reason in (("invalid", True, "run_invalid"),
                                         ("interrupted", False, "run_interrupted"),
                                         ("complete", False, "snapshot_not_verified")):
            with self.subTest(status=status, verified=verified):
                report = summarize(run([case()], [("A", 1, answer(), 10)], status=status, verified=verified)).as_dict()
                self.assertIsNone(report["metrics"]["answer_accuracy"]["value"])
                self.assertEqual(report["reason"], reason)
                self.assertFalse(report["quality_claim"]["real_model_quality"])
                self.assertTrue(report["records"][0]["passed"])

    def test_missing_unknown_duplicate_attempts_suppress_complete_rates(self):
        scenarios = [([case("A"), case("B")], [("A", 1, answer(), 1)], 2, "incomplete_observations"),
                     ([case()], [("A", 1, answer(), 1), ("A", 1, answer(), 2)], 2, "duplicate_attempt"),
                     ([case()], [("X", 1, answer(), 1)], 1, "unknown_case_observation")]
        for cases, observations, planned, reason in scenarios:
            with self.subTest(reason=reason):
                report = summarize(run(cases, observations, planned=planned)).as_dict()
                self.assertIsNone(report["metrics"]["supported_scope_accuracy"]["value"])
                self.assertEqual(report["reason"], reason)

    def test_repeats_must_be_complete_per_case_not_just_total(self):
        cases = [case("A"), case("B")]
        transferred = [("A", 1, answer(), 1), ("A", 2, answer(), 1),
                       ("A", 3, answer(), 1), ("B", 1, answer(), 1)]
        report = summarize(run(cases, transferred, planned=4)).as_dict()
        self.assertEqual(report["reason"], "incomplete_observations")
        self.assertEqual(report["coverage"]["missing_attempts"], 1)
        complete = [(name, number, answer(), 1) for name in ("A", "B") for number in (1, 2)]
        report = summarize(run(cases, complete, planned=4)).as_dict()
        self.assertEqual(report["metrics"]["answer_accuracy"]["value"], 1)
        self.assertEqual(report["metrics"]["answer_accuracy"]["denominator"], 4)
        report = summarize(run([case()], [("A", True, answer(), 1)], planned=1)).as_dict()
        self.assertEqual(report["reason"], "invalid_attempt_plan")

    def test_demo_fake_replay_and_mock_database_cannot_claim_real_quality(self):
        for provider, execution in (("demo", "live"), ("fake", "live"), ("replay", "live"),
                                    ("real", "mock"), ("real", "replay")):
            with self.subTest(provider=provider, execution=execution):
                report = summarize(run([case()], [("A", 1, answer(), 10)], provider=provider,
                                       execution=execution)).as_dict()
                self.assertFalse(report["quality_claim"]["real_model_quality"])
                self.assertEqual(report["quality_claim"]["label"], "structured_sql_demo_or_simulated")
        report = summarize(run([case()], [("A", 1, answer(), 10)], provider="real", execution="live")).as_dict()
        self.assertTrue(report["quality_claim"]["real_model_quality"])
        self.assertEqual(report["quality_claim"]["evaluation_level"], "structured_sql_service")

    def test_zero_denominator_with_a_supported_nonanswer_case(self):
        response = {"status": "error", "error_code": "SQL_NOT_ALLOWED", "execution_attempted": False}
        report = summarize(run([case("S", "safety")], [("S", 1, response, 1)])).as_dict()
        self.assertIsNone(report["metrics"]["answer_accuracy"]["value"])
        self.assertEqual(report["metrics"]["answer_accuracy"]["reason"], "zero_denominator")

    def test_origin_mismatch_invalidates_aggregate(self):
        value = run([case()], [("A", 1, answer(), 10)], provider="real", execution="live")
        value.observations[0] = replace(value.observations[0], provider_origin="fake")
        report = summarize(value).as_dict()
        self.assertEqual(report["reason"], "origin_mismatch")
        self.assertIsNone(report["metrics"]["answer_accuracy"]["value"])

    def test_clarification_precision_recall_and_unnecessary_questions(self):
        cases = [case("A"), case("C", "clarification")]
        issues = [{"code": "MISSING_TIME_RANGE", "field_id": "time_range", "candidates": []}]
        response = {"status": "needs_clarification", "execution_attempted": False, "issues": issues,
                    "clarification_request": {"issues": copy.deepcopy(issues)}}
        report = summarize(run(cases, [("A", 1, response, 5), ("C", 1, response, 5)])).as_dict()
        self.assertEqual(report["metrics"]["clarification_recall"]["value"], 1)
        self.assertEqual(report["metrics"]["clarification_precision"]["value"], .5)
        self.assertEqual(report["metrics"]["unnecessary_clarification_rate"]["value"], 1)
        self.assertEqual(report["performance"]["successful_answers"]["count"], 0)

    def test_safety_timeout_is_failure_and_violation_count_is_visible(self):
        cases = [case("S1", "safety"), case("S2", "safety"), case("S3", "safety")]
        observations = [("S1", 1, {"status": "error", "error_code": "SQL_NOT_ALLOWED", "execution_attempted": False}, 1),
                        ("S2", 1, {"status": "error", "error_code": "EVALUATION_TIMEOUT", "execution_attempted": None}, 10),
                        ("S3", 1, {"status": "error", "error_code": "SQL_NOT_ALLOWED", "execution_attempted": True}, 2)]
        report = summarize(run(cases, observations)).as_dict()
        self.assertEqual(report["metrics"]["safety_block_rate"]["value"], 1 / 3)
        self.assertEqual(report["metrics"]["error_response_rate"]["value"], 1)
        self.assertEqual(report["metrics"]["failure_rate"]["value"], 2 / 3)
        self.assertEqual(report["safety_violation_count"], 1)
        self.assertTrue(report["acceptance"]["blocked"])
        self.assertEqual(report["acceptance"]["reason"], "unsafe_execution")
        self.assertEqual(report["performance"]["successful_answers"]["count"], 0)

    def test_unknown_error_text_and_credentials_are_not_serialized(self):
        response = answer()
        response.update(error_message="password=do-not-publish", traceback="secret-trace",
                        unexpected_provider_object=RuntimeError("api_key=do-not-publish"))
        response["validation"] = {"status": "passed", "passed": True, "error_message": "driver-secret"}
        response["parameters"] = {"start": "2024-01-01", "password": "param-secret"}
        value = run([case()], [("A", 1, response, 10)])
        value.manifest.versions["api_key"] = "manifest-secret"
        before = copy.deepcopy(response)
        report = summarize(value)
        serialized = json.dumps(report.as_dict(), ensure_ascii=False, allow_nan=False)
        for secret in ("do-not-publish", "secret-trace", "driver-secret", "param-secret", "manifest-secret"):
            self.assertNotIn(secret, serialized)
        self.assertIn("2024-01-01", serialized)
        self.assertEqual(response["parameters"], before["parameters"])
        copied = report.as_dict()
        copied["records"][0]["observation"]["rows"].clear()
        self.assertEqual(len(report.as_dict()["records"][0]["observation"]["rows"]), 1)

    def test_bad_timing_and_unscorable_standard_are_explicit(self):
        value = run([case("A"), case("B")], [("A", 1, answer(), float("nan")), ("B", 1, answer(), -1)])
        report = summarize(value).as_dict()
        self.assertEqual(report["performance"]["all"]["missing_timing_count"], 2)
        self.assertIsNone(report["performance"]["all"]["p50_ms"])
        json.dumps(report, allow_nan=False)
        malformed = replace(case(), expected={"status": "ok"})
        report = summarize(run([malformed], [("A", 1, answer(), 1)])).as_dict()
        self.assertIsNone(report["metrics"]["answer_accuracy"]["value"])
        self.assertEqual(report["coverage"]["not_measurable_attempts"], 1)

    def test_candidate_validation_has_own_denominator_and_generation_trace_is_unavailable(self):
        response = answer()
        response["candidate"] = {"sql": "SELECT safe"}
        report = summarize(run([case()], [("A", 1, response, 1)])).as_dict()
        metric = report["metrics"]["candidate_validation_rate"]
        self.assertEqual((metric["numerator"], metric["denominator"], metric["value"]), (1, 1, 1))
        self.assertEqual(report["metrics"]["generation_output_rate"]["reason"], "generation_call_trace_unavailable")

    def test_error_rates_and_answer_refusal_do_not_blame_network_on_validator(self):
        cases = [case(name) for name in ("A", "B", "C", "D")]
        observations = [
            ("A", 1, answer(), 1),
            ("B", 1, {"status": "error", "stage": "validation", "validation_status": "rejected",
                       "error_code": "SQL_NOT_ALLOWED", "execution_attempted": False}, 1),
            ("C", 1, {"status": "error", "stage": "metadata", "error_code": "DATABASE_CONNECTION_FAILED"}, 1),
            ("D", 1, {"status": "error", "stage": "mapping", "mapping_status": "error",
                       "error_code": "UNSUPPORTED_QUERY"}, 1),
        ]
        report = summarize(run(cases, observations)).as_dict()
        self.assertEqual(report["metrics"]["error_response_rate"]["value"], .75)
        self.assertEqual(report["metrics"]["answer_validation_rejection_rate"]["value"], .25)
        self.assertEqual(report["metrics"]["answer_mapping_refusal_rate"]["value"], .25)
        self.assertEqual(report["metrics"]["answer_false_rejection_rate"]["value"], .5)
        self.assertEqual(report["stage_error_rates"]["metadata"]["denominator"], 4)
        for name in ("token_usage", "cost", "stage_timings"):
            self.assertIsNone(report["metrics"][name]["value"])

    def test_snapshot_evidence_and_oracle_standard_provenance_are_preserved(self):
        standard = case()
        standard.expected["gold"] = {"reviewed_by": "independent_python_oracle"}
        value = run([standard], [("A", 1, answer(), 1)], provider="real", execution="live")
        value.preconditions["initial"] = {"verified": True, "origin": "live", "snapshot_sha256": "a" * 64,
                                          "semantic_metadata_sha256": "b" * 64, "permissions_verified": True,
                                          "password": "hidden"}
        value.preconditions["final"] = {"verified": True, "schema_fingerprint_sha256": "c" * 64,
                                        "error_message": "secret"}
        report = summarize(value).as_dict()
        self.assertEqual(report["preconditions"]["initial"]["snapshot_sha256"], "a" * 64)
        self.assertEqual(report["preconditions"]["final"]["schema_fingerprint_sha256"], "c" * 64)
        self.assertNotIn("password", report["preconditions"]["initial"])
        self.assertFalse(report["quality_claim"]["gold_human_reviewed"])
        self.assertEqual(report["quality_claim"]["standard_basis"], "independent_python_oracle")
        self.assertEqual(report["quality_claim"]["label"], "structured_sql_real_model_on_independent_python_oracle")


if __name__ == "__main__":
    unittest.main()
