"""Chinook 业务语义、映射、组装和入口分流的离线测试。"""
from __future__ import annotations

import copy
import json
import tempfile
import unittest
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from agent_core.sql.example import OfflineExampleProvider
from agent_core.sql.llm_sql_generator import LLMSQLGenerator, prepare_generation_inputs

from database.chinook_query import ChinookSQLGenerator, ChinookSchemaLinkingService
from database.chinook_snapshot import enrich_snapshot
from database.setup_chinook import ChinookSetupError, TABLES, chinook_config, prepare_script
from database.sql_common import ServiceFault
from database.sql_contracts import SQLCandidate
from database.sql_service import SQLService
from database.sql_validator import SQLValidator
from database.readonly_sql_executor import ReadOnlySQLExecutor
from database.query_profiles import chinook_amount_type

BASE = Path(__file__).resolve().parents[1] / "database"
LIMITS = {"max_rows": 100, "db_timeout_ms": 5000}


def query(value="美国", field="country", operation="eq"):
    return {"source_id": "chinook_db", "metric_id": "sales_amount",
            "metric_definition_id": "chinook_sales_amount_v1",
            "time_range": {"start": "2024-01-01", "end_exclusive": "2025-01-01"},
            "filters": [{"field_id": field, "op": operation, "value": value}]}


def fixture():
    config = chinook_config({"statement_timeout_ms": 5000, "max_result_rows": 1000,
                            "project_ref": "old-cloud-project", "project_name": "old-project"})
    fields = {
        "invoice": [("invoice_id", "integer"), ("customer_id", "integer"),
                    ("total", "numeric(10,2)"), ("invoice_date", "timestamp without time zone"),
                    ("billing_country", "character varying(40)"), ("billing_city", "character varying(40)")],
        "customer": [("customer_id", "integer"), ("support_rep_id", "integer")],
        "employee": [("employee_id", "integer")],
        "invoice_line": [("invoice_line_id", "integer"), ("invoice_id", "integer"), ("track_id", "integer")],
        "track": [("track_id", "integer")],
    }
    columns = [{"schema_name": "public", "table_name": table, "column_name": name,
                "data_type": kind, "ordinal_position": index, "is_nullable": True,
                "is_generated": False, "default_or_generation_expression": None, "description": None}
               for table, rows in fields.items() for index, (name, kind) in enumerate(rows, 1)]
    snapshot = {"objects": [{"schema_name": "public", "table_name": table, "object_type": "table", "description": None}
                            for table in TABLES], "columns": columns, "keys": [],
                "foreign_keys": [{"source_schema": "public", "source_table": "invoice", "source_column": "customer_id",
                                  "target_schema": "public", "target_table": "customer", "target_column": "customer_id",
                                  "constraint_name": "invoice_customer_id_fkey", "column_position": 1,
                                  "definition": "FOREIGN KEY (customer_id) REFERENCES public.customer(customer_id)"}],
                "read_date": "2026-10-04", "metadata_mode": "provided", "captured_at": None,
                "permissions_verified": False}
    data = {"countries": [{"country": "USA"}, {"country": "Canada"}, {"country": "Brazil"}],
            "cities": [{"billing_country": "USA", "billing_city": "New York"},
                       {"billing_country": "USA", "billing_city": "Boston"},
                       {"billing_country": "Canada", "billing_city": "Toronto"},
                       {"billing_country": "USA", "billing_city": "Springfield"},
                       {"billing_country": "Canada", "billing_city": "Springfield"},
                       {"billing_country": "USA", "billing_city": None}],
            "employees": [{"employee_id": 3, "first_name": "Jane", "last_name": "Peacock",
                           "full_name": "Jane Peacock"}],
            "customers": [{"customer_id": 1, "first_name": "Luís", "last_name": "Gonçalves",
                           "full_name": "Luís Gonçalves", "support_rep_id": 3},
                          {"customer_id": 2, "first_name": "Leonie", "last_name": "Köhler",
                           "full_name": "Leonie Köhler", "support_rep_id": 3}]}
    return config, snapshot, data


