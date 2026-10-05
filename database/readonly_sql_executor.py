"""第二项执行包 → 实时复核 → 有预算的参数化只读执行。

执行器只接受内部 ValidatedSQL 和可信请求/结构，拒绝任意 SQL 或 JSON 包。
同一只读 RR 事务内锁定目标表、刷新元数据、复核映射和检验，执行重建 SQL。
"""
from __future__ import annotations

import asyncio
import copy
import json
import time
from collections.abc import Mapping
from decimal import Decimal
from pathlib import Path

from .enrich_schema_snapshot import enrich_snapshot
from .metadata_reader import LiveMetadataReader
from .schema_linking_service import SchemaLinkingService
from .sql_common import ServiceFault, positive_limit
from .sql_contracts import SQLCandidate, ValidatedSQL
from .sql_validator import SQLValidator
from .query_profiles import query_profile, output_columns

_FIELD_IDS = {
    "customer_id": "customer",
    "billing_country": "country", "billing_city": "city",
}
_PERMISSIONS_SQL = """
SELECT has_table_privilege(%s, 'INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER') AS can_write,
       has_any_column_privilege(%s, 'INSERT, UPDATE, REFERENCES') AS can_write_columns,
       has_schema_privilege('public', 'CREATE') AS can_create,
       c.relowner = (SELECT oid FROM pg_roles WHERE rolname = current_user) AS is_owner,
       c.relrowsecurity AS rls_enabled,
       row_security_active(c.oid) AS rls_active
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'public' AND c.relname = 'invoice' AND c.relkind IN ('r', 'p')
"""


def _fault(code, message):
    raise ServiceFault(code, message)


def _candidate(package):
    return SQLCandidate(
        sql=package.candidate_sql, source_id=package.source_id,
        metric_definition_id=package.metric_definition_id,
        parameter_names=tuple(package.parameters),
        schema_fingerprint_sha256=package.schema_fingerprint_sha256,
        **{key: getattr(package, key) for key in ("session_id", "task_id", "request_id", "step_id")},
    )


def _business_query(request):
    """恢复已确认的结构化需求；使用确定编号重新匹配当前业务字典。"""
    return {
        "source_id": request["source_id"], "metric_id": "sales_amount",
        "metric_definition_id": request["metric_definition_id"],
        "time_range": {"start": request["time_filter"]["start"],
                       "end_exclusive": request["time_filter"]["end_exclusive"]},
        "filters": [{"field_id": _FIELD_IDS[item["column"]], "op": item["op"],
                     "value": copy.deepcopy(item["value"])}
                    for item in request["filters"]],
    }


def _schema(request, snapshot):
    schema_name, table_name = request["table"].split(".", 1)
    return {
        "source_id": request["source_id"], "table": request["table"],
        "metric_definition_id": request["metric_definition_id"],
        "schema_fingerprint_sha256": snapshot["snapshot_metadata"]["schema_fingerprint_sha256"],
        "columns": [{"name": row["column_name"], "data_type": row["data_type"]}
                    for row in snapshot["columns"]
                    if row["schema_name"] == schema_name and row["table_name"] == table_name],
    }


