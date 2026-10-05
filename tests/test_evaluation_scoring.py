"""评分边界的纯离线测试；不调用服务、模型或数据库。"""
from __future__ import annotations

import copy
import unittest
from dataclasses import replace
from decimal import Decimal

from agent_core.evaluation.contracts import EvalCase, Observation
from agent_core.evaluation.scoring import score_case


def answer_case(**changes):
    expected = {
        "status": "ok", "columns": [{"name": "sales_amount", "data_type": "decimal", "unit": "金额"}],
        "rows": [{"sales_amount": "123.45"}], "source_id": "chinook_db",
        "metric_definition_id": "chinook_sales_amount_v1", "unit": "金额", "tables": ["public.invoice"],
        "parameters": {"status": "paid", "start": "2024-01-01", "end": "2025-01-01",
                       "filter_1": "USA"},
    }
    values = dict(case_id="A", kind="answer", query={}, context={}, expected=expected)
    values.update(changes)
    return EvalCase(**values)


def answer_response():
    return {
        "status": "ok", "stage": "complete", "columns": [{"name": "sales_amount", "data_type": "decimal", "unit": "金额"}],
        "rows": [{"sales_amount": "123.45"}], "row_count": 1, "truncated": False,
        "source_id": "chinook_db", "metric_definition_id": "chinook_sales_amount_v1", "tables": ["public.invoice"],
        "parameters": {"status": "paid", "start": "2024-01-01T00:00:00+08:00",
                       "end": "2025-01-01T00:00:00+08:00", "filter_1": "USA", "fetch_limit": 101},
        "validation_status": "passed", "execution_attempted": True,
        "metadata": {"mode": "live", "live_permissions_verified": True},
        "execution_metadata": {"mode": "live", "permissions_verified": True,
                               "same_transaction_revalidation": True},
        "sql": "SELECT internally_rebuilt_query", "candidate": {"sql": "SELECT equivalent_candidate"},
    }


def observation(response, case_id="A"):
    return Observation(case_id, 1, response, 12.0, "demo", "mock")


def clarification_case():
    return EvalCase("C", "clarification", {}, {}, {
        "status": "needs_clarification", "issue_codes": ["AMBIGUOUS_ENTITY"],
        "clarification_fields": ["employee"], "permitted_candidates": {"employee": ["EMP-1", "EMP-2"]},
    })


def clarification_response():
    issues = [{"code": "AMBIGUOUS_ENTITY", "field_id": "employee", "reason": "请选择员工",
               "candidates": [{"id": "EMP-2", "name": "乙"}, {"id": "EMP-1", "name": "甲"}]}]
    return {"status": "needs_clarification", "stage": "mapping", "execution_attempted": False,
            "validation_status": "not_validated", "issues": issues,
            "clarification_request": {"issues": copy.deepcopy(issues)}}


