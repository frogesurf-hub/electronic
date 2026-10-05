"""第一至第三项服务衔接、停止路径、请求总预算与取消。"""
from __future__ import annotations

import asyncio
import copy
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_core.sql.example import OfflineExampleProvider
from agent_core.sql.generation_service import SQLGenerationService
from database.metadata_reader import FileMetadataReader
from database.sql_common import ServiceFault
from database.sql_contracts import ValidatedSQL

BASE = Path(__file__).resolve().parents[1] / "database"
QUERY = {
    "source_id": "chinook_db", "metric_id": "sales_amount", "metric_definition_id": "chinook_sales_amount_v1",
    "time_range": {"start": "2024-01-01", "end_exclusive": "2025-01-01"},
    "filters": [{"field_id": "country", "op": "eq", "value": "美国"}],
}


class FakeLiveReader:
    def __init__(self):
        self.calls = 0

    async def read_async(self, limits):
        self.calls += 1
        metadata = FileMetadataReader(BASE).read(limits)
        metadata.update(mode="live", permissions_verified=True, captured_at="2026-10-03T00:00:00+00:00")
        return metadata


class FakeExecutor:
    def __init__(self, *, failure=None, delay=None):
        self.calls, self.failure, self.delay = [], failure, delay
        self.started, self.closed = asyncio.Event(), asyncio.Event()

    async def execute(self, package, request, schema, *, limits, on_execute):
        self.calls.append((package, copy.deepcopy(request), copy.deepcopy(schema), dict(limits)))
        try:
            if self.failure is not None and not self.failure.execution_attempted:
                raise self.failure
            on_execute()
            self.started.set()
            if self.delay is not None:
                await asyncio.sleep(self.delay)
            if self.failure is not None:
                raise self.failure
            return {
                "columns": [{"name": "sales_amount", "data_type": "decimal", "unit": "元"}],
                "rows": [{"sales_amount": "2323749.97"}], "row_count": 1,
                "truncated": False, "execution_attempted": True, "limits": dict(limits),
                "metadata": {"mode": "live", "permissions_verified": True,
                             "same_transaction_revalidation": True},
            }
        finally:
            self.closed.set()


class InvalidProvider:
    async def generate(self, request):
        from agent_core.providers import LLMResponse
        return LLMResponse('{"sql":"DELETE FROM public.invoice"}')


class ReadOnlyGenerationServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.config = json.loads((BASE / "source_config.json").read_text(encoding="utf-8-sig"))
        for target in ("psycopg.connect", "psycopg.AsyncConnection.connect",
                       "database.sql_common.resolve_connection_parameters"):
            guard = patch(target, side_effect=AssertionError("服务离线测试禁止真实连接"))
            mock = guard.start()
            self.addCleanup(guard.stop)
            self.addCleanup(mock.assert_not_called)
        self.reader, self.executor = FakeLiveReader(), FakeExecutor()

    def service(self, provider=None, reader=None):
        return SQLGenerationService(provider or OfflineExampleProvider(), base_dir=BASE,
                                    metadata_reader=reader or self.reader, executor=self.executor,
                                    config=self.config)

    async def test_three_stages_return_result_and_internal_validated_package(self):
        before = copy.deepcopy(QUERY)
        result = await self.service().query(QUERY, {"max_rows": 10, "db_timeout_ms": 1500,
                                                  "request_id": "three-stages"})
        self.assertEqual(result["status"], "ok", result)
        self.assertEqual(result["route_to"], "answer")
        self.assertEqual(result["stage"], "complete")
        self.assertTrue(result["execution_attempted"])
        self.assertEqual(result["rows"], [{"sales_amount": "2323749.97"}])
        self.assertEqual(result["row_count"], 1)
        package, request, schema, limits = self.executor.calls[0]
        self.assertIsInstance(package, ValidatedSQL)
        self.assertEqual(package.request_id, "three-stages")
        self.assertEqual(package.parameters["filter_0"], "USA")
        self.assertEqual(package.parameters["fetch_limit"], 11)
        self.assertEqual(limits["lock_timeout_ms"], 1000)
        self.assertEqual(limits["db_timeout_ms"], 1500)
        self.assertEqual(result["sql"], package.sql)
        self.assertFalse(result["candidate"]["validated"])
        self.assertEqual(QUERY, before)

    async def test_file_metadata_cannot_start_business_execution_or_model(self):
        class NeverProvider:
            async def generate(self, request):
                raise AssertionError("文件模式不能继续业务查询")
        result = await self.service(NeverProvider(), FileMetadataReader(BASE)).query(QUERY)
        self.assertEqual(result["error_code"], "LIVE_METADATA_REQUIRED")
        self.assertFalse(result["execution_attempted"])
        self.assertEqual(self.executor.calls, [])

    async def test_validation_rejection_stops_before_executor(self):
        result = await self.service(InvalidProvider()).query(QUERY)
        self.assertEqual(result["validation_status"], "rejected")
        self.assertEqual(result["status"], "error")
        self.assertEqual(self.executor.calls, [])
        self.assertFalse(result["execution_attempted"])

    async def test_clarification_stops_before_executor(self):
        query = copy.deepcopy(QUERY)
        del query["metric_definition_id"]
        result = await self.service().query(query)
        self.assertEqual(result["status"], "needs_clarification", result)
        self.assertEqual(result["route_to"], "clarification")
        self.assertEqual(self.executor.calls, [])
        self.assertEqual(result["rows"], [])

    async def test_executor_fault_keeps_accurate_stage_and_attempt_flag(self):
        for attempted, code in ((False, "DATABASE_PERMISSION_DENIED"), (True, "QUERY_TIMEOUT")):
            with self.subTest(attempted=attempted):
                self.executor = FakeExecutor(failure=ServiceFault(code, "受控失败", execution_attempted=attempted))
                result = await self.service().query(QUERY)
                self.assertEqual(result["status"], "error")
                self.assertEqual(result["stage"], "execution")
                self.assertEqual(result["error_code"], code)
                self.assertEqual(result["execution_attempted"], attempted)
                self.assertIsNone(result["route_to"])
                self.assertEqual(result["rows"], [])

    async def test_request_deadline_cancels_execution_and_waits_for_cleanup(self):
        self.executor = FakeExecutor(delay=10)
        result = await self.service().query(QUERY, {"request_timeout_ms": 100})
        self.assertEqual(result["error_code"], "REQUEST_TIMEOUT", result)
        self.assertEqual(result["stage"], "execution")
        self.assertTrue(result["execution_attempted"])
        self.assertTrue(self.executor.closed.is_set())

    async def test_external_cancellation_propagates_after_execution_cleanup(self):
        self.executor = FakeExecutor(delay=10)
        task = asyncio.create_task(self.service().query(QUERY))
        await asyncio.wait_for(self.executor.started.wait(), 2)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(self.executor.closed.is_set())

    async def test_invalid_total_budget_fails_before_metadata(self):
        result = await self.service().query(QUERY, {"request_timeout_ms": True})
        self.assertEqual(result["error_code"], "INVALID_QUERY")
        self.assertFalse(result["execution_attempted"])
        self.assertEqual(self.reader.calls, 0)


if __name__ == "__main__":
    unittest.main()