class FakeReader:
    def __init__(self, snapshot, data):
        self.payload, self.calls = {"snapshot": snapshot, "data": data, "mode": "provided"}, 0

    def read(self, limits):
        self.calls += 1
        return copy.deepcopy(self.payload)


class RecordingExecutor:
    def __init__(self):
        self.calls = []

    def __call__(self, sql, parameters, limits):
        self.calls.append((sql, copy.deepcopy(parameters), dict(limits)))
        return {"rows": [{"sales_amount": Decimal("42.00")}], "truncated": False}


class ChinookFixture(unittest.TestCase):
    def setUp(self):
        for target in ("psycopg.connect", "psycopg.AsyncConnection.connect",
                       "database.sql_common.resolve_connection_parameters"):
            guard = patch(target, side_effect=AssertionError("Chinook 离线测试禁止连接真实数据库或读取凭据"))
            mocked = guard.start()
            self.addCleanup(guard.stop)
            self.addCleanup(mocked.assert_not_called)
        self.config, self.raw_snapshot, self.data = fixture()
        self.snapshot = enrich_snapshot(self.raw_snapshot, self.data, self.config)

    def match(self, request):
        return ChinookSchemaLinkingService().match(request, self.snapshot, entities=self.data)


class ChinookSnapshotTests(ChinookFixture):
    def test_snapshot_has_invoice_metric_types_and_no_assumed_currency(self):
        definition = self.snapshot["metric_definitions"][0]
        self.assertEqual(definition["query_spec"]["from"], "public.invoice")
        self.assertEqual(definition["query_spec"]["filter"], {})
        self.assertEqual(definition["query_spec"]["timestamp_semantics"], "naive_calendar")
        self.assertIsNone(definition["currency"])
        self.assertEqual(definition["unit"], "金额")
        self.assertNotIn("project_ref", self.config)
        self.assertEqual(self.snapshot["join_semantics"]["historical_attribution"]["country"],
                         "public.invoice.billing_country")

    def test_enrichment_does_not_modify_inputs_or_invent_schema_freshness(self):
        before = copy.deepcopy((self.raw_snapshot, self.data, self.config))
        enriched = enrich_snapshot(self.raw_snapshot, self.data, self.config)
        self.assertEqual((self.raw_snapshot, self.data, self.config), before)
        self.assertIsNone(enriched["snapshot_metadata"]["schema_captured_at"])
        self.assertFalse(enriched["snapshot_metadata"]["freshness_policy"]["enrichment_renews_schema_freshness"])

    def test_missing_invoice_column_is_metadata_error(self):
        raw = copy.deepcopy(self.raw_snapshot)
        raw["columns"] = [row for row in raw["columns"] if row["column_name"] != "total"]
        with self.assertRaises(ServiceFault) as caught:
            enrich_snapshot(raw, self.data, self.config)
        self.assertEqual(caught.exception.code, "METADATA_ERROR")


