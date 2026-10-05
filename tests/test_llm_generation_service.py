"""LLM 生成入口的离线契约测试：只产生候选，禁止访问数据库。"""
from __future__ import annotations

import asyncio
import copy
import json
import os
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from agent_core.providers.models import LLMRequest, LLMResponse
from agent_core.sql.generation_service import SQLGenerationService
from database.metadata_reader import FileMetadataReader


DATABASE_DIR = Path(__file__).resolve().parents[1] / "database"
SQL_TEXT = (
    "SELECT COALESCE(SUM(o.total), 0) AS sales_amount "
    "FROM public.invoice o WHERE o.invoice_date >= :start AND o.invoice_date < :end "
    "AND o.billing_country = :filter_0 LIMIT :fetch_limit"
)


def sales_query(value="美国", field="country"):
    return {
        "question": "查询2024年美国销售额，不扣退款",
        "source_id": "chinook_db",
        "metric_id": "sales_amount",
        "metric_definition_id": "chinook_sales_amount_v1",
        "time_range": {"start": "2024-01-01", "end_exclusive": "2025-01-01"},
        "filters": [{"field_id": field, "op": "eq", "value": value}],
    }


class FakeProvider:
    def __init__(self, content=None):
        self.content = json.dumps({"sql": SQL_TEXT}) if content is None else content
        self.requests = []

    async def generate(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        await asyncio.sleep(0)
        return LLMResponse(content=self.content)


class SQLGenerationServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.config = json.loads((DATABASE_DIR / "source_config.json").read_text(encoding="utf-8-sig"))
        guard = patch(
            "database.sql_common.ReadOnlyClient.transaction",
            side_effect=AssertionError("生成阶段的离线测试禁止访问数据库"),
        )
        self.database_guard = guard.start()
        self.addCleanup(guard.stop)
        self.addCleanup(self.database_guard.assert_not_called)
        self.provider = FakeProvider()

    def service(self, provider=None):
        return SQLGenerationService(
            self.provider if provider is None else provider,
            base_dir=DATABASE_DIR,
            metadata_reader=FileMetadataReader(DATABASE_DIR, self.config),
            config=self.config,
        )

    async def test_beijing_maps_to_real_identifier_and_returns_unvalidated_candidate(self):
        result = await self.service().generate(sales_query(), {"max_rows": 10})

        self.assertEqual(result["status"], "generated")
        self.assertEqual(result["route_to"], "sql_validation")
        self.assertIs(result["execution_attempted"], False)
        self.assertEqual(result["candidate"]["sql"], SQL_TEXT)
        self.assertIs(result["candidate"]["validated"], False)
        self.assertEqual(result["candidate"]["source_id"], "chinook_db")
        self.assertEqual(result["candidate"]["metric_definition_id"], "chinook_sales_amount_v1")
        self.assertEqual(result["parameters"]["filter_0"], "USA")
        self.assertNotIn("status", result["parameters"])
        self.assertEqual(result["parameters"]["fetch_limit"], 11)
        self.assertEqual(len(self.provider.requests), 1)
        self.assertIsInstance(self.provider.requests[0], LLMRequest)
        json.dumps(result, allow_nan=False)

    async def test_dates_are_trusted_server_parameters_with_beijing_timezone(self):
        result = await self.service().generate(sales_query())

        self.assertEqual(result["status"], "generated")
        for name, expected in (("start", "2024-01-01"), ("end", "2025-01-01")):
            value = result["parameters"][name]
            self.assertIsInstance(value, str)
            parsed = datetime.fromisoformat(value)
            self.assertEqual(parsed.date().isoformat(), expected)
            self.assertIsNone(parsed.tzinfo)
        self.assertEqual(
            set(result["candidate"]["parameter_names"]),
            set(result["parameters"]),
        )

    async def test_duplicate_employee_returns_both_candidates_without_calling_model(self):
        result = await self.service().generate(sales_query("Mark", "customer"))

        self.assertEqual(result["status"], "needs_clarification")
        self.assertEqual(result["route_to"], "clarification")
        self.assertIs(result["execution_attempted"], False)
        issues = result["clarification_request"]["issues"]
        ambiguous = next(item for item in issues if item["code"] == "AMBIGUOUS_ENTITY")
        self.assertEqual(
            {item["id"] for item in ambiguous["candidates"]},
            {14, 55},
        )
        self.assertEqual(self.provider.requests, [])

    async def test_missing_time_routes_to_clarification_without_calling_model(self):
        query = sales_query()
        del query["time_range"]

        result = await self.service().generate(query)

        self.assertEqual(result["status"], "needs_clarification")
        self.assertIs(result["execution_attempted"], False)
        self.assertIn(
            "MISSING_TIME_RANGE",
            {issue["code"] for issue in result["clarification_request"]["issues"]},
        )
        self.assertEqual(self.provider.requests, [])

    async def test_unknown_source_is_rejected_before_model_call(self):
        query = sales_query()
        query["source_id"] = "unregistered_db"

        result = await self.service().generate(query)

        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error_code"], "UNKNOWN_SOURCE")
        self.assertIs(result["execution_attempted"], False)
        self.assertEqual(self.provider.requests, [])

    async def test_invalid_model_json_returns_generation_error(self):
        provider = FakeProvider(content="```sql\nSELECT 1\n```")

        result = await self.service(provider).generate(sales_query())

        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error_code"], "LLM_RESPONSE_INVALID")
        self.assertIs(result["execution_attempted"], False)
        self.assertEqual(len(provider.requests), 1)

    async def test_context_identifiers_survive_generation_and_clarification(self):
        context = {
            "session_id": "session-1", "task_id": "task-2",
            "request_id": "request-3", "step_id": "step-4",
        }
        service = self.service()
        generated = await service.generate(sales_query(), context)
        clarification = await service.generate(sales_query("Mark", "customer"), context)

        self.assertEqual(generated["status"], "generated")
        self.assertEqual(clarification["status"], "needs_clarification")
        for key, value in context.items():
            self.assertEqual(generated[key], value)
            self.assertEqual(generated["candidate"][key], value)
            self.assertEqual(clarification[key], value)
        self.assertEqual(len(self.provider.requests), 1)

    async def test_model_receives_schema_without_passwords_or_raw_order_records(self):
        secret = "offline-password-sentinel-7b34"
        with patch.dict(os.environ, {"PGPASSWORD": secret}, clear=False):
            result = await self.service().generate(sales_query())

        self.assertEqual(result["status"], "generated")
        content = "\n".join(message.content for message in self.provider.requests[0].messages)
        self.assertNotIn("PGPASSWORD", content)
        self.assertNotIn(secret, content)
        self.assertNotIn("customer@example.com", content)
        self.assertNotIn("postal_code", content)
        self.assertNotIn("USA", content)
        self.assertNotIn("2024-01-01", content)
        self.assertIn("public.invoice", content)
        self.assertIn("total", content)

    async def test_query_and_context_are_not_modified(self):
        query = sales_query()
        context = {"session_id": "session-original", "max_rows": 7}
        before_query, before_context = copy.deepcopy(query), copy.deepcopy(context)

        result = await self.service().generate(query, context)

        self.assertEqual(result["status"], "generated")
        self.assertEqual(query, before_query)
        self.assertEqual(context, before_context)


if __name__ == "__main__":
    unittest.main()
