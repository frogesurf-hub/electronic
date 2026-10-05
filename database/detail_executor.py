"""明细内部执行包 → 同事务结构/权限复核 → 有预算的只读分页。"""
from __future__ import annotations

import asyncio
import copy
import json
import re
import time
from collections.abc import Mapping
from datetime import datetime
from decimal import Decimal, localcontext
from pathlib import Path

from .detail_contracts import DetailCandidate, ValidatedDetailSQL
from .detail_query import prepare_detail
from .detail_validator import DetailSQLValidator
from .metadata_reader import LiveMetadataReader
from .sql_common import ServiceFault, positive_limit

_PERMISSIONS_SQL = """
SELECT pg_catalog.has_table_privilege(%s, 'SELECT') AS can_select,
       pg_catalog.has_table_privilege(%s, 'INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER') AS can_write,
       pg_catalog.has_any_column_privilege(%s, 'INSERT, UPDATE, REFERENCES') AS can_write_columns,
       pg_catalog.has_schema_privilege('public', 'CREATE') AS can_create,
       pg_catalog.has_database_privilege(current_database(), 'CREATE') AS can_create_database,
       pg_catalog.pg_has_role(current_user, c.relowner, 'USAGE') AS is_owner,
       EXISTS (SELECT 1 FROM pg_catalog.pg_roles r WHERE r.rolname = current_user
               AND (r.rolsuper OR r.rolbypassrls OR r.rolcreatedb OR r.rolcreaterole)) AS privileged_role,
       c.relrowsecurity AS rls_enabled,
       pg_catalog.row_security_active(c.oid) AS rls_active
FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.oid = %s::pg_catalog.regclass AND n.nspname = 'public' AND c.relkind = 'r'
"""
_PERMISSION_KEYS = {"can_select", "can_write", "can_write_columns", "can_create", "can_create_database",
                    "is_owner", "privileged_role", "rls_enabled", "rls_active"}
_DENIED_FLAGS = {"can_write", "can_write_columns", "can_create", "can_create_database", "is_owner", "privileged_role"}
_SESSION_KEYS = ("session_id", "task_id", "request_id", "step_id")


def _fault(code, message):
    raise ServiceFault(code, message)


def _candidate(packet):
    return DetailCandidate(sql=packet.candidate_sql, source_id=packet.source_id, table=packet.table,
                           parameter_names=tuple(packet.parameters),
                           schema_fingerprint_sha256=packet.schema_fingerprint_sha256,
                           **{key: getattr(packet, key) for key in _SESSION_KEYS})


def _same_packet(actual, expected):
    """递归类型敏感比较，防止 True==1 / False==0 隐藏包字段篡改。"""
    if type(actual) is not type(expected):
        return False
    if type(expected) is dict:
        return (set(actual) == set(expected)
                and all(_same_packet(actual[key], value) for key, value in expected.items()))
    if type(expected) is list:
        return len(actual) == len(expected) and all(_same_packet(a, b) for a, b in zip(actual, expected))
    return actual == expected


def _expected_oid(column):
    physical = column["physical_data_type"].lower()
    if physical in {"integer", "int4"}:
        return 23
    if physical == "text":
        return 25
    if physical.startswith("character varying"):
        return 1043
    if physical == "numeric" or physical.startswith("numeric("):
        return 1700
    if physical in {"timestamp without time zone", "timestamp"}:
        return 1114
    _fault("METADATA_ERROR", "明细结果的物理字段类型未获准")


def _json_cell(value, column):
    if value is None:
        if column["nullable"] is not True:
            _fault("RESULT_CONTRACT_MISMATCH", "非空字段返回 NULL")
        return None
    kind = column["data_type"]
    if kind == "integer" and type(value) is int and -(2 ** 31) <= value < 2 ** 31:
        return value
    if kind == "text" and type(value) is str:
        return value
    if kind == "decimal" and type(value) is Decimal and value.is_finite():
        # 按实时 numeric(p,2) 精度验证；不隐式舍入畸形返回。
        precision = int(re.fullmatch(r"numeric\(([1-9]\d*),\s*2\)", column["physical_data_type"])[1])
        with localcontext() as context:
            context.prec = max(28, precision + 4)
            if value.copy_abs() < Decimal(10) ** (precision - 2) and value == value.quantize(Decimal("0.01")):
                return format(value, ".2f")
    if kind == "timestamp" and type(value) is datetime and value.tzinfo is None:
        return value.isoformat()
    _fault("RESULT_CONTRACT_MISMATCH", "数据库返回值与明细字段类型不一致")