class ChinookMappingTests(ChinookFixture):
    def test_country_alias_and_region_compatibility_map_to_billing_country(self):
        for field, value in (("country", "美国"), ("region", "USA"), ("billing_country", "United States")):
            with self.subTest(field=field, value=value):
                decision = self.match(query(value, field))
                self.assertEqual(decision["status"], "ready", decision)
                request = decision["sql_generation_request"]
                self.assertEqual(request["filters"], [{"column": "billing_country", "op": "eq", "value": "USA"}])
                self.assertEqual(set(request["time_filter"]), {"column", "start", "end_exclusive", "timezone"})

    def test_integer_customer_and_country_in_are_bound_with_correct_types(self):
        requests = (query(1, "customer"), query([1, 2], "customer", "in"), query(["美国", "加拿大"], operation="in"))
        for request in requests:
            with self.subTest(request=request):
                decision = self.match(request)
                self.assertEqual(decision["status"], "ready", decision)
                sql, parameters = ChinookSQLGenerator().generate(decision["sql_generation_request"], LIMITS)
                self.assertIsInstance(parameters["start"], datetime)
                self.assertIsNone(parameters["start"].tzinfo)
                self.assertIsNone(parameters["end"].tzinfo)
                self.assertNotIn("payment_status", sql)
                if request["filters"][0]["field_id"] == "customer":
                    value = parameters["filter_0"]
                    self.assertTrue(all(isinstance(item, int) for item in value) if isinstance(value, list)
                                    else isinstance(value, int))

    def test_missing_time_metric_and_filter_values_return_clarification(self):
        both_missing = query()
        both_missing.pop("time_range")
        both_missing.pop("metric_definition_id")
        decision = self.match(both_missing)
        self.assertEqual(decision["status"], "needs_clarification")
        self.assertEqual({row["code"] for row in decision["issues"]}, {"MISSING_TIME_RANGE", "MISSING_METRIC_DEFINITION"})
        for operation, value in (("eq", None), ("eq", ""), ("eq", "  "), ("in", [])):
            decision = self.match(query(value, operation=operation))
            self.assertEqual(decision["status"], "needs_clarification")
            self.assertIn("MISSING_FILTER_VALUE", {row["code"] for row in decision["issues"]})

    def test_offset_times_are_clarified_without_timezone_conversion(self):
        for value in ("2024-01-01T00:00:00Z", "2024-01-01T00:00:00+08:00", "2024-01-01T00:00:00-05:00"):
            request = query()
            request["time_range"]["start"] = value
            before = copy.deepcopy(request)
            decision = self.match(request)
            self.assertEqual(decision["status"], "needs_clarification")
            self.assertIn("AMBIGUOUS_TIME_SEMANTICS", {row["code"] for row in decision["issues"]})
            self.assertNotIn("sql_generation_request", decision)
            self.assertEqual(request, before)

    def test_same_name_customer_keeps_all_integer_candidates(self):
        duplicate = copy.deepcopy(self.data["customers"][0])
        duplicate["customer_id"] = 3
        self.data["customers"].append(duplicate)
        self.snapshot = enrich_snapshot(self.raw_snapshot, self.data, self.config)
        decision = self.match(query("Luís Gonçalves", "customer"))
        self.assertEqual(decision["status"], "needs_clarification")
        issue = next(row for row in decision["issues"] if row["code"] == "AMBIGUOUS_ENTITY")
        self.assertEqual({row["id"] for row in issue["candidates"]}, {1, 3})
        self.assertIn("time_range", decision["clarification_request"]["confirmed_conditions"])

    def test_unknown_or_injection_country_never_returns_sql_request(self):
        for value in ("未知国家", "USA'; DROP TABLE public.invoice; --"):
            decision = self.match(query(value))
            self.assertEqual(decision["status"], "needs_clarification")
            self.assertEqual(decision["issues"][0]["code"], "UNKNOWN_ENTITY")
            self.assertNotIn("sql_generation_request", decision)

    def test_unknown_city_requires_clarification_without_sql_request(self):
        decision = self.match(query("不存在的城市", "city"))
        self.assertEqual(decision["status"], "needs_clarification", decision)
        self.assertNotIn("sql_generation_request", decision)
        self.assertTrue(decision["issues"])

    def test_unsupported_filters_are_errors(self):
        for field in ("employee", "track", "genre"):
            decision = self.match(query(1, field))
            self.assertEqual((decision["status"], decision["error_code"]), ("error", "UNSUPPORTED_FILTER"))

    def test_generator_binds_malicious_literal_and_rejects_identifiers(self):
        request = self.match(query())["sql_generation_request"]
        malicious = "USA'; DROP TABLE public.invoice; --"
        request["filters"][0]["value"] = malicious
        sql, parameters = ChinookSQLGenerator().generate(request, LIMITS)
        self.assertNotIn(malicious, sql)
        self.assertEqual(parameters["filter_0"], malicious)
        request["table"] = "public.invoice; DROP TABLE public.invoice"
        with self.assertRaises(ServiceFault):
            ChinookSQLGenerator().generate(request, LIMITS)


