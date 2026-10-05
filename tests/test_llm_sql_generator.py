"""LLM 异步契约、输出边界和可信参数的离线测试。"""
from __future__ import annotations

import asyncio
import copy
import json
import unittest
from dataclasses import replace
from datetime import timedelta

from agent_core.providers import LLMResponse, ProviderTimeoutError
from agent_core.sql.llm_sql_generator import LLMSQLGenerator, prepare_generation_inputs
from database.sql_common import ServiceFault

SQL = (
    "SELECT COALESCE(SUM(o.total), 0) AS sales_amount FROM public.invoice o "
    "WHERE o.invoice_date >= :start AND o.invoice_date < :end "
    "AND o.billing_country = :filter_0 LIMIT :fetch_limit"
)


def request():
    return {
        "source_id": "chinook_db", "metric_definition_id": "chinook_sales_amount_v1",
        "table": "public.invoice", "grain": "invoice",
        "aggregation": {"operation": "sum", "column": "total", "alias": "sales_amount"},
        "filters": [
            {"column": "billing_country", "op": "eq", "value": "USA"},
        ],
        "time_filter": {
            "column": "invoice_date", "start": "2024-01-01", "end_exclusive": "2025-01-01",
            "timezone": "Asia/Shanghai",
        },
    }


def snapshot():
    return {
        "access_scope": {
            "source_id": "chinook_db", "allowed_operations": ["SELECT"],
            "scopes": {"business_query": {"configured_allowlist": ["public.invoice"]}},
        },
        "columns": [{
            "schema_name": "public", "table_name": "invoice",
            "column_name": name, "data_type": kind,
        } for name, kind in (
            ("total", "numeric(18,2)"), ("invoice_date", "timestamp without time zone"),
            ("billing_country", "text"), ("billing_city", "text"),
        )],
        "snapshot_metadata": {"schema_fingerprint_sha256": "a" * 64},
    }


class FakeProvider:
    def __init__(self, content=None, *, error=None):
        self.content = json.dumps({"sql": SQL}) if content is None else content
        self.error = error
        self.requests = []

    async def generate(self, value):
        self.requests.append(value)
        if self.error is not None:
            raise self.error
        return LLMResponse(self.content)


class WaitingProvider:
    def __init__(self):
        self.entered = asyncio.Event()
        self.cancelled = False

    async def generate(self, value):
        self.entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise


class GeneratorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.prepared = prepare_generation_inputs(request(), snapshot())

    async def invoke(self, provider, *, limits=None, context=None, **options):
        return await LLMSQLGenerator(provider, **options).generate(
            self.prepared.generation_request, self.prepared.schema_context,
            self.prepared.parameter_specs, limits, context,
        )

    async def test_provider_contract_and_candidate_provenance(self):
        provider = FakeProvider()
        candidate = await self.invoke(provider, context={"request_id": "r-1"})
        self.assertEqual(candidate.sql, SQL)
        self.assertFalse(candidate.validated)
        self.assertEqual(candidate.request_id, "r-1")
        self.assertEqual(candidate.schema_fingerprint_sha256, "a" * 64)
        self.assertEqual([m.role for m in provider.requests[0].messages], ["system", "user"])
        self.assertEqual(set(candidate.parameter_names), set(self.prepared.parameters))

    async def test_request_and_context_values_are_not_forwarded_to_model(self):
        provider = FakeProvider()
        schema = copy.deepcopy(self.prepared.schema_context)
        schema["password"] = "schema-secret"
        await LLMSQLGenerator(provider).generate(
            self.prepared.generation_request, schema, self.prepared.parameter_specs,
            context={"password": "context-secret", "request_id": "request-private"},
        )
        text = "\n".join(m.content for m in provider.requests[0].messages)
        for value in ("USA", "2024-01-01", "schema-secret", "context-secret", "request-private"):
            self.assertNotIn(value, text)
        data = json.loads(provider.requests[0].messages[1].content)
        self.assertIn({"name": "start", "data_type": "timestamp"}, data["parameters"])

    async def test_rejects_invalid_or_ambiguous_response_objects(self):
        invalid = (
            "", "not json", "```json\n{\"sql\":\"SELECT 1\"}\n```", "[]",
            '{"sql":"SELECT 1","sql":"SELECT 2"}',
            '{"sql":"SELECT 1","parameters":{"status":"unpaid"}}',
            '{"sql":"SELECT 1","validated":true}', '{"sql":null}',
            '{"sql":123}', '{"sql":"   "}', '{"sql":NaN}', '{"sql":"\\u0000"}',
        )
        for content in invalid:
            with self.subTest(content=content):
                with self.assertRaises(ServiceFault) as caught:
                    await self.invoke(FakeProvider(content))
                self.assertEqual(caught.exception.code, "LLM_RESPONSE_INVALID")

    async def test_rejects_non_text_provider_response(self):
        with self.assertRaises(ServiceFault) as caught:
            await self.invoke(FakeProvider(content=42))
        self.assertEqual(caught.exception.code, "LLM_RESPONSE_INVALID")

    async def test_bounds_raw_response_before_json_parsing(self):
        with self.assertRaises(ServiceFault) as caught:
            await self.invoke(FakeProvider("x" * 101), max_response_chars=100)
        self.assertEqual(caught.exception.code, "LLM_RESPONSE_TOO_LARGE")

    async def test_bounds_sql_text_separately(self):
        with self.assertRaises(ServiceFault) as caught:
            await self.invoke(FakeProvider(), max_sql_chars=20)
        self.assertEqual(caught.exception.code, "LLM_SQL_TOO_LARGE")

    async def test_timeout_cancels_provider(self):
        provider = WaitingProvider()
        with self.assertRaises(ServiceFault) as caught:
            await self.invoke(provider, limits={"llm_timeout_ms": 10})
        self.assertEqual(caught.exception.code, "LLM_TIMEOUT")
        self.assertTrue(provider.cancelled)

    async def test_caller_cancellation_is_not_converted_to_error(self):
        provider = WaitingProvider()
        task = asyncio.create_task(self.invoke(provider))
        await provider.entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(provider.cancelled)

    async def test_existing_provider_timeout_contract_is_preserved(self):
        with self.assertRaises(ServiceFault) as caught:
            await self.invoke(FakeProvider(error=ProviderTimeoutError("vendor details")))
        self.assertEqual(caught.exception.code, "LLM_TIMEOUT")
        self.assertNotIn("vendor details", str(caught.exception))

    async def test_provider_failure_does_not_leak_vendor_text(self):
        with self.assertRaises(ServiceFault) as caught:
            await self.invoke(FakeProvider(error=RuntimeError("secret-api-key")))
        self.assertEqual(caught.exception.code, "LLM_PROVIDER_ERROR")
        self.assertNotIn("secret-api-key", str(caught.exception))

    async def test_wrong_parameter_specs_rejected_before_model_call(self):
        provider = FakeProvider()
        specs = list(self.prepared.parameter_specs)
        specs[0] = replace(specs[0], column="bad_amount")
        with self.assertRaises(ServiceFault) as caught:
            await LLMSQLGenerator(provider).generate(
                self.prepared.generation_request, self.prepared.schema_context, specs,
            )
        self.assertEqual(caught.exception.code, "PARAMETER_SPEC_MISMATCH")
        self.assertEqual(provider.requests, [])

    async def test_wrong_schema_rejected_before_model_call(self):
        provider = FakeProvider()
        schema = copy.deepcopy(self.prepared.schema_context)
        schema["columns"] = [c for c in schema["columns"] if c["name"] != "invoice_date"]
        with self.assertRaises(ServiceFault) as caught:
            await LLMSQLGenerator(provider).generate(
                self.prepared.generation_request, schema, self.prepared.parameter_specs,
            )
        self.assertEqual(caught.exception.code, "METADATA_ERROR")
        self.assertEqual(provider.requests, [])

    async def test_invalid_limits_and_identifiers_prevent_model_call(self):
        provider = FakeProvider()
        for limits, context in (({"llm_timeout_ms": 0}, {}), ({}, {"request_id": 123})):
            with self.subTest(limits=limits, context=context):
                with self.assertRaises(ServiceFault):
                    await self.invoke(provider, limits=limits, context=context)
        self.assertEqual(provider.requests, [])

    def test_server_parameters_are_typed_and_row_limit_is_capped(self):
        prepared = prepare_generation_inputs(request(), snapshot(), {"max_rows": 5000})
        self.assertEqual(prepared.parameters["fetch_limit"], 1001)
        self.assertIsNone(prepared.parameters["start"].tzinfo)
        self.assertEqual(prepared.parameters["filter_0"], "USA")

    def test_array_filter_remains_typed_and_not_mutated(self):
        original = request()
        original["filters"][0] = {"column": "billing_country", "op": "in", "value": ["USA", "Canada"]}
        before = copy.deepcopy(original)
        prepared = prepare_generation_inputs(original, snapshot())
        self.assertEqual(prepared.parameters["filter_0"], ["USA", "Canada"])
        spec = next(s for s in prepared.parameter_specs if s.name == "filter_0")
        self.assertEqual(spec.data_type, "text[]")
        self.assertEqual(original, before)

    def test_invalid_request_shape_has_stable_error(self):
        value = request()
        value["time_filter"] = "invalid"
        with self.assertRaises(ServiceFault) as caught:
            prepare_generation_inputs(value, snapshot())
        self.assertEqual(caught.exception.code, "INVALID_GENERATION_REQUEST")


if __name__ == "__main__":
    unittest.main()
