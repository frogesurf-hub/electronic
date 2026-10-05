"""查询统一入口：读取元数据、增强、映射、澄清分流及 SQL 执行。"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path

if __package__:
    from .enrich_schema_snapshot import enrich_snapshot
    from .metadata_reader import LiveMetadataReader
    from .schema_linking_service import SchemaLinkingService
    from .sql_common import ServiceFault, ReadOnlyClient, json_value, positive_limit
    from .sql_generator import SQLGenerator
else:
    from enrich_schema_snapshot import enrich_snapshot
    from metadata_reader import LiveMetadataReader
    from schema_linking_service import SchemaLinkingService
    from sql_common import ServiceFault, ReadOnlyClient, json_value, positive_limit
    from sql_generator import SQLGenerator

LOGGER = logging.getLogger(__name__)


class PsycopgExecutor:
    def __init__(self, config=None, client=None):
        self.client = client if client is not None else ReadOnlyClient(config)

    def __call__(self, sql, parameters, limits):
        attempted = False
        try:
            with self.client.transaction(limits) as cursor:
                attempted = True
                cursor.execute(sql, parameters)
                rows = cursor.fetchmany(limits["max_rows"] + 1)
                return {"rows": rows[:limits["max_rows"]],
                        "truncated": len(rows) > limits["max_rows"]}
        except ServiceFault as error:
            error.execution_attempted = attempted
            raise


class SQLService:
    """接收已提取的结构化需求；默认先实时读取数据库元数据。"""

    def __init__(self, base_dir=None, executor=None, metadata_reader=None,
                 linking_service=None, sql_generator=None, config=None):
        base = Path(base_dir) if base_dir else Path(__file__).resolve().parent
        self.config = dict(config) if config is not None else json.loads(
            (base / "source_config.json").read_text(encoding="utf-8-sig")
        )
        self.config["_base_dir"] = str(base.resolve())
        if (self.config.get("source_id") != "chinook_db" or self.config.get("dataset") != "chinook"
                or self.config.get("database") != "chinook"):
            raise ServiceFault("INVALID_CONFIGURATION", "项目仅支持 Chinook 数据库")
        self.metadata_reader = (metadata_reader if metadata_reader is not None
                                else LiveMetadataReader(self.config))
        self.linking_service = (linking_service if linking_service is not None
                                else SchemaLinkingService())
        self.sql_generator = sql_generator if sql_generator is not None else SQLGenerator()
        self.executor = executor if executor is not None else PsycopgExecutor(self.config)

    def query(self, query, context=None):
        started = time.perf_counter()
        response = {
            "status": "error", "columns": [], "rows": [], "sql": None,
            "parameters": {}, "source_id": self.config["source_id"],
            "tables": [], "metric_definition_id": None, "truncated": False,
            "issues": [], "execution_attempted": False, "stage": "input",
            "route_to": None,
        }
        try:
            if not isinstance(query, dict) or (context is not None and not isinstance(context, dict)):
                raise ServiceFault("INVALID_QUERY", "query 和 context 必须为字典")
            context = {} if context is None else context
            for key in ("session_id", "task_id", "request_id", "step_id"):
                if key in context:
                    if not isinstance(context[key], str):
                        raise ServiceFault("INVALID_QUERY", f"{key} 必须为字符串")
                    response[key] = context[key]
            response["metric_definition_id"] = query.get("metric_definition_id")
            limits = {
                "max_rows": positive_limit(context.get("max_rows", 100),
                                           min(self.config["max_result_rows"], 1000), "max_rows"),
                "db_timeout_ms": positive_limit(context.get("db_timeout_ms", 5000),
                                                min(self.config["statement_timeout_ms"], 5000),
                                                "db_timeout_ms"),
            }

            # 需求到达后读取；不在初始化时缓存本地快照或业务名单。
            response["stage"] = "metadata"
            metadata = self.metadata_reader.read(limits)
            try:
                snapshot = enrich_snapshot(metadata["snapshot"], metadata["data"], self.config)
            except (KeyError, ValueError, TypeError) as error:
                raise ServiceFault("METADATA_ERROR", "数据库结构或业务元数据不完整、不一致") from error
            mode = metadata.get("mode", "provided")
            if mode == "live":
                verified = metadata.get("permissions_verified") is True
                if not verified:
                    raise ServiceFault("DATABASE_PERMISSION_DENIED", "实时元数据权限尚未验证")
                snapshot["snapshot_metadata"].update(
                    semantic_source=metadata.get("semantic_source", "database"),
                    semantic_database_consistency_verified=metadata.get("semantic_source", "database") == "database",
                    schema_captured_at=metadata["captured_at"], schema_capture_precision="second",
                )
                snapshot["access_scope"].update(
                    live_permissions_verified=True, permissions_checked_at=metadata["captured_at"],
                )
            response["metadata"] = {
                "mode": mode,
                "schema_fingerprint_sha256": snapshot["snapshot_metadata"]["schema_fingerprint_sha256"],
                "captured_at": snapshot["snapshot_metadata"]["schema_captured_at"],
                "live_permissions_verified": snapshot["access_scope"]["live_permissions_verified"],
            }

            response["stage"] = "mapping"
            decision = self.linking_service.match(
                query, snapshot, context, entities=metadata["data"]
            )
            response["mapping_status"] = decision["status"]
            if decision["status"] == "needs_clarification":
                response.update(
                    status="needs_clarification", route_to="clarification",
                    clarification_request=json_value(decision["clarification_request"]),
                    issues=json_value(decision.get("issues", decision["clarification_request"].get("issues", []))),
                )
            elif decision["status"] == "error":
                response.update(status="error", error_code=decision["error_code"],
                                error_message=decision["error_message"], issues=decision.get("issues", []))
            elif decision["status"] == "ready":
                request = decision["sql_generation_request"]
                response.update(sql_generation_request=json_value(request),
                                metric_definition_id=request["metric_definition_id"],
                                tables=[request["table"]], stage="generation")
                sql, parameters = self.sql_generator.generate(request, limits)
                response.update(sql=sql, parameters=json_value(parameters), stage="execution")
                execution = self.executor(sql, parameters, limits)
                response.update(status="ok", route_to="answer", stage="complete",
                                execution_attempted=True,
                                columns=[{"name": "sales_amount", "data_type": "decimal",
                                          "unit": self.config.get("amount_unit", "元")}],
                                rows=json_value(execution["rows"]), truncated=execution["truncated"])
            else:
                raise ServiceFault("INTERNAL_ERROR", "映射模块返回了未知状态")
        except ServiceFault as error:
            response.update(status=error.status, error_code=error.code, error_message=str(error),
                            issues=error.issues, execution_attempted=error.execution_attempted)
        except Exception:
            LOGGER.exception("SQLService unexpected error")
            response.update(status="error", error_code="INTERNAL_ERROR",
                            error_message="服务内部错误，请检查后端日志")
        response["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 2)
        return response