class ChinookCityTests(ChinookFixture):
    def test_city_dictionary_preserves_names_countries_and_excludes_null(self):
        cities = {row["city"]: row["billing_countries"] for row in self.snapshot["city_entities"]}
        self.assertEqual(cities, {"Boston": ["USA"], "New York": ["USA"],
                                  "Springfield": ["Canada", "USA"], "Toronto": ["Canada"]})
        field = next(row for row in self.snapshot["business_fields"] if row["field_id"] == "city")
        self.assertEqual(field["physical_column"], "public.invoice.billing_city")
        self.assertEqual(field["allowed_ops"], ["eq", "in"])
        self.assertIn("账单城市", field["name_aliases"])
        self.assertIn("city", self.snapshot["condition_rules"]["tasks"]["metric_aggregate"]["optional_filters"])

    def test_city_field_aliases_casefold_and_in_bind_exact_names(self):
        for field in ("city", "billing_city", "城市", "账单城市"):
            decision = self.match(query(" bOsToN ", field))
            self.assertEqual(decision["status"], "ready", decision)
            self.assertEqual(decision["sql_generation_request"]["filters"],
                             [{"column": "billing_city", "op": "eq", "value": "Boston"}])
        decision = self.match(query(["boston", "Springfield", "BOSTON"], "city", "in"))
        self.assertEqual(decision["status"], "ready", decision)
        sql, parameters = ChinookSQLGenerator().generate(decision["sql_generation_request"], LIMITS)
        self.assertIn("i.billing_city = ANY(%(filter_0)s)", sql)
        self.assertEqual(parameters["filter_0"], ["Boston", "Springfield"])

    def test_country_city_remain_independent_and_predicates(self):
        request = query("加拿大")
        request["filters"].append({"field_id": "city", "op": "eq", "value": "Boston"})
        decision = self.match(request)
        self.assertEqual(decision["status"], "ready", decision)
        sql, parameters = ChinookSQLGenerator().generate(decision["sql_generation_request"], LIMITS)
        self.assertIn("i.billing_country = %(filter_0)s AND i.billing_city = %(filter_1)s", sql)
        self.assertEqual((parameters["filter_0"], parameters["filter_1"]), ("Canada", "Boston"))

    def test_case_only_city_conflict_keeps_all_exact_candidates(self):
        self.data["cities"].append({"billing_country": "Canada", "billing_city": "BOSTON"})
        self.snapshot = enrich_snapshot(self.raw_snapshot, self.data, self.config)
        decision = self.match(query("Boston", "city"))
        self.assertEqual(decision["status"], "needs_clarification", decision)
        issue = next(row for row in decision["issues"] if row["code"] == "AMBIGUOUS_ENTITY")
        self.assertEqual({row["id"] for row in issue["candidates"]}, {"Boston", "BOSTON"})
        self.assertNotIn("sql_generation_request", decision)

    def test_missing_unknown_injected_or_nonstring_city_never_produces_sql(self):
        for value in (None, "", "  ", "Unknown city", "Boston'; DROP TABLE public.invoice; --"):
            decision = self.match(query(value, "city"))
            self.assertEqual(decision["status"], "needs_clarification", decision)
            self.assertNotIn("sql_generation_request", decision)
        for value in (1, True, {}, ["Boston"]):
            decision = self.match(query(value, "city"))
            self.assertEqual((decision["status"], decision["error_code"]), ("error", "INVALID_QUERY"))

    def test_mixed_predicate_types_and_aware_time_are_rejected(self):
        request = self.match(query(1, "customer"))["sql_generation_request"]
        for column, value in (("customer_id", "1"), ("customer_id", True), ("customer_id", 1.0),
                              ("billing_city", 1), ("billing_country", True)):
            with self.subTest(column=column, value=value):
                changed = copy.deepcopy(request)
                changed["filters"] = [{"column": column, "op": "eq", "value": value}]
                with self.assertRaises(ServiceFault):
                    ChinookSQLGenerator().generate(changed, LIMITS)
        request["time_filter"]["start"] = "2024-01-01T00:00:00+08:00"
        with self.assertRaises(ServiceFault) as caught:
            ChinookSQLGenerator().generate(request, LIMITS)
        self.assertEqual(caught.exception.code, "INVALID_QUERY")

    def test_amount_schema_requires_explicit_two_digit_scale(self):
        for kind in ("numeric", "numeric(10,3)", "numeric(10,1)", "numeric(0,2)",
                     "numeric(1001,2)", "numeric(10,-2)", "text"):
            with self.subTest(kind=kind):
                self.assertFalse(chinook_amount_type(kind))
                raw = copy.deepcopy(self.raw_snapshot)
                next(row for row in raw["columns"] if row["column_name"] == "total")["data_type"] = kind
                with self.assertRaises(ServiceFault) as caught:
                    enrich_snapshot(raw, self.data, self.config)
                self.assertEqual(caught.exception.code, "METADATA_ERROR")
        self.assertTrue(chinook_amount_type("numeric(10,2)"))
        self.assertTrue(chinook_amount_type("numeric(1000, 2)"))


