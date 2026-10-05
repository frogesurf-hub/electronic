"""Offline detail Provider contract, prompt isolation and cancellation tests."""
from __future__ import annotations

import asyncio
import copy
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_core.providers import LLMResponse, ProviderTimeoutError
from agent_core.sql.detail_example import DetailDemoProvider
from agent_core.sql.detail_generation import DetailLLMSQLGenerator
from database.detail_query import prepare_detail
from database.metadata_reader import FileMetadataReader
from database.sql_common import ServiceFault
from database.sql_contracts import ParameterSpec

BASE = Path(__file__).resolve().parents[1] / "database"


class RecordingProvider:
    def __init__(self, content=None):
        self.content, self.requests = content, []

    async def generate(self, request):
        self.requests.append(request)
        if self.content is None:
            return await DetailDemoProvider().generate(request)
        return LLMResponse(self.content)


class DetailLLMGeneratorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        for name in ("database.sql_common.ReadOnlyClient.transaction", "database.readonly_connection.AsyncReadOnlyClient.transaction",
                     "database.sql_common.resolve_connection_parameters", "database.readonly_connection.resolve_connection_parameters"):
            guard = patch(name, side_effect=AssertionError("Offline generation must not access database/credentials"))
            mocked = guard.start()
            self.addCleanup(guard.stop)
            self.addCleanup(mocked.assert_not_called)
        self.config = json.loads((BASE / "source_config.json").read_text(encoding="utf-8-sig"))
        self.snapshot = FileMetadataReader(BASE).read({"max_rows": 1000, "db_timeout_ms": 5000})["snapshot"]
        self.query = {"source_id": "chinook_db", "task": "table_detail", "table": "public.customer",
                      "columns": ["customer_id", "company", "first_name"],
                      "filters": [{"column": "company", "op": "is_null"},
                                  {"column": "first_name", "op": "contains", "value": "secret_%\\value"}],
                      "order_by": [{"column": "first_name", "direction": "desc"}],
                      "page": {"size": 500, "offset": 987654}, "question": "PRIVATE_QUESTION"}
        self.prepared = prepare_detail(self.query, self.snapshot, self.config)

    async def generate(self, provider=None, *, prepared=None, limits=None, context=None, generator=None):
        prepared = self.prepared if prepared is None else prepared
        generator = DetailLLMSQLGenerator(provider or RecordingProvider()) if generator is None else generator
        return await generator.generate(prepared.generation_request, prepared.schema_context, prepared.parameter_specs,
                                        limits, context)

    async def test_candidate_is_unvalidated_and_prompt_has_no_values_facts_or_question(self):
        provider = RecordingProvider()
        before = copy.deepcopy(self.prepared)
        candidate = await self.generate(provider, context={"request_id": "trace", "PGPASSWORD": "NEVER_IN_PROMPT"})
        self.assertFalse(candidate.validated)
        self.assertEqual(candidate.task, "table_detail")
        self.assertEqual(candidate.request_id, "trace")
        self.assertEqual(set(candidate.parameter_names), {"filter_1", "fetch_limit", "page_offset"})
        payload = json.loads(provider.requests[0].messages[1].content)
        self.assertEqual(payload["query_spec"]["predicates"][0]["parameter"], None)
        self.assertEqual(payload["query_spec"]["predicates"][1]["parameter"], "filter_1")
        self.assertTrue(all("value" not in item for item in payload["parameters"]))
        self.assertTrue(all(item["nulls"] == "last" for item in payload["query_spec"]["order_by"]))
        self.assertEqual(payload["query_spec"]["order_by"][-1]["column"], "customer_id")
        text = "\n".join(message.content for message in provider.requests[0].messages)
        for forbidden in ("secret_%", "PRIVATE_QUESTION", "PGPASSWORD", "NEVER_IN_PROMPT", "987654", "independent_python_oracle"):
            self.assertNotIn(forbidden, text)
        self.assertEqual(self.prepared, before)

    async def test_mismatched_specs_are_rejected_before_provider(self):
        provider = RecordingProvider()
        prepared = self.prepared
        specs = (*prepared.parameter_specs[:-1], ParameterSpec("page_offset", "text", None, "offset"))
        with self.assertRaises(ServiceFault) as caught:
            await DetailLLMSQLGenerator(provider).generate(prepared.generation_request, prepared.schema_context, specs)
        self.assertEqual(caught.exception.code, "PARAMETER_SPEC_MISMATCH")
        self.assertEqual(provider.requests, [])

    async def test_schema_and_source_mismatch_never_reach_provider(self):
        for mutation in (lambda value: value.generation_request.update(source_id="sales_db"),
                         lambda value: value.schema_context.update(table="public.invoice"),
                         lambda value: value.schema_context.update(schema_fingerprint_sha256=None)):
            with self.subTest(mutation=mutation):
                prepared, provider = copy.deepcopy(self.prepared), RecordingProvider()
                mutation(prepared)
                with self.assertRaises(ServiceFault):
                    await self.generate(provider, prepared=prepared)
                self.assertEqual(provider.requests, [])

    async def test_strict_json_rejects_duplicates_markdown_extra_keys_types_and_nul(self):
        for content in ('{"sql":"SELECT 1","sql":"SELECT 2"}', '```json\n{"sql":"SELECT 1"}\n```',
                        '{"sql":"SELECT 1","validated":true}', '{"sql":null}', '{"sql":NaN}',
                        '[]', '{"sql":""}', '{"sql":"SELECT\\u0000"}', '{"sql":"\\ud800"}'):
            with self.subTest(content=content):
                with self.assertRaises(ServiceFault) as caught:
                    await self.generate(RecordingProvider(content))
                self.assertEqual(caught.exception.code, "LLM_RESPONSE_INVALID")

    async def test_response_and_sql_length_limits_are_enforced(self):
        for content, response_limit, sql_limit, code in (("x" * 41, 40, 30, "LLM_RESPONSE_TOO_LARGE"),
                                                       (json.dumps({"sql": "S" * 31}), 100, 30, "LLM_SQL_TOO_LARGE")):
            with self.subTest(code=code):
                generator = DetailLLMSQLGenerator(RecordingProvider(content), max_response_chars=response_limit, max_sql_chars=sql_limit)
                with self.assertRaises(ServiceFault) as caught:
                    await self.generate(generator=generator)
                self.assertEqual(caught.exception.code, code)

    async def test_local_timeout_is_bounded_and_provider_timeout_is_classified(self):
        class Slow:
            async def generate(self, request):
                await asyncio.sleep(1)
        class TimedOut:
            async def generate(self, request):
                raise ProviderTimeoutError("untrusted provider details")
        for provider in (Slow(), TimedOut()):
            with self.subTest(provider=provider):
                with self.assertRaises(ServiceFault) as caught:
                    await self.generate(provider, limits={"llm_timeout_ms": 5})
                self.assertEqual(caught.exception.code, "LLM_TIMEOUT")
                self.assertNotIn("untrusted", str(caught.exception))

    async def test_external_cancellation_propagates(self):
        class Cancelled:
            async def generate(self, request):
                raise asyncio.CancelledError
        with self.assertRaises(asyncio.CancelledError):
            await self.generate(Cancelled())

    async def test_provider_failures_do_not_echo_secrets(self):
        class Broken:
            async def generate(self, request):
                raise RuntimeError("PGPASSWORD=not-for-output")
        with self.assertRaises(ServiceFault) as caught:
            await self.generate(Broken())
        self.assertEqual(caught.exception.code, "LLM_PROVIDER_ERROR")
        self.assertNotIn("PGPASSWORD", str(caught.exception))

    async def test_invalid_budget_and_context_are_rejected_before_provider(self):
        for limits, context in (({"llm_timeout_ms": True}, None), ({"llm_timeout_ms": 0}, None), ({}, {"request_id": 1})):
            provider = RecordingProvider()
            with self.subTest(limits=limits, context=context):
                with self.assertRaises(ServiceFault):
                    await self.generate(provider, limits=limits, context=context)
                self.assertEqual(provider.requests, [])


if __name__ == "__main__":
    unittest.main()
