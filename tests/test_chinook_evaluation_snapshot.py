"""Independent Chinook source binding and RO snapshot guards; no live DB access."""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import unittest
from contextlib import asynccontextmanager
from decimal import Decimal
from unittest.mock import patch

from agent_core.evaluation.chinook_oracle import (
    canonical_chinook_snapshot_hash, chinook_semantic_metadata_hash, parse_chinook_sql,
)
from agent_core.evaluation.chinook_snapshot import (
    ROOT, ChinookSnapshotVerifier, _schema_hash,
)
from agent_core.evaluation.contracts import RunManifest


def fixtures():
    paths = {"source_sql": "database/Chinook_PostgreSql.sql",
             "schema": "database/generated/chinook_schema_snapshot.json",
             "semantic_config": "database/source_config.json",
             "oracle": "src/agent_core/evaluation/chinook_oracle.py"}
    config = json.loads((ROOT / paths["semantic_config"]).read_text(encoding="utf-8-sig"))
    schema = json.loads((ROOT / paths["schema"]).read_text(encoding="utf-8-sig"))
    data = parse_chinook_sql(ROOT / paths["source_sql"])
    digest = canonical_chinook_snapshot_hash(data)
    manifest = {"suite_version": "chinook-fixture-v1", "data_snapshot_id": f"sha256:{digest}",
                "snapshot_sha256": digest, "semantic_metadata_sha256": chinook_semantic_metadata_hash(data),
                "schema_fingerprint_sha256": _schema_hash(schema), "artifacts": paths}
    for label, field in {"source_sql": "source_sql_sha256", "schema": "schema_file_sha256",
                         "semantic_config": "semantic_config_sha256", "oracle": "oracle_file_sha256"}.items():
        manifest[field] = hashlib.sha256((ROOT / paths[label]).read_bytes()).hexdigest()
    metadata_data = json.loads((ROOT / "database/generated/chinook_metadata.json").read_text(encoding="utf-8-sig"))
    metadata = {"snapshot": schema, "data": metadata_data, "mode": "live", "permissions_verified": True}
    return data, config, manifest, metadata


def identity():
    return {"database_name": "chinook", "username": "chinook_reader", "session_username": "chinook_reader",
            "server_port": 5432, "rolsuper": False, "rolbypassrls": False, "rolcreatedb": False,
            "rolcreaterole": False, "database_create": False, "privileged_membership": False,
            "transaction_read_only": "on", "transaction_isolation": "repeatable read", "row_security": "on",
            "search_path": "pg_catalog", "statement_timeout_ms": 5000, "lock_timeout_ms": 1000}


def permissions():
    names = ("album", "artist", "customer", "employee", "genre", "invoice", "invoice_line",
             "media_type", "playlist", "playlist_track", "track")
    return [{"schema_name": "public", "table_name": name, "object_kind": "r", "owns_table": False,
             "schema_usage": True, "schema_create": False, "table_select": True, "table_write": False,
             "column_write": False, "rls_enabled": False, "rls_active": False} for name in names]


class FakeSource:
    def __init__(self, data, metadata):
        self.data, self.metadata = copy.deepcopy(data), copy.deepcopy(metadata)
        self.identity, self.permissions = identity(), permissions()
        self.calls, self.closed = [], 0
        self.active = False
        self.latest = ""

    @asynccontextmanager
    async def transaction(self, limits):
        self.limits = copy.deepcopy(limits)
        self.active = True
        try:
            yield self
        finally:
            self.active = False
            self.closed += 1

    async def execute(self, sql, params=None):
        if not self.active:
            raise AssertionError("query escaped its transaction")
        self.latest = sql
        self.calls.append((sql, params))

    async def fetchone(self):
        return copy.deepcopy(self.identity)

    async def fetchmany(self, size):
        if "FROM pg_catalog.pg_class" in self.latest:
            return copy.deepcopy(self.permissions[:size])
        if "FROM public.invoice ORDER BY" in self.latest:
            return copy.deepcopy(self.data["invoices"][:size])
        if "FROM public.customer ORDER BY" in self.latest:
            return copy.deepcopy(self.data["customers"][:size])
        raise AssertionError("unexpected snapshot query")

    async def aread_in_transaction(self, cursor):
        if cursor is not self or not self.active:
            raise AssertionError("metadata used a different transaction")
        return copy.deepcopy(self.metadata)


async def direct(factory):
    return await factory()


class ChinookSnapshotTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.data, self.config, self.manifest, self.metadata = fixtures()
        self.source = FakeSource(self.data, self.metadata)
        for target in ("psycopg.connect", "psycopg.AsyncConnection.connect"):
            guard = patch(target, side_effect=AssertionError("offline tests forbid database connections"))
            mock = guard.start()
            self.addCleanup(guard.stop)
            self.addCleanup(mock.assert_not_called)
        guard = patch("agent_core.evaluation.chinook_snapshot.run_database", side_effect=direct)
        guard.start()
        self.addCleanup(guard.stop)

    def run_manifest(self, origin="live", **changes):
        values = dict(run_id="test", suite_version=self.manifest["suite_version"],
                      data_snapshot_id=self.manifest["data_snapshot_id"], provider_origin="fake",
                      execution_origin=origin)
        values.update(changes)
        return RunManifest(**values)

    def verifier(self, **changes):
        values = dict(mode="live", config=self.config, client=self.source, metadata_reader=self.source)
        values.update(changes)
        return ChinookSnapshotVerifier(self.manifest, **values)

    async def test_file_fixture_hashes_are_bound_and_live_cannot_use_file_mode(self):
        verifier = self.verifier(mode="file")
        result = await verifier(self.run_manifest("mock"))
        self.assertTrue(result["verified"], result)
        self.assertEqual(result["projection_row_counts"], {"invoices": 412, "customers": 59})
        self.assertEqual(result["schema_fingerprint_sha256"], self.manifest["schema_fingerprint_sha256"])
        self.assertEqual(self.source.calls, [])
        result = await verifier(self.run_manifest())
        self.assertEqual(result["error_code"], "VERIFIER_ORIGIN_MISMATCH")

    async def test_changed_artifact_hash_or_escaping_path_blocks_database_calls(self):
        for changes, code in (({"source_sql_sha256": "0" * 64}, "FIXTURE_CHANGED"),
                              ({"artifacts": self.manifest["artifacts"] | {"schema": "../outside.json"}}, "INVALID_MANIFEST")):
            verifier = ChinookSnapshotVerifier(self.manifest | changes, mode="live", config=self.config,
                                               client=self.source, metadata_reader=self.source)
            result = await verifier(self.run_manifest())
            self.assertFalse(result["verified"])
            self.assertEqual(result["error_code"], code)
        self.assertEqual(self.source.calls, [])

    async def test_runtime_config_and_run_versions_must_match_frozen_source(self):
        result = await self.verifier(config=self.config | {"currency": "USD"})(self.run_manifest())
        self.assertEqual(result["error_code"], "CONFIGURATION_MISMATCH")
        result = await self.verifier()(self.run_manifest(suite_version="other"))
        self.assertEqual(result["error_code"], "SNAPSHOT_MISMATCH")
        self.assertEqual(self.source.calls, [])

    async def test_live_facts_schema_identity_permissions_and_locks_share_one_snapshot(self):
        result = await self.verifier()(self.run_manifest())
        self.assertTrue(result["verified"], result)
        self.assertEqual(result["database_name"], "chinook")
        self.assertEqual(result["reader_role"], "chinook_reader")
        self.assertTrue(result["permissions_verified"])
        self.assertEqual(result["snapshot_sha256"], self.manifest["snapshot_sha256"])
        self.assertEqual(self.source.closed, 1)
        self.assertTrue(self.source.limits["_lock_invoice_before_snapshot"])
        self.assertTrue(self.source.limits["_lock_chinook_schema_before_snapshot"])
        self.assertIn("public.invoice", self.source.calls[0][0])
        self.assertIn("public.customer", self.source.calls[0][0])
        self.assertTrue(self.source.calls[0][0].startswith("LOCK TABLE"))
        projections = [(sql, args) for sql, args in self.source.calls if "ORDER BY" in sql and " LIMIT %s" in sql]
        self.assertEqual(len(projections), 2)
        self.assertEqual([args for _, args in projections], [(413,), (60,)])
        self.assertTrue(all("SELECT" in sql and "INSERT" not in sql and "DROP" not in sql for sql, _ in projections))

    async def test_wrong_live_database_role_session_or_port_is_rejected(self):
        for change in ({"database_name": "electronic_cup"}, {"username": "postgres"},
                       {"session_username": "postgres"}, {"server_port": 5433}):
            with self.subTest(change=change):
                self.source.identity = identity() | change
                result = await self.verifier()(self.run_manifest())
                self.assertEqual(result["error_code"], "SOURCE_MISMATCH")

    async def test_role_transaction_and_timeout_limits_are_strict(self):
        for change, code in (({"rolsuper": True}, "INVALID_READER_ROLE"),
                             ({"privileged_membership": True}, "INVALID_READER_ROLE"),
                             ({"database_create": True}, "INVALID_READER_ROLE"),
                             ({"transaction_read_only": "off"}, "INVALID_READER_ROLE"),
                             ({"transaction_isolation": "read committed"}, "INVALID_READER_ROLE"),
                             ({"search_path": "public, pg_catalog"}, "INVALID_READER_ROLE"),
                             ({"row_security": "off"}, "INVALID_READER_ROLE"),
                             ({"statement_timeout_ms": 0}, "INVALID_EXECUTION_LIMITS"),
                             ({"lock_timeout_ms": 1001}, "INVALID_EXECUTION_LIMITS"),
                             ({"lock_timeout_ms": True}, "INVALID_EXECUTION_LIMITS")):
            with self.subTest(change=change):
                self.source.identity = identity() | change
                result = await self.verifier()(self.run_manifest())
                self.assertEqual(result["error_code"], code)

    async def test_table_and_column_write_owner_schema_create_or_inactive_rls_are_rejected(self):
        for change in ({"table_write": True}, {"column_write": True}, {"owns_table": True},
                       {"schema_create": True}, {"table_select": False},
                       {"rls_enabled": True, "rls_active": False}):
            with self.subTest(change=change):
                self.source.permissions = permissions()
                self.source.permissions[0].update(change)
                result = await self.verifier()(self.run_manifest())
                self.assertEqual(result["error_code"], "INVALID_READER_ROLE")

    async def test_missing_extra_or_replaced_source_object_is_rejected(self):
        for change in (lambda rows: rows.pop(), lambda rows: rows.append(dict(rows[0], table_name="extra")),
                       lambda rows: rows[0].update(table_name="other")):
            self.source.permissions = permissions()
            change(self.source.permissions)
            result = await self.verifier()(self.run_manifest())
            self.assertEqual(result["error_code"], "SOURCE_OBJECT_MISMATCH")

    async def test_same_count_and_sum_with_changed_invoice_rows_does_not_match_source(self):
        self.source.data["invoices"][0]["total"] = str(Decimal(self.source.data["invoices"][0]["total"]) + Decimal("0.01"))
        self.source.data["invoices"][1]["total"] = str(Decimal(self.source.data["invoices"][1]["total"]) - Decimal("0.01"))
        result = await self.verifier()(self.run_manifest())
        self.assertFalse(result["verified"])
        self.assertEqual(result["error_code"], "LIVE_DATA_MISMATCH")

    async def test_customer_names_city_and_extra_row_are_part_of_the_frozen_projection(self):
        changes = [lambda data: data["customers"][0].update(first_name="changed"),
                   lambda data: data["invoices"][0].update(billing_city="changed"),
                   lambda data: data["invoices"].append(dict(data["invoices"][0], invoice_id=99999))]
        for change in changes:
            self.source.data = copy.deepcopy(self.data)
            change(self.source.data)
            result = await self.verifier()(self.run_manifest())
            self.assertFalse(result["verified"])
            self.assertEqual(result["error_code"], "LIVE_DATA_MISMATCH")

    async def test_changed_live_schema_and_unverified_metadata_fail(self):
        self.source.metadata["snapshot"]["columns"][0]["data_type"] = "text"
        result = await self.verifier()(self.run_manifest())
        self.assertFalse(result["verified"])
        self.assertEqual(result["error_code"], "SCHEMA_CHANGED")
        self.source.metadata = copy.deepcopy(self.metadata)
        self.source.metadata["permissions_verified"] = False
        result = await self.verifier()(self.run_manifest())
        self.assertEqual(result["error_code"], "LIVE_METADATA_REQUIRED")

    async def test_external_cancel_propagates_after_transaction_cleanup(self):
        started = asyncio.Event()

        class SlowReader:
            async def aread_in_transaction(self, cursor):
                started.set()
                await asyncio.sleep(10)

        verifier = self.verifier(metadata_reader=SlowReader())
        task = asyncio.create_task(verifier(self.run_manifest()))
        await asyncio.wait_for(started.wait(), 2)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(self.source.closed, 1)
        self.assertFalse(self.source.active)


if __name__ == "__main__":
    unittest.main()