class ChinookServiceTests(ChinookFixture):
    def service(self):
        self.reader = FakeReader(self.raw_snapshot, self.data)
        self.executor = RecordingExecutor()
        return SQLService(BASE, config=self.config, metadata_reader=self.reader, executor=self.executor)

    def test_default_source_routing_uses_chinook_invoice_and_amount_unit(self):
        service = self.service()
        result = service.query(query(), {"request_id": "chinook-test"})
        self.assertEqual(result["status"], "ok", result)
        self.assertEqual(result["request_id"], "chinook-test")
        self.assertEqual(result["rows"], [{"sales_amount": "42.00"}])
        self.assertEqual(result["tables"], ["public.invoice"])
        self.assertEqual(result["columns"][0]["unit"], "金额")
        self.assertEqual(len(self.executor.calls), 1)
        self.assertEqual(self.reader.calls, 1)
        sql, parameters, _ = self.executor.calls[0]
        self.assertIn("SUM(i.total)", sql)
        self.assertIsNone(parameters["start"].tzinfo)

    def test_clarification_never_calls_business_executor(self):
        service = self.service()
        for request in (query(None), query("未知国家")):
            result = service.query(request)
            self.assertEqual(result["status"], "needs_clarification", result)
            self.assertEqual(result["route_to"], "clarification")
            self.assertFalse(result["execution_attempted"])
            self.assertIsNone(result["sql"])
        self.assertEqual(self.executor.calls, [])


class ChinookSetupTests(unittest.TestCase):
    def test_user_script_is_independently_counted_and_dangerous_header_removed(self):
        sql, facts = prepare_script()
        self.assertEqual(facts["tables"], 11)
        self.assertEqual(facts["foreign_keys"], 11)
        self.assertEqual(facts["row_counts"]["track"], 3503)
        self.assertEqual(facts["row_counts"]["playlist_track"], 8715)
        self.assertEqual(facts["invoice_date_min"], "2021-01-01T00:00:00")
        self.assertEqual(facts["invoice_date_max"], "2025-12-22T00:00:00")
        self.assertEqual(facts["invoice_total"], "2328.60")
        self.assertNotIn("DROP DATABASE", sql)
        self.assertNotIn("CREATE DATABASE", sql)
        self.assertNotIn("\\c chinook", sql)

    def test_unreviewed_mutation_appended_to_script_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            script = Path(temporary) / "unsafe.sql"
            script.write_text((BASE / "Chinook_PostgreSql.sql").read_text(encoding="utf-8-sig")
                              + "\nDELETE FROM invoice;\n", encoding="utf-8")
            with self.assertRaises(ChinookSetupError) as caught:
                prepare_script(script)
            self.assertEqual(caught.exception.code, "UNREVIEWED_SCRIPT")


CHINOOK_CANDIDATE = (
    "SELECT COALESCE(SUM(i.total), 0) AS sales_amount FROM public.invoice i "
    "WHERE i.invoice_date >= :start AND i.invoice_date < :end "
    "AND i.billing_country = :filter_0 LIMIT :fetch_limit"
)


