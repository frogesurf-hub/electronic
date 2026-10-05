import asyncio
import json
from copy import deepcopy
from datetime import datetime
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest

from agent_core.core.business_flow import BusinessFlow
from agent_core.core.message import Message
from agent_core.providers import LLMResponse, ProviderAuthenticationError
from agent_core.tests.fakes import FixedClock, create_test_agent
from agent_core.understanding.service import UnderstandingService

SESSION = str(uuid4())
NOW = datetime(2026, 10, 5, 16, tzinfo=ZoneInfo("Asia/Shanghai"))
READY = {
    "problem_type": "sql",
    "metric_id": "sales_amount",
    "time_range": {"start": "2024-01-01", "end_exclusive": "2025-01-01"},
    "filters": [{"field_id": "country", "op": "eq", "value": "美国"}],
}
OK = {
    "status": "ok",
    "rows": [{"sales_amount": "127.98"}],
    "columns": [{"name": "sales_amount", "data_type": "decimal", "unit": "金额"}],
    "sql": "SELECT SUM(total) FROM public.invoice WHERE billing_country = %(filter_0)s",
    "source_id": "chinook_db",
    "metric_definition_id": "chinook_sales_amount_v1",
    "parameters": {"filter_0": "USA"},
    "tables": ["public.invoice"],
    "execution_attempted": True,
    "stage": "complete",
    "truncated": False,
    "row_count": 1,
}


class SequenceProvider:
    def __init__(self, *outputs):
        self.outputs = list(outputs)
        self.requests = []

    async def generate(self, request):
        self.requests.append(request)
        item = self.outputs.pop(0)
        if isinstance(item, Exception):
            raise item
        return LLMResponse(json.dumps(item) if isinstance(item, dict) else item)


class FakeSQL:
    def __init__(self, result=None):
        self.calls = []
        self.result = deepcopy(OK) if result is None else result

    async def query(self, query, context):
        self.calls.append((query, context))
        if isinstance(self.result, Exception):
            raise self.result
        return deepcopy(self.result)


def request(text="2024 年美国销售额是多少？", *, session=SESSION, task=None):
    payload = {"message": text, "session_id": session}
    if task is not None:
        payload["task_id"] = task
    return Message(type="chat", source="desktop", payload=payload)


def flow_for(*outputs, sql=None):
    provider = SequenceProvider(*outputs)
    service = FakeSQL() if sql is None else sql
    flow = BusinessFlow(
        UnderstandingService(provider),
        service,
        FixedClock(NOW),
    )
    return flow, provider, service


def test_ready_query_calls_existing_service_and_preserves_actual_result():
    flow, provider, sql = flow_for(READY)
    message = request()
    result = asyncio.run(flow.process_message(message))
    assert result.payload["status"] == "ok"
    assert "127.98" in result.payload["message"]
    assert "币种" in result.payload["message"]
    assert result.payload["rows"] == OK["rows"]
    assert result.payload["sql"] == OK["sql"]
    assert result.payload["request_id"] == message.id
    query, context = sql.calls[0]
    assert query["time_range"] == READY["time_range"]
    assert query["metric_definition_id"] == "chinook_sales_amount_v1"
    assert context["request_id"] == message.id
    assert context["session_id"] == SESSION
    assert context["task_id"] == result.payload["task_id"]
    assert [step["service"] for step in result.payload["trace"]] == [
        "understanding",
        "sql",
    ]
    assert result.payload["trace"][-1]["execution_attempted"] is True
    extracted = json.loads(provider.requests[0].messages[-1].content)
    assert extracted["current_datetime"] == NOW.isoformat()
    assert "SQL" not in query


def test_clarification_resumes_task_with_history_and_no_premature_sql():
    missing = {**READY, "time_range": {"start": None, "end_exclusive": None}}
    supplemented = {**READY, "continues_task": True}
    flow, provider, sql = flow_for(missing, supplemented)

    async def scenario():
        first = await flow.process_message(request("美国销售额是多少？"))
        assert first.payload["status"] == "needs_clarification"
        assert not sql.calls
        second_request = request("2024 年", task=first.payload["task_id"])
        second = await flow.process_message(second_request)
        assert second.payload["task_id"] == first.payload["task_id"]
        assert second.payload["request_id"] == second_request.id
        assert second.payload["status"] == "ok"
        assert len(sql.calls) == 1
        context = json.loads(provider.requests[1].messages[-1].content)
        assert context["pending_clarification"] is True
        assert context["previous"]["filters"] == READY["filters"]
        assert context["history"][0]["content"] == "美国销售额是多少？"

    asyncio.run(scenario())