class DetailReadOnlyExecutor:
    def __init__(self, config=None, *, client=None, metadata_reader=None):
        if config is None:
            base = Path(__file__).resolve().parent
            config = json.loads((base / "source_config.json").read_text(encoding="utf-8-sig"))
            config["_base_dir"] = str(base)
        if not isinstance(config, Mapping):
            raise ValueError("明细执行配置必须为映射")
        self.config = copy.deepcopy(dict(config))
        self.client = client
        self.metadata_reader = metadata_reader if metadata_reader is not None else LiveMetadataReader(self.config)

    def _limits(self, limits):
        limits = {} if limits is None else limits
        if not isinstance(limits, Mapping):
            _fault("INVALID_QUERY", "执行限制必须为映射")
        db = positive_limit(limits.get("db_timeout_ms", 5000),
                            min(self.config.get("statement_timeout_ms", 5000), 5000), "db_timeout_ms")
        cap = min(self.config.get("max_result_rows", 1000), 1000)
        return {
            "max_rows": positive_limit(limits.get("max_rows", cap), cap, "max_rows"),
            "db_timeout_ms": db,
            "lock_timeout_ms": positive_limit(limits.get("lock_timeout_ms", 1000), min(db, 1000), "lock_timeout_ms"),
            "connect_timeout_ms": positive_limit(limits.get("connect_timeout_ms", 3000), 5000, "connect_timeout_ms"),
            "execution_timeout_ms": positive_limit(limits.get("execution_timeout_ms", 10000), 15000, "execution_timeout_ms"),
        }

    def _check_package(self, packet, request, schema, limits):
        if type(packet) is not ValidatedDetailSQL:
            _fault("INVALID_EXECUTION_PACKAGE", "明细执行只接受内部 ValidatedDetailSQL 对象")
        try:
            if (type(packet._parameter_items) is not tuple or type(packet._output_columns) is not tuple
                    or any(type(item) is not tuple or len(item) != 2 or type(item[0]) is not str for item in packet._parameter_items)
                    or len({item[0] for item in packet._parameter_items}) != len(packet._parameter_items)):
                _fault("INVALID_EXECUTION_PACKAGE", "内部包参数列表结构异常或名称重复")
            checked = DetailSQLValidator().validate(_candidate(packet), request, schema, packet.parameters, limits)
            if not checked.passed or not _same_packet(packet.as_dict(), checked.validated_sql.as_dict()):
                _fault("INVALID_EXECUTION_PACKAGE", "执行包与可信请求、结构或当前策略不一致")
        except ServiceFault:
            raise
        except (TypeError, ValueError, KeyError, AttributeError):
            _fault("INVALID_EXECUTION_PACKAGE", "明细执行包结构不合法")
        return checked.validated_sql

    async def execute(self, package, generation_request, schema_context, *, limits=None, on_execute=None):
        started, attempted = time.perf_counter(), False
        try:
            budgets = self._limits(limits)
            if not isinstance(generation_request, Mapping) or not isinstance(schema_context, Mapping):
                _fault("INVALID_EXECUTION_PACKAGE", "必须提供可信明细请求和结构")
            if on_execute is not None and not callable(on_execute):
                _fault("INVALID_QUERY", "on_execute 必须为可调用对象")
            request, schema = copy.deepcopy(dict(generation_request)), copy.deepcopy(dict(schema_context))
            checked = self._check_package(package, request, schema, budgets)
            if (self.config.get("dataset") != "chinook" or self.config.get("source_id") != checked.source_id
                    or checked.table not in self.config.get("query_table_allowlist", [])):
                _fault("TABLE_NOT_ALLOWED", "执行数据源或表不属于当前 Chinook 白名单")
            from .readonly_connection import AsyncReadOnlyClient, run_database

            async def run():
                nonlocal attempted
                client = self.client if self.client is not None else AsyncReadOnlyClient(self.config)
                # AsyncReadOnlyClient 在 BEGIN 后、首次 SELECT 建立快照前锁定全部 11 表。
                async with client.transaction({**budgets, "_lock_chinook_schema_before_snapshot": True}) as cursor:
                    metadata = await self.metadata_reader.aread_in_transaction(cursor)
                    if metadata.get("mode") != "live" or metadata.get("permissions_verified") is not True:
                        _fault("LIVE_METADATA_REQUIRED", "执行必须取得同事务实时结构与已验证权限")
                    current_prepared = prepare_detail(request, metadata["snapshot"], self.config, limits=budgets)
                    if current_prepared.schema_context["schema_fingerprint_sha256"] != checked.schema_fingerprint_sha256:
                        _fault("SCHEMA_CHANGED", "数据库结构变化，请重新生成 SQL")
                    if current_prepared.generation_request != request:
                        _fault("SEMANTIC_CONTEXT_CHANGED", "当前明细字段、类型或分页规则发生变化")
                    current = self._check_package(checked, current_prepared.generation_request,
                                                  current_prepared.schema_context, budgets)
                    target = checked.table
                    await cursor.execute(_PERMISSIONS_SQL, (target, target, target, target))
                    permission = await cursor.fetchone()
                    if (not isinstance(permission, Mapping) or set(permission) != _PERMISSION_KEYS
                            or any(type(permission[key]) is not bool for key in _PERMISSION_KEYS)
                            or permission["can_select"] is not True
                            or any(permission[key] for key in _DENIED_FLAGS)
                            or (permission["rls_enabled"] and not permission["rls_active"])):
                        _fault("DATABASE_PERMISSION_DENIED", "目标表 SELECT、只读身份、CREATE 或 RLS 权限复核失败")
                    if on_execute is not None:
                        on_execute()
                    attempted = True
                    await cursor.execute(current.sql, current.parameters)
                    indexed = {column["name"]: column for column in current_prepared.schema_context["columns"]}
                    columns = [indexed[name] for name in request["columns"]]
                    description = cursor.description
                    if (description is None or len(description) != len(columns)
                            or any(item.name != column["name"] or item.type_code != _expected_oid(column)
                                   for item, column in zip(description, columns))):
                        _fault("RESULT_CONTRACT_MISMATCH", "数据库返回列名、顺序或 OID 与可信投影不一致")
                    rows = await cursor.fetchmany(current.max_rows + 1)
                    if type(rows) is not list or len(rows) > current.max_rows + 1:
                        _fault("RESULT_CONTRACT_MISMATCH", "明细结果超过受控读取范围")
                    returned = []
                    expected_keys = set(request["columns"])
                    for row in rows:
                        if not isinstance(row, Mapping) or set(row) != expected_keys:
                            _fault("RESULT_CONTRACT_MISMATCH", "明细行字段与可信投影不一致")
                        returned.append({column["name"]: _json_cell(row[column["name"]], column) for column in columns})
                    has_more = len(returned) > current.max_rows
                    returned = returned[:current.max_rows]
                    return {
                        "columns": current.output_columns, "rows": returned, "row_count": len(returned),
                        "page": {"size": current.max_rows, "offset": current.page_offset},
                        "has_more": has_more, "next_offset": current.page_offset + current.max_rows if has_more else None,
                        "truncated": has_more, "execution_attempted": True, "limits": budgets,
                        "metadata": {"mode": "live", "permissions_verified": True,
                                     "schema_fingerprint_sha256": current.schema_fingerprint_sha256,
                                     "captured_at": metadata["captured_at"], "same_transaction_revalidation": True},
                    }

            result = await run_database(run)
            result["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 2)
            return result
        except asyncio.CancelledError:
            raise
        except ServiceFault as error:
            error.execution_attempted = attempted
            raise
        except Exception as error:
            raise ServiceFault("EXECUTION_ERROR", "只读明细执行或结果复核失败", execution_attempted=attempted) from error
