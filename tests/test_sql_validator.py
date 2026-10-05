"""候选 SQL 检验的离线契约测试；禁止执行数据库语句。"""
from __future__ import annotations

import copy
import json
import unittest
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from agent_core.sql.llm_sql_generator import prepare_generation_inputs
from database.enrich_schema_snapshot import enrich_snapshot
from database.metadata_reader import FileMetadataReader
from database.schema_linking_service import SchemaLinkingService
from database.sql_common import json_value
from database.sql_contracts import SQLCandidate
from database.sql_generator import SQLGenerator
from database.sql_validator import SQLValidator


DATABASE_DIR = Path(__file__).resolve().parents[1] / "database"
LIMITS = {"max_rows": 10, "db_timeout_ms": 5000}
BASE_SQL = (
    "SELECT COALESCE(SUM(o.total), 0) AS sales_amount "
    "FROM public.invoice AS o WHERE o.invoice_date >= :start AND o.invoice_date < :end "
    "AND o.billing_country = :filter_0 LIMIT :fetch_limit"
)


class SQLValidatorTests(unittest.TestCase):
    def setUp(self):
        guard = patch(
            "database.sql_common.ReadOnlyClient.transaction",
            side_effect=AssertionError("SQL 检验测试禁止访问数据库"),
        )
        self.database_guard = guard.start()
        self.addCleanup(guard.stop)
        self.addCleanup(self.database_guard.assert_not_called)
        self.config = json.loads((DATABASE_DIR / "source_config.json").read_text(encoding="utf-8-sig"))
        self.metadata = FileMetadataReader(DATABASE_DIR, self.config).read(LIMITS)
        self.snapshot = enrich_snapshot(self.metadata["snapshot"], self.metadata["data"], self.config)
        self.validator = SQLValidator()
        self.scalar = self.prepare([{ "field_id": "country", "op": "eq", "value": "美国" }])

    def prepare(self, filters):
        query = {
            "source_id": "chinook_db", "metric_id": "sales_amount",
            "metric_definition_id": "chinook_sales_amount_v1",
            "time_range": {"start": "2024-01-01", "end_exclusive": "2025-01-01"},
            "filters": filters,
        }
        decision = SchemaLinkingService().match(
            query, self.snapshot, entities=self.metadata["data"],
        )
        self.assertEqual(decision["status"], "ready", decision)
        return prepare_generation_inputs(decision["sql_generation_request"], self.snapshot, LIMITS)

    def candidate(self, sql, prepared=None):
        prepared = self.scalar if prepared is None else prepared
        return SQLCandidate(
            sql=sql, source_id="chinook_db", metric_definition_id="chinook_sales_amount_v1",
            parameter_names=tuple(spec.name for spec in prepared.parameter_specs),
            schema_fingerprint_sha256=prepared.schema_context["schema_fingerprint_sha256"],
        )

    def validate(self, sql=BASE_SQL, *, prepared=None, parameters=None, candidate=None, schema=None):
        prepared = self.scalar if prepared is None else prepared
        return self.validator.validate(
            self.candidate(sql, prepared) if candidate is None else candidate,
            prepared.generation_request,
            prepared.schema_context if schema is None else schema,
            prepared.parameters if parameters is None else parameters,
            limits=LIMITS,
        )

    def assert_rejected(self, result, error_code=None):
        self.assertEqual(result.status, "rejected")
        self.assertIs(result.passed, False)
        self.assertIsNone(result.validated_sql)
        self.assertTrue(result.error_code)
        if error_code is not None:
            self.assertEqual(result.error_code, error_code)

    def assert_passed(self, result, prepared, original_sql):
        self.assertEqual(result.status, "passed", result)
        self.assertIs(result.passed, True)
        artifact = result.validated_sql
        self.assertIsNotNone(artifact)
        canonical_sql, typed_parameters = SQLGenerator().generate(prepared.generation_request, LIMITS)
        self.assertEqual(artifact.sql, canonical_sql)
        self.assertEqual(artifact.candidate_sql, original_sql)
        self.assertEqual(artifact.parameters, typed_parameters)
        json.dumps(artifact.as_dict(), ensure_ascii=False, allow_nan=False)
        return artifact

    def test_scalar_candidate_passes_with_canonical_parameterized_artifact(self):
        artifact = self.assert_passed(self.validate(), self.scalar, BASE_SQL)
        self.assertEqual(artifact.parameters["filter_0"], "USA")
        self.assertIsInstance(artifact.parameters["start"], datetime)
        self.assertIn("%(filter_0)s", artifact.sql)
        self.assertNotIn("USA", artifact.sql)

    def test_array_candidate_passes_with_trusted_entity_identifiers(self):
        prepared = self.prepare([{ "field_id": "country", "op": "in", "value": ["美国", "加拿大"] }])
        sql = BASE_SQL.replace("o.billing_country = :filter_0", "o.billing_country = ANY(:filter_0)")
        artifact = self.assert_passed(self.validate(sql, prepared=prepared), prepared, sql)
        self.assertEqual(artifact.parameters["filter_0"], ["USA", "Canada"])
        self.assertIn("ANY(%(filter_0)s)", artifact.sql)

    def test_query_without_entity_filter_passes(self):
        prepared = self.prepare([])
        sql = BASE_SQL.replace("AND o.billing_country = :filter_0 ", "")
        self.assert_passed(self.validate(sql, prepared=prepared), prepared, sql)

    def test_normal_alias_and_reordered_parenthesized_and_are_equivalent(self):
        sql = (
            "SELECT COALESCE(SUM(sale.total), 0) AS sales_amount "
            "FROM public.invoice sale WHERE (sale.billing_country = :filter_0) "
            "AND (sale.invoice_date < :end) "
            "AND sale.invoice_date >= :start LIMIT :fetch_limit"
        )
        self.assert_passed(self.validate(sql), self.scalar, sql)

    def test_keyword_case_and_exact_quoted_alias_are_accepted(self):
        lowercase = BASE_SQL
        for keyword in ("SELECT", "COALESCE", "SUM", "AS", "FROM", "WHERE", "AND", "LIMIT"):
            lowercase = lowercase.replace(keyword, keyword.lower())
        quoted_alias = BASE_SQL.replace("AS o WHERE", 'AS "Sale" WHERE').replace("o.", '"Sale".')
        for sql in (lowercase, quoted_alias):
            with self.subTest(sql=sql):
                self.assert_passed(self.validate(sql), self.scalar, sql)
        wrong_case = quoted_alias.replace('"Sale".', '"sale".')
        self.assert_rejected(self.validate(wrong_case))

    def test_normalized_or_quoted_or_qualified_function_names_are_rejected(self):
        variants = {
            "nvl": BASE_SQL.replace("COALESCE(", "NVL("),
            "ifnull": BASE_SQL.replace("COALESCE(", "IFNULL("),
            "quoted_coalesce": BASE_SQL.replace("COALESCE(", '"COALESCE"('),
            "quoted_sum": BASE_SQL.replace("SUM(", '"SUM"('),
            "qualified_coalesce": BASE_SQL.replace("COALESCE(", "pg_catalog.COALESCE("),
            "qualified_sum": BASE_SQL.replace("SUM(", "pg_catalog.SUM("),
        }
        for label, sql in variants.items():
            with self.subTest(function_spelling=label):
                self.assert_rejected(self.validate(sql))

    def test_parser_normalized_non_postgresql_syntax_is_rejected(self):
        variants = {
            "double_equal": BASE_SQL.replace("= :filter_0", "== :filter_0"),
            "alias_column_list": BASE_SQL.replace("AS o WHERE", "AS o(renamed_column) WHERE"),
            "only": BASE_SQL.replace("FROM public.invoice", "FROM ONLY public.invoice"),
            "limit_with_ties": BASE_SQL + " WITH TIES",
            "fetch_with_ties": BASE_SQL.replace("LIMIT :fetch_limit", "FETCH FIRST :fetch_limit ROWS WITH TIES"),
        }
        for label, sql in variants.items():
            with self.subTest(syntax=label):
                self.assert_rejected(self.validate(sql))

    def test_named_parameters_must_have_contiguous_unquoted_tokens(self):
        for spelling in (':"filter_0"', ": filter_0", ":/*c*/filter_0"):
            with self.subTest(spelling=spelling):
                self.assert_rejected(self.validate(BASE_SQL.replace(":filter_0", spelling)))

    def test_unary_plus_cannot_be_discarded_to_make_an_invalid_candidate_pass(self):
        variants = {
            "filter_parameter": BASE_SQL.replace(":filter_0", "+:filter_0"),
            "filter_column": BASE_SQL.replace("o.billing_country", "+o.billing_country"),
            "start_parameter": BASE_SQL.replace("o.invoice_date >= :start", "o.invoice_date >= +:start"),
            "amount_column": BASE_SQL.replace("SUM(o.total)", "SUM(+o.total)"),
            "limit_parameter": BASE_SQL.replace("LIMIT :fetch_limit", "LIMIT +:fetch_limit"),
        }
        for label, sql in variants.items():
            with self.subTest(unary_plus=label):
                self.assert_rejected(self.validate(sql))

    def test_any_requires_parentheses_and_allows_extra_normal_parentheses(self):
        prepared = self.prepare([{ "field_id": "country", "op": "in", "value": ["美国", "加拿大"] }])
        missing_parentheses = BASE_SQL.replace("o.billing_country = :filter_0", "o.billing_country = ANY :filter_0")
        self.assert_rejected(self.validate(missing_parentheses, prepared=prepared))
        extra_parentheses = BASE_SQL.replace("o.billing_country = :filter_0", "o.billing_country = ANY((:filter_0))")
        self.assert_passed(self.validate(extra_parentheses, prepared=prepared), prepared, extra_parentheses)

    def test_reserved_end_alias_is_rejected_unquoted_and_accepted_when_quoted(self):
        unquoted = BASE_SQL.replace("AS o WHERE", "AS end WHERE").replace("o.", "end.")
        self.assert_rejected(self.validate(unquoted))
        quoted = BASE_SQL.replace("AS o WHERE", 'AS "end" WHERE').replace("o.", '"end".')
        self.assert_passed(self.validate(quoted), self.scalar, quoted)

    def test_sql_length_limit_accepts_boundary_and_rejects_one_character_over(self):
        self.validator = SQLValidator(max_sql_chars=len(BASE_SQL))
        self.assert_passed(self.validate(), self.scalar, BASE_SQL)
        self.assert_rejected(self.validate(BASE_SQL + " "), "SQL_TOO_LARGE")

    def test_ast_node_and_depth_budgets_reject_otherwise_valid_queries(self):
        self.validator = SQLValidator(max_ast_nodes=8)
        self.assert_rejected(self.validate(), "SQL_TOO_COMPLEX")
        self.validator = SQLValidator(max_ast_depth=3)
        self.assert_rejected(self.validate(), "SQL_TOO_COMPLEX")
        self.validator = SQLValidator()
        self.assert_passed(self.validate(), self.scalar, BASE_SQL)

    def test_excessive_nested_parentheses_are_rejected_before_execution(self):
        sql = BASE_SQL.replace("o.billing_country = :filter_0", "(" * 65 + "o.billing_country = :filter_0" + ")" * 65)
        self.assert_rejected(self.validate(sql), "SQL_TOO_COMPLEX")

    def test_json_transport_dates_are_accepted_and_restored_to_typed_parameters(self):
        parameters = json.loads(json.dumps(json_value(self.scalar.parameters)))
        self.assertIsInstance(parameters["start"], str)
        artifact = self.assert_passed(self.validate(parameters=parameters), self.scalar, BASE_SQL)
        self.assertIsInstance(artifact.parameters["start"], datetime)

    def test_multiple_statements_are_rejected(self):
        self.assert_rejected(self.validate(BASE_SQL + "; SELECT 1"), "MULTIPLE_STATEMENTS")

    def test_malformed_and_empty_sql_are_rejected(self):
        for sql in ("", "SELECT FROM public.invoice WHERE (", "not valid SQL !!!"):
            with self.subTest(sql=sql):
                self.assert_rejected(self.validate(sql))

    def test_writes_ddl_and_non_query_commands_are_rejected(self):
        for sql in (
            "DELETE FROM public.invoice",
            "UPDATE public.invoice SET total = 0",
            "INSERT INTO public.invoice(order_id) VALUES ('attack')",
            "DROP TABLE public.invoice",
            "CREATE TABLE business.attack(id integer)",
            "SET statement_timeout = 0",
            "CALL business.attack()",
        ):
            with self.subTest(sql=sql):
                self.assert_rejected(self.validate(sql))

    def test_uncontrolled_query_structures_are_rejected(self):
        variants = {
            "cte": "WITH unused AS (SELECT 1) " + BASE_SQL,
            "join": BASE_SQL.replace("AS o WHERE", "AS o JOIN public.customer r USING (billing_country) WHERE"),
            "subquery": BASE_SQL.replace("public.invoice AS o", "(SELECT * FROM public.invoice) AS o"),
            "union": BASE_SQL + " UNION SELECT 0 AS sales_amount",
            "group_by": BASE_SQL.replace(" LIMIT", " GROUP BY o.billing_country LIMIT"),
            "distinct": BASE_SQL.replace("SELECT COALESCE", "SELECT DISTINCT COALESCE"),
            "window": BASE_SQL.replace("SUM(o.total)", "SUM(o.total) OVER ()"),
            "extra_projection": BASE_SQL.replace(" AS sales_amount FROM", " AS sales_amount, COUNT(*) AS n FROM"),
            "locking": BASE_SQL + " FOR UPDATE",
            "select_into": BASE_SQL.replace(" FROM public.invoice", " INTO TEMP attack FROM public.invoice"),
        }
        for label, sql in variants.items():
            with self.subTest(shape=label):
                self.assert_rejected(self.validate(sql))

    def test_wrong_table_projection_function_cast_or_literal_are_rejected(self):
        variants = {
            "wrong_table": BASE_SQL.replace("public.invoice", "public.invoice_line"),
            "wrong_schema": BASE_SQL.replace("public.invoice", "public.orders"),
            "wrong_amount": BASE_SQL.replace("SUM(o.total)", "SUM(o.bad_amount)"),
            "wrong_aggregate": BASE_SQL.replace("SUM(o.total)", "AVG(o.total)"),
            "no_coalesce": BASE_SQL.replace("COALESCE(SUM(o.total), 0)", "SUM(o.total)"),
            "wrong_coalesce": BASE_SQL.replace("SUM(o.total), 0", "SUM(o.total), 1"),
            "wrong_output_name": BASE_SQL.replace("AS sales_amount", "AS unexpected_amount"),
            "extra_function": BASE_SQL.replace("SUM(o.total)", "SUM(abs(o.total))"),
            "sleep": BASE_SQL.replace("SUM(o.total)", "SUM(pg_sleep(1))"),
            "cast": BASE_SQL.replace("o.billing_country = :filter_0", "o.billing_country = CAST(:filter_0 AS text)"),
            "literal_entity": BASE_SQL.replace(":filter_0", "'USA'"),
            "quoted_parameter": BASE_SQL.replace(":filter_0", "':filter_0'"),
        }
        for label, sql in variants.items():
            with self.subTest(change=label):
                self.assert_rejected(self.validate(sql))

    def test_missing_or_repurposed_conditions_and_or_are_rejected(self):
        variants = {
            "missing_start": BASE_SQL.replace("o.invoice_date >= :start AND ", ""),
            "missing_end": BASE_SQL.replace("AND o.invoice_date < :end ", ""),
            "missing_entity": BASE_SQL.replace("AND o.billing_country = :filter_0 ", ""),
            "inclusive_end": BASE_SQL.replace("o.invoice_date < :end", "o.invoice_date <= :end"),
            "exclusive_start": BASE_SQL.replace("o.invoice_date >= :start", "o.invoice_date > :start"),
            "wrong_time_field": BASE_SQL.replace("o.invoice_date", "o.bad_date"),
            "wrong_entity_field": BASE_SQL.replace("o.billing_country", "o.customer_id"),
            "swapped_parameters": BASE_SQL.replace(":start", ":temporary").replace(":end", ":start").replace(":temporary", ":end"),
            "or": BASE_SQL.replace("AND o.billing_country", "OR o.billing_country"),
            "tautology": BASE_SQL.replace(" LIMIT", " OR 1 = 1 LIMIT"),
            "duplicate": BASE_SQL.replace(" LIMIT", " AND o.billing_country = :filter_0 LIMIT"),
        }
        for label, sql in variants.items():
            with self.subTest(change=label):
                self.assert_rejected(self.validate(sql))

    def test_missing_literal_or_arithmetic_limit_and_offset_are_rejected(self):
        variants = (
            BASE_SQL.replace(" LIMIT :fetch_limit", ""),
            BASE_SQL.replace("LIMIT :fetch_limit", "LIMIT 1000000"),
            BASE_SQL.replace("LIMIT :fetch_limit", "LIMIT ALL"),
            BASE_SQL.replace("LIMIT :fetch_limit", "LIMIT :fetch_limit + 1"),
            BASE_SQL + " OFFSET 1",
        )
        for sql in variants:
            with self.subTest(sql=sql):
                self.assert_rejected(self.validate(sql))

    def test_parameter_set_and_candidate_parameter_declaration_must_match(self):
        missing = copy.deepcopy(self.scalar.parameters)
        del missing["filter_0"]
        extra = {**self.scalar.parameters, "unused": "attack"}
        for parameters in (missing, extra):
            with self.subTest(keys=sorted(parameters)):
                self.assert_rejected(self.validate(parameters=parameters))
        altered_candidate = replace(self.candidate(BASE_SQL), parameter_names=("status", "start", "end", "fetch_limit"))
        self.assert_rejected(self.validate(candidate=altered_candidate))
        self.assert_rejected(self.validate(BASE_SQL.replace(":filter_0", ":unexpected")))

    def test_scalar_date_status_and_limit_parameter_values_cannot_be_tampered(self):
        for name, value in (
            ("filter_0", "Canada"),
            ("start", "2023-01-01T00:00:00+08:00"),
            ("end", "2026-01-01T00:00:00+08:00"),
            ("fetch_limit", 1000000),
        ):
            with self.subTest(parameter=name):
                parameters = copy.deepcopy(self.scalar.parameters)
                parameters[name] = value
                self.assert_rejected(self.validate(parameters=parameters), "PARAMETER_VALUE_MISMATCH")

    def test_array_parameter_values_cannot_be_tampered(self):
        prepared = self.prepare([{ "field_id": "country", "op": "in", "value": ["美国", "加拿大"] }])
        sql = BASE_SQL.replace("o.billing_country = :filter_0", "o.billing_country = ANY(:filter_0)")
        parameters = copy.deepcopy(prepared.parameters)
        parameters["filter_0"].append("Brazil")
        self.assert_rejected(
            self.validate(sql, prepared=prepared, parameters=parameters),
            "PARAMETER_VALUE_MISMATCH",
        )

    def test_float_or_boolean_result_limit_is_rejected_even_if_numerically_equal(self):
        for value in (11.0, True, False):
            with self.subTest(value=value):
                parameters = copy.deepcopy(self.scalar.parameters)
                parameters["fetch_limit"] = value
                self.assert_rejected(self.validate(parameters=parameters), "PARAMETER_VALUE_MISMATCH")

    def test_array_bindings_require_a_list_of_strings(self):
        prepared = self.prepare([{ "field_id": "country", "op": "in", "value": ["美国", "加拿大"] }])
        sql = BASE_SQL.replace("o.billing_country = :filter_0", "o.billing_country = ANY(:filter_0)")
        for value in (["USA", 1], ["USA", True], ["USA", None], ("USA", "Canada")):
            with self.subTest(value=value):
                parameters = copy.deepcopy(prepared.parameters)
                parameters["filter_0"] = value
                self.assert_rejected(
                    self.validate(sql, prepared=prepared, parameters=parameters),
                    "PARAMETER_VALUE_MISMATCH",
                )

    def test_missing_fingerprint_and_incompatible_or_missing_columns_are_rejected(self):
        for value in (None, "", "A" * 64):
            with self.subTest(fingerprint=value):
                schema = copy.deepcopy(self.scalar.schema_context)
                schema["schema_fingerprint_sha256"] = value
                self.assert_rejected(self.validate(schema=schema), "METADATA_ERROR")
        schema = copy.deepcopy(self.scalar.schema_context)
        del schema["schema_fingerprint_sha256"]
        self.assert_rejected(self.validate(schema=schema), "METADATA_ERROR")
        self.assert_rejected(self.validate(candidate=replace(self.candidate(BASE_SQL), schema_fingerprint_sha256=None)))

        for field, incompatible_type in (
            ("total", "text"),
            ("invoice_date", "timestamp with time zone"),
            ("billing_country", "integer"),
        ):
            with self.subTest(field=field):
                schema = copy.deepcopy(self.scalar.schema_context)
                row = next(item for item in schema["columns"] if item["name"] == field)
                row["data_type"] = incompatible_type
                self.assert_rejected(self.validate(schema=schema), "METADATA_ERROR")
                schema["columns"] = [item for item in schema["columns"] if item["name"] != field]
                self.assert_rejected(self.validate(schema=schema), "METADATA_ERROR")
        schema = copy.deepcopy(self.scalar.schema_context)
        schema["columns"].append(copy.deepcopy(schema["columns"][0]))
        self.assert_rejected(self.validate(schema=schema), "METADATA_ERROR")

    def test_candidate_identity_and_schema_fingerprint_must_match(self):
        candidate = self.candidate(BASE_SQL)
        for altered in (
            replace(candidate, source_id="unregistered_db"),
            replace(candidate, metric_definition_id="net_chinook_sales_amount_v1"),
            replace(candidate, schema_fingerprint_sha256="0" * 64),
        ):
            with self.subTest(candidate=altered):
                self.assert_rejected(self.validate(candidate=altered))
        schema = copy.deepcopy(self.scalar.schema_context)
        schema["schema_fingerprint_sha256"] = "1" * 64
        self.assert_rejected(self.validate(schema=schema))

    def test_execution_parameters_are_isolated_from_input_and_each_returned_copy(self):
        prepared = self.prepare([{ "field_id": "country", "op": "in", "value": ["美国", "加拿大"] }])
        sql = BASE_SQL.replace("o.billing_country = :filter_0", "o.billing_country = ANY(:filter_0)")
        parameters = copy.deepcopy(prepared.parameters)
        artifact = self.assert_passed(self.validate(sql, prepared=prepared, parameters=parameters), prepared, sql)

        parameters["filter_0"].append("Brazil")
        parameters["unexpected"] = "cancelled"
        first_view = artifact.parameters
        first_view["filter_0"].append("Germany")
        first_view["fetch_limit"] = 1000000

        self.assertEqual(artifact.parameters["filter_0"], ["USA", "Canada"])
        self.assertNotIn("unexpected", artifact.parameters)
        self.assertEqual(artifact.parameters["fetch_limit"], 11)

    def test_validation_does_not_modify_the_candidate_request_schema_or_parameters(self):
        candidate = self.candidate(BASE_SQL)
        before_candidate = candidate.as_dict()
        before_request = copy.deepcopy(self.scalar.generation_request)
        before_schema = copy.deepcopy(self.scalar.schema_context)
        before_parameters = copy.deepcopy(self.scalar.parameters)

        self.assert_passed(self.validate(candidate=candidate), self.scalar, BASE_SQL)

        self.assertEqual(candidate.as_dict(), before_candidate)
        self.assertEqual(self.scalar.generation_request, before_request)
        self.assertEqual(self.scalar.schema_context, before_schema)
        self.assertEqual(self.scalar.parameters, before_parameters)


if __name__ == "__main__":
    unittest.main()
