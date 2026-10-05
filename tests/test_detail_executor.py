"""明细执行器离线验证；实时事务及游标均为替身，禁止连接数据库。"""
from __future__ import annotations

import asyncio
import copy
import json
import unittest
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from database.detail_contracts import DetailCandidate
from database.detail_executor import DetailReadOnlyExecutor
from database.detail_query import TABLES, canonical_sql, prepare_detail
from database.detail_validator import DetailSQLValidator
from database.sql_common import ServiceFault

_DATABASE = Path(__file__).resolve().parents[1] / "database"
_NO_WRITES = {"can_select": True, "can_write": False, "can_write_columns": False, "can_create": False,
              "can_create_database": False, "is_owner": False, "privileged_role": False,
              "rls_enabled": False, "rls_active": False}
_OIDS = {"integer": 23, "text": 1043, "decimal": 1700, "timestamp": 1114}


async def _direct_database(factory):
    return await factory()


class FakeCursor:
    def __init__(self, events, schema, columns):
        self.events = events
        self.permissions = copy.deepcopy(_NO_WRITES)
        self.failure = None
        self.block = False
        self.query_started = asyncio.Event()
        self.calls, self.business_calls, self.fetch_sizes = [], [], []
        self.set_columns(schema, columns)

    def set_columns(self, schema, columns):
        indexed = {row["name"]: row for row in schema["columns"]}
        self.description = tuple(SimpleNamespace(name=name, type_code=_OIDS[indexed[name]["data_type"]]) for name in columns)
        cells = {"integer": 1, "text": "synthetic", "decimal": Decimal("1.23"), "timestamp": datetime(2010, 1, 2, 3, 4, 5)}
        self.rows = [{name: cells[indexed[name]["data_type"]] for name in columns}]

    async def execute(self, sql, parameters=None):
        self.calls.append((sql, copy.deepcopy(parameters)))
        if sql.startswith("SELECT t."):
            self.events.append("business")
            self.business_calls.append((sql, copy.deepcopy(parameters)))
            self.query_started.set()
            if self.failure is not None:
                raise self.failure
            if self.block:
                await asyncio.Future()
        else:
            self.events.append("control")

    async def fetchone(self):
        self.events.append("permissions")
        return copy.deepcopy(self.permissions)

    async def fetchmany(self, size):
        self.events.append("fetch")
        self.fetch_sizes.append(size)
        return copy.deepcopy(self.rows[:size])


class FakeClient:
    def __init__(self, cursor, events):
        self.cursor, self.events, self.limits = cursor, events, []

    @asynccontextmanager
    async def transaction(self, limits):
        self.limits.append(copy.deepcopy(limits))
        self.events.append("transaction")
        try:
            yield self.cursor
        finally:
            self.events.append("transaction_closed")


class FakeMetadata:
    def __init__(self, raw, events):
        self.payload = {"snapshot": copy.deepcopy(raw), "data": {}, "mode": "live",
                        "permissions_verified": True, "captured_at": "2026-10-04T00:00:00+00:00"}
        self.events, self.cursors = events, []

    async def aread_in_transaction(self, cursor):
        self.events.append("metadata")
        self.cursors.append(cursor)
        return copy.deepcopy(self.payload)


class DetailExecutorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        for target in ("psycopg.connect", "psycopg.AsyncConnection.connect", "database.sql_common.resolve_connection_parameters"):
            guard = patch(target, side_effect=AssertionError("离线执行禁止连接数据库或访问凭据"))
            mock = guard.start()
            self.addCleanup(guard.stop)
            self.addCleanup(mock.assert_not_called)
        bridge = patch("database.readonly_connection.run_database", side_effect=_direct_database)
        bridge.start()
        self.addCleanup(bridge.stop)
        self.raw = json.loads((_DATABASE / "generated/chinook_schema_snapshot.json").read_text(encoding="utf-8-sig"))
        self.config = json.loads((_DATABASE / "source_config.json").read_text(encoding="utf-8-sig"))
        self.prepared, self.packet = self.prepare("invoice", columns=["invoice_id", "invoice_date", "billing_city", "total"],
                                                   filters=[{"column": "customer_id", "op": "in", "value": [1, 2]}],
                                                   page={"size": 2, "offset": 5})
        self.events = []
        self.cursor = FakeCursor(self.events, self.prepared.schema_context, self.prepared.generation_request["columns"])
        self.client = FakeClient(self.cursor, self.events)
        self.metadata = FakeMetadata(self.raw, self.events)

    def prepare(self, table, **parts):
        prepared = prepare_detail({"task": "table_detail", "table": table, **parts}, self.raw, self.config)
        candidate = DetailCandidate(sql=canonical_sql(prepared.generation_request), source_id="chinook_db",
                                    table=prepared.generation_request["table"], parameter_names=tuple(prepared.parameters),
                                    schema_fingerprint_sha256=prepared.schema_context["schema_fingerprint_sha256"])
        result = DetailSQLValidator().validate(candidate, prepared.generation_request, prepared.schema_context, prepared.parameters)
        self.assertTrue(result.passed, result.as_dict())
        return prepared, result.validated_sql

    async def execute(self, *, packet=None, prepared=None, limits=None, config=None, on_execute=None):
        prepared = self.prepared if prepared is None else prepared
        executor = DetailReadOnlyExecutor(self.config if config is None else config, client=self.client, metadata_reader=self.metadata)
        return await executor.execute(self.packet if packet is None else packet, prepared.generation_request,
                                      prepared.schema_context, limits=limits, on_execute=on_execute)

    async def preflight_fault(self, **changes):
        previous_calls = len(self.cursor.business_calls)
        with self.assertRaises(ServiceFault) as caught:
            await self.execute(**changes)
        self.assertFalse(caught.exception.execution_attempted)
        self.assertEqual(len(self.cursor.business_calls), previous_calls)
        return caught.exception

    async def result_fault(self):
        with self.assertRaises(ServiceFault) as caught:
            await self.execute()
        self.assertTrue(caught.exception.execution_attempted)
        self.assertEqual(caught.exception.code, "RESULT_CONTRACT_MISMATCH")
        self.assertEqual(self.events[-1], "transaction_closed")
        return caught.exception

    async def test_same_transaction_revalidation_typed_json_and_bound_values(self):
        result = await self.execute()
        self.assertEqual(self.metadata.cursors, [self.cursor])
        self.assertTrue(self.client.limits[0]["_lock_chinook_schema_before_snapshot"])
        self.assertLess(self.events.index("metadata"), self.events.index("permissions"))
        self.assertLess(self.events.index("permissions"), self.events.index("business"))
        sql, values = self.cursor.business_calls[0]
        self.assertEqual(sql, self.packet.sql)
        self.assertEqual(values, {"filter_0": [1, 2], "fetch_limit": 3, "page_offset": 5})
        self.assertEqual(self.cursor.fetch_sizes, [3])
        self.assertEqual(result["rows"], [{"invoice_id": 1, "invoice_date": "2010-01-02T03:04:05", "billing_city": "synthetic", "total": "1.23"}])
        self.assertEqual(result["page"], {"size": 2, "offset": 5})
        self.assertFalse(result["has_more"])
        self.assertIsNone(result["next_offset"])
        self.assertTrue(result["metadata"]["same_transaction_revalidation"])
        json.dumps(result, ensure_ascii=False, allow_nan=False)

    async def test_all_tables_all_columns_are_executable_with_typed_oids(self):
        for table in TABLES:
            with self.subTest(table=table):
                prepared, packet = self.prepare(table)
                self.cursor.set_columns(prepared.schema_context, prepared.generation_request["columns"])
                result = await self.execute(packet=packet, prepared=prepared)
                self.assertEqual(list(result["rows"][0]), prepared.generation_request["columns"])
                self.assertEqual(len(result["columns"]), len(prepared.generation_request["columns"]))

    async def test_empty_exact_page_and_more_than_page_pagination(self):
        row = copy.deepcopy(self.cursor.rows[0])
        for count, more in ((0, False), (1, False), (2, False), (3, True)):
            with self.subTest(count=count):
                self.cursor.rows = [copy.deepcopy(row) for _ in range(count)]
                result = await self.execute()
                self.assertEqual(result["row_count"], min(count, 2))
                self.assertEqual(result["has_more"], more)
                self.assertEqual(result["truncated"], more)
                self.assertEqual(result["next_offset"], 7 if more else None)

    async def test_nullable_cells_remain_null(self):
        self.cursor.rows[0]["billing_city"] = None
        result = await self.execute()
        self.assertIsNone(result["rows"][0]["billing_city"])

    async def test_json_candidate_and_arbitrary_sql_cannot_execute(self):
        candidate = DetailCandidate(self.packet.candidate_sql, self.packet.source_id, self.packet.table,
                                    tuple(self.packet.parameters), self.packet.schema_fingerprint_sha256)
        for value in (candidate, self.packet.as_dict(), self.packet.sql, 123):
            with self.subTest(kind=type(value).__name__):
                await self.preflight_fault(packet=value)
        self.assertEqual(self.client.limits, [])

    async def test_all_tampered_packet_fields_block_before_business(self):
        parameters = dict(self.packet._parameter_items)
        parameters["filter_0"] = (3, 4)
        variants = (replace(self.packet, sql="DELETE FROM public.invoice"), replace(self.packet, sql_sha256="0" * 64),
                    replace(self.packet, candidate_sha256="0" * 64), replace(self.packet, policy_version="invented"),
                    replace(self.packet, _parameter_items=tuple(parameters.items())), replace(self.packet, max_rows=3),
                    replace(self.packet, page_offset=0), replace(self.packet, db_timeout_ms=4000),
                    replace(self.packet, schema_fingerprint_sha256="0" * 64), replace(self.packet, _output_columns=()))
        for packet in variants:
            with self.subTest(field=packet.as_dict()):
                await self.preflight_fault(packet=packet)

    async def test_private_output_mutation_is_caught_by_packet_rebuild(self):
        self.packet._output_columns[0]["nullable"] = True
        await self.preflight_fault()

    async def test_boolean_numeric_equivalence_and_duplicate_internal_parameters_are_rejected(self):
        prepared, packet = self.prepare("track", page={"size": 1, "offset": 0})
        self.cursor.set_columns(prepared.schema_context, prepared.generation_request["columns"])
        for malformed in (replace(packet, max_rows=True), replace(packet, page_offset=False),
                          replace(packet, _parameter_items=(*packet._parameter_items, packet._parameter_items[0]))):
            with self.subTest(packet=malformed.as_dict()):
                await self.preflight_fault(prepared=prepared, packet=malformed)
        packet._output_columns[0]["nullable"] = 0
        await self.preflight_fault(prepared=prepared, packet=packet)

    async def test_budget_source_and_whitelist_changes_block_before_business(self):
        for limits in ({"max_rows": 1}, {"db_timeout_ms": 4000}, {"execution_timeout_ms": 0}, {"max_rows": True}):
            with self.subTest(limits=limits):
                await self.preflight_fault(limits=limits)
        for config in ({**self.config, "source_id": "sales_db"}, {**self.config, "dataset": "sales"},
                       {**self.config, "database": "wrong"}, {**self.config, "query_table_allowlist": ["public.track"]}):
            with self.subTest(config=config["source_id"]):
                await self.preflight_fault(config=config)

    async def test_same_transaction_fresh_schema_and_primary_key_drift_block(self):
        original = copy.deepcopy(self.metadata.payload)
        row = next(row for row in self.metadata.payload["snapshot"]["columns"] if row["table_name"] == "invoice" and row["column_name"] == "total")
        row["data_type"] = "numeric(12,2)"
        error = await self.preflight_fault()
        self.assertEqual(error.code, "SCHEMA_CHANGED")
        self.metadata.payload = copy.deepcopy(original)
        key = next(row for row in self.metadata.payload["snapshot"]["keys"] if row["table_name"] == "invoice")
        key["definition"] = "PRIMARY KEY (customer_id)"
        await self.preflight_fault()

    async def test_file_or_unverified_metadata_is_rejected(self):
        for change in ({"mode": "file"}, {"permissions_verified": False}, {"permissions_verified": None}):
            with self.subTest(change=change):
                old = copy.deepcopy(self.metadata.payload)
                self.metadata.payload.update(change)
                self.assertEqual((await self.preflight_fault()).code, "LIVE_METADATA_REQUIRED")
                self.metadata.payload = old

    async def test_every_permission_denial_is_checked_before_business(self):
        for key in ("can_write", "can_write_columns", "can_create", "can_create_database", "is_owner", "privileged_role"):
            with self.subTest(key=key):
                self.cursor.permissions = {**_NO_WRITES, key: True}
                self.assertEqual((await self.preflight_fault()).code, "DATABASE_PERMISSION_DENIED")
        for permission in ({**_NO_WRITES, "can_select": False}, {**_NO_WRITES, "rls_enabled": True, "rls_active": False},
                           {**_NO_WRITES, "can_select": 1}, {}, None):
            self.cursor.permissions = permission
            self.assertEqual((await self.preflight_fault()).code, "DATABASE_PERMISSION_DENIED")

    async def test_result_description_checks_order_name_and_exact_physical_oid(self):
        original = self.cursor.description
        for description in (original[:-1], tuple(reversed(original)),
                            (SimpleNamespace(name="unexpected", type_code=23), *original[1:]),
                            (original[0], SimpleNamespace(name="invoice_date", type_code=1184), *original[2:]),
                            (*original[:2], SimpleNamespace(name="billing_city", type_code=25), original[3])):
            with self.subTest(description=description):
                self.cursor.description = description
                await self.result_fault()

    async def test_result_cells_reject_wrong_types_nulls_scale_and_timezone(self):
        original = copy.deepcopy(self.cursor.rows[0])
        malformed = (("invoice_id", True), ("invoice_id", "1"), ("invoice_id", None), ("invoice_id", 2 ** 31),
                     ("billing_city", 1), ("invoice_date", datetime(2010, 1, 1, tzinfo=timezone.utc)),
                     ("invoice_date", "2010-01-01"), ("total", 1.23), ("total", "1.23"), ("total", None),
                     ("total", Decimal("NaN")), ("total", Decimal("Infinity")), ("total", Decimal("1.234")),
                     ("total", Decimal("100000000")))
        for name, value in malformed:
            with self.subTest(name=name, value=value):
                self.cursor.rows = [{**original, name: value}]
                await self.result_fault()

    async def test_row_keys_and_hidden_pagination_sentinel_are_validated(self):
        row = copy.deepcopy(self.cursor.rows[0])
        for rows in ([{"unexpected": 1}], [{**row, "extra": "x"}], [(1, 2)], [row, row, {**row, "total": "invalid"}]):
            with self.subTest(rows=rows):
                self.cursor.rows = rows
                await self.result_fault()

    async def test_database_timeout_is_attempted_and_transaction_closes(self):
        self.cursor.failure = ServiceFault("QUERY_TIMEOUT", "合成超时")
        with self.assertRaises(ServiceFault) as caught:
            await self.execute()
        self.assertEqual(caught.exception.code, "QUERY_TIMEOUT")
        self.assertTrue(caught.exception.execution_attempted)
        self.assertEqual(self.events[-1], "transaction_closed")

    async def test_outer_cancellation_propagates_after_transaction_cleanup(self):
        self.cursor.block = True
        task = asyncio.create_task(self.execute())
        await self.cursor.query_started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(self.events[-1], "transaction_closed")

    async def test_callback_once_immediately_before_execution_and_failure_is_not_attempted(self):
        await self.execute(on_execute=lambda: self.events.append("callback"))
        self.assertEqual(self.events.count("callback"), 1)
        self.assertEqual(self.events.index("callback") + 1, self.events.index("business"))
        def failure():
            raise RuntimeError("合成回调失败")
        error = await self.preflight_fault(on_execute=failure)
        self.assertEqual(error.code, "EXECUTION_ERROR")
        self.assertEqual(self.events[-1], "transaction_closed")

    async def test_execute_does_not_mutate_package_request_schema_or_metadata(self):
        packet, prepared, metadata = self.packet.as_dict(), copy.deepcopy(self.prepared), copy.deepcopy(self.metadata.payload)
        await self.execute()
        self.assertEqual(self.packet.as_dict(), packet)
        self.assertEqual(self.prepared, prepared)
        self.assertEqual(self.metadata.payload, metadata)


if __name__ == "__main__":
    unittest.main()