@pytest.mark.parametrize("text", ["2024", "2024年", " 2024 年 "])
def test_standalone_year_clarifies_intent_without_provider_or_sql(text):
    flow, provider, sql = flow_for()
    result = asyncio.run(flow.process_message(request(text)))
    assert result.type == "response"
    assert result.payload["status"] == "needs_clarification"
    assert result.payload["problem_type"] == "undetermined"
    assert result.payload["issues"][0]["code"] == "MISSING_INTENT"
    assert "指标" in result.payload["message"]
    assert not provider.requests and not sql.calls


def test_metric_reply_resumes_year_first_task_and_supplies_time_to_understanding():
    flow, provider, sql = flow_for({**READY, "continues_task": True})

    async def scenario():
        first = await flow.process_message(request("2024年"))
        second = await flow.process_message(
            request("美国销售额", task=first.payload["task_id"])
        )
        context = json.loads(provider.requests[0].messages[-1].content)
        assert context["pending_clarification"] is True
        assert context["previous"]["problem_type"] == "undetermined"
        assert context["previous"]["time_range"] == READY["time_range"]
        assert context["previous"]["metric_id"] is None
        assert context["previous"]["filters"] == []
        assert context["history"][0]["content"] == "2024年"
        assert second.payload["task_id"] == first.payload["task_id"]
        assert second.payload["status"] == "ok"
        assert sql.calls[0][0]["time_range"] == READY["time_range"]
        assert len(sql.calls) == 1

    asyncio.run(scenario())


def test_year_after_completed_query_does_not_inherit_metric_or_country():
    flow, provider, sql = flow_for(READY, {**READY, "continues_task": True})

    async def scenario():
        completed = await flow.process_message(request())
        first = await flow.process_message(request("2024年"))
        assert first.payload["status"] == "needs_clarification"
        assert first.payload["task_id"] != completed.payload["task_id"]
        assert len(sql.calls) == 1 and len(provider.requests) == 1
        await flow.process_message(request("美国销售额", task=first.payload["task_id"]))
        context = json.loads(provider.requests[-1].messages[-1].content)
        assert context["previous"]["metric_id"] is None
        assert context["previous"]["filters"] == []

    asyncio.run(scenario())


def test_complete_new_question_can_replace_year_first_clarification():
    fresh = {
        **READY,
        "continues_task": False,
        "time_range": {"start": "2023-01-01", "end_exclusive": "2024-01-01"},
        "filters": [],
    }
    flow, _, sql = flow_for(fresh)

    async def scenario():
        first = await flow.process_message(request("2024年"))
        second = await flow.process_message(
            request("改查 2023 年全球销售额", task=first.payload["task_id"])
        )
        assert second.payload["task_id"] != first.payload["task_id"]
        assert second.payload["status"] == "ok"
        assert sql.calls[0][0]["time_range"] == fresh["time_range"]
        assert sql.calls[0][0]["filters"] == []

    asyncio.run(scenario())


def test_model_can_mark_other_incomplete_fragments_as_undetermined():
    flow, _, sql = flow_for({"problem_type": "undetermined"})
    result = asyncio.run(flow.process_message(request("美国")))
    assert result.payload["status"] == "needs_clarification"
    assert [item["code"] for item in result.payload["issues"]] == ["MISSING_INTENT"]
    assert not sql.calls


def test_unrelated_request_still_returns_unsupported_instead_of_clarifying():
    flow, _, sql = flow_for({"problem_type": "unsupported"})
    result = asyncio.run(flow.process_message(request("写一首诗")))
    assert result.payload["status"] == "error"
    assert result.payload["code"] == "UNSUPPORTED_REQUEST"
    assert not sql.calls


@pytest.mark.parametrize(
    "changes",
    [
        {"time_range": {"start": "2025-01-01", "end_exclusive": "2024-01-01"}},
        {"metric_id": None},
        {"filters": [{"field_id": "country", "value": ""}]},
        {"filters": [{"field_id": "customer", "value": "张伟"}]},
        {"filters": [{"field_id": "country", "op": "in", "value": "USA"}]},
        {"issues": ["口径存在两种定义，请确认。"]},
    ],
)
def test_uncertain_or_conflicting_conditions_cannot_call_sql(changes):
    flow, _, sql = flow_for({**READY, **changes})
    result = asyncio.run(flow.process_message(request()))
    assert result.payload["status"] == "needs_clarification"
    assert result.payload["issues"]
    assert not sql.calls


