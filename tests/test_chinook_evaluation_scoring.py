"""Chinook 的无时区时间、整数实体与源币种评分边界。"""
import copy
import unittest

from agent_core.evaluation.contracts import EvalCase, Observation
from agent_core.evaluation.scoring import score_case


def answer():
    expected = {"status": "ok", "source_id": "chinook_db", "metric_definition_id": "chinook_sales_amount_v1",
                "unit": "金额", "columns": [{"name": "sales_amount", "data_type": "decimal", "unit": "金额"}],
                "rows": [{"sales_amount": "127.98"}], "tables": ["public.invoice"],
                "parameters": {"start": "2024-01-01T00:00:00", "end": "2025-01-01T00:00:00",
                               "filter_0": 1, "fetch_limit": 101}}
    case = EvalCase("CH-A", "answer", {}, {}, expected, scope="chinook_sql_v1")
    response = {**copy.deepcopy(expected), "row_count": 1, "truncated": False,
                "validation_status": "passed", "execution_attempted": True,
                "metadata": {"mode": "live", "live_permissions_verified": True},
                "execution_metadata": {"mode": "live", "permissions_verified": True,
                                       "same_transaction_revalidation": True}}
    return case, response


def score(case, response):
    return score_case(case, Observation(case.case_id, 1, response, 1.0, "demo", "mock"))


class ChinookScoringTests(unittest.TestCase):
    def test_naive_calendar_parameter_accepts_date_without_timezone_conversion(self):
        case, response = answer()
        response["parameters"]["start"] = "2024-01-01"
        self.assertTrue(score(case, response).passed)

    def test_timezone_equivalent_instant_still_fails_naive_calendar_contract(self):
        for time in ("2024-01-01T00:00:00+08:00", "2023-12-31T16:00:00Z"):
            case, response = answer()
            response["parameters"]["start"] = time
            result = score(case, response)
            self.assertFalse(result.checks["parameters"])
            self.assertEqual(result.failure_category, "parameter_mismatch")

    def test_customer_id_type_and_extra_paid_predicate_cannot_pass(self):
        for value in (True, "1", 1.0):
            case, response = answer()
            response["parameters"]["filter_0"] = value
            self.assertFalse(score(case, response).checks["parameters"])
        case, response = answer()
        response["parameters"]["status"] = "paid"
        self.assertFalse(score(case, response).checks["parameters"])

    def test_unspecified_currency_is_not_scored_as_yuan(self):
        case, response = answer()
        response["columns"][0]["unit"] = "元"
        self.assertFalse(score(case, response).checks["unit"])

    def test_clarification_candidates_preserve_integer_ids(self):
        case = EvalCase("CH-C", "clarification", {}, {}, {
            "status": "needs_clarification", "execution_allowed": False,
            "issue_codes": ["AMBIGUOUS_ENTITY"], "clarification_fields": ["customer"],
            "permitted_candidates": {"customer": [1, 2]}}, scope="chinook_sql_v1")
        issues = [{"code": "AMBIGUOUS_ENTITY", "field_id": "customer", "candidates": [{"id": 2}, {"id": 1}]}]
        response = {"status": "needs_clarification", "execution_attempted": False,
                    "issues": issues, "clarification_request": {"issues": copy.deepcopy(issues)}}
        self.assertTrue(score(case, response).passed)
        for invalid in (True, "1"):
            response["issues"][0]["candidates"][1]["id"] = invalid
            response["clarification_request"]["issues"] = copy.deepcopy(response["issues"])
            self.assertFalse(score(case, response).passed)
