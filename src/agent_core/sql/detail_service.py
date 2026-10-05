"""Chinook detail mapping -> LLM -> independent validation -> read-only execution."""
from __future__ import annotations

import asyncio
import copy
import json
import logging
import time
from collections.abc import Mapping
from pathlib import Path

from database.detail_query import prepare_detail
from database.metadata_reader import LiveMetadataReader
from database.sql_common import ServiceFault, json_value, positive_limit

from .detail_generation import DetailLLMSQLGenerator, PROMPT_VERSION, SESSION_KEYS

LOGGER = logging.getLogger(__name__)


class DetailSQLService:
    def __init__(self, provider, *, base_dir=None, config=None, metadata_reader=None, executor=None,
                 llm_timeout_seconds=15.0):
        base = Path(base_dir) if base_dir is not None else Path(__file__).resolve().parents[3] / "database"
        source = config if config is not None else json.loads((base / "source_config.json").read_text(encoding="utf-8-sig"))
        self.config = copy.deepcopy(source)
        if (not isinstance(self.config, dict) or self.config.get("source_id") != "chinook_db"
                or self.config.get("dataset") != "chinook" or self.config.get("database") != "chinook"):
            raise ServiceFault("INVALID_CONFIGURATION", "明细服务仅支持 Chinook 配置")
        self.config["_base_dir"] = str(base.resolve())
        self.metadata_reader = metadata_reader if metadata_reader is not None else LiveMetadataReader(self.config)
        self.generator = DetailLLMSQLGenerator(provider, timeout_seconds=llm_timeout_seconds)
        self.executor = executor

    async def generate(self, query, context=None):
        return await self._generate(query, context, validate_sql=False)

    async def generate_and_validate(self, query, context=None):
        return await self._generate(query, context, validate_sql=True)

    async def query(self, query, context=None):
        started, response = time.perf_counter(), {}
        try:
            if context is not None and not isinstance(context, Mapping):
                raise ServiceFault("INVALID_QUERY", "context 必须为映射")
            timeout = positive_limit((context or {}).get("request_timeout_ms", 30000), 60000, "request_timeout_ms")
            async with asyncio.timeout(timeout / 1000):
                return await self._generate(query, context, validate_sql=True, execute_sql=True, response=response)
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            response.update(status="error", route_to=None, error_code="REQUEST_TIMEOUT",
                            error_message="明细请求超过总时间预算", rows=[], columns=[], row_count=0,
                            has_more=False, next_offset=None)
        except ServiceFault as error:
            response.update(status="error", route_to=None, error_code=error.code, error_message=str(error),
                            rows=[], columns=[], row_count=0, has_more=False, next_offset=None)
        response.setdefault("task", "table_detail")
        response.setdefault("stage", "input")
        response.setdefault("execution_attempted", False)
        response["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 2)
        return response

    async def _generate(self, query, context, *, validate_sql, execute_sql=False, response=None):
        started = time.perf_counter()
        response = {} if response is None else response
        response.update(status="error", route_to=None, stage="input", task="table_detail", source_id="chinook_db",
                        generation_mode="llm", prompt_version=PROMPT_VERSION, candidate=None, parameters={},
                        issues=[], execution_attempted=False, validation_status="not_validated")
        if validate_sql:
            response["execution_package"] = None
        if execute_sql:
            response.update(rows=[], columns=[], row_count=0, table=None, tables=[], sql=None,
                            page=None, has_more=False, next_offset=None)
        try:
            if not isinstance(query, Mapping) or (context is not None and not isinstance(context, Mapping)):
                raise ServiceFault("INVALID_QUERY", "query 和 context 必须为映射")
            query, context = copy.deepcopy(dict(query)), {} if context is None else copy.deepcopy(dict(context))
            for key in SESSION_KEYS:
                if key in context:
                    if not isinstance(context[key], str):
                        raise ServiceFault("INVALID_QUERY", f"{key} 必须为字符串")
                    response[key] = context[key]
            if query.get("source_id", "chinook_db") != "chinook_db":
                raise ServiceFault("UNKNOWN_SOURCE", "明细数据源必须为 chinook_db")
            if query.get("task") != "table_detail":
                raise ServiceFault("UNSUPPORTED_QUERY", "明细请求需明确 task=table_detail")
            limits = {
                "max_rows": positive_limit(context.get("max_rows", 1000), min(self.config["max_result_rows"], 1000), "max_rows"),
                "db_timeout_ms": positive_limit(context.get("db_timeout_ms", 5000), min(self.config["statement_timeout_ms"], 5000), "db_timeout_ms"),
                "llm_timeout_ms": positive_limit(context.get("llm_timeout_ms", 15000), 15000, "llm_timeout_ms"),
            }
            if execute_sql:
                limits.update(
                    execution_timeout_ms=positive_limit(context.get("execution_timeout_ms", 10000), 15000, "execution_timeout_ms"),
                    connect_timeout_ms=positive_limit(context.get("connect_timeout_ms", 3000), 5000, "connect_timeout_ms"),
                    lock_timeout_ms=positive_limit(context.get("lock_timeout_ms", 1000), min(limits["db_timeout_ms"], 1000), "lock_timeout_ms"),
                )
            response["stage"] = "metadata"
            async_read = getattr(self.metadata_reader, "read_async", None)
            metadata = (await async_read(limits) if execute_sql and callable(async_read)
                        else await asyncio.to_thread(self.metadata_reader.read, limits))
            if not isinstance(metadata, dict) or not isinstance(metadata.get("snapshot"), dict):
                raise ServiceFault("METADATA_ERROR", "明细结构元数据不完整")
            if execute_sql and (metadata.get("mode") != "live" or metadata.get("permissions_verified") is not True):
                raise ServiceFault("LIVE_METADATA_REQUIRED", "明细执行需要实时结构与权限")
            response["stage"] = "mapping"
            prepared = prepare_detail(query, metadata["snapshot"], self.config, limits)
            request, schema = prepared.generation_request, prepared.schema_context
            response.update(mapping_status="ready", table=request["table"], page=copy.deepcopy(request["page"]),
                            sql_generation_request=json_value(request),
                            metadata={"mode": metadata.get("mode", "provided"),
                                      "schema_fingerprint_sha256": schema["schema_fingerprint_sha256"],
                                      "captured_at": metadata.get("captured_at", metadata["snapshot"].get("captured_at")),
                                      "live_permissions_verified": metadata.get("permissions_verified") is True})
            response["stage"] = "generation"
            candidate = await self.generator.generate(request, schema, prepared.parameter_specs, limits, context)
            response.update(status="generated", route_to="sql_validation", stage="generated",
                            candidate=candidate.as_dict(), parameters=json_value(prepared.parameters),
                            parameter_specs=[{"name": spec.name, "data_type": spec.data_type,
                                              "column": spec.column, "operation": spec.operation}
                                             for spec in prepared.parameter_specs])
            if validate_sql:
                from database.detail_validator import DetailSQLValidator

                response["stage"] = "validation"
                result = DetailSQLValidator().validate(candidate, request, schema, prepared.parameters, limits)
                response["validation"] = {"status": result.status, "passed": result.passed,
                                          "error_code": result.error_code, "error_message": result.error_message}
                if not result.passed:
                    response.update(status="error", route_to=None, validation_status="rejected",
                                    error_code=result.error_code, error_message=result.error_message)
                else:
                    response.update(status="validated", route_to="sql_execution", stage="validated",
                                    validation_status="passed", execution_package=result.validated_sql.as_dict())
                    if execute_sql:
                        from database.detail_executor import DetailReadOnlyExecutor

                        response.update(stage="execution", sql=result.validated_sql.sql, tables=[request["table"]])
                        executor = self.executor if self.executor is not None else DetailReadOnlyExecutor(self.config)
                        execution = await executor.execute(result.validated_sql, request, schema, limits=limits,
                                                           on_execute=lambda: response.update(execution_attempted=True))
                        response.update(status="ok", route_to="answer", stage="complete",
                                        execution_attempted=execution["execution_attempted"], columns=execution["columns"],
                                        rows=execution["rows"], row_count=execution["row_count"], page=execution["page"],
                                        has_more=execution["has_more"], next_offset=execution["next_offset"],
                                        truncated=execution.get("truncated", execution["has_more"]),
                                        execution_metadata=execution["metadata"], execution_limits=execution["limits"],
                                        execution_elapsed_ms=execution.get("elapsed_ms"))
        except asyncio.CancelledError:
            raise
        except ServiceFault as error:
            response.update(status=error.status, route_to="clarification" if error.status == "needs_clarification" else None,
                            error_code=error.code, error_message=str(error), issues=json_value(error.issues),
                            execution_attempted=response.get("execution_attempted") is True or error.execution_attempted)
            if error.status == "needs_clarification":
                response.update(mapping_status="needs_clarification", clarification_request={
                    "query": json_value(query), "context": json_value(context), "issues": json_value(error.issues)})
        except Exception as error:
            LOGGER.error("Detail SQL unexpected failure: %s", type(error).__name__)
            response.update(status="error", route_to=None, error_code="INTERNAL_ERROR", error_message="明细查询内部错误")
        if execute_sql and response["status"] != "ok":
            response.update(rows=[], columns=[], row_count=0, has_more=False, next_offset=None)
        response["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 2)
        return response