class ChinookGenerationValidationTests(ChinookFixture):
    def prepare(self, request=None):
        decision = self.match(query() if request is None else request)
        self.assertEqual(decision["status"], "ready", decision)
        return prepare_generation_inputs(decision["sql_generation_request"], self.snapshot, LIMITS)

    def validate(self, sql=CHINOOK_CANDIDATE, *, prepared=None, parameters=None, schema=None):
        prepared = self.prepare() if prepared is None else prepared
        candidate = SQLCandidate(
            sql=sql, source_id="chinook_db", metric_definition_id="chinook_sales_amount_v1",
            parameter_names=tuple(spec.name for spec in prepared.parameter_specs),
            schema_fingerprint_sha256=prepared.schema_context["schema_fingerprint_sha256"],
        )
        return SQLValidator().validate(candidate, prepared.generation_request,
                                       prepared.schema_context if schema is None else schema,
                                       prepared.parameters if parameters is None else parameters,
                                       limits=LIMITS)

    def test_preparation_preserves_integer_array_and_naive_timestamp_specs(self):
        for value, operation, dtype in ((1, "eq", "integer"), ([1, 2], "in", "integer[]")):
            with self.subTest(operation=operation):
                prepared = self.prepare(query(value, "customer", operation))
                specs = {spec.name: spec.data_type for spec in prepared.parameter_specs}
                self.assertEqual(specs["filter_0"], dtype)
                self.assertEqual(specs["start"], "timestamp")
                self.assertEqual(specs["end"], "timestamp")
                self.assertIsNone(prepared.parameters["start"].tzinfo)
                self.assertEqual(prepared.parameters["filter_0"], value)

    def test_valid_candidate_yields_canonical_naive_execution_artifact(self):
        result = self.validate()
        self.assertTrue(result.passed, result)
        self.assertEqual(result.status, "passed")
        self.assertEqual(result.validated_sql.parameters["filter_0"], "USA")
        self.assertIsNone(result.validated_sql.parameters["start"].tzinfo)
        self.assertIn("SUM(i.total)", result.validated_sql.sql)
        self.assertNotIn("payment_status", result.validated_sql.sql)

    def test_missing_country_wrong_sum_join_and_multiple_statements_are_rejected(self):
        variants = (
            CHINOOK_CANDIDATE.replace("AND i.billing_country = :filter_0 ", ""),
            CHINOOK_CANDIDATE.replace("SUM(i.total)", "SUM(i.customer_id)"),
            CHINOOK_CANDIDATE.replace("public.invoice i WHERE", "public.invoice i JOIN public.customer c ON c.customer_id=i.customer_id WHERE"),
            CHINOOK_CANDIDATE + "; DELETE FROM public.invoice",
        )
        for sql in variants:
            with self.subTest(sql=sql):
                result = self.validate(sql)
                self.assertEqual(result.status, "rejected")
                self.assertFalse(result.passed)
                self.assertIsNone(result.validated_sql)

    def test_forged_amount_column_and_timestamp_type_are_rejected(self):
        prepared = self.prepare()
        for kind in ("amount_name", "time_type"):
            schema = copy.deepcopy(prepared.schema_context)
            for column in schema["columns"]:
                if kind == "amount_name" and column["name"] == "total":
                    column["name"] = "paid_amount"
                if kind == "time_type" and column["name"] == "invoice_date":
                    column["data_type"] = "timestamp with time zone"
            result = self.validate(prepared=prepared, schema=schema)
            self.assertFalse(result.passed)
            self.assertIsNone(result.validated_sql)

    def test_offset_or_wrong_integer_type_parameters_are_rejected(self):
        for value in (datetime(2024, 1, 1, tzinfo=timezone.utc), "2024-01-01T00:00:00+08:00"):
            prepared = self.prepare()
            parameters = copy.deepcopy(prepared.parameters)
            parameters["start"] = value
            self.assertFalse(self.validate(prepared=prepared, parameters=parameters).passed)
        prepared = self.prepare(query(1, "customer"))
        parameters = copy.deepcopy(prepared.parameters)
        parameters["filter_0"] = "1"
        sql = CHINOOK_CANDIDATE.replace("billing_country", "customer_id")
        self.assertFalse(self.validate(sql, prepared=prepared, parameters=parameters).passed)


class ChinookLLMTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        ChinookFixture.setUp(self)

    async def test_fixed_provider_generates_unvalidated_chinook_candidate_without_values(self):
        decision = ChinookSchemaLinkingService().match(query(), self.snapshot, entities=self.data)
        prepared = prepare_generation_inputs(decision["sql_generation_request"], self.snapshot, LIMITS)

        class RecordingProvider(OfflineExampleProvider):
            async def generate(self, request):
                self.request = request
                return await super().generate(request)

        provider = RecordingProvider()
        candidate = await LLMSQLGenerator(provider).generate(
            prepared.generation_request, prepared.schema_context, prepared.parameter_specs,
            LIMITS, {"request_id": "chinook-candidate-test"},
        )
        self.assertFalse(candidate.validated)
        self.assertEqual(candidate.source_id, "chinook_db")
        self.assertIn("SUM(i.total)", candidate.sql)
        self.assertNotIn("payment_status", candidate.sql)
        payload = json.loads(provider.request.messages[-1].content)
        self.assertEqual(payload["query_spec"]["timestamp_semantics"], "naive_calendar")
        self.assertIsNone(payload["query_spec"]["time_zone"])
        prompt = "\n".join(message.content for message in provider.request.messages)
        self.assertNotIn("2024-01-01", prompt)
        self.assertNotIn("USA", prompt)
        result = SQLValidator().validate(candidate, prepared.generation_request, prepared.schema_context,
                                         prepared.parameters, limits=LIMITS)
        self.assertTrue(result.passed, result)


class PipelineCursor:
    def __init__(self):
        self.business_calls = []
        self.description = (SimpleNamespace(name="sales_amount", type_code=1700),)

    async def execute(self, sql, parameters=None):
        if sql.startswith("SELECT COALESCE(SUM("):
            self.business_calls.append((sql, copy.deepcopy(parameters)))

    async def fetchone(self):
        return {key: False for key in ("can_write", "can_write_columns", "can_create", "is_owner",
                                      "rls_enabled", "rls_active")}

    async def fetchmany(self, size):
        return [{"sales_amount": Decimal("42.00")}]


class PipelineClient:
    def __init__(self):
        self.cursor = PipelineCursor()
        self.transactions = 0

    @asynccontextmanager
    async def transaction(self, limits):
        self.transactions += 1
        yield self.cursor


class PipelineReader:
    def __init__(self, snapshot, data):
        self.payload = {"snapshot": snapshot, "data": data, "mode": "live",
                        "permissions_verified": True, "captured_at": "2026-10-04T00:00:00+00:00"}

    async def aread_in_transaction(self, cursor):
        return copy.deepcopy(self.payload)


class ChinookCityValidatedPipelineTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        ChinookFixture.setUp(self)

    def prepare(self, request=None):
        decision = ChinookSchemaLinkingService().match(
            query(["Boston", "Springfield"], "city", "in") if request is None else request,
            self.snapshot, entities=self.data,
        )
        self.assertEqual(decision["status"], "ready", decision)
        return prepare_generation_inputs(decision["sql_generation_request"], self.snapshot, LIMITS)

    async def candidate(self, prepared):
        return await LLMSQLGenerator(OfflineExampleProvider()).generate(
            prepared.generation_request, prepared.schema_context, prepared.parameter_specs, LIMITS,
        )

    def validate(self, candidate, prepared, parameters=None):
        return SQLValidator().validate(candidate, prepared.generation_request, prepared.schema_context,
                                       prepared.parameters if parameters is None else parameters, LIMITS)

    def executor(self):
        self.client = PipelineClient()
        self.reader = PipelineReader(self.raw_snapshot, self.data)
        return ReadOnlySQLExecutor(self.config, client=self.client, metadata_reader=self.reader)

    async def test_city_generation_validation_execution_preserve_typed_bindings(self):
        prepared = self.prepare()
        self.assertEqual(next(spec.data_type for spec in prepared.parameter_specs
                              if spec.column == "billing_city"), "text[]")
        candidate = await self.candidate(prepared)
        validation = self.validate(candidate, prepared)
        self.assertTrue(validation.passed, validation)
        executor = self.executor()
        result = await executor.execute(validation.validated_sql, prepared.generation_request,
                                        prepared.schema_context, limits=LIMITS)
        self.assertEqual(result["rows"], [{"sales_amount": "42.00"}])
        self.assertEqual(result["columns"][0]["unit"], "金额")
        self.assertTrue(result["metadata"]["same_transaction_revalidation"])
        self.assertEqual(self.client.cursor.business_calls,
                         [(validation.validated_sql.sql, validation.validated_sql.parameters)])
        self.assertEqual(self.client.cursor.business_calls[0][1]["filter_0"], ["Boston", "Springfield"])
        self.assertIsNone(self.client.cursor.business_calls[0][1]["start"].tzinfo)

    async def test_malicious_candidates_never_produce_an_execution_package(self):
        prepared = self.prepare()
        candidate = await self.candidate(prepared)
        sql = candidate.sql
        for malicious in ("DELETE FROM public.invoice", sql + "; DELETE FROM public.invoice",
                          sql.replace("FROM public.invoice", "FROM public.customer"),
                          sql.replace(" AND i.billing_city = ANY(:filter_0)", ""),
                          sql.replace("i.billing_city = ANY(:filter_0)", "i.billing_city = 'Boston'"),
                          sql.replace(" AND i.billing_city", " OR i.billing_city")):
            with self.subTest(sql=malicious):
                result = self.validate(replace(candidate, sql=malicious), prepared)
                self.assertFalse(result.passed, result)
                self.assertIsNone(result.validated_sql)
        executor = self.executor()
        with self.assertRaises(ServiceFault) as caught:
            await executor.execute(candidate, prepared.generation_request, prepared.schema_context, limits=LIMITS)
        self.assertFalse(caught.exception.execution_attempted)
        self.assertEqual(self.client.transactions, 0)
        self.assertEqual(self.client.cursor.business_calls, [])

    async def test_altered_parameters_sql_and_source_are_blocked_before_execution(self):
        prepared = self.prepare()
        candidate = await self.candidate(prepared)
        package = self.validate(candidate, prepared).validated_sql
        parameters = package.parameters
        parameters["filter_0"] = ["Boston"]
        executor = self.executor()
        for altered in (replace(package, _parameter_items=tuple(parameters.items())),
                        replace(package, sql="DELETE FROM public.invoice")):
            with self.assertRaises(ServiceFault) as caught:
                await executor.execute(altered, prepared.generation_request, prepared.schema_context, limits=LIMITS)
            self.assertFalse(caught.exception.execution_attempted)
        self.assertEqual(self.client.transactions, 0)
        executor.config["source_id"] = "sales_db"
        with self.assertRaises(ServiceFault) as caught:
            await executor.execute(package, prepared.generation_request, prepared.schema_context, limits=LIMITS)
        self.assertEqual(caught.exception.code, "TABLE_NOT_ALLOWED")
        self.assertFalse(caught.exception.execution_attempted)
        self.assertEqual(self.client.cursor.business_calls, [])

    async def test_live_city_entity_drift_blocks_business_execution(self):
        prepared = self.prepare()
        package = self.validate(await self.candidate(prepared), prepared).validated_sql
        executor = self.executor()
        self.reader.payload["data"]["cities"] = [{"billing_country": "USA", "billing_city": "Boston"}]
        with self.assertRaises(ServiceFault) as caught:
            await executor.execute(package, prepared.generation_request, prepared.schema_context, limits=LIMITS)
        self.assertEqual(caught.exception.code, "SEMANTIC_CONTEXT_CHANGED")
        self.assertFalse(caught.exception.execution_attempted)
        self.assertEqual(self.client.cursor.business_calls, [])

    async def test_validator_rejects_undeclared_or_fractional_cent_scale(self):
        prepared = self.prepare()
        candidate = await self.candidate(prepared)
        for data_type in ("numeric", "numeric(10,3)"):
            changed = copy.deepcopy(prepared.schema_context)
            next(row for row in changed["columns"] if row["name"] == "total")["data_type"] = data_type
            result = SQLValidator().validate(candidate, prepared.generation_request, changed,
                                             prepared.parameters, LIMITS)
            self.assertEqual(result.error_code, "METADATA_ERROR")


if __name__ == "__main__":
    unittest.main(verbosity=2)
