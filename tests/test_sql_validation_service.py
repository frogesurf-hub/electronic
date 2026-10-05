"""真实文件元数据/映射/生成/检验的衔接测试；不访问数据库和模型 API。"""
from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_core.providers import LLMResponse
from agent_core.sql.generation_service import SQLGenerationService
from database.metadata_reader import FileMetadataReader

DATABASE_DIR = Path(__file__).resolve().parents[1] / "database"
VALID_SQL = (
    "SELECT COALESCE(SUM(x.total), 0) AS sales_amount "
    "FROM public.invoice x WHERE x.billing_country = :filter_0 "
    "AND x.invoice_date < :end "
    "AND x.invoice_date >= :start LIMIT :fetch_limit"
)
QUERY = {
    "source_id": "chinook_db", "metric_id": "sales_amount",
    "metric_definition_id": "chinook_sales_amount_v1",
    "time_range": {"start": "2024-01-01", "end_exclusive": "2025-01-01"},
    "filters": [{"field_id": "country", "op": "eq", "value": "美国"}],
}


class ExampleProvider:
    def __init__(self, sql=VALID_SQL):
        self.sql, self.calls = sql, 0

    async def generate(self, request):
        self.calls += 1
        return LLMResponse(json.dumps({"sql": self.sql}))


class SQLValidationServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.config = json.loads((DATABASE_DIR / "source_config.json").read_text(encoding="utf-8-sig"))
        guard = patch("database.sql_common.ReadOnlyClient.transaction",
                      side_effect=AssertionError("检验衔接测试禁止数据库访问"))
        self.database_guard = guard.start()
        self.addCleanup(guard.stop)
        self.addCleanup(self.database_guard.assert_not_called)

    def service(self, provider):
        return SQLGenerationService(provider, base_dir=DATABASE_DIR,
                                    metadata_reader=FileMetadataReader(DATABASE_DIR, self.config), config=self.config)

    async def test_generation_flows_to_validator_using_same_metadata_and_bindings(self):
        provider = ExampleProvider()
        result = await self.service(provider).generate_and_validate(
            QUERY, {"max_rows": 10, "db_timeout_ms": 2500, "request_id": "req-validation"},
        )
        self.assertEqual(result["status"], "validated", result)
        self.assertEqual(result["route_to"], "sql_execution")
        self.assertTrue(result["validation"]["passed"])
        self.assertEqual(provider.calls, 1)
        self.assertFalse(result["execution_attempted"])
        self.assertFalse(result["candidate"]["validated"])
        package = result["execution_package"]
        self.assertTrue(package["validated"])
        self.assertEqual(package["candidate_sql"], VALID_SQL)
        self.assertIn("%(filter_0)s", package["sql"])
        self.assertEqual(package["parameters"]["filter_0"], "USA")
        self.assertEqual(package["parameters"]["fetch_limit"], 11)
        self.assertEqual(package["db_timeout_ms"], 2500)
        self.assertEqual(package["max_rows"], 10)
        self.assertEqual(package["request_id"], "req-validation")
        self.assertEqual(package["schema_fingerprint_sha256"],
                         result["metadata"]["schema_fingerprint_sha256"])
        json.dumps(result, ensure_ascii=False, allow_nan=False)

    async def test_unsafe_candidates_are_rejected_without_execution_package(self):
        for sql in (
            VALID_SQL + "; DELETE FROM public.invoice",
            VALID_SQL.replace("x.billing_country = :filter_0 AND ", ""),
            VALID_SQL.replace("x.invoice_date < :end", "x.invoice_date <= :end"),
            VALID_SQL.replace(":filter_0", "'Canada'"),
            "SELECT FROM WHERE (",
        ):
            with self.subTest(sql=sql):
                provider = ExampleProvider(sql)
                result = await self.service(provider).generate_and_validate(QUERY)
                self.assertEqual(result["status"], "error", result)
                self.assertEqual(result["validation_status"], "rejected")
                self.assertIsNone(result["route_to"])
                self.assertIsNone(result["execution_package"])
                self.assertFalse(result["execution_attempted"])
                self.assertEqual(provider.calls, 1)

    async def test_clarification_stops_before_model_and_validator(self):
        provider = ExampleProvider()
        query = copy.deepcopy(QUERY)
        del query["metric_definition_id"]
        with patch("database.sql_validator.SQLValidator.validate",
                   side_effect=AssertionError("尚待澄清不能检验 SQL")) as validator:
            result = await self.service(provider).generate_and_validate(query)
        self.assertEqual(result["status"], "needs_clarification", result)
        self.assertEqual(provider.calls, 0)
        validator.assert_not_called()
        self.assertIsNone(result["execution_package"])

    async def test_original_generation_api_still_returns_unvalidated_candidate(self):
        provider = ExampleProvider()
        with patch("database.sql_validator.SQLValidator.validate",
                   side_effect=AssertionError("第一项 API 不主动调用第二项")) as validator:
            result = await self.service(provider).generate(QUERY)
        self.assertEqual(result["status"], "generated", result)
        self.assertEqual(result["route_to"], "sql_validation")
        self.assertEqual(result["validation_status"], "not_validated")
        self.assertNotIn("execution_package", result)
        validator.assert_not_called()


if __name__ == "__main__":
    unittest.main()
