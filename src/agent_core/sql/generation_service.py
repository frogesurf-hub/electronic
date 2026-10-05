"""映射 → LLM 候选生成 → 检验 → 可选只读执行。"""
from __future__ import annotations

import asyncio
import copy
import json
import logging
import time
from collections.abc import Mapping
from pathlib import Path

from database.enrich_schema_snapshot import enrich_snapshot
from database.metadata_reader import LiveMetadataReader
from database.schema_linking_service import SchemaLinkingService
from database.sql_common import ServiceFault, json_value, positive_limit

from .llm_sql_generator import (
    LLMSQLGenerator,
    PROMPT_VERSION,
    SESSION_KEYS,
    prepare_generation_inputs,
)

LOGGER = logging.getLogger(__name__)


class SQLGenerationService:
    """默认实时读取元数据；离线文件读取必须显式注入。

    generate() 返回 generated/needs_clarification/error。
    generated 只表示获得候选，不等于已通过 SQL 检验或已执行。
    generate_and_validate() 在同一份结构上下文下继续第二项检验。
    query() 继续第三项，只在实时元数据和检验通过后执行。
    """

    def __init__(self, provider, *, base_dir=None, metadata_reader=None,
                 linking_service=None, llm_timeout_seconds=15.0, executor=None, config=None,
                 detail_executor=None):
        base = Path(base_dir) if base_dir is not None else Path(__file__).resolve().parents[3] / "database"
        self.config = dict(config) if config is not None else json.loads((base / "source_config.json").read_text(encoding="utf-8-sig"))
        if (self.config.get("source_id") != "chinook_db" or self.config.get("dataset") != "chinook"
                or self.config.get("database") != "chinook"):
            raise ServiceFault("INVALID_CONFIGURATION", "SQL 服务仅支持 Chinook 数据源配置")
        self.config["_base_dir"] = str(base.resolve())
        self.metadata_reader = (metadata_reader if metadata_reader is not None
                                else LiveMetadataReader(self.config))
        self.linking_service = (linking_service if linking_service is not None
                                else SchemaLinkingService())
        self.generator = LLMSQLGenerator(provider, timeout_seconds=llm_timeout_seconds)
        self.executor = executor
        from .detail_service import DetailSQLService

        self.detail_service = DetailSQLService(provider, base_dir=base, config=self.config,
                                               metadata_reader=self.metadata_reader,
                                               executor=detail_executor,
                                               llm_timeout_seconds=llm_timeout_seconds)

    async def generate(self, query, context=None):
        return await self._generate(query, context, validate_sql=False)

    async def generate_and_validate(self, query, context=None):
        """第一项接第二项；validated 返回执行包，失败不提供执行包。"""
        return await self._generate(query, context, validate_sql=True)

    async def query(self, query, context=None):
        """连续完成第一至第三项；整段请求有总预算，取消直接传播。"""
        started, response = time.perf_counter(), {}
        try:
            if context is not None and not isinstance(context, Mapping):
                raise ServiceFault("INVALID_QUERY", "context 必须为映射")
            timeout_ms = positive_limit((context or {}).get("request_timeout_ms", 30000),
                                        60000, "request_timeout_ms")
            async with asyncio.timeout(timeout_ms / 1000):
                return await self._generate(query, context, validate_sql=True,
                                            execute_sql=True, response=response)
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            response.update(status="error", route_to=None, error_code="REQUEST_TIMEOUT",
                            error_message="SQL 请求超过总时间预算", rows=[], columns=[], row_count=0)
        except ServiceFault as error:
            response.update(status="error", route_to=None, error_code=error.code,
                            error_message=str(error), rows=[], columns=[], row_count=0)
        response.setdefault("stage", "input")
        response.setdefault("execution_attempted", False)
        response["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 2)
        return response

    async def _generate(self, query, context, *, validate_sql, execute_sql=False, response=None):
        if isinstance(query, Mapping) and query.get("task") == "table_detail":
            return await self.detail_service._generate(query, context, validate_sql=validate_sql,
                                                      execute_sql=execute_sql, response=response)
        started = time.perf_counter()
        response = {} if response is None else response
        response.update({
            "status": "error", "route_to": None, "stage": "input",
            "generation_mode": "llm", "prompt_version": "chinook-sql-generation-v1" if self.config.get("dataset") == "chinook" else PROMPT_VERSION,
            "source_id": self.config["source_id"], "metric_definition_id": None,
            "candidate": None, "parameters": {}, "issues": [],
            "execution_attempted": False, "validation_status": "not_validated",
        })
        if validate_sql:
            response["execution_package"] = None
        if execute_sql:
            response.update(rows=[], columns=[], row_count=0, truncated=False, sql=None, tables=[])
        try:
            if not isinstance(query, Mapping) or (context is not None and not isinstance(context, Mapping)):
                raise ServiceFault("INVALID_QUERY", "query 和 context 必须为映射")
            query = copy.deepcopy(dict(query))
            context = {} if context is None else copy.deepcopy(dict(context))
            for key in SESSION_KEYS:
                if key in context:
                    if not isinstance(context[key], str):
                        raise ServiceFault("INVALID_QUERY", f"{key} 必须为字符串")
                    response[key] = context[key]
            response["metric_definition_id"] = query.get("metric_definition_id")
            if query.get("source_id") not in (None, "chinook_db"):
                raise ServiceFault("UNKNOWN_SOURCE", "项目仅支持 chinook_db 数据源")
            limits = {
                "max_rows": positive_limit(context.get("max_rows", 100),
                                           min(self.config["max_result_rows"], 1000), "max_rows"),
                "db_timeout_ms": positive_limit(context.get("db_timeout_ms", 5000),
                                                min(self.config["statement_timeout_ms"], 5000),
                                                "db_timeout_ms"),
                "llm_timeout_ms": positive_limit(context.get("llm_timeout_ms", 15000),
                                                 15000, "llm_timeout_ms"),
            }
            if execute_sql:
                limits.update(
                    execution_timeout_ms=positive_limit(context.get("execution_timeout_ms", 10000),
                                                        15000, "execution_timeout_ms"),
                    connect_timeout_ms=positive_limit(context.get("connect_timeout_ms", 3000),
                                                      5000, "connect_timeout_ms"),
                    lock_timeout_ms=positive_limit(context.get("lock_timeout_ms", 1000),
                                                   min(limits["db_timeout_ms"], 1000), "lock_timeout_ms"),
                )
            response["stage"] = "metadata"
            # 第一、二项保持已有入口；第三项默认用可取消的异步数据库驱动。
            async_reader = getattr(self.metadata_reader, "read_async", None)
            if execute_sql and callable(async_reader):
                metadata = await async_reader(limits)
            else:
                metadata = await asyncio.to_thread(self.metadata_reader.read, limits)
            if execute_sql and (metadata.get("mode") != "live" or metadata.get("permissions_verified") is not True):
                raise ServiceFault("LIVE_METADATA_REQUIRED", "只读执行必须先获得实时元数据及权限")
            try:
                snapshot = enrich_snapshot(metadata["snapshot"], metadata["data"], self.config)
                mode = metadata.get("mode", "provided")
                if mode == "live":
                    if metadata.get("permissions_verified") is not True:
                        raise ServiceFault("DATABASE_PERMISSION_DENIED", "实时元数据权限尚未验证")
                    snapshot["snapshot_metadata"].update(
                        semantic_source=metadata.get("semantic_source", "database"),
                        semantic_database_consistency_verified=metadata.get("semantic_source", "database") == "database",
                        schema_captured_at=metadata["captured_at"], schema_capture_precision="second",
                    )
                    snapshot["access_scope"].update(
                        live_permissions_verified=True, permissions_checked_at=metadata["captured_at"],
                    )
            except ServiceFault:
                raise
            except (KeyError, TypeError, ValueError) as error:
                raise ServiceFault("METADATA_ERROR", "结构或业务元数据不完整、不一致") from error
            response["metadata"] = {
                "mode": mode,
                "schema_fingerprint_sha256": snapshot["snapshot_metadata"]["schema_fingerprint_sha256"],
                "captured_at": snapshot["snapshot_metadata"]["schema_captured_at"],
                "live_permissions_verified": snapshot["access_scope"]["live_permissions_verified"],
            }
            response["stage"] = "mapping"
            decision = self.linking_service.match(query, snapshot, context, entities=metadata["data"])
            response["mapping_status"] = decision["status"]
            if decision["status"] == "needs_clarification":
                response.update(
                    status="needs_clarification", route_to="clarification",
                    clarification_request=json_value(decision["clarification_request"]),
                    issues=json_value(decision.get("issues", [])),
                )
            elif decision["status"] == "error":
                response.update(status="error", error_code=decision["error_code"],
                                error_message=decision["error_message"], issues=decision.get("issues", []))
            elif decision["status"] == "ready":
                request = decision["sql_generation_request"]
                prepared = prepare_generation_inputs(request, snapshot, limits)
                response.update(stage="generation", sql_generation_request=json_value(request),
                                metric_definition_id=request["metric_definition_id"])
                candidate = await self.generator.generate(
                    prepared.generation_request, prepared.schema_context,
                    prepared.parameter_specs, limits, context,
                )
                response.update(
                    status="generated", route_to="sql_validation", stage="generated",
                    candidate=candidate.as_dict(), parameters=json_value(prepared.parameters),
                    parameter_specs=[{
                        "name": item.name, "data_type": item.data_type,
                        "column": item.column, "operation": item.operation,
                    } for item in prepared.parameter_specs],
                )
                if validate_sql:
                    # 独立 database 模块复用可信请求和同一快照；不依赖 Agent Provider。
                    from database.sql_validator import SQLValidator

                    response["stage"] = "validation"
                    result = SQLValidator().validate(
                        candidate, prepared.generation_request, prepared.schema_context,
                        prepared.parameters, limits,
                    )
                    response["validation"] = {
                        "status": result.status, "passed": result.passed,
                        "error_code": result.error_code, "error_message": result.error_message,
                    }
                    if result.passed:
                        response.update(
                            status="validated", route_to="sql_execution", stage="validated",
                            validation_status="passed",
                            execution_package=result.validated_sql.as_dict(),
                        )
                        if execute_sql:
                            from database.readonly_sql_executor import ReadOnlySQLExecutor

                            response.update(stage="execution", sql=result.validated_sql.sql,
                                            tables=[request["table"]])
                            executor = self.executor if self.executor is not None else ReadOnlySQLExecutor(self.config)
                            execution = await executor.execute(
                                result.validated_sql, prepared.generation_request, prepared.schema_context,
                                limits=limits,
                                on_execute=lambda: response.update(execution_attempted=True),
                            )
                            response.update(
                                status="ok", route_to="answer", stage="complete",
                                execution_attempted=execution["execution_attempted"],
                                columns=execution["columns"], rows=execution["rows"],
                                row_count=execution["row_count"], truncated=execution["truncated"],
                                execution_metadata=execution["metadata"], execution_limits=execution["limits"],
                                execution_elapsed_ms=execution.get("elapsed_ms"),
                            )
                    else:
                        response.update(
                            status="error", route_to=None, validation_status="rejected",
                            error_code=result.error_code, error_message=result.error_message,
                        )
            else:
                raise ServiceFault("INTERNAL_ERROR", "映射模块返回了未知状态")
        except asyncio.CancelledError:
            # 上层取消不是普通模型失败；禁止在取消后继续生成或执行。
            raise
        except ServiceFault as error:
            response.update(status=error.status, error_code=error.code,
                            error_message=str(error), issues=error.issues, route_to=None,
                            execution_attempted=error.execution_attempted)
        except Exception as error:
            # 不把底层异常文本、连接配置或模型原文回传给用户。
            LOGGER.error("SQL generation unexpected failure: %s", type(error).__name__)
            response.update(status="error", error_code="INTERNAL_ERROR", error_message="SQL 生成服务内部错误")
            response["route_to"] = None
        response["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 2)
        return response
