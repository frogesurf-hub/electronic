"""明细 SQL 安全及精确请求匹配；不连接数据库。"""
from __future__ import annotations

import copy
import json
import unittest
from dataclasses import replace
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from database.detail_contracts import DetailCandidate
from database.detail_query import TABLES, canonical_sql, prepare_detail
from database.detail_validator import DetailSQLValidator

_DATABASE = Path(__file__).resolve().parents[1] / "database"


class DetailValidatorTests(unittest.TestCase):
    def setUp(self):
        for target in ("psycopg.connect", "psycopg.AsyncConnection.connect", "database.sql_common.resolve_connection_parameters"):
            guard = patch(target, side_effect=AssertionError("离线 SQL 检验禁止连接或访问凭据"))
            mock = guard.start()
            self.addCleanup(guard.stop)
            self.addCleanup(mock.assert_not_called)
        self.snapshot = json.loads((_DATABASE / "generated/chinook_schema_snapshot.json").read_text(encoding="utf-8-sig"))
        self.config = json.loads((_DATABASE / "source_config.json").read_text(encoding="utf-8-sig"))
        self.prepared = self.prepare("track", filters=[{"column": "milliseconds", "op": "gte", "value": 1000}], page={"size": 2, "offset": 3})

    def prepare(self, table, **parts):
        return prepare_detail({"task": "table_detail", "table": table, **parts}, self.snapshot, self.config)

    def candidate(self, prepared=None, sql=None):
        prepared = self.prepared if prepared is None else prepared
        return DetailCandidate(sql=canonical_sql(prepared.generation_request) if sql is None else sql,
                               source_id="chinook_db", table=prepared.generation_request["table"],
                               parameter_names=tuple(prepared.parameters),
                               schema_fingerprint_sha256=prepared.schema_context["schema_fingerprint_sha256"])

    def validate(self, prepared=None, candidate=None, parameters=None, validator=None):
        prepared = self.prepared if prepared is None else prepared
        return (DetailSQLValidator() if validator is None else validator).validate(
            self.candidate(prepared) if candidate is None else candidate,
            prepared.generation_request, prepared.schema_context,
            prepared.parameters if parameters is None else parameters)

    def test_all_eleven_tables_all_columns_and_composite_primary_key_pass(self):
        for table in TABLES:
            with self.subTest(table=table):
                prepared = self.prepare(table)
                result = self.validate(prepared)
                self.assertTrue(result.passed, result.as_dict())
                self.assertEqual([c["name"] for c in result.validated_sql.output_columns], prepared.generation_request["columns"])
                self.assertEqual(result.validated_sql.parameters["fetch_limit"], 101)
                self.assertIn("%(page_offset)s", result.validated_sql.sql)
        prepared = self.prepare("playlist_track")
        self.assertEqual(prepared.generation_request["order_by"], [
            {"column": "playlist_id", "direction": "asc"}, {"column": "track_id", "direction": "asc"}])

    def test_every_allowed_predicate_and_typed_value_passes(self):
        for operation in ("eq", "ne", "gt", "gte", "lt", "lte", "in", "is_null", "is_not_null"):
            condition = {"column": "milliseconds", "op": operation}
            if operation not in {"is_null", "is_not_null"}:
                condition["value"] = [1000, 2000] if operation == "in" else 1000
            with self.subTest(operation=operation):
                self.assertTrue(self.validate(self.prepare("track", filters=[condition])).passed)
        for operation in ("eq", "ne", "in", "contains", "starts_with", "is_null", "is_not_null"):
            condition = {"column": "composer", "op": operation}
            if operation not in {"is_null", "is_not_null"}:
                condition["value"] = ["A", "B"] if operation == "in" else "a%b_c\\d"
            with self.subTest(operation=operation):
                prepared = self.prepare("track", filters=[condition])
                self.assertTrue(self.validate(prepared).passed)
                if operation == "contains":
                    self.assertEqual(prepared.parameters["filter_0"], "%a\\%b\\_c\\\\d%")
        for column, value, dtype in (("total", "1.23", Decimal), ("invoice_date", "2011-01-01", datetime)):
            prepared = self.prepare("invoice", filters=[{"column": column, "op": "gte", "value": value}])
            self.assertIs(type(prepared.parameters["filter_0"]), dtype)
            self.assertTrue(self.validate(prepared).passed)

    def test_equivalent_alias_parentheses_and_condition_order_pass(self):
        prepared = self.prepare("track", columns=["track_id", "name"], filters=[
            {"column": "name", "op": "contains", "value": "a"},
            {"column": "milliseconds", "op": "gt", "value": 1000},
            {"column": "composer", "op": "is_not_null"}],
            order_by=[{"column": "name", "direction": "desc"}])
        sql = ("SELECT (x.track_id) AS track_id, x.name AS name FROM public.track x "
               "WHERE ((x.milliseconds > :filter_1) AND NOT(x.composer IS NULL)) "
               "AND (x.name LIKE :filter_0 ESCAPE '\\') "
               "ORDER BY x.name DESC NULLS LAST, x.track_id ASC NULLS LAST "
               "LIMIT (:fetch_limit) OFFSET (:page_offset);")
        result = self.validate(prepared, self.candidate(prepared, sql))
        self.assertTrue(result.passed, result.as_dict())
        self.assertEqual(result.validated_sql.sql, canonical_sql(prepared.generation_request, named=False))

    def test_extra_sql_structures_and_semantic_changes_are_rejected(self):
        prepared = self.prepare("track", columns=["track_id", "name"], filters=[{"column": "milliseconds", "op": "gte", "value": 1000}],
                                order_by=[{"column": "name", "direction": "desc"}])
        sql = canonical_sql(prepared.generation_request)
        variants = [
            sql + "; SELECT 1", sql + ";;", sql.replace("SELECT", "DELETE", 1),
            sql.replace("SELECT t.track_id, t.name", "SELECT *"),
            sql.replace("SELECT t.track_id, t.name", "SELECT DISTINCT t.track_id, t.name"),
            sql.replace("t.track_id, t.name", "t.name, t.track_id", 1),
            sql.replace("t.name FROM", "lower(t.name) FROM"),
            sql.replace("public.track AS t", "public.invoice AS t"),
            sql.replace("public.track AS t", "public.track AS t JOIN public.album a ON a.album_id=t.album_id"),
            sql.replace("public.track AS t", "(SELECT * FROM public.track) AS t"),
            sql.replace("t.milliseconds >= :filter_0", "t.milliseconds >= 1000"),
            sql.replace("t.milliseconds >= :filter_0", "t.milliseconds >= :filter_0 OR t.track_id = :filter_0"),
            sql.replace("t.milliseconds >= :filter_0", "t.milliseconds >= :filter_0 AND t.track_id = :filter_0"),
            sql.replace("t.milliseconds >= :filter_0", "t.milliseconds >= :filter_0 AND t.milliseconds >= :filter_0"),
            sql.replace("t.milliseconds >= :filter_0", "t.milliseconds > :filter_0"),
            sql.replace(" WHERE t.milliseconds >= :filter_0", ""),
            sql.replace("DESC NULLS LAST", "DESC"), sql.replace("DESC NULLS LAST", "DESC NULLS FIRST"),
            sql.replace(", t.track_id ASC NULLS LAST", ""),
            sql.replace("LIMIT :fetch_limit", "LIMIT 2"),
            sql.replace(" OFFSET :page_offset", ""), sql.replace("OFFSET :page_offset", "OFFSET :fetch_limit"),
            sql.replace(":filter_0", ": filter_0"), sql.replace(":filter_0", ':"filter_0"'),
            sql.replace(":filter_0", "$1"), sql.replace(":filter_0", ":filter_9"),
            sql + " FOR UPDATE", sql + " UNION SELECT track_id,name FROM public.track",
            sql.replace("t.name FROM", "t.name AS hidden FROM"),
        ]
        for variant in variants:
            with self.subTest(sql=variant):
                result = self.validate(prepared, self.candidate(prepared, variant))
                self.assertFalse(result.passed, result.as_dict())
                self.assertIsNone(result.validated_sql)

    def test_like_requires_exact_escape_and_bound_pattern(self):
        prepared = self.prepare("track", filters=[{"column": "name", "op": "contains", "value": "a%b"}])
        sql = canonical_sql(prepared.generation_request)
        for variant in (sql.replace(" ESCAPE '\\'", ""), sql.replace("ESCAPE '\\'", "ESCAPE '#'"),
                        sql.replace("LIKE :filter_0", "ILIKE :filter_0")):
            self.assertFalse(self.validate(prepared, self.candidate(prepared, variant)).passed)
        self.assertFalse(self.validate(prepared, parameters={**prepared.parameters, "filter_0": "%a%b%"}).passed)

    def test_candidate_context_and_parameters_are_exact(self):
        candidate = self.candidate()
        for variant in (candidate.as_dict(), candidate.sql, replace(candidate, source_id="sales_db"),
                        replace(candidate, table="public.invoice"), replace(candidate, schema_fingerprint_sha256="0" * 64),
                        replace(candidate, parameter_names=("filter_0", "filter_0", "fetch_limit", "page_offset")),
                        replace(candidate, parameter_names=("filter_0", "fetch_limit")), replace(candidate, request_id=123)):
            with self.subTest(variant=type(variant).__name__):
                self.assertFalse(self.validate(candidate=variant).passed)
        for parameters in ({**self.prepared.parameters, "extra": 1}, {**self.prepared.parameters, "filter_0": True},
                           {**self.prepared.parameters, "filter_0": "1000"}, {**self.prepared.parameters, "fetch_limit": 2},
                           {**self.prepared.parameters, "page_offset": 0}):
            self.assertFalse(self.validate(parameters=parameters).passed)

    def test_ast_size_depth_and_length_limits_block_complexity(self):
        candidate = self.candidate()
        for validator in (DetailSQLValidator(max_sql_chars=10), DetailSQLValidator(max_ast_nodes=3), DetailSQLValidator(max_ast_depth=2)):
            self.assertFalse(self.validate(validator=validator).passed)
        sql = candidate.sql.replace("t.milliseconds >= :filter_0", "(" * 97 + "t.milliseconds >= :filter_0" + ")" * 97)
        self.assertEqual(self.validate(candidate=replace(candidate, sql=sql)).error_code, "SQL_TOO_COMPLEX")
        for value in (True, 0, -1, "1"):
            with self.assertRaises(ValueError):
                DetailSQLValidator(max_ast_nodes=value)

    def test_supported_sixty_four_conditions_fit_bounded_ast_budget(self):
        for table, column, operation, value in (("employee", "employee_id", "gte", 1), ("track", "name", "contains", "a")):
            with self.subTest(table=table):
                prepared = self.prepare(table, filters=[{"column": column, "op": operation, "value": value} for _ in range(64)])
                result = self.validate(prepared)
                self.assertTrue(result.passed, result.as_dict())

    def test_verified_packet_properties_and_inputs_are_isolated(self):
        prepared = self.prepare("track", filters=[{"column": "track_id", "op": "in", "value": [1, 2]}])
        before = copy.deepcopy(prepared)
        packet = self.validate(prepared).validated_sql
        packet.parameters["filter_0"].append(3)
        packet.output_columns[0]["nullable"] = True
        self.assertEqual(packet.parameters["filter_0"], [1, 2])
        self.assertFalse(packet.output_columns[0]["nullable"])
        self.assertEqual(prepared, before)


if __name__ == "__main__":
    unittest.main()