@pytest.mark.parametrize(
    "changes",
    [
        {"unsupported_conditions": ["按国家排名"]},
        {"metric_id": "other"},
        {"problem_type": "qa"},
        {"problem_type": "fusion"},
        {"problem_type": "unsupported"},
    ],
)
def test_unsupported_or_unconnected_routes_do_not_silently_run_sql(changes):
    flow, _, sql = flow_for({**READY, **changes})
    result = asyncio.run(flow.process_message(request()))
    assert result.payload["status"] == "error"
    assert not sql.calls


@pytest.mark.parametrize(
    "output",
    [
        "not JSON",
        {**READY, "sql": "DELETE FROM invoice"},
        {**READY, "source_id": "other_db"},
        {**READY, "time_range": {"start": "bad-date", "end_exclusive": None}},
    ],
)
def test_invalid_model_output_fails_before_sql(output):
    flow, _, sql = flow_for(output)
    result = asyncio.run(flow.process_message(request()))
    assert result.payload["code"] == "INVALID_UNDERSTANDING"
    assert not sql.calls


def test_sessions_are_isolated_and_new_question_replaces_pending_task():
    missing = {**READY, "time_range": {}}
    fresh = {**READY, "filters": [], "continues_task": False}
    flow, provider, sql = flow_for(missing, READY, fresh)

    async def scenario():
        first = await flow.process_message(request("美国销售额"))
        other = await flow.process_message(request(session=str(uuid4())))
        context = json.loads(provider.requests[1].messages[-1].content)
        assert context["previous"] is None and context["history"] == []
        assert other.payload["task_id"] != first.payload["task_id"]
        new = await flow.process_message(request("2024 年全球销售额"))
        assert new.payload["task_id"] != first.payload["task_id"]
        assert sql.calls[-1][0]["filters"] == []

    asyncio.run(scenario())


def test_sql_clarification_is_returned_and_keeps_same_task():
    service = FakeSQL(
        {
            "status": "needs_clarification",
            "stage": "mapping",
            "issues": [{"code": "AMBIGUOUS_COUNTRY", "reason": "请确认账单国家。"}],
            "execution_attempted": False,
        }
    )
    flow, _, _ = flow_for(READY, {**READY, "continues_task": True}, sql=service)

    async def scenario():
        first = await flow.process_message(request())
        assert first.payload["status"] == "needs_clarification"
        assert "账单国家" in first.payload["message"]
        assert first.payload["trace"][-1]["execution_attempted"] is False
        service.result = OK
        second = await flow.process_message(
            request("美国", task=first.payload["task_id"])
        )
        assert second.payload["task_id"] == first.payload["task_id"]
        assert second.payload["status"] == "ok"

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "sql_result",
    [
        {**OK, "rows": [], "row_count": 0},
        {"status": "error", "error_code": "DB_TIMEOUT", "error_message": "查询超时。"},
        RuntimeError("secret-connection-details"),
    ],
)
def test_empty_results_and_safe_sql_errors(sql_result):
    flow, _, _ = flow_for(READY, sql=FakeSQL(sql_result))
    result = asyncio.run(flow.process_message(request()))
    expected = (
        "ok"
        if isinstance(sql_result, dict) and sql_result["status"] == "ok"
        else "error"
    )
    assert result.payload["status"] == expected
    assert "secret-connection-details" not in result.to_json()


def test_provider_error_does_not_expose_secret_or_run_sql():
    flow, _, sql = flow_for(ProviderAuthenticationError("secret-key"))
    result = asyncio.run(flow.process_message(request()))
    assert result.payload["code"] == "UNDERSTANDING_UNAVAILABLE"
    assert "secret-key" not in result.to_json()
    assert not sql.calls


def test_expired_task_is_rejected_before_provider_or_sql():
    flow, provider, sql = flow_for()
    result = asyncio.run(flow.process_message(request(task=str(uuid4()))))
    assert result.payload["code"] == "TASK_EXPIRED"
    assert not provider.requests and not sql.calls


def test_existing_agent_dispatches_chat_to_business_flow():
    flow, provider, sql = flow_for(READY)
    agent = create_test_agent(provider)
    agent._business_flow = flow
    result = asyncio.run(agent.process_message(request()))
    assert result.payload["status"] == "ok"
    assert len(sql.calls) == 1
    assert "Aria" not in provider.requests[0].messages[0].content