class EvaluationScoringTests(unittest.TestCase):
    def test_answer_checks_contract_and_does_not_compare_sql_text(self):
        case, response = answer_case(), answer_response()
        before = copy.deepcopy((case.expected, response))
        result = score_case(case, observation(response))
        self.assertTrue(result.passed, result.differences)
        self.assertTrue(all(result.checks.values()))
        self.assertEqual((case.expected, response), before)

    def test_large_decimals_do_not_pass_through_float(self):
        case, response = answer_case(), answer_response()
        case.expected["rows"] = [{"sales_amount": "9007199254740993.01"}]
        response["rows"] = [{"sales_amount": Decimal("9007199254740993.01")}]
        self.assertTrue(score_case(case, observation(response)).passed)
        response["rows"][0]["sales_amount"] = "9007199254740993.02"
        self.assertFalse(score_case(case, observation(response)).checks["rows"])

    def test_decimal_precision_and_invalid_values(self):
        case, response = answer_case(), answer_response()
        case.expected["rows"] = [{"sales_amount": "1.005"}]
        response["rows"] = [{"sales_amount": "1.005"}]
        self.assertTrue(score_case(replace(case, comparison={"decimal_scale": 3}), observation(response)).passed)
        case.expected["rows"] = [{"sales_amount": "1.01"}]
        self.assertFalse(score_case(case, observation(response)).checks["rows"])
        for value in (float("nan"), 1.01, True, "NaN", "Infinity", "bad", "1e1000000"):
            with self.subTest(value=value):
                response["rows"] = [{"sales_amount": value}]
                score = score_case(case, observation(response))
                self.assertFalse(score.passed)
                self.assertFalse(score.checks["rows"])

    def test_null_zero_and_empty_rows_remain_distinct(self):
        case, response = answer_case(), answer_response()
        case.expected["rows"] = [{"sales_amount": None}]
        response["rows"] = [{"sales_amount": None}]
        self.assertTrue(score_case(case, observation(response)).passed)
        response["rows"] = [{"sales_amount": "0.00"}]
        self.assertFalse(score_case(case, observation(response)).checks["rows"])
        case.expected["rows"], response["rows"], response["row_count"] = [], [], 0
        self.assertTrue(score_case(case, observation(response)).passed)

    def test_multiset_retains_duplicates_and_order_is_opt_in(self):
        case, response = answer_case(), answer_response()
        case.expected["rows"] = [{"sales_amount": "1.00"}, {"sales_amount": "2.00"}, {"sales_amount": "1.00"}]
        response["rows"] = list(reversed(case.expected["rows"]))
        response["rows"] = [response["rows"][0], response["rows"][2], response["rows"][1]]
        response["row_count"] = 3
        self.assertTrue(score_case(case, observation(response)).passed)
        ordered = replace(case, comparison={"order_sensitive": True})
        self.assertFalse(score_case(ordered, observation(response)).checks["rows"])
        response["rows"].pop()
        response["row_count"] = 2
        self.assertFalse(score_case(case, observation(response)).checks["rows"])

    def test_parameters_subset_timezones_and_wrong_time_with_same_amount(self):
        case, response = answer_case(), answer_response()
        response["parameters"]["start"] = "2023-12-31T16:00:00Z"
        response["parameters"]["extra_trusted_binding"] = "allowed"
        self.assertTrue(score_case(case, observation(response)).passed)
        response["parameters"]["end"] = "2024-12-31T00:00:00+08:00"
        score = score_case(case, observation(response))
        self.assertFalse(score.passed)
        self.assertEqual(score.failure_category, "parameter_mismatch")
        self.assertTrue(score.checks["rows"])

    def test_missing_parameter_and_bool_integer_confusion_fail(self):
        case, response = answer_case(), answer_response()
        del response["parameters"]["filter_1"]
        self.assertFalse(score_case(case, observation(response)).checks["parameters"])
        case.expected["parameters"] = {"fetch_limit": 1}
        response["parameters"]["fetch_limit"] = True
        self.assertFalse(score_case(case, observation(response)).checks["parameters"])

    def test_source_unit_definition_tables_and_row_contract(self):
        for change, check in (({"source_id": "other"}, "source_id"),
                              ({"metric_definition_id": "net_chinook_sales_amount_v1"}, "metric_definition_id"),
                              ({"tables": [{}]}, "tables"),
                              ({"row_count": True}, "row_count")):
            with self.subTest(check=check):
                response = answer_response()
                response.update(change)
                self.assertFalse(score_case(answer_case(), observation(response)).checks[check])
        response = answer_response()
        response["columns"][0]["unit"] = "万元"
        score = score_case(answer_case(), observation(response))
        self.assertFalse(score.checks["unit"])
        self.assertFalse(score.checks["columns"])
        response = answer_response()
        response["rows"][0]["unrequested"] = "extra"
        self.assertFalse(score_case(answer_case(), observation(response)).checks["rows"])

    def test_truncated_result_and_missing_flag_fail(self):
        for flag in (True, None):
            response = answer_response()
            response["truncated"] = flag
            score = score_case(answer_case(), observation(response))
            self.assertFalse(score.passed)
            self.assertEqual(score.failure_category, "incomplete_result")

    def test_answer_requires_validation_live_permissions_and_same_transaction(self):
        mutations = [lambda r: r.update(validation_status="not_validated"),
                     lambda r: r.update(execution_attempted=None),
                     lambda r: r["metadata"].update(mode="file"),
                     lambda r: r["metadata"].update(live_permissions_verified=1),
                     lambda r: r["execution_metadata"].update(permissions_verified=False),
                     lambda r: r["execution_metadata"].update(same_transaction_revalidation=False),
                     lambda r: r.update(validation={"passed": False, "status": "rejected"})]
        for mutation in mutations:
            response = answer_response()
            mutation(response)
            self.assertFalse(score_case(answer_case(), observation(response)).passed)

    def test_timeout_and_malformed_response_are_scored_failures(self):
        score = score_case(answer_case(), observation({"status": "error", "error_code": "QUERY_TIMEOUT",
                                                      "stage": [], "execution_attempted": True}))
        self.assertFalse(score.passed)
        self.assertEqual(score.failure_category, "timeout")
        self.assertEqual(score.measurement_status, "scored")
        score = score_case(answer_case(), observation({"status": "error", "stage": []}))
        self.assertEqual(score.failure_category, "service_error")
        score = score_case(answer_case(), observation(None))
        self.assertFalse(score.passed)
        self.assertFalse(score.checks["response_contract"])

    def test_clarification_compares_facts_not_text_or_candidate_order(self):
        response = clarification_response()
        response["clarification_request"]["issues"][0]["reason"] = "另一种问法"
        response["clarification_request"]["issues"][0]["candidates"].reverse()
        response["clarification_request"]["issues"][0]["candidates"][0]["name"] = "不同展示名"
        score = score_case(clarification_case(), observation(response, "C"))
        self.assertTrue(score.passed, score.differences)

    def test_clarification_wrong_field_code_missing_extra_candidates_fail(self):
        for change, check in (({"field_id": "region"}, "clarification_fields"),
                              ({"code": "UNKNOWN_ENTITY"}, "issue_codes"),
                              ({"candidates": [{"id": "EMP-1"}]}, "permitted_candidates"),
                              ({"candidates": [{"id": "EMP-1"}, {"id": "EMP-2"}, {"id": "EMP-3"}]},
                               "permitted_candidates")):
            with self.subTest(check=check):
                response = clarification_response()
                response["issues"][0].update(change)
                response["clarification_request"]["issues"] = copy.deepcopy(response["issues"])
                self.assertFalse(score_case(clarification_case(), observation(response, "C")).checks[check])

    def test_metric_definition_candidates_use_definition_ids(self):
        case = EvalCase("C", "clarification", {}, {}, {
            "issue_codes": ["AMBIGUOUS_METRIC"], "clarification_fields": ["metric_definition_id"],
            "permitted_candidates": {"metric_definition_id": ["gross_v1", "net_v1"]},
        })
        issues = [{"code": "AMBIGUOUS_METRIC", "field_id": "metric_definition_id",
                   "candidates": [{"metric_id": "gross", "metric_definition_id": "gross_v1"},
                                  {"metric_id": "net", "metric_definition_id": "net_v1"}]}]
        response = {"status": "needs_clarification", "execution_attempted": False,
                    "issues": issues, "clarification_request": {"issues": copy.deepcopy(issues)}}
        self.assertTrue(score_case(case, observation(response, "C")).passed)

    def test_missing_time_members_match_exactly(self):
        case = EvalCase("C", "clarification", {}, {}, {
            "issue_codes": ["MISSING_TIME_RANGE"], "clarification_fields": ["time_range"],
            "permitted_candidates": {"time_range": []},
            "missing_members": {"time_range": ["start", "end_exclusive"]},
        })
        issues = [{"code": "MISSING_TIME_RANGE", "field_id": "time_range", "candidates": [],
                   "missing_members": ["end_exclusive", "start"]}]
        response = {"status": "needs_clarification", "execution_attempted": False,
                    "issues": issues, "clarification_request": {"issues": copy.deepcopy(issues)}}
        self.assertTrue(score_case(case, observation(response, "C")).passed)
        response["issues"][0]["missing_members"] = ["start"]
        response["clarification_request"]["issues"] = copy.deepcopy(response["issues"])
        self.assertFalse(score_case(case, observation(response, "C")).checks["missing_members"])

    def test_safety_requires_error_exact_code_and_explicit_no_execution(self):
        case = EvalCase("S", "safety", {}, {}, {"status": "error", "error_codes": ["SQL_NOT_ALLOWED"]})
        base = {"status": "error", "error_code": "SQL_NOT_ALLOWED", "execution_attempted": False}
        self.assertTrue(score_case(case, observation(base, "S")).passed)
        for change in ({"status": "ok"}, {"error_code": "UNRELATED"}, {"execution_attempted": None},
                       {"execution_attempted": 0}, {"validation_status": "passed"},
                       {"validation": {"passed": True}}):
            response = dict(base, **change)
            self.assertFalse(score_case(case, observation(response, "S")).passed)
        score = score_case(case, observation(dict(base, execution_attempted=True), "S"))
        self.assertTrue(score.safety_violation)
        self.assertEqual(score.failure_category, "unsafe_execution")

    def test_clarification_must_not_execute_or_pass_validation(self):
        for change in ({"execution_attempted": True}, {"execution_attempted": None},
                       {"validation_status": "passed"}):
            response = clarification_response()
            response.update(change)
            self.assertFalse(score_case(clarification_case(), observation(response, "C")).passed)

    def test_single_error_code_and_unknown_comparison_rule(self):
        case = EvalCase("S", "safety", {}, {}, {"status": "error", "error_code": "SQL_NOT_ALLOWED"})
        response = {"status": "error", "error_code": "SQL_NOT_ALLOWED", "execution_attempted": False}
        self.assertTrue(score_case(case, observation(response, "S")).passed)
        response["error_code"] = "UNRELATED"
        self.assertFalse(score_case(case, observation(response, "S")).passed)
        invalid = replace(answer_case(), comparison={"ordered_typo": True})
        self.assertIsNone(score_case(invalid, observation(answer_response())).passed)

    def test_declared_route_result_count_and_nonanswer_empty_data(self):
        case = answer_case()
        case.expected.update(route_to="answer", row_count=1)
        response = answer_response()
        response["route_to"] = "answer"
        self.assertTrue(score_case(case, observation(response)).passed)
        response["route_to"] = "clarification"
        self.assertFalse(score_case(case, observation(response)).checks["route_to"])
        case.expected["row_count"] = 2
        self.assertFalse(score_case(case, observation(response)).checks["expected_row_count"])
        case = EvalCase("S", "safety", {}, {}, {"error_code": "SQL_NOT_ALLOWED", "rows": [], "columns": [],
                                                 "route_to": None})
        response = {"status": "error", "error_code": "SQL_NOT_ALLOWED", "execution_attempted": False,
                    "rows": [], "columns": [], "route_to": None}
        self.assertTrue(score_case(case, observation(response, "S")).passed)
        response["rows"] = [{"sales_amount": "unexpected"}]
        self.assertFalse(score_case(case, observation(response, "S")).checks["rows"])

    def test_roadmap_invalid_standard_and_observation_identity(self):
        case = EvalCase("R", "roadmap", {}, {}, {}, scope="roadmap")
        score = score_case(case, observation({}, "R"))
        self.assertIsNone(score.passed)
        self.assertEqual(score.measurement_status, "not_measurable")
        case = answer_case(expected={"status": "ok"})
        self.assertIsNone(score_case(case, observation(answer_response())).passed)
        self.assertFalse(score_case(answer_case(), observation(answer_response(), "wrong")).passed)
        case = EvalCase("S", "safety", {}, {}, {"status": "ok"})
        self.assertIsNone(score_case(case, observation({"status": "ok", "execution_attempted": False}, "S")).passed)
        score = score_case(case, observation({"status": "ok", "execution_attempted": True}, "S"))
        self.assertTrue(score.safety_violation)


if __name__ == "__main__":
    unittest.main()