class ReadOnlySQLExecutor:
    def __init__(self, config=None, *, client=None, metadata_reader=None):
        if config is None:
            base = Path(__file__).resolve().parent
            config = json.loads((base / "source_config.json").read_text(encoding="utf-8-sig"))
            config["_base_dir"] = str(base)
        self.config = copy.deepcopy(config)
        if (self.config.get("source_id") != "chinook_db" or self.config.get("dataset") != "chinook"
                or self.config.get("database") != "chinook"):
            raise ServiceFault("INVALID_CONFIGURATION", "只读查询仅支持 Chinook 数据库")
        self.client = client
        self.metadata_reader = metadata_reader if metadata_reader is not None else LiveMetadataReader(self.config)

    def _limits(self, limits):
        if limits is None:
            limits = {}
        if not isinstance(limits, Mapping):
            _fault("INVALID_QUERY", "执行限制必须为映射")
        db = positive_limit(limits.get("db_timeout_ms", 5000),
                            min(self.config["statement_timeout_ms"], 5000), "db_timeout_ms")
        return {
            "max_rows": positive_limit(limits.get("max_rows", 100),
                                       min(self.config["max_result_rows"], 1000), "max_rows"),
            "db_timeout_ms": db,
            "lock_timeout_ms": positive_limit(limits.get("lock_timeout_ms", 1000), min(db, 1000), "lock_timeout_ms"),
            "connect_timeout_ms": positive_limit(limits.get("connect_timeout_ms", 3000), 5000, "connect_timeout_ms"),
            "execution_timeout_ms": positive_limit(limits.get("execution_timeout_ms", 10000), 15000, "execution_timeout_ms"),
        }

    def _check_package(self, package, request, schema, limits):
        if type(package) is not ValidatedSQL:
            _fault("INVALID_EXECUTION_PACKAGE", "只读执行必须使用第二项的内部 ValidatedSQL 对象")
        try:
            checked = SQLValidator().validate(_candidate(package), request, schema, package.parameters, limits)
            if not checked.passed or checked.validated_sql.as_dict() != package.as_dict():
                _fault("INVALID_EXECUTION_PACKAGE", "执行包与可信请求、结构或当前检验策略不一致")
        except ServiceFault:
            raise
        except (TypeError, ValueError, KeyError, AttributeError):
            _fault("INVALID_EXECUTION_PACKAGE", "执行包结构不合法")
        return checked.validated_sql

    async def execute(self, package, generation_request, schema_context, *, limits=None, on_execute=None):
        """返回 JSON 可用结果；失败抛 ServiceFault，外部取消直接传播。"""
        started, attempted = time.perf_counter(), False
        try:
            budgets = self._limits(limits)
            if not isinstance(generation_request, Mapping) or not isinstance(schema_context, Mapping):
                _fault("INVALID_EXECUTION_PACKAGE", "必须提供服务端可信请求和结构上下文")
            if on_execute is not None and not callable(on_execute):
                _fault("INVALID_QUERY", "on_execute 必须为可调用对象")
            request, schema = copy.deepcopy(dict(generation_request)), copy.deepcopy(dict(schema_context))
            checked = self._check_package(package, request, schema, budgets)
            profile = query_profile(request)
            target_table = request["table"]
            if (self.config.get("source_id") != checked.source_id
                    or target_table not in self.config.get("query_table_allowlist", [])):
                _fault("TABLE_NOT_ALLOWED", "执行的数据源或表未在当前配置中获准")

            from .readonly_connection import AsyncReadOnlyClient, run_database

            async def run():
                nonlocal attempted
                client = self.client if self.client is not None else AsyncReadOnlyClient(self.config)
                transaction_limits = {**budgets, "_lock_invoice_before_snapshot": True}
                async with client.transaction(transaction_limits) as cursor:
                    # 锁只来自固定代码，阻止目标表在复核与查询之间被 DDL 改动。
                    await cursor.execute("LOCK TABLE public.invoice IN ACCESS SHARE MODE")
                    metadata = await self.metadata_reader.aread_in_transaction(cursor)
                    if metadata.get("mode") != "live" or metadata.get("permissions_verified") is not True:
                        _fault("LIVE_METADATA_REQUIRED", "业务执行必须在当前事务中取得实时元数据及权限")
                    snapshot = enrich_snapshot(metadata["snapshot"], metadata["data"], self.config)
                    if snapshot["snapshot_metadata"]["schema_fingerprint_sha256"] != checked.schema_fingerprint_sha256:
                        _fault("SCHEMA_CHANGED", "数据库结构已变化，请重新映射和生成 SQL")
                    # 物理指纹不包含业务规则；重新映射当前规则和确定编号。
                    decision = SchemaLinkingService().match(
                        _business_query(request), snapshot, entities=metadata["data"],
                    )
                    if decision.get("status") != "ready":
                        _fault("SEMANTIC_CONTEXT_CHANGED", "当前指标规则或实体绑定已变化，请重新确认需求")
                    current_request = decision["sql_generation_request"]
                    current_schema = _schema(current_request, snapshot)
                    current = self._check_package(checked, current_request, current_schema, budgets)

                    await cursor.execute(_PERMISSIONS_SQL,
                                         (target_table, target_table))
                    permission = await cursor.fetchone()
                    required = {"can_write", "can_write_columns", "can_create", "is_owner", "rls_enabled", "rls_active"}
                    if (not isinstance(permission, Mapping) or set(permission) != required
                            or any(type(permission[key]) is not bool for key in required)
                            or any(permission[key] for key in ("can_write", "can_write_columns", "can_create", "is_owner"))
                            or (permission["rls_enabled"] and not permission["rls_active"])):
                        _fault("DATABASE_PERMISSION_DENIED", "业务表的只读权限或 RLS 约束未通过复核")
                    if on_execute is not None:
                        on_execute()
                    attempted = True
                    # 始终执行内部重建 SQL；参数与 SQL 分开绑定。
                    await cursor.execute(current.sql, current.parameters)
                    description = cursor.description
                    if (description is None or len(description) != 1
                            or description[0].name != "sales_amount" or description[0].type_code != 1700):
                        _fault("RESULT_CONTRACT_MISMATCH", "数据库返回列与 sales_amount/numeric 契约不一致")
                    rows = await cursor.fetchmany(budgets["max_rows"] + 1)
                    if not isinstance(rows, list) or len(rows) > budgets["max_rows"] + 1:
                        _fault("RESULT_CONTRACT_MISMATCH", "数据库结果超过受控读取范围")
                    for row in rows:
                        if (not isinstance(row, Mapping) or set(row) != {"sales_amount"}
                                or type(row["sales_amount"]) is not Decimal or not row["sales_amount"].is_finite()):
                            _fault("RESULT_CONTRACT_MISMATCH", "销售额必须是有限 Decimal 数值")
                    returned = [{"sales_amount": format(row["sales_amount"], ".2f")}
                                for row in rows[:budgets["max_rows"]]]
                    return {
                        "columns": output_columns(checked.source_id), "rows": returned,
                        "row_count": len(returned), "truncated": len(rows) > budgets["max_rows"],
                        "execution_attempted": True, "limits": budgets,
                        "metadata": {"mode": "live", "permissions_verified": True,
                                     "schema_fingerprint_sha256": current.schema_fingerprint_sha256,
                                     "captured_at": metadata["captured_at"],
                                     "same_transaction_revalidation": True},
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
            raise ServiceFault("EXECUTION_ERROR", "只读执行或结果复核失败",
                               execution_attempted=attempted) from error
