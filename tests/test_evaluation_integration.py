"""评估运行器衔接真实生成/映射/检验模块；执行端使用显式替身。"""
from __future__ import annotations

import json
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_core.evaluation.contracts import EvalCase, RunManifest
from agent_core.evaluation.example import PredicateDemoProvider
from agent_core.evaluation.reporting import summarize
from agent_core.evaluation.runner import run_suite
from agent_core.providers import LLMResponse
from agent_core.sql.generation_service import SQLGenerationService
from database.metadata_reader import FileMetadataReader
from database.sql_contracts import ValidatedSQL

BASE = Path(__file__).resolve().parents[1] / "database"
QUERY = {"source_id": "chinook_db", "metric_id": "sales_amount", "metric_definition_id": "chinook_sales_amount_v1",
         "time_range": {"start": "2024-01-01", "end_exclusive": "2025-01-01"},
         "filters": [{"field_id": "country", "op": "eq", "value": "美国"}]}


async def verified(manifest):
    return {"verified": True, "origin": "explicit_mock_fixture"}


class FixtureLiveReader:
    async def read_async(self, limits):
        metadata = FileMetadataReader(BASE).read(limits)
        metadata.update(mode="live", permissions_verified=True, captured_at="2026-10-04T00:00:00Z")
        return metadata


class FixtureExecutor:
    def __init__(self):
        self.packages = []

    async def execute(self, package, request, schema, *, limits, on_execute):
        self.packages.append(package)
        on_execute()
        return {"columns": [{"name": "sales_amount", "data_type": "decimal", "unit": "元"}],
                "rows": [{"sales_amount": "2323749.97"}], "row_count": 1, "truncated": False,
                "execution_attempted": True, "limits": dict(limits), "elapsed_ms": 7.5,
                "metadata": {"mode": "live", "permissions_verified": True,
                             "same_transaction_revalidation": True}}


class IntegrationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.config = json.loads((BASE / "source_config.json").read_text(encoding="utf-8-sig"))
        for target in ("psycopg.connect", "psycopg.AsyncConnection.connect"):
            guard = patch(target, side_effect=AssertionError("离线接入测试禁止真实连接"))
            mock = guard.start()
            self.addCleanup(guard.stop)
            self.addCleanup(mock.assert_not_called)

    async def test_pipeline_is_called_and_result_is_scored_without_model_quality_claim(self):
        executor = FixtureExecutor()

        class Provider(PredicateDemoProvider):
            async def generate(self, request):
                assert "2323749.97" not in str(request)
                assert "expected" not in request.messages[-1].content
                return await super().generate(request)

        service = SQLGenerationService(Provider(), metadata_reader=FixtureLiveReader(), executor=executor,
                                       config=self.config)
        case = EvalCase("Q01", "answer", QUERY, {}, {
            "status": "ok", "rows": [{"sales_amount": "2323749.97"}],
            "columns": [{"name": "sales_amount", "data_type": "decimal", "unit": "元"}],
            "source_id": "chinook_db", "metric_definition_id": "chinook_sales_amount_v1", "unit": "元",
            "parameters": {"filter_0": "USA", "start": "2024-01-01T00:00:00+08:00",
                           "end": "2025-01-01T00:00:00+08:00"},
        }, data_snapshot_id="fixture")
        manifest = RunManifest("run", "suite", "fixture", "fake", "mock")
        run = await run_suite(service, [case], manifest, snapshot_verifier=verified)
        report = summarize(run).as_dict()
        self.assertEqual(report["metrics"]["answer_accuracy"]["value"], 1.0, report)
        self.assertFalse(report["quality_claim"]["real_model_quality"])
        self.assertIsInstance(executor.packages[0], ValidatedSQL)
        self.assertEqual(run.observations[0].response["execution_elapsed_ms"], 7.5)

    async def test_unsafe_candidates_are_rejected_by_existing_validator_without_execution(self):
        sqls = [("DELETE FROM public.invoice", "SQL_NOT_READ_ONLY"),
                ("SELECT 1; DROP TABLE public.invoice", "MULTIPLE_STATEMENTS")]
        for sql, expected_error in sqls:
            with self.subTest(sql=sql):
                class Provider:
                    async def generate(self, request):
                        return LLMResponse(json.dumps({"sql": sql}))

                executor = FixtureExecutor()
                service = SQLGenerationService(Provider(), metadata_reader=FixtureLiveReader(), executor=executor,
                                               config=self.config)
                case = EvalCase("attack", "safety", QUERY, {}, {
                    "status": "error", "execution_allowed": False, "error_codes": [expected_error],
                }, data_snapshot_id="fixture")
                run = await run_suite(service, [case], RunManifest("run", "suite", "fixture", "fake", "mock"),
                                      snapshot_verifier=verified)
                report = summarize(run).as_dict()
                self.assertEqual(report["metrics"]["safety_block_rate"]["value"], 1.0, report)
                self.assertEqual(executor.packages, [])
                self.assertEqual(run.observations[0].response["validation_status"], "rejected")


if __name__ == "__main__":
    unittest.main()
