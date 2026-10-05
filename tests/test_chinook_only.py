"""移除旧数据源后，公共入口必须在模型及数据库访问前拒绝旧请求。"""
import json
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock

from agent_core.sql.generation_service import SQLGenerationService
from database.metadata_reader import FileMetadataReader, LiveMetadataReader
from database.query_profiles import query_profile
from database.readonly_sql_executor import ReadOnlySQLExecutor
from database.sql_common import ServiceFault
from database.sql_service import SQLService

ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT / "database"


class ChinookOnlyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.config = json.loads((BASE / "source_config.json").read_text(encoding="utf-8-sig"))

    async def test_removed_source_never_reaches_metadata_model_or_executor(self):
        provider, metadata, executor = Mock(), Mock(), Mock()
        provider.generate = AsyncMock()
        metadata.read_async = AsyncMock()
        service = SQLGenerationService(provider, config=self.config, metadata_reader=metadata,
                                       executor=executor, detail_executor=executor)
        for entrypoint in (service.generate, service.generate_and_validate, service.query):
            for task in (None, "table_detail"):
                query = {"source_id": "sales_db", "metric_id": "sales_amount", "table": "business.orders"}
                if task:
                    query["task"] = task
                result = await entrypoint(query)
                self.assertEqual(result["error_code"], "UNKNOWN_SOURCE")
                self.assertFalse(result["execution_attempted"])
        metadata.read.assert_not_called()
        metadata.read_async.assert_not_called()
        provider.generate.assert_not_called()
        executor.assert_not_called()

    def test_removed_source_configuration_cannot_reactivate_legacy_query(self):
        source = {**self.config, "source_id": "sales_db", "dataset": "sales", "database": "electronic_cup"}
        for factory in (lambda: FileMetadataReader(BASE, source), lambda: LiveMetadataReader(source),
                        lambda: SQLService(config=source), lambda: ReadOnlySQLExecutor(source),
                        lambda: SQLGenerationService(Mock(generate=AsyncMock()), config=source)):
            with self.subTest(factory=factory):
                with self.assertRaises(ServiceFault):
                    factory()
        with self.assertRaises(ServiceFault) as caught:
            query_profile({"source_id": "sales_db"})
        self.assertEqual(caught.exception.code, "UNKNOWN_SOURCE")

    def test_obsolete_query_entrypoints_and_fixtures_are_deleted(self):
        for path in ("database/source_config.sales.json", "database/queries.json", "database/schema_examples.sql",
                     "database/verify_sql_pipeline_local.py", "database/verify_local_migration.py",
                     "evaluation/build_dataset.py", "evaluation/datasets/sql_v1.jsonl",
                     "src/agent_core/evaluation/oracle.py", "database/generated/cases.json"):
            self.assertFalse((ROOT / path).exists(), path)


if __name__ == "__main__":
    unittest.main()
