"""只读执行器的异步离线测试；事务、游标和实时元数据均为替身。"""
from __future__ import annotations

import copy
import json
import unittest
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from agent_core.sql.llm_sql_generator import prepare_generation_inputs
from database.enrich_schema_snapshot import enrich_snapshot
from database.metadata_reader import FileMetadataReader
from database.readonly_sql_executor import ReadOnlySQLExecutor
from database.schema_linking_service import SchemaLinkingService
from database.sql_common import ServiceFault
from database.sql_contracts import SQLCandidate
from database.sql_validator import SQLValidator


DATABASE_DIR = Path(__file__).resolve().parents[1] / "database"
LIMITS = {"max_rows": 10, "db_timeout_ms": 5000}
SCALAR_SQL = (
    "SELECT COALESCE(SUM(o.total), 0) AS sales_amount "
    "FROM public.invoice o WHERE o.invoice_date >= :start AND o.invoice_date < :end "
    "AND o.billing_country = :filter_0 LIMIT :fetch_limit"
)
NO_WRITES = {
    "can_write": False, "can_write_columns": False, "can_create": False,
    "is_owner": False, "rls_enabled": False, "rls_active": False,
}


class FakeCursor:
    def __init__(self, events, *, rows=None, permission=None, failure=None):
        self.events = events
        self.rows = [{"sales_amount": Decimal("2323749.97")}] if rows is None else rows
        self.permission = copy.deepcopy(NO_WRITES if permission is None else permission)
        self.failure = failure
        self.calls = []
        self.business_calls = []
        self.fetch_sizes = []
        self.description = (SimpleNamespace(name="sales_amount", type_code=1700),)

    async def execute(self, sql, parameters=None):
        self.calls.append((sql, copy.deepcopy(parameters)))
        if sql.lstrip().startswith("SELECT COALESCE(SUM("):
            self.events.append("public")
            self.business_calls.append((sql, copy.deepcopy(parameters)))
            if self.failure is not None:
                raise self.failure
        else:
            self.events.append("control")

    async def fetchone(self):
        self.events.append("permissions")
        return copy.deepcopy(self.permission)

    async def fetchmany(self, size):
        self.events.append("fetch")
        self.fetch_sizes.append(size)
        return copy.deepcopy(self.rows[:size])


class FakeClient:
    def __init__(self, cursor, events):
        self.cursor, self.events = cursor, events
        self.limits = []

    @asynccontextmanager
    async def transaction(self, limits):
        self.limits.append(copy.deepcopy(limits))
        self.events.append("transaction")
        try:
            yield self.cursor
        finally:
            self.events.append("transaction_closed")


class FakeMetadataReader:
    def __init__(self, payload, events):
        self.payload, self.events = payload, events
        self.cursors = []

    async def aread_in_transaction(self, cursor):
        self.events.append("metadata")
        self.cursors.append(cursor)
        return copy.deepcopy(self.payload)


class ReadOnlySQLExecutorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        for target in (
            "psycopg.connect", "psycopg.AsyncConnection.connect",
            "database.sql_common.resolve_connection_parameters",
        ):
            guard = patch(target, side_effect=AssertionError("离线执行测试禁止连接数据库或读取密码"))
            mock = guard.start()
            self.addCleanup(guard.stop)
            self.addCleanup(mock.assert_not_called)
        self.config = json.loads((DATABASE_DIR / "source_config.json").read_text(encoding="utf-8-sig"))
        self.metadata = FileMetadataReader(DATABASE_DIR, self.config).read(LIMITS)
        self.metadata.update(mode="live", permissions_verified=True, captured_at="2026-10-03T00:00:00+00:00")
        self.metadata["snapshot"]["metadata_mode"] = "live"
        self.metadata["snapshot"]["permissions_verified"] = True
        self.metadata["snapshot"]["permissions"] = [{
            "object_name": name, "schema_exists": True, "object_exists": True,
            "schema_usage": True, "table_select": True, "rls_enabled": False, "rls_active": False,
        } for name in sorted(set(
            self.config["query_table_allowlist"] + self.config["metadata_tables"] + self.config["evidence_tables"]
        ))]
        self.snapshot = enrich_snapshot(self.metadata["snapshot"], self.metadata["data"], self.config)
        self.scalar, self.package = self.prepare("eq", "美国")
        self.events = []
        self.cursor = FakeCursor(self.events)
        self.client = FakeClient(self.cursor, self.events)
        self.reader = FakeMetadataReader(self.metadata, self.events)

    def prepare(self, operation, value):
        query = {
            "source_id": "chinook_db", "metric_id": "sales_amount",
            "metric_definition_id": "chinook_sales_amount_v1",
            "time_range": {"start": "2024-01-01", "end_exclusive": "2025-01-01"},
            "filters": [{"field_id": "country", "op": operation, "value": value}],
        }
        decision = SchemaLinkingService().match(query, self.snapshot, entities=self.metadata["data"])
        self.assertEqual(decision["status"], "ready", decision)
        prepared = prepare_generation_inputs(decision["sql_generation_request"], self.snapshot, LIMITS)
        sql = SCALAR_SQL if operation == "eq" else SCALAR_SQL.replace("= :filter_0", "= ANY(:filter_0)")
        candidate = SQLCandidate(
            sql=sql, source_id="chinook_db", metric_definition_id="chinook_sales_amount_v1",
            parameter_names=tuple(spec.name for spec in prepared.parameter_specs),
            schema_fingerprint_sha256=prepared.schema_context["schema_fingerprint_sha256"],
            session_id="session-execution", request_id="request-execution",
        )
        result = SQLValidator().validate(
            candidate, prepared.generation_request, prepared.schema_context, prepared.parameters, LIMITS,
        )
        self.assertTrue(result.passed, result)
        return prepared, result.validated_sql

    def executor(self):
        return ReadOnlySQLExecutor(self.config, client=self.client, metadata_reader=self.reader)

    async def execute(self, package=None, prepared=None, *, limits=None, on_execute=None):
        prepared = self.scalar if prepared is None else prepared
        return await self.executor().execute(
            self.package if package is None else package,
            prepared.generation_request, prepared.schema_context,
            limits=LIMITS if limits is None else limits, on_execute=on_execute,
        )

    async def assert_fault_before_business(self, **arguments):
        with self.assertRaises(ServiceFault) as caught:
            await self.execute(**arguments)
        self.assertFalse(caught.exception.execution_attempted)
        self.assertEqual(self.cursor.business_calls, [])
        return caught.exception

    async def test_executes_canonical_sql_with_typed_bindings_in_one_transaction(self):
        result = await self.execute()

        self.assertEqual(self.cursor.business_calls, [(self.package.sql, self.package.parameters)])
        bound_parameters = self.cursor.business_calls[0][1]
        self.assertIsInstance(bound_parameters["start"], datetime)
        self.assertEqual(bound_parameters["filter_0"], "USA")
        self.assertNotIn("USA", self.cursor.business_calls[0][0])
        self.assertEqual(self.reader.cursors, [self.cursor])
        self.assertEqual(len(self.client.limits), 1)
        self.assertTrue(any(sql.strip().upper().startswith("LOCK TABLE PUBLIC.INVOICE IN ACCESS SHARE MODE")
                            for sql, _ in self.cursor.calls))
        self.assertLess(self.events.index("metadata"), self.events.index("public"))
        self.assertLess(self.events.index("permissions"), self.events.index("public"))
        self.assertEqual(self.events[-1], "transaction_closed")
        self.assertEqual(result["rows"], [{"sales_amount": "2323749.97"}])
        self.assertEqual(result["row_count"], 1)
        self.assertFalse(result["truncated"])
        self.assertTrue(result["execution_attempted"])
        self.assertEqual(result["columns"], [{"name": "sales_amount", "data_type": "decimal", "unit": "金额"}])
        self.assertEqual(self.cursor.fetch_sizes, [11])
        json.dumps(result, ensure_ascii=False, allow_nan=False)

    async def test_array_values_remain_separately_bound_lists(self):
        prepared, package = self.prepare("in", ["美国", "加拿大"])

        result = await self.execute(package, prepared)

        self.assertTrue(result["execution_attempted"])
        self.assertEqual(self.cursor.business_calls[0][1]["filter_0"], ["USA", "Canada"])
        self.assertIn("ANY(%(filter_0)s)", self.cursor.business_calls[0][0])

    async def test_candidates_dicts_and_strings_cannot_be_executed(self):
        candidate = SQLCandidate(
            sql=SCALAR_SQL, source_id="chinook_db", metric_definition_id="chinook_sales_amount_v1",
            parameter_names=tuple(self.package.parameters),
            schema_fingerprint_sha256=self.package.schema_fingerprint_sha256,
        )
        for value in (candidate, self.package.as_dict(), self.package.sql):
            with self.subTest(kind=type(value).__name__):
                await self.assert_fault_before_business(package=value)

    async def test_tampered_artifact_fields_never_reach_business_query(self):
        parameters = dict(self.package._parameter_items)
        parameters["filter_0"] = "Canada"
        variants = (
            replace(self.package, sql="DELETE FROM public.invoice"),
            replace(self.package, sql_sha256="0" * 64),
            replace(self.package, candidate_sha256="0" * 64),
            replace(self.package, _parameter_items=tuple(parameters.items())),
            replace(self.package, max_rows=1000),
            replace(self.package, db_timeout_ms=1),
            replace(self.package, policy_version="invented-policy"),
            replace(self.package, schema_fingerprint_sha256="0" * 64),
        )
        for package in variants:
            with self.subTest(package=package):
                await self.assert_fault_before_business(package=package)

    async def test_limits_inconsistent_with_validated_package_are_rejected(self):
        for limits in ({"max_rows": 11, "db_timeout_ms": 5000}, {"max_rows": 10, "db_timeout_ms": 4000}):
            with self.subTest(limits=limits):
                await self.assert_fault_before_business(limits=limits)

    async def test_fresh_schema_drift_blocks_execution(self):
        row = next(item for item in self.reader.payload["snapshot"]["columns"]
                   if item["schema_name"] == "public" and item["table_name"] == "invoice"
                   and item["column_name"] == "total")
        row["data_type"] = "text"

        await self.assert_fault_before_business()

    async def test_fresh_metric_semantics_drift_blocks_execution(self):
        self.reader.payload["data"]["countries"] = [{"country": "Canada"}]

        await self.assert_fault_before_business()

    async def test_unverified_or_non_live_metadata_blocks_execution(self):
        for values in ({"mode": "file"}, {"permissions_verified": False}):
            with self.subTest(values=values):
                before = copy.deepcopy(self.reader.payload)
                self.reader.payload.update(values)
                await self.assert_fault_before_business()
                self.reader.payload = before

    async def test_write_create_or_owner_permissions_block_execution(self):
        for flag in ("can_write", "can_write_columns", "can_create", "is_owner"):
            with self.subTest(flag=flag):
                self.cursor.permission = {**NO_WRITES, flag: True}
                await self.assert_fault_before_business()

    async def test_enabled_but_inactive_rls_blocks_execution(self):
        self.cursor.permission.update(rls_enabled=True, rls_active=False)
        for row in self.reader.payload["snapshot"]["permissions"]:
            if row["object_name"] == "public.invoice":
                row.update(rls_enabled=True, rls_active=False)

        await self.assert_fault_before_business()

    async def test_business_database_fault_is_marked_as_execution_attempted(self):
        self.cursor.failure = ServiceFault("QUERY_TIMEOUT", "合成数据库超时")

        with self.assertRaises(ServiceFault) as caught:
            await self.execute()

        self.assertEqual(caught.exception.code, "QUERY_TIMEOUT")
        self.assertTrue(caught.exception.execution_attempted)
        self.assertEqual(len(self.cursor.business_calls), 1)

    async def test_on_execute_runs_once_immediately_before_business_query(self):
        def on_execute():
            self.events.append("callback")

        await self.execute(on_execute=on_execute)

        self.assertEqual(self.events.count("callback"), 1)
        self.assertEqual(self.events.index("callback") + 1, self.events.index("public"))
        self.events.clear()
        self.cursor.business_calls.clear()
        await self.assert_fault_before_business(package=self.package.as_dict(), on_execute=on_execute)
        self.assertNotIn("callback", self.events)

    async def test_failing_on_execute_callback_does_not_mark_business_execution_attempted(self):
        def failing_callback():
            self.events.append("failing_callback")
            raise RuntimeError("合成回调失败")

        fault = await self.assert_fault_before_business(on_execute=failing_callback)

        self.assertEqual(fault.code, "EXECUTION_ERROR")
        self.assertEqual(self.events.count("failing_callback"), 1)
        self.assertNotIn("public", self.events)
        self.assertEqual(self.events[-1], "transaction_closed")

    async def test_empty_and_truncated_results_report_returned_row_count(self):
        self.cursor.rows = []
        result = await self.execute()
        self.assertEqual(result["rows"], [])
        self.assertEqual(result["row_count"], 0)
        self.assertFalse(result["truncated"])
        self.cursor.rows = [{"sales_amount": Decimal(index)} for index in range(11)]
        result = await self.execute()
        self.assertTrue(result["truncated"])
        self.assertEqual(result["row_count"], 10)
        self.assertEqual(len(result["rows"]), 10)
        self.assertEqual(result["rows"][-1], {"sales_amount": "9.00"})

    async def test_result_columns_must_match_the_expected_numeric_projection(self):
        for description in (
            (SimpleNamespace(name="unexpected", type_code=1700),),
            (SimpleNamespace(name="sales_amount", type_code=25),),
            (SimpleNamespace(name="sales_amount", type_code=1700), SimpleNamespace(name="extra", type_code=1700)),
        ):
            with self.subTest(description=description):
                self.cursor.description = description
                with self.assertRaises(ServiceFault) as caught:
                    await self.execute()
                self.assertTrue(caught.exception.execution_attempted)

    async def test_result_values_and_even_truncation_sentinel_must_be_finite_decimals(self):
        for value in (None, "1.00", 1, Decimal("NaN"), Decimal("Infinity")):
            with self.subTest(value=value):
                self.cursor.rows = [{"sales_amount": value}]
                with self.assertRaises(ServiceFault) as caught:
                    await self.execute()
                self.assertTrue(caught.exception.execution_attempted)
        self.cursor.rows = [{"sales_amount": Decimal(1)} for _ in range(10)] + [{"sales_amount": "invalid-sentinel"}]
        with self.assertRaises(ServiceFault) as caught:
            await self.execute()
        self.assertTrue(caught.exception.execution_attempted)

    async def test_unexpected_row_keys_are_rejected(self):
        for row in ({"other": Decimal(1)}, {"sales_amount": Decimal(1), "extra": "x"}, (Decimal(1),)):
            with self.subTest(row=row):
                self.cursor.rows = [row]
                with self.assertRaises(ServiceFault) as caught:
                    await self.execute()
                self.assertTrue(caught.exception.execution_attempted)

    async def test_execution_does_not_modify_package_or_trusted_input(self):
        package_before = self.package.as_dict()
        request_before = copy.deepcopy(self.scalar.generation_request)
        schema_before = copy.deepcopy(self.scalar.schema_context)
        metadata_before = copy.deepcopy(self.reader.payload)

        await self.execute()

        self.assertEqual(self.package.as_dict(), package_before)
        self.assertEqual(self.scalar.generation_request, request_before)
        self.assertEqual(self.scalar.schema_context, schema_before)
        self.assertEqual(self.reader.payload, metadata_before)


if __name__ == "__main__":
    unittest.main()
