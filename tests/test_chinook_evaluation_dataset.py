"""Chinook frozen gold, loader boundaries and prompt separation; no DB/model calls."""
from __future__ import annotations

import copy
import hashlib
import json
import tempfile
import unittest
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from agent_core.evaluation.chinook_dataset import load_chinook_cases
from agent_core.evaluation.chinook_oracle import (
    CHINOOK_ORACLE_VERSION, CHINOOK_PROJECTION_VERSION,
    canonical_chinook_snapshot_hash, chinook_money_cents, chinook_semantic_metadata_hash,
    chinook_time, evaluate_chinook_sales, parse_chinook_sql,
)
from agent_core.evaluation.contracts import EvalCase, EvaluationError, RunConfig, RunManifest
from agent_core.evaluation.runner import EvaluationRunner
from agent_core.evaluation.dataset import _artifact

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "database" / "Chinook_PostgreSql.sql"
CASES = ROOT / "evaluation" / "datasets" / "chinook_sql_v1.jsonl"
MANIFEST = CASES.with_name("chinook_manifest.json")


def query(*, filters=None, start="2021-01-01", end="2026-01-01"):
    return {"source_id": "chinook_db", "metric_id": "sales_amount",
            "metric_definition_id": "chinook_sales_amount_v1",
            "time_range": {"start": start, "end_exclusive": end},
            "filters": [] if filters is None else filters}


class OfflineGuards:
    def setUp(self):
        super().setUp()
        for name in ("psycopg.connect", "psycopg.AsyncConnection.connect",
                     "database.sql_common.resolve_connection_parameters",
                     "database.readonly_connection.resolve_connection_parameters"):
            guard = patch(name, side_effect=AssertionError("Evaluation tests forbid connections and credentials"))
            mocked = guard.start()
            self.addCleanup(guard.stop)
            self.addCleanup(mocked.assert_not_called)


