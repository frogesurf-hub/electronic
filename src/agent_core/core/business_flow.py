"""统一业务编排：理解、澄清、支线调用与结果组装。"""

import logging
from time import perf_counter
from typing import Any, Protocol
from uuid import UUID, uuid4

from pydantic import ValidationError

from agent_core.core.message import Message
from agent_core.providers import LLMProviderError
from agent_core.retrieval.qa_service import DocumentQAService
from agent_core.sessions.store import SessionState, SessionStore
from agent_core.temporal import Clock
from agent_core.understanding.service import UnderstandingService

logger = logging.getLogger(__name__)


class QueryService(Protocol):
    async def query(
        self, query: dict[str, object], context: dict[str, object]
    ) -> dict[str, Any]: ...


class BusinessFlow:
    def __init__(
        self,
        understanding: UnderstandingService,
        sql_service: QueryService,
        clock: Clock,
        *,
        sessions: SessionStore | None = None,
        qa_service: DocumentQAService | None = None,
    ) -> None:
        self._understanding = understanding
        self._sql = sql_service
        self._clock = clock
        self._sessions = sessions if sessions is not None else SessionStore()
        self._qa = qa_service

    async def process_message(self, message: Message) -> Message:
        payload: dict[str, Any] = {
            "message": "",
            "status": "error",
            "problem_type": None,
            "session_id": None,
            "task_id": None,
            "request_id": message.id,
            "issues": [],
            "citations": [],
            "trace": [],
        }
        text = message.payload.get("message")
        if not isinstance(text, str) or not text.strip() or len(text) > 4000:
            return self._error(
                payload, "INVALID_INPUT", "请输入 1 至 4000 字符的问题。"
            )
        raw_session = message.payload.get("session_id")
        try:
            session_id = (
                str(UUID(raw_session)) if raw_session is not None else str(uuid4())
            )
        except (ValueError, TypeError, AttributeError):
            return self._error(payload, "INVALID_SESSION", "会话编号无效，请新建会话。")
        payload["session_id"] = session_id
        try:
            state = self._sessions.get(session_id)
        except RuntimeError:
            return self._error(payload, "SESSION_BUSY", "当前会话较多，请稍后重试。")
        async with state.lock:
            requested_task = message.payload.get("task_id")
            if requested_task is not None and (
                not isinstance(requested_task, str)
                or not state.pending
                or requested_task != state.task_id
            ):
                return self._error(
                    payload,
                    "TASK_EXPIRED",
                    "待补充任务已结束或过期，请重新发送完整问题。",
                )
            payload["task_id"] = state.task_id if state.pending else str(uuid4())
            started = perf_counter()
            try:
                interpretation = await self._understanding.extract(
                    text.strip(),
                    now=self._clock.now(),
                    history=list(state.history),
                    previous=state.previous,
                    pending=state.pending,
                )
            except (ValidationError, ValueError):
                payload["trace"].append(
                    self._trace(message.id, "understanding", "error", started)
                )
                return self._error(
                    payload,
                    "INVALID_UNDERSTANDING",
                    "模型未返回有效的业务条件，请重新表述问题；尚未调用 SQL 服务。",
                )
            except (LLMProviderError, TimeoutError):
                payload["trace"].append(
                    self._trace(message.id, "understanding", "error", started)
                )
                return self._error(
                    payload,
                    "UNDERSTANDING_UNAVAILABLE",
                    "理解服务暂时不可用，请稍后重试；尚未调用 SQL 服务。",
                )
            payload["trace"].append(
                self._trace(message.id, "understanding", "ok", started)
            )
            if state.pending and not interpretation.continues_task:
                payload["task_id"] = str(uuid4())
            state.task_id = payload["task_id"]
            state.previous = interpretation
            state.pending = False
            payload["problem_type"] = interpretation.problem_type

            if interpretation.problem_type == "undetermined":
                state.pending = True
                reason = (
                    "你想查询什么指标（如销售额），还是查阅相关文档？请补充具体问题。"
                )
                payload.update(
                    status="needs_clarification",
                    issues=[{"code": "MISSING_INTENT", "reason": reason}],
                    message=reason,
                )
                result = self._response(payload)
            elif interpretation.problem_type == "qa" and self._qa is not None:
                if interpretation.issues:
                    state.pending = True
                    payload.update(
                        status="needs_clarification",
                        issues=[
                            {"code": "AMBIGUOUS_REQUIREMENT", "reason": x}
                            for x in interpretation.issues
                        ],
                        message=" ".join(interpretation.issues),
                    )
                    result = self._response(payload)
                else:
                    qa = await self._qa.answer(
                        text.strip(),
                        query=interpretation.qa_query,
                        doc_ids=interpretation.doc_ids,
                        version=interpretation.doc_version,
                        as_of=interpretation.doc_as_of,
                        request_id=message.id,
                    )
                    payload["trace"].extend(qa.pop("trace"))
                    payload.update(qa)
                    payload["document_scope"] = {
                        "query": interpretation.qa_query or text.strip(),
                        "doc_ids": interpretation.doc_ids,
                        "version": interpretation.doc_version,
                        "as_of": (
                            interpretation.doc_as_of.isoformat()
                            if interpretation.doc_as_of
                            else None
                        ),
                    }
                    result = (
                        self._response(payload)
                        if payload["status"] != "error"
                        else self._error(payload, payload["code"], payload["message"])
                    )
            elif interpretation.problem_type in {"qa", "fusion"}:
                target = (
                    "文档问答" if interpretation.problem_type == "qa" else "跨源融合"
                )
                result = self._error(
                    payload,
                    "SERVICE_NOT_READY",
                    f"已识别为{target}需求，该服务尚未接入。",
                )
            elif interpretation.problem_type == "unsupported":
                result = self._error(
                    payload,
                    "UNSUPPORTED_REQUEST",
                    "当前支持发票销售额查询和根据演示文档回答问题。",
                )
            elif (
                interpretation.unsupported_conditions
                or interpretation.metric_id == "other"
            ):
                payload["issues"] = [
                    {"code": "UNSUPPORTED_CONDITION", "reason": x}
                    for x in interpretation.unsupported_conditions
                ]
                result = self._error(
                    payload,
                    "UNSUPPORTED_QUERY",
                    "当前仅支持发票销售额汇总及国家、城市、客户编号筛选；"
                    "此问题含尚未支持的指标或查询条件，未调用 SQL 服务。",
                )
            elif issues := interpretation.clarification_issues():
                state.pending = True
                payload.update(
                    status="needs_clarification",
                    issues=issues,
                    message=" ".join(item["reason"] for item in issues),
                )
                result = self._response(payload)
            else:
                query = interpretation.confirmed_query()
                payload["confirmed_query"] = query
                context: dict[str, object] = {
                    "session_id": session_id,
                    "task_id": state.task_id,
                    "request_id": message.id,
                    "step_id": f"{message.id}:sql",
                }
                started = perf_counter()
                try:
                    sql_result = await self._sql.query(query, context)
                except Exception as error:
                    logger.error("SQL service failed: %s", type(error).__name__)
                    payload["trace"].append(
                        self._trace(message.id, "sql", "error", started)
                    )
                    result = self._error(
                        payload,
                        "SQL_SERVICE_FAILED",
                        "查询服务暂时不可用，请稍后重试。",
                    )
                else:
                    payload["trace"].append(
                        {
                            **self._trace(
                                message.id, "sql", sql_result["status"], started
                            ),
                            "stage": sql_result.get("stage"),
                            "execution_attempted": sql_result.get(
                                "execution_attempted", False
                            ),
                        }
                    )
                    result = self._assemble_sql(payload, sql_result, state)
            state.remember(text.strip(), result.payload["message"])
            return result

    @staticmethod
    def _trace(
        request_id: str, service: str, status: str, started: float
    ) -> dict[str, object]:
        return {
            "step_id": f"{request_id}:{service}",
            "service": service,
            "status": status,
            "elapsed_ms": round((perf_counter() - started) * 1000, 2),
        }

    def _assemble_sql(
        self, payload: dict[str, Any], result: dict[str, Any], state: SessionState
    ) -> Message:
        status = result["status"]
        payload["issues"] = result.get("issues", [])
        if status == "needs_clarification":
            state.pending = True
            reasons = [
                str(item.get("reason", "请补充查询条件。"))
                for item in payload["issues"]
            ]
            payload.update(
                status=status, message=" ".join(reasons) or "请进一步明确查询条件。"
            )
            return self._response(payload)
        if status != "ok":
            return self._error(
                payload,
                result.get("error_code", "SQL_ERROR"),
                result.get("error_message")
                or "查询未能完成，请检查查询条件或数据库状态。",
            )
        for key in (
            "source_id",
            "metric_definition_id",
            "columns",
            "rows",
            "sql",
            "parameters",
            "tables",
            "truncated",
            "row_count",
            "execution_attempted",
        ):
            payload[key] = result.get(key)
        rows = result.get("rows", [])
        if rows:
            amount = rows[0].get("sales_amount")
            payload["message"] = (
                f"发票销售额为 {amount}（金额，数据源未指定币种）。"
                "\n口径：Chinook 发票 total 汇总，不推断退款或利润。"
            )
        else:
            payload["message"] = "查询成功，当前条件下没有记录。"
        payload["status"] = "ok"
        return self._response(payload)

    @staticmethod
    def _response(payload: dict[str, Any]) -> Message:
        return Message(type="response", source="Query Agent", payload=payload)

    @staticmethod
    def _error(payload: dict[str, Any], code: str, text: str) -> Message:
        payload.update(status="error", code=code, message=text)
        return Message(type="error", source="Query Agent", payload=payload)
