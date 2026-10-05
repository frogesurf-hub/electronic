"""Offline detail service integration: real mapping/LLM contract/AST, fake database."""
from __future__ import annotations

import asyncio
import copy
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_core.providers import LLMResponse
from agent_core.sql.detail_example import DetailDemoProvider
from agent_core.sql.detail_service import DetailSQLService
from agent_core.sql.generation_service import SQLGenerationService
from database.detail_contracts import ValidatedDetailSQL
from database.detail_query import TABLES
from database.metadata_reader import FileMetadataReader
from database.sql_common import ServiceFault

BASE = Path(__file__).resolve().parents[1] / "database"
QUERY = {"task": "table_detail", "source_id": "chinook_db", "table": "public.customer",
         "columns": ["customer_id", "company"], "filters": [{"column": "company", "op": "is_null"}],
         "page": {"size": 2, "offset": 0}}


class RecordingProvider:
    def __init__(self, content=None, delay=0):
        self.content, self.delay, self.requests = content, delay, []
        self.started, self.closed = asyncio.Event(), asyncio.Event()

    async def generate(self, request):
        self.requests.append(request)
        self.started.set()
        try:
            if self.delay:
                await asyncio.sleep(self.delay)
            return await DetailDemoProvider().generate(request) if self.content is None else LLMResponse(self.content)
        finally:
            self.closed.set()


class FakeLiveReader:
    def __init__(self, *, delay=0, malformed=False):
        self.delay, self.malformed = delay, malformed
        self.sync_calls, self.async_calls = [], []
        self.started, self.closed = asyncio.Event(), asyncio.Event()

    def read(self, limits):
        self.sync_calls.append(dict(limits))
        return self._metadata(limits)

    async def read_async(self, limits):
        self.async_calls.append(dict(limits))
        self.started.set()
        try:
            if self.delay:
                await asyncio.sleep(self.delay)
            return self._metadata(limits)
        finally:
            self.closed.set()

    def _metadata(self, limits):
        if self.malformed:
            return {"snapshot": None}
        metadata = FileMetadataReader(BASE).read(limits)
        metadata.update(mode="live", permissions_verified=True, captured_at="2026-10-04T00:00:00+00:00")
        return metadata


class FakeExecutor:
    def __init__(self, *, failure=None, delay=0, rows=None, has_more=True):
        self.failure, self.delay = failure, delay
        self.rows = [{"customer_id": 1, "company": None}, {"customer_id": 2, "company": None}] if rows is None else rows
        self.has_more, self.calls = has_more, []
        self.started, self.closed = asyncio.Event(), asyncio.Event()

    async def execute(self, package, request, schema, *, limits, on_execute):
        self.calls.append((package, copy.deepcopy(request), copy.deepcopy(schema), dict(limits)))
        try:
            if self.failure is not None and not self.failure.execution_attempted:
                raise self.failure
            on_execute()
            self.started.set()
            if self.delay:
                await asyncio.sleep(self.delay)
            if self.failure is not None:
                raise self.failure
            return {"columns": package.output_columns, "rows": copy.deepcopy(self.rows), "row_count": len(self.rows),
                    "page": copy.deepcopy(request["page"]), "has_more": self.has_more,
                    "next_offset": request["page"]["offset"] + request["page"]["size"] if self.has_more else None,
                    "truncated": self.has_more, "execution_attempted": True, "limits": dict(limits),
                    "metadata": {"mode": "live", "permissions_verified": True,
                                 "same_transaction_revalidation": True,
                                 "schema_fingerprint_sha256": schema["schema_fingerprint_sha256"]}, "elapsed_ms": 1}
        finally:
            self.closed.set()


class DetailGenerationServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        for name in ("psycopg.connect", "psycopg.AsyncConnection.connect",
                     "database.sql_common.ReadOnlyClient.transaction", "database.readonly_connection.AsyncReadOnlyClient.transaction",
                     "database.sql_common.resolve_connection_parameters", "database.readonly_connection.resolve_connection_parameters"):
            guard = patch(name, side_effect=AssertionError("Offline service tests must not access DB or credentials"))
            mocked = guard.start()
            self.addCleanup(guard.stop)
            self.addCleanup(mocked.assert_not_called)
        self.config = json.loads((BASE / "source_config.json").read_text(encoding="utf-8-sig"))
        self.provider, self.reader, self.executor = RecordingProvider(), FakeLiveReader(), FakeExecutor()

    def service(self, *, provider=None, reader=None, executor=None, parent=True):
        cls = SQLGenerationService if parent else DetailSQLService
        options = {"detail_executor" if parent else "executor": self.executor if executor is None else executor}
        return cls(self.provider if provider is None else provider, base_dir=BASE, config=self.config,
                   metadata_reader=self.reader if reader is None else reader, **options)

    async def test_generate_returns_unvalidated_candidate_without_executor(self):
        result = await self.service().generate(QUERY)
        self.assertEqual((result["status"], result["route_to"]), ("generated", "sql_validation"))
        self.assertFalse(result["candidate"]["validated"])
        self.assertFalse(result["execution_attempted"])
        self.assertEqual(result["parameters"], {"fetch_limit": 3, "page_offset": 0})
        self.assertEqual(self.reader.async_calls, [])
        self.assertEqual(len(self.reader.sync_calls), 1)
        self.assertEqual(self.executor.calls, [])

    async def test_validate_returns_frozen_package_without_execution(self):
        result = await self.service().generate_and_validate(QUERY, {"request_id": "detail-trace"})
        self.assertEqual((result["status"], result["route_to"]), ("validated", "sql_execution"))
        self.assertEqual(result["validation_status"], "passed")
        self.assertTrue(result["execution_package"]["validated"])
        self.assertEqual(result["execution_package"]["request_id"], "detail-trace")
        self.assertIn("%(fetch_limit)s", result["execution_package"]["sql"])
        self.assertFalse(result["execution_attempted"])
        self.assertEqual(self.executor.calls, [])

    async def test_query_returns_typed_null_headers_and_next_page(self):
        query, context = copy.deepcopy(QUERY), {"request_id": "trace", "db_timeout_ms": 1500}
        before = copy.deepcopy((query, context))
        result = await self.service().query(query, context)
        self.assertEqual((result["status"], result["route_to"], result["task"]), ("ok", "answer", "table_detail"))
        self.assertEqual(result["rows"], [{"customer_id": 1, "company": None}, {"customer_id": 2, "company": None}])
        self.assertEqual(result["columns"], [{"name": "customer_id", "data_type": "integer", "nullable": False},
                                              {"name": "company", "data_type": "text", "nullable": True}])
        self.assertEqual((result["row_count"], result["has_more"], result["next_offset"]), (2, True, 2))
        self.assertTrue(result["truncated"])
        self.assertTrue(result["execution_attempted"])
        self.assertTrue(result["execution_metadata"]["same_transaction_revalidation"])
        package, request, schema, limits = self.executor.calls[0]
        self.assertIs(type(package), ValidatedDetailSQL)
        self.assertEqual(package.parameters, {"fetch_limit": 3, "page_offset": 0})
        self.assertEqual(package.request_id, "trace")
        self.assertEqual(limits["db_timeout_ms"], 1500)
        self.assertEqual(limits["max_rows"], 1000)
        self.assertEqual(package.max_rows, 2)
        self.assertEqual((query, context), before)
        self.assertEqual(self.reader.sync_calls, [])
        self.assertEqual(len(self.reader.async_calls), 1)
        json.dumps(result, allow_nan=False)

    async def test_page_500_uses_501_fetch_and_explicit_cap_can_reduce_page(self):
        query = copy.deepcopy(QUERY)
        query["page"] = {"size": 500, "offset": 10}
        for context, size in (({}, 500), ({"max_rows": 200}, 200)):
            with self.subTest(context=context):
                result = await self.service().generate_and_validate(query, context)
                self.assertEqual(result["status"], "validated", result)
                self.assertEqual(result["page"], {"size": size, "offset": 10})
                self.assertEqual(result["parameters"], {"fetch_limit": size + 1, "page_offset": 10})
        self.assertEqual(query["page"]["size"], 500)

    async def test_all_11_tables_and_64_columns_are_validated(self):
        expected = {"album": 3, "artist": 2, "customer": 13, "employee": 15, "genre": 2,
                    "invoice": 9, "invoice_line": 5, "media_type": 2, "playlist": 2, "playlist_track": 2, "track": 9}
        total = 0
        for table in TABLES:
            with self.subTest(table=table):
                result = await self.service().generate_and_validate({"task": "table_detail", "table": table, "columns": "all"})
                self.assertEqual(result["status"], "validated", result)
                columns = result["execution_package"]["output_columns"]
                self.assertEqual(len(columns), expected[table.split(".")[1]])
                self.assertEqual([item["name"] for item in columns], result["sql_generation_request"]["columns"])
                total += len(columns)
        self.assertEqual(total, 64)
        self.assertEqual(self.executor.calls, [])

    async def test_all_typed_filters_keep_values_server_side(self):
        query = {"task": "table_detail", "table": "invoice", "columns": ["invoice_id", "invoice_date", "total"],
                 "filters": [{"column": "customer_id", "op": "in", "value": [1, 2, 1]},
                             {"column": "invoice_date", "op": "gte", "value": "2024-01-01"},
                             {"column": "total", "op": "lte", "value": "123.40"},
                             {"column": "billing_city", "op": "starts_with", "value": "private_%\\city"}]}
        result = await self.service().generate_and_validate(query)
        self.assertEqual(result["status"], "validated", result)
        self.assertEqual(result["parameters"]["filter_0"], [1, 2])
        self.assertEqual(result["parameters"]["filter_1"], "2024-01-01T00:00:00")
        self.assertEqual(result["parameters"]["filter_2"], "123.40")
        self.assertEqual(result["parameters"]["filter_3"], "private\\_\\%\\\\city%")
        payload = "\n".join(message.content for message in self.provider.requests[-1].messages)
        self.assertNotIn("private_", payload)
        self.assertNotIn("123.40", payload)
        self.assertNotIn("2024-01-01", payload)

    async def test_missing_table_filter_value_or_timezone_routes_clarification_before_provider(self):
        queries = [("MISSING_TABLE", {"task": "table_detail"}),
                   ("MISSING_FILTER_VALUE", {"task": "table_detail", "table": "artist", "filters": [{"column": "name", "op": "eq"}]}),
                   ("AMBIGUOUS_TIME_SEMANTICS", {"task": "table_detail", "table": "invoice",
                                               "filters": [{"column": "invoice_date", "op": "gte", "value": "2024-01-01T00:00:00+08:00"}]})]
        for code, query in queries:
            with self.subTest(code=code):
                result = await self.service().query(query)
                self.assertEqual((result["status"], result["route_to"], result["error_code"]), ("needs_clarification", "clarification", code))
                self.assertFalse(result["execution_attempted"])
                self.assertEqual(result["rows"], [])
                self.assertTrue(result["clarification_request"]["issues"])
        self.assertEqual(self.provider.requests, [])
        self.assertEqual(self.executor.calls, [])

    async def test_unknown_source_blocked_before_metadata_or_provider(self):
        query = copy.deepcopy(QUERY)
        query["source_id"] = "sales_db"
        result = await self.service().query(query)
        self.assertEqual(result["error_code"], "UNKNOWN_SOURCE")
        self.assertEqual(self.reader.async_calls, [])
        self.assertEqual(self.provider.requests, [])
        self.assertFalse(result["execution_attempted"])

    async def test_invalid_projection_table_or_operator_never_reaches_provider(self):
        for change in ({"table": "business.orders"}, {"columns": ["password"]},
                       {"filters": [{"column": "customer_id", "op": "contains", "value": "1"}]},
                       {"page": {"size": True}}):
            with self.subTest(change=change):
                result = await self.service().query({**QUERY, **change})
                self.assertEqual(result["status"], "error")
                self.assertIsNone(result["route_to"])
                self.assertEqual(result["rows"], [])
                self.assertFalse(result["execution_attempted"])
        self.assertEqual(self.provider.requests, [])
        self.assertEqual(self.executor.calls, [])

    async def test_file_metadata_cannot_execute_and_bad_metadata_is_classified(self):
        for reader, code in ((FileMetadataReader(BASE), "LIVE_METADATA_REQUIRED"),
                             (FakeLiveReader(malformed=True), "METADATA_ERROR")):
            with self.subTest(code=code):
                result = await self.service(reader=reader).query(QUERY)
                self.assertEqual(result["error_code"], code)
                self.assertEqual(result["rows"], [])
                self.assertFalse(result["execution_attempted"])
        self.assertEqual(self.provider.requests, [])
        self.assertEqual(self.executor.calls, [])

    async def test_attack_and_malformed_json_never_execute(self):
        for content in ('{"sql":"DELETE FROM public.customer"}', '{"sql":"SELECT * FROM public.customer"}',
                        '{"sql":"SELECT 1; SELECT 2"}', '{"sql":"SELECT 1","parameters":{"fetch_limit":1}}'):
            with self.subTest(content=content):
                result = await self.service(provider=RecordingProvider(content)).query(QUERY)
                self.assertEqual(result["status"], "error", result)
                self.assertIsNone(result["route_to"])
                self.assertIsNone(result["execution_package"])
                self.assertEqual(result["rows"], [])
                self.assertFalse(result["execution_attempted"])
        self.assertEqual(self.executor.calls, [])

    async def test_execution_failure_preserves_attempt_and_clears_answer_rows(self):
        for attempted in (False, True):
            with self.subTest(attempted=attempted):
                executor = FakeExecutor(failure=ServiceFault("EXECUTION_TEST_FAILURE", "controlled failure", execution_attempted=attempted))
                result = await self.service(executor=executor).query(QUERY)
                self.assertEqual(result["error_code"], "EXECUTION_TEST_FAILURE")
                self.assertEqual(result["execution_attempted"], attempted)
                self.assertIsNone(result["route_to"])
                self.assertEqual((result["rows"], result["columns"], result["row_count"]), ([], [], 0))
                self.assertFalse(result["has_more"])
                self.assertIsNone(result["next_offset"])

    async def test_request_timeout_covers_metadata_before_model(self):
        reader = FakeLiveReader(delay=1)
        result = await self.service(reader=reader).query(QUERY, {"request_timeout_ms": 5})
        self.assertEqual(result["error_code"], "REQUEST_TIMEOUT")
        self.assertEqual(result["stage"], "metadata")
        self.assertFalse(result["execution_attempted"])
        self.assertTrue(reader.closed.is_set())
        self.assertEqual(self.provider.requests, [])

    async def test_request_timeout_covers_llm_before_business(self):
        provider = RecordingProvider(delay=1)
        result = await self.service(provider=provider).query(QUERY, {"request_timeout_ms": 40})
        self.assertEqual(result["error_code"], "REQUEST_TIMEOUT")
        self.assertEqual(result["stage"], "generation")
        self.assertFalse(result["execution_attempted"])
        self.assertTrue(provider.closed.is_set())
        self.assertEqual(self.executor.calls, [])

    async def test_request_timeout_during_execution_retains_attempt(self):
        executor = FakeExecutor(delay=1)
        result = await self.service(executor=executor).query(QUERY, {"request_timeout_ms": 100})
        self.assertEqual(result["error_code"], "REQUEST_TIMEOUT")
        self.assertEqual(result["stage"], "execution")
        self.assertTrue(result["execution_attempted"])
        self.assertEqual(result["rows"], [])
        self.assertTrue(executor.closed.is_set())

    async def test_direct_service_has_same_request_timeout(self):
        reader = FakeLiveReader(delay=1)
        result = await self.service(reader=reader, parent=False).query(QUERY, {"request_timeout_ms": 5})
        self.assertEqual(result["error_code"], "REQUEST_TIMEOUT")
        self.assertFalse(result["execution_attempted"])
        self.assertEqual(result["task"], "table_detail")

    async def test_cancellation_propagates_and_closes_pending_executor(self):
        executor = FakeExecutor(delay=10)
        pending = asyncio.create_task(self.service(executor=executor).query(QUERY))
        await asyncio.wait_for(executor.started.wait(), 1)
        pending.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await pending
        self.assertTrue(executor.closed.is_set())

    async def test_empty_last_page_keeps_headers_and_no_next_offset(self):
        query = {**QUERY, "page": {"size": 2, "offset": 999}}
        result = await self.service(executor=FakeExecutor(rows=[], has_more=False)).query(query)
        self.assertEqual(result["status"], "ok", result)
        self.assertEqual(result["row_count"], 0)
        self.assertEqual(result["rows"], [])
        self.assertEqual(len(result["columns"]), 2)
        self.assertFalse(result["has_more"])
        self.assertFalse(result["truncated"])
        self.assertIsNone(result["next_offset"])

    async def test_unknown_context_types_and_limits_do_not_call_model(self):
        for context in ([1], {"request_id": 4}, {"max_rows": True}, {"db_timeout_ms": 0}):
            with self.subTest(context=context):
                result = await self.service().query(QUERY, context)
                self.assertEqual(result["error_code"], "INVALID_QUERY")
                self.assertFalse(result["execution_attempted"])
        self.assertEqual(self.provider.requests, [])

    async def test_legacy_config_rejected_before_any_database_access(self):
        for cls in (SQLGenerationService, DetailSQLService):
            with self.subTest(cls=cls):
                with self.assertRaises(ServiceFault) as error:
                    cls(self.provider, base_dir=BASE, config={**self.config, "source_id": "sales_db"}, metadata_reader=self.reader)
                self.assertEqual(error.exception.code, "INVALID_CONFIGURATION")


if __name__ == "__main__":
    unittest.main()
