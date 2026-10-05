"""Verify frozen Chinook inputs and live source facts using one bounded RO snapshot.

The source INSERTs are parsed offline; no reference SQL or imported statement is
executed here. Before/after checks require an evaluation database without other
writers: they cannot prove that a change was not made and restored in between.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
from collections.abc import Mapping
from pathlib import Path

from database.enrich_schema_snapshot import enrich_snapshot
from database.metadata_reader import LiveMetadataReader
from database.readonly_connection import AsyncReadOnlyClient, run_database
from database.sql_common import ServiceFault

from .chinook_oracle import (
    CHINOOK_SNAPSHOT_TABLES, canonical_chinook_snapshot_hash,
    chinook_semantic_metadata_hash, parse_chinook_sql,
)
from .contracts import EvaluationError
from .dataset import _artifact

ROOT = Path(__file__).resolve().parents[3]
_HASH = re.compile(r"[a-f0-9]{64}\Z")
_TABLE_NAMES = (
    "album", "artist", "customer", "employee", "genre", "invoice", "invoice_line",
    "media_type", "playlist", "playlist_track", "track",
)
_LOCK_SQL = "LOCK TABLE " + ", ".join("public." + name for name in _TABLE_NAMES) + " IN ACCESS SHARE MODE"
_IDENTITY_SQL = """
SELECT current_database() AS database_name, current_user AS username,
       session_user AS session_username, inet_server_port() AS server_port,
       r.rolsuper, r.rolbypassrls, r.rolcreatedb, r.rolcreaterole,
       current_setting('transaction_read_only') AS transaction_read_only,
       current_setting('transaction_isolation') AS transaction_isolation,
       current_setting('row_security') AS row_security,
       current_setting('search_path') AS search_path,
       (SELECT setting::integer FROM pg_catalog.pg_settings
        WHERE name='statement_timeout') AS statement_timeout_ms,
       (SELECT setting::integer FROM pg_catalog.pg_settings
        WHERE name='lock_timeout') AS lock_timeout_ms,
       has_database_privilege(current_user, current_database(), 'CREATE') AS database_create,
       EXISTS(SELECT 1 FROM pg_catalog.pg_roles elevated
              WHERE elevated.rolname <> current_user
                AND (elevated.rolsuper OR elevated.rolbypassrls OR elevated.rolcreaterole
                     OR elevated.rolcreatedb)
                AND pg_catalog.pg_has_role(current_user, elevated.oid, 'MEMBER'))
         AS privileged_membership
FROM pg_catalog.pg_roles r WHERE r.rolname=current_user
"""
_PERMISSIONS_SQL = """
SELECT n.nspname AS schema_name, c.relname AS table_name, c.relkind AS object_kind,
       c.relowner=(SELECT oid FROM pg_catalog.pg_roles WHERE rolname=current_user) AS owns_table,
       has_schema_privilege(current_user,n.oid,'USAGE') AS schema_usage,
       has_schema_privilege(current_user,n.oid,'CREATE') AS schema_create,
       has_table_privilege(current_user,c.oid,'SELECT') AS table_select,
       has_table_privilege(current_user,c.oid,'INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER') AS table_write,
       has_any_column_privilege(current_user,c.oid,'INSERT,UPDATE,REFERENCES') AS column_write,
       c.relrowsecurity AS rls_enabled, pg_catalog.row_security_active(c.oid) AS rls_active
FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
WHERE n.nspname='public' AND c.relkind IN ('r','p','v','m','f') ORDER BY c.relname
"""


def _json_bytes(contents):
    def unique(pairs):
        result = {}
        for name, value in pairs:
            if name in result:
                raise EvaluationError("INVALID_MANIFEST", "Duplicate frozen JSON member")
            result[name] = value
        return result

    try:
        return json.loads(contents.decode("utf-8-sig"), object_pairs_hook=unique,
                          parse_constant=lambda _: (_ for _ in ()).throw(
                              EvaluationError("INVALID_MANIFEST", "Nonfinite frozen JSON value")))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as error:
        raise EvaluationError("INVALID_MANIFEST", "Invalid frozen JSON artifact") from error


def _schema_hash(snapshot):
    if not isinstance(snapshot, Mapping):
        raise EvaluationError("INVALID_MANIFEST", "Frozen schema must be an object")
    structural = {key: snapshot.get(key) for key in ("objects", "columns", "keys", "foreign_keys")}
    if any(not isinstance(value, list) for value in structural.values()):
        raise EvaluationError("INVALID_MANIFEST", "Frozen schema projection is incomplete")
    return hashlib.sha256(json.dumps(structural, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()


class ChinookSnapshotVerifier:
    """Bind a frozen source/config/schema to the dedicated live Chinook reader.

    ``mode='file'`` is reserved for explicit mock/replay runs. A live run checks
    the actual database and role, table and column write privileges, ownership,
    transaction settings, all source-relevant facts and the physical schema.
    """

    def __init__(self, dataset_manifest, *, mode, base_dir=ROOT, config=None,
                 client=None, metadata_reader=None):
        if mode not in {"file", "live"} or not isinstance(dataset_manifest, Mapping):
            raise EvaluationError("INVALID_VERIFIER", "Invalid Chinook verifier configuration")
        if config is not None and not isinstance(config, Mapping):
            raise EvaluationError("INVALID_VERIFIER", "Source configuration must be an object")
        self.manifest = copy.deepcopy(dict(dataset_manifest))
        self.mode, self.root = mode, Path(base_dir).resolve()
        self.config = None if config is None else copy.deepcopy(dict(config))
        self.client, self.metadata_reader = client, metadata_reader

    def _file_snapshot(self):
        artifacts = self.manifest.get("artifacts")
        if not isinstance(artifacts, Mapping):
            raise EvaluationError("INVALID_MANIFEST", "Frozen artifacts are required")
        fields = {"source_sql": "source_sql_sha256", "schema": "schema_file_sha256",
                  "semantic_config": "semantic_config_sha256"}
        if "oracle" in artifacts:
            fields["oracle"] = "oracle_file_sha256"
        paths = {}
        for label, hash_field in fields.items():
            value, expected = artifacts.get(label), self.manifest.get(hash_field)
            if (not isinstance(value, str) or not value or Path(value).is_absolute()
                    or not isinstance(expected, str) or not _HASH.fullmatch(expected)):
                raise EvaluationError("INVALID_MANIFEST", "Frozen artifact path/hash is invalid")
            path = _artifact(self.root, value)
            try:
                contents = path.read_bytes()
            except OSError as error:
                raise EvaluationError("ARTIFACT_NOT_FOUND", "Frozen artifact is unavailable") from error
            if hashlib.sha256(contents).hexdigest() != expected:
                raise EvaluationError("FIXTURE_CHANGED", "Frozen artifact content changed")
            paths[label] = path

        frozen_config = _json_bytes(paths["semantic_config"].read_bytes())
        if (not isinstance(frozen_config, dict) or frozen_config.get("dataset") != "chinook"
                or frozen_config.get("source_id") != "chinook_db"
                or frozen_config.get("database") != "chinook"
                or frozen_config.get("reader_role") != "chinook_reader"):
            raise EvaluationError("SOURCE_MISMATCH", "Frozen source is not the dedicated Chinook source")
        if self.config is not None and {
            key: value for key, value in self.config.items() if key != "_base_dir"
        } != frozen_config:
            raise EvaluationError("CONFIGURATION_MISMATCH", "Runtime source semantics differ from the frozen source")
        configuration = copy.deepcopy(frozen_config if self.config is None else self.config)
        configuration.setdefault("_base_dir", str(paths["source_sql"].parent))
        data = parse_chinook_sql(paths["source_sql"])
        digest, semantic = canonical_chinook_snapshot_hash(data), chinook_semantic_metadata_hash(data)
        schema = _schema_hash(_json_bytes(paths["schema"].read_bytes()))
        if (digest != self.manifest.get("snapshot_sha256")
                or semantic != self.manifest.get("semantic_metadata_sha256")
                or self.manifest.get("data_snapshot_id") != f"sha256:{digest}"):
            raise EvaluationError("FIXTURE_CHANGED", "Frozen source projection changed")
        declared_schema = self.manifest.get("schema_fingerprint_sha256")
        if declared_schema is not None and declared_schema != schema:
            raise EvaluationError("FIXTURE_CHANGED", "Frozen physical schema changed")
        return {"snapshot_sha256": digest, "semantic_metadata_sha256": semantic,
                "schema_fingerprint_sha256": schema,
                "projection_row_counts": {key: len(data[key]) for key in CHINOOK_SNAPSHOT_TABLES}}, configuration

    async def __call__(self, run_manifest):
        if (run_manifest.suite_version != self.manifest.get("suite_version")
                or run_manifest.data_snapshot_id != self.manifest.get("data_snapshot_id")):
            return {"verified": False, "error_code": "SNAPSHOT_MISMATCH"}
        if (self.mode == "live") != (run_manifest.execution_origin == "live"):
            return {"verified": False, "error_code": "VERIFIER_ORIGIN_MISMATCH"}
        try:
            frozen, config = self._file_snapshot()
            if self.mode == "file":
                return {"verified": True, "origin": "chinook_source_fixture", **frozen}
            actual = await self._live_snapshot(config, frozen["projection_row_counts"])
            mismatch = [key for key in ("snapshot_sha256", "semantic_metadata_sha256",
                                         "schema_fingerprint_sha256", "projection_row_counts")
                        if actual.get(key) != frozen[key]]
            actual["verified"] = not mismatch and actual.get("permissions_verified") is True
            if not actual["verified"]:
                actual["error_code"] = "SCHEMA_CHANGED" if "schema_fingerprint_sha256" in mismatch else "LIVE_DATA_MISMATCH"
            return actual
        except (EvaluationError, ServiceFault) as error:
            return {"verified": False, "error_code": error.code}
        except Exception:
            # Connection details, driver text and source rows never enter reports.
            return {"verified": False, "error_code": "SNAPSHOT_VERIFICATION_FAILED"}

    async def _live_snapshot(self, config, row_counts):
        limits = {"max_rows": 100, "db_timeout_ms": 5000, "lock_timeout_ms": 1000,
                  "connect_timeout_ms": 3000, "execution_timeout_ms": 15000,
                  "_lock_invoice_before_snapshot": True,
                  "_lock_chinook_schema_before_snapshot": True}

        async def read():
            client = self.client if self.client is not None else AsyncReadOnlyClient(config)
            reader = self.metadata_reader if self.metadata_reader is not None else LiveMetadataReader(config)
            async with client.transaction(limits) as cursor:
                # The client's internal flag locks these fixed tables before its
                # identity SELECT establishes the RR snapshot; this is redundant.
                await cursor.execute(_LOCK_SQL)
                await cursor.execute(_IDENTITY_SQL)
                identity = await cursor.fetchone()
                self._check_identity(identity, config)
                await cursor.execute(_PERMISSIONS_SQL)
                permissions = await cursor.fetchmany(len(_TABLE_NAMES) + 1)
                self._check_permissions(permissions)
                metadata = await reader.aread_in_transaction(cursor)
                if metadata.get("mode") != "live" or metadata.get("permissions_verified") is not True:
                    raise EvaluationError("LIVE_METADATA_REQUIRED", "A live verified source snapshot is required")
                data = {}
                for key, (table, primary_key, columns) in CHINOOK_SNAPSHOT_TABLES.items():
                    expected_count = row_counts[key]
                    if type(expected_count) is not int or not 0 <= expected_count <= 100000:
                        raise EvaluationError("SNAPSHOT_LIMIT_EXCEEDED", "Frozen projection exceeds its read budget")
                    sql = f"SELECT {', '.join(columns)} FROM {table} ORDER BY {primary_key} LIMIT %s"
                    await cursor.execute(sql, (expected_count + 1,))
                    data[key] = await cursor.fetchmany(expected_count + 1)
                    if len(data[key]) != expected_count:
                        raise EvaluationError("LIVE_DATA_MISMATCH", "Live projection row counts changed")
                schema = enrich_snapshot(metadata["snapshot"], metadata["data"], config)
                result = {"origin": "live_readonly_chinook", "database_name": identity["database_name"],
                          "reader_role": identity["username"], "permissions_verified": True,
                          "snapshot_sha256": canonical_chinook_snapshot_hash(data),
                          "semantic_metadata_sha256": chinook_semantic_metadata_hash(data),
                          "schema_fingerprint_sha256": schema["snapshot_metadata"]["schema_fingerprint_sha256"],
                          "projection_row_counts": {key: len(data[key]) for key in CHINOOK_SNAPSHOT_TABLES}}
            return result

        return await run_database(read)

    @staticmethod
    def _check_identity(identity, config):
        if (not isinstance(identity, Mapping)
                or identity.get("database_name") != config["database"]
                or identity.get("username") != config["reader_role"]
                or identity.get("session_username") != config["reader_role"]
                or identity.get("server_port") != 5432):
            raise EvaluationError("SOURCE_MISMATCH", "Live source database or dedicated identity differs")
        if (any(identity.get(flag) is not False for flag in
                ("rolsuper", "rolbypassrls", "rolcreatedb", "rolcreaterole",
                 "database_create", "privileged_membership"))
                or identity.get("transaction_read_only") != "on"
                or identity.get("transaction_isolation") != "repeatable read"
                or identity.get("row_security") != "on"
                or identity.get("search_path") != "pg_catalog"):
            raise EvaluationError("INVALID_READER_ROLE", "Live transaction or role is not isolated and read-only")
        for key, cap in (("statement_timeout_ms", 5000), ("lock_timeout_ms", 1000)):
            if type(identity.get(key)) is not int or not 1 <= identity[key] <= cap:
                raise EvaluationError("INVALID_EXECUTION_LIMITS", "Live transaction timeout limits are missing")

    @staticmethod
    def _check_permissions(rows):
        if (not isinstance(rows, list) or len(rows) != len(_TABLE_NAMES)
                or any(not isinstance(row, Mapping) for row in rows)
                or {(row.get("schema_name"), row.get("table_name")) for row in rows}
                != {("public", name) for name in _TABLE_NAMES}):
            raise EvaluationError("SOURCE_OBJECT_MISMATCH", "Live source tables differ from the Chinook dataset")
        for row in rows:
            if (row.get("object_kind") != "r"
                    or any(row.get(flag) is not True for flag in ("schema_usage", "table_select"))
                    or any(row.get(flag) is not False for flag in
                           ("owns_table", "schema_create", "table_write", "column_write"))
                    or type(row.get("rls_enabled")) is not bool
                    or row.get("rls_enabled") is True and row.get("rls_active") is not True):
                raise EvaluationError("INVALID_READER_ROLE", "Live source read permissions or ownership are unsafe")


# Preserve the factory name used by the older evaluation entry point when dispatching.
SnapshotVerifier = ChinookSnapshotVerifier