class ChinookOracleTests(OfflineGuards, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = parse_chinook_sql(SOURCE)

    def test_known_invoice_totals_and_exact_integer_customer(self):
        requests = (
            (query(), "2328.60"),
            (query(start="2024-01-01", end="2025-01-01"), "477.53"),
            (query(start="2024-01-01", end="2025-01-01", filters=[
                {"field_id": "country", "op": "eq", "value": "美国"}]), "127.98"),
            (query(filters=[{"field_id": "customer", "op": "eq", "value": 1}]), "39.62"),
        )
        for request, amount in requests:
            with self.subTest(amount=amount):
                gold = evaluate_chinook_sales(request, self.data)
                self.assertEqual(gold["rows"], [{"sales_amount": amount}])
                self.assertEqual(gold["unit"], "金额")
                self.assertIsNone(datetime.fromisoformat(gold["parameters"]["start"]).tzinfo)
        gold = evaluate_chinook_sales(requests[-1][0], self.data)
        self.assertIs(type(gold["parameters"]["filter_0"]), int)

    def test_half_open_calendar_boundaries_and_zero_are_exact(self):
        first = evaluate_chinook_sales(query(start="2021-01-01", end="2021-01-02"), self.data)
        self.assertEqual(first["rows"], [{"sales_amount": "1.98"}])
        empty = evaluate_chinook_sales(query(start="2030-01-01", end="2031-01-01"), self.data)
        self.assertEqual(empty["rows"], [{"sales_amount": "0.00"}])

    def test_customer_in_preserves_integer_ids_and_deduplicates(self):
        gold = evaluate_chinook_sales(query(filters=[
            {"field_id": "customer", "op": "in", "value": [1, "2", 1]}]), self.data)
        self.assertEqual(gold["parameters"]["filter_0"], [1, 2])
        self.assertTrue(all(type(value) is int for value in gold["parameters"]["filter_0"]))
        for invalid in (True, 1.0, -1, {}, [1]):
            with self.subTest(invalid=invalid), self.assertRaises(EvaluationError):
                evaluate_chinook_sales(query(filters=[
                    {"field_id": "customer", "op": "eq", "value": invalid}]), self.data)

    def test_calendar_times_reject_offsets_instead_of_converting(self):
        for value in ("2024-01-01T00:00:00Z", "2024-01-01T00:00:00+08:00",
                      datetime(2024, 1, 1, tzinfo=timezone.utc)):
            with self.subTest(value=value), self.assertRaises(EvaluationError) as caught:
                chinook_time(value)
            self.assertEqual(caught.exception.code, "AMBIGUOUS_TIME_SEMANTICS")

    def test_snapshot_is_stable_across_order_and_exact_postgres_transports(self):
        transported = copy.deepcopy(self.data)
        for rows in transported.values():
            rows.reverse()
        for invoice in transported["invoices"]:
            invoice["total"] = Decimal(invoice["total"])
            invoice["invoice_date"] = datetime.fromisoformat(invoice["invoice_date"])
        self.assertEqual(canonical_chinook_snapshot_hash(transported), canonical_chinook_snapshot_hash(self.data))
        self.assertEqual(chinook_semantic_metadata_hash(transported), chinook_semantic_metadata_hash(self.data))
        transported["invoices"][0]["total"] += Decimal("0.01")
        self.assertNotEqual(canonical_chinook_snapshot_hash(transported), canonical_chinook_snapshot_hash(self.data))

    def test_snapshot_rejects_bool_float_string_ids_and_fractional_cents(self):
        for value in (True, 1.0, "1"):
            modified = copy.deepcopy(self.data)
            modified["invoices"][0]["customer_id"] = value
            with self.subTest(value=value), self.assertRaises(EvaluationError):
                canonical_chinook_snapshot_hash(modified)
        for value in (True, 0.1, "0.001", "NaN", "Infinity"):
            with self.subTest(value=value), self.assertRaises(EvaluationError):
                chinook_money_cents(value)

    def test_source_sql_expressions_are_never_executed_as_seed_literals(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "nonliteral.sql"
            source = SOURCE.read_text(encoding="utf-8-sig")
            modified = source.replace(", 1.98)", ", pg_sleep(5))", 1)
            self.assertNotEqual(source, modified)
            path.write_text(modified, encoding="utf-8")
            with self.assertRaises(EvaluationError) as caught:
                parse_chinook_sql(path)
            self.assertEqual(caught.exception.code, "INVALID_SOURCE_SQL")


class ChinookDatasetTests(OfflineGuards, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = parse_chinook_sql(SOURCE)
        cls.config = json.loads((ROOT / "database/source_config.json").read_text(encoding="utf-8-sig"))

    def portable_bundle(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        paths = {
            "source_sql": "database/Chinook_PostgreSql.sql",
            "schema": "database/generated/chinook_schema_snapshot.json",
            "semantic_config": "database/source_config.json",
            "oracle": "src/agent_core/evaluation/chinook_oracle.py",
        }
        for relative in paths.values():
            destination = root / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes((ROOT / relative).read_bytes())
        dataset_dir = root / "evaluation/datasets"
        dataset_dir.mkdir(parents=True)
        cases, manifest = dataset_dir / "chinook_sql_v1.jsonl", dataset_dir / "chinook_manifest.json"
        snapshot_hash = canonical_chinook_snapshot_hash(self.data)
        manifest_data = {
            "format_version": 1, "source_id": "chinook_db", "suite_version": "chinook-offline-tests-v1",
            "projection_version": CHINOOK_PROJECTION_VERSION, "oracle_version": CHINOOK_ORACLE_VERSION,
            "split": "dev", "data_snapshot_id": f"sha256:{snapshot_hash}", "snapshot_sha256": snapshot_hash,
            "semantic_metadata_sha256": chinook_semantic_metadata_hash(self.data),
            "snapshot_row_counts": {key: len(rows) for key, rows in self.data.items()}, "artifacts": paths,
        }
        for label, key in (("source_sql", "source_sql_sha256"), ("schema", "schema_file_sha256"),
                           ("semantic_config", "semantic_config_sha256"), ("oracle", "oracle_file_sha256")):
            manifest_data[key] = hashlib.sha256((root / paths[label]).read_bytes()).hexdigest()
        request = query(filters=[{"field_id": "customer", "op": "eq", "value": 1}])
        expected = evaluate_chinook_sales(request, self.data, config=self.config)
        expected["tables"] = ["public.invoice"]
        row = {"case_id": "CA01", "kind": "answer", "query": request, "context": {}, "expected": expected,
               "comparison": {"decimal_scale": 2, "order_sensitive": False}, "scope": "chinook_sql_v1",
               "family": "integer_customer", "split": "dev", "data_snapshot_id": manifest_data["data_snapshot_id"]}
        manifest.write_text(json.dumps(manifest_data), encoding="utf-8")
        self.write_cases(cases, manifest, [row])
        return root, cases, manifest, row

    def write_cases(self, cases, manifest, rows):
        self.write_raw_cases(cases, manifest, "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n", rows)

    def write_raw_cases(self, cases, manifest, payload, rows):
        cases.write_text(payload, encoding="utf-8")
        data = json.loads(manifest.read_text(encoding="utf-8"))
        data.update(cases_sha256=hashlib.sha256(cases.read_bytes()).hexdigest(), case_count=len(rows),
                    kind_counts=dict(Counter(row["kind"] for row in rows)))
        manifest.write_text(json.dumps(data), encoding="utf-8")

    def assert_rejected(self, cases, manifest, code=None):
        with self.assertRaises(EvaluationError) as caught:
            load_chinook_cases(cases, manifest)
        if code is not None:
            self.assertEqual(caught.exception.code, code)

    def test_portable_manifest_loads_exact_invoice_standard(self):
        _, cases, manifest, _ = self.portable_bundle()
        bundle = load_chinook_cases(cases, manifest)
        self.assertEqual(len(bundle.cases), 1)
        self.assertEqual(bundle.cases[0].expected["rows"], [{"sales_amount": "39.62"}])
        self.assertIs(type(bundle.cases[0].expected["parameters"]["filter_0"]), int)

    def test_distributed_bundle_is_development_and_has_independent_gold(self):
        bundle = load_chinook_cases(CASES, MANIFEST)
        self.assertGreaterEqual(len(bundle.cases), 30)
        self.assertTrue(all(case.split == "dev" for case in bundle.cases))
        answers = [case for case in bundle.cases if case.kind == "answer"]
        self.assertTrue(any(case.expected["rows"] == [{"sales_amount": "2328.60"}] for case in answers))
        self.assertTrue(any(case.expected["rows"] == [{"sales_amount": "477.53"}] for case in answers))
        self.assertTrue(any(case.expected["rows"] == [{"sales_amount": "127.98"}] for case in answers))
        self.assertTrue(all(case.expected["gold"]["reviewed_by"] == "independent_python_oracle" for case in answers))
        self.assertTrue(all("expected" not in case.query and "gold" not in case.query for case in bundle.cases))

    def test_modified_case_bytes_fail_hash_before_gold_is_used(self):
        _, cases, manifest, _ = self.portable_bundle()
        cases.write_bytes(cases.read_bytes() + b"\n")
        self.assert_rejected(cases, manifest, "HASH_MISMATCH")

    def test_every_frozen_artifact_and_canonical_hash_is_checked(self):
        _, cases, manifest, _ = self.portable_bundle()
        original = json.loads(manifest.read_text(encoding="utf-8"))
        for key in ("source_sql_sha256", "schema_file_sha256", "semantic_config_sha256", "oracle_file_sha256",
                    "snapshot_sha256", "semantic_metadata_sha256"):
            with self.subTest(key=key):
                data = {**original, key: "0" * 64}
                manifest.write_text(json.dumps(data), encoding="utf-8")
                self.assert_rejected(cases, manifest, "HASH_MISMATCH")

    def test_duplicate_json_members_in_case_and_manifest_are_rejected(self):
        _, cases, manifest, row = self.portable_bundle()
        payload = json.dumps(row).replace('"case_id": "CA01"', '"case_id": "CA01", "case_id": "CA02"') + "\n"
        self.write_raw_cases(cases, manifest, payload, [row])
        self.assert_rejected(cases, manifest, "INVALID_JSON")
        self.write_cases(cases, manifest, [row])
        payload = manifest.read_text(encoding="utf-8").replace('"split": "dev"', '"split": "dev", "split": "test"')
        manifest.write_text(payload, encoding="utf-8")
        self.assert_rejected(cases, manifest, "INVALID_JSON")

    def test_duplicate_case_ids_and_split_snapshot_mismatch_are_rejected(self):
        _, cases, manifest, row = self.portable_bundle()
        self.write_cases(cases, manifest, [row, copy.deepcopy(row)])
        self.assert_rejected(cases, manifest, "DUPLICATE_CASE_ID")
        for key, value in (("split", "test"), ("data_snapshot_id", "sha256:" + "0" * 64)):
            changed = {**row, key: value}
            self.write_cases(cases, manifest, [changed])
            self.assert_rejected(cases, manifest, "INVALID_CASE")

    def test_paths_cannot_escape_project_or_select_credential_profiles(self):
        _, cases, manifest, _ = self.portable_bundle()
        original = json.loads(manifest.read_text(encoding="utf-8"))
        for path in ("../outside.sql", "C:/outside.sql", "C:outside.sql", "E:\\outside.sql",
                     "database/.local/connection.json", ".env", "database\\..\\outside.sql"):
            with self.subTest(path=path):
                data = copy.deepcopy(original)
                data["artifacts"]["source_sql"] = path
                manifest.write_text(json.dumps(data), encoding="utf-8")
                self.assert_rejected(cases, manifest, "INVALID_MANIFEST")

    def test_rehashed_incorrect_money_or_time_gold_still_fails_oracle(self):
        _, cases, manifest, row = self.portable_bundle()
        mutations = (
            lambda changed: changed["expected"]["rows"][0].update(sales_amount="0.00"),
            lambda changed: changed["expected"]["parameters"].update(start="2022-01-01T00:00:00"),
            lambda changed: changed["expected"]["parameters"].update(filter_0=2),
        )
        for mutate in mutations:
            changed = copy.deepcopy(row)
            mutate(changed)
            self.write_cases(cases, manifest, [changed])
            self.assert_rejected(cases, manifest, "GOLD_MISMATCH")

    def test_resolved_link_cannot_target_a_credential_directory(self):
        root, _, _, _ = self.portable_bundle()
        for blocked in (".local", ".env"):
            with self.subTest(blocked=blocked), patch.object(Path, "resolve", return_value=root / blocked / "dummy.json"):
                with self.assertRaises(EvaluationError) as caught:
                    _artifact(root, "apparently_public_alias/dummy.json")
                self.assertEqual(caught.exception.code, "INVALID_MANIFEST")

    def test_customer_gold_cannot_coerce_bools_floats_or_strings_to_integer_ids(self):
        _, cases, manifest, row = self.portable_bundle()
        for value in (True, 1.0, "1"):
            with self.subTest(value=value):
                changed = copy.deepcopy(row)
                changed["expected"]["parameters"]["filter_0"] = value
                self.write_cases(cases, manifest, [changed])
                self.assert_rejected(cases, manifest)

    def test_customer_array_gold_has_exact_element_types(self):
        _, cases, manifest, row = self.portable_bundle()
        request = query(filters=[{"field_id": "customer", "op": "in", "value": [1, 2]}])
        row["query"] = request
        row["expected"] = {**evaluate_chinook_sales(request, self.data, config=self.config), "tables": ["public.invoice"]}
        for value in ([True, 2], [1.0, 2], ["1", 2]):
            with self.subTest(value=value):
                changed = copy.deepcopy(row)
                changed["expected"]["parameters"]["filter_0"] = value
                self.write_cases(cases, manifest, [changed])
                self.assert_rejected(cases, manifest)

    def test_aware_gold_parameter_and_extra_binding_are_rejected(self):
        _, cases, manifest, row = self.portable_bundle()
        for values in ({"start": "2021-01-01T00:00:00+08:00"}, {"status": "paid"}, {"filter_2": 1}):
            changed = copy.deepcopy(row)
            changed["expected"]["parameters"].update(values)
            self.write_cases(cases, manifest, [changed])
            self.assert_rejected(cases, manifest)

    def test_answer_limits_require_an_explicit_supported_range(self):
        _, cases, manifest, row = self.portable_bundle()
        for value in (True, 0, -1, 1001):
            with self.subTest(max_rows=value):
                changed = copy.deepcopy(row)
                changed["context"]["max_rows"] = value
                if value == 1001:
                    changed["expected"]["parameters"]["fetch_limit"] = 1001
                self.write_cases(cases, manifest, [changed])
                self.assert_rejected(cases, manifest, "INVALID_CASE")

    def test_malformed_scope_or_manifest_split_raise_controlled_error(self):
        _, cases, manifest, row = self.portable_bundle()
        for value in ([], {}, True, 1):
            with self.subTest(scope=value):
                self.write_cases(cases, manifest, [{**row, "scope": value}])
                self.assert_rejected(cases, manifest, "INVALID_CASE")
        self.write_cases(cases, manifest, [row])
        original = json.loads(manifest.read_text(encoding="utf-8"))
        for value in ([], {}, True, 1):
            with self.subTest(split=value):
                manifest.write_text(json.dumps({**original, "split": value}), encoding="utf-8")
                self.assert_rejected(cases, manifest, "INVALID_MANIFEST")


class ChinookPromptSeparationTests(OfflineGuards, unittest.IsolatedAsyncioTestCase):
    async def test_runner_sends_only_query_and_context_without_gold_or_comparison(self):
        data = parse_chinook_sql(SOURCE)
        snapshot_id = "sha256:" + canonical_chinook_snapshot_hash(data)
        case = EvalCase("PRIVATE_GOLD_CASE", "answer", query(), {},
                        {"secret_gold": "GOLD_NEVER_IN_TARGET"}, {"secret_comparison": "RULE_NEVER_IN_TARGET"},
                        scope="chinook_sql_v1", data_snapshot_id=snapshot_id)

        class Target:
            async def query(self, request, context):
                self.received = (copy.deepcopy(request), copy.deepcopy(context))
                return {"status": "error", "execution_attempted": False}

        async def verify(manifest):
            return {"verified": True}

        target = Target()
        manifest = RunManifest("separation-test", "test-suite", snapshot_id, "demo", "mock")
        run = await EvaluationRunner().run_suite(target, (case,), manifest, RunConfig(), snapshot_verifier=verify)
        self.assertEqual(run.status, "complete")
        self.assertEqual(target.received[0], case.query)
        serialized = json.dumps(target.received)
        self.assertNotIn("GOLD_NEVER_IN_TARGET", serialized)
        self.assertNotIn("RULE_NEVER_IN_TARGET", serialized)

    async def test_distributed_gold_and_bound_values_do_not_enter_llm_prompt(self):
        from agent_core.sql.example import OfflineExampleProvider
        from agent_core.sql.generation_service import SQLGenerationService
        from database.metadata_reader import FileMetadataReader

        class RecordingProvider(OfflineExampleProvider):
            def __init__(self):
                self.requests = []

            async def generate(self, request):
                self.requests.append(request)
                return await super().generate(request)

        config = json.loads((ROOT / "database/source_config.json").read_text(encoding="utf-8-sig"))
        provider = RecordingProvider()
        service = SQLGenerationService(provider, base_dir=ROOT / "database", config=config,
                                       metadata_reader=FileMetadataReader(ROOT / "database", config=config))
        bundle = load_chinook_cases(CASES, MANIFEST)
        for case in bundle.cases:
            if case.kind == "roadmap":
                continue
            with self.subTest(case_id=case.case_id):
                calls = len(provider.requests)
                response = await service.generate(case.query, case.context)
                if case.kind == "clarification":
                    self.assertEqual(response["status"], "needs_clarification", response)
                    self.assertEqual({issue["code"] for issue in response["issues"]}, set(case.expected["issue_codes"]))
                    self.assertEqual({issue["field_id"] for issue in response["issues"]},
                                     set(case.expected["clarification_fields"]))
                    for field, candidates in case.expected["permitted_candidates"].items():
                        key = "metric_definition_id" if field == "metric_definition_id" else "id"
                        actual = [item[key] for issue in response["issues"] if issue["field_id"] == field
                                  for item in issue["candidates"]]
                        self.assertEqual(Counter((type(item).__name__, item) for item in actual),
                                         Counter((type(item).__name__, item) for item in candidates))
                    for field, members in case.expected.get("missing_members", {}).items():
                        actual = {item for issue in response["issues"] if issue["field_id"] == field
                                  for item in issue.get("missing_members", [])}
                        self.assertEqual(actual, set(members))
                    self.assertEqual(len(provider.requests), calls)
                    self.assertFalse(response["execution_attempted"])
                    continue
                if case.kind == "safety":
                    self.assertEqual(response["status"], "error", response)
                    self.assertEqual(response["error_code"], case.expected["error_code"])
                    self.assertEqual(len(provider.requests), calls)
                    self.assertFalse(response["execution_attempted"])
                    continue
                self.assertEqual(response["status"], "generated", response)
                self.assertEqual(response["parameters"], case.expected["parameters"])
                payload = json.loads(provider.requests[-1].messages[-1].content)
                self.assertNotIn("expected", payload)
                self.assertNotIn("gold", payload)
                prompt = "\n".join(message.content for message in provider.requests[-1].messages)
                self.assertNotIn("independent_python_oracle", prompt)
                self.assertNotIn(json.dumps(case.expected["rows"], ensure_ascii=False), prompt)
                self.assertNotIn(case.expected["parameters"]["start"], prompt)
                self.assertNotIn("PGPASSWORD", prompt)


if __name__ == "__main__":
    unittest.main(verbosity=2)
