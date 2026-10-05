"""Offline Chinook table-detail contracts, typed bindings and canonical SQL."""
from __future__ import annotations

import copy
import json
import unittest
from dataclasses import FrozenInstanceError
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from database.detail_contracts import DetailCandidate, DetailValidationResult, ValidatedDetailSQL
from database.detail_query import (
    TABLES, DetailQueryMapper, canonical_sql, output_columns, prepare_detail, prepare_detail_request,
)
from database.sql_common import ServiceFault

ROOT = Path(__file__).resolve().parents[1]


def query(table="public.track", **kwargs):
    return {"task": "table_detail", "source_id": "chinook_db", "table": table, **kwargs}


class DetailQueryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.raw = json.loads((ROOT / "database/generated/chinook_schema_snapshot.json").read_text(encoding="utf-8-sig"))
        cls.source = json.loads((ROOT / "database/source_config.json").read_text(encoding="utf-8-sig"))

    def setUp(self):
        self.snapshot, self.config = copy.deepcopy(self.raw), copy.deepcopy(self.source)
        for name in ("psycopg.connect", "psycopg.AsyncConnection.connect",
                     "database.sql_common.resolve_connection_parameters"):
            guard = patch(name, side_effect=AssertionError("Detail query tests forbid DB/credentials"))
            mocked = guard.start()
            self.addCleanup(guard.stop)
            self.addCleanup(mocked.assert_not_called)

    def prepare(self, request=None, limits=None):
        return prepare_detail(query() if request is None else request, self.snapshot, self.config, limits)

    def assert_fault(self, request, code="INVALID_QUERY"):
        with self.assertRaises(ServiceFault) as caught:
            self.prepare(request)
        self.assertEqual(caught.exception.code, code)
        self.assertFalse(caught.exception.execution_attempted)
        return caught.exception

    def test_all_eleven_tables_expand_all_sixty_four_columns_in_structural_order(self):
        total = 0
        for table in TABLES:
            with self.subTest(table=table):
                prepared = self.prepare(query(table))
                expected = sorted((row for row in self.snapshot["columns"]
                                   if "public." + row["table_name"] == table), key=lambda row: row["ordinal_position"])
                self.assertEqual(prepared.generation_request["columns"], [row["column_name"] for row in expected])
                self.assertEqual(prepared.generation_request["page"], {"size": 100, "offset": 0})
                self.assertEqual(prepared.parameters, {"fetch_limit": 101, "page_offset": 0})
                sql = canonical_sql(prepared.generation_request)
                self.assertNotIn("SELECT *", sql)
                self.assertIn(f"FROM {table} AS t", sql)
                self.assertNotIn(" WHERE ", sql)
                total += len(expected)
        self.assertEqual(total, 64)

    def test_explicit_projection_order_and_unprojected_sort_columns_are_preserved(self):
        prepared = self.prepare(query(columns=["name", "composer"], order_by=[{"column": "milliseconds", "direction": "desc"}]))
        self.assertEqual(prepared.generation_request["columns"], ["name", "composer"])
        sql = canonical_sql(prepared.generation_request)
        self.assertTrue(sql.startswith("SELECT t.name, t.composer "))
        self.assertIn("ORDER BY t.milliseconds DESC NULLS LAST, t.track_id ASC NULLS LAST", sql)
        self.assertEqual(output_columns(prepared.generation_request, prepared.schema_context), [
            {"name": "name", "data_type": "text", "nullable": False},
            {"name": "composer", "data_type": "text", "nullable": True},
        ])

    def test_short_name_is_resolved_only_to_a_controlled_chinook_table(self):
        self.assertEqual(self.prepare(query("track")).generation_request["table"], "public.track")
        for name in ("business.orders", "public.pg_roles", "public.invoice; DELETE FROM public.invoice", "\"track\"", "TRACK"):
            self.assert_fault(query(name), "TABLE_NOT_ALLOWED")
        for name in (1, [], {}):
            self.assert_fault(query(name))

    def test_missing_table_clarifies_all_authorized_candidates_without_sql(self):
        for name in (None, "", "  "):
            result = DetailQueryMapper(self.config).match(query(name), self.snapshot, {"request_id": "missing-table"})
            self.assertEqual(result["status"], "needs_clarification")
            self.assertEqual(result["issues"][0]["code"], "MISSING_TABLE")
            self.assertEqual({row["id"] for row in result["issues"][0]["candidates"]}, set(TABLES))
            self.assertFalse(result["execution_attempted"])
            self.assertNotIn("prepared_detail", result)

    def test_configuration_and_source_cannot_authorize_other_databases_or_tables(self):
        self.assert_fault(query(source_id="sales_db"), "UNKNOWN_SOURCE")
        for field, value in (("source_id", "sales_db"), ("dataset", "sales"), ("database", "sales_db"),
                             ("query_table_allowlist", ["public.track", "public.pg_roles"])):
            config = {**self.config, field: value}
            with self.subTest(field=field), self.assertRaises(ServiceFault):
                prepare_detail(query(), self.snapshot, config)
        self.config["query_table_allowlist"] = ["public.album"]
        self.assert_fault(query(), "TABLE_NOT_ALLOWED")

    def test_input_has_no_free_sql_or_undeclared_properties(self):
        for request in (query(sql="SELECT * FROM public.track"), query(task="arbitrary_sql"),
                        query(question=1), query(metric_definition_id="chinook_sales_amount_v1")):
            self.assert_fault(request)

    def test_projection_rejects_duplicates_unknowns_expressions_and_empty_lists(self):
        for columns in ([], ["name", "name"], ["password"], ["t.name"], ["SUM(unit_price)"], "name", [True]):
            self.assert_fault(query(columns=columns), "COLUMN_NOT_ALLOWED")

    def test_primary_key_is_always_completed_for_composite_key_tables(self):
        default = self.prepare(query("playlist_track"))
        self.assertEqual(default.schema_context["primary_key"], ["playlist_id", "track_id"])
        self.assertEqual(default.generation_request["order_by"], [
            {"column": "playlist_id", "direction": "asc"}, {"column": "track_id", "direction": "asc"},
        ])
        explicit = self.prepare(query("playlist_track", order_by=[{"column": "track_id", "direction": "DESC"}]))
        self.assertEqual(explicit.generation_request["order_by"], [
            {"column": "track_id", "direction": "desc"}, {"column": "playlist_id", "direction": "asc"},
        ])
        self.assertEqual(canonical_sql(explicit.generation_request).count("NULLS LAST"), 2)

    def test_sort_rejects_duplicate_unknown_column_and_free_sql_direction(self):
        for order in ([{"column": "name", "direction": "desc; DELETE"}],
                      [{"column": "unknown", "direction": "asc"}],
                      [{"column": "name", "direction": "asc"}, {"column": "name", "direction": "desc"}],
                      [{"column": "name", "direction": "asc", "nulls": "first"}], "name"):
            self.assert_fault(query(order_by=order))

    def test_page_limits_and_parameterized_offset(self):
        prepared = self.prepare(query(page={"size": 1000, "offset": 1_000_000}))
        self.assertEqual(prepared.parameters, {"fetch_limit": 1001, "page_offset": 1_000_000})
        self.assertIn("LIMIT :fetch_limit OFFSET :page_offset", canonical_sql(prepared.generation_request))
        self.assertIn("LIMIT %(fetch_limit)s OFFSET %(page_offset)s", canonical_sql(prepared.generation_request, named=False))
        for page in ({"size": 0}, {"size": True}, {"size": 1001}, {"offset": -1},
                     {"offset": 1_000_001}, {"offset": 0.0}, {"cursor": "unknown"}):
            self.assert_fault(query(page=page))

    def test_explicit_service_limit_caps_page_without_changing_offset(self):
        prepared = self.prepare(query(page={"size": 500, "offset": 20}), {"max_rows": 10})
        self.assertEqual(prepared.generation_request["page"], {"size": 10, "offset": 20})
        self.assertEqual(prepared.parameters, {"fetch_limit": 11, "page_offset": 20})

    def test_integer_predicates_preserve_true_ints_and_complete_and_conditions(self):
        filters = [{"column": "milliseconds", "op": "gte", "value": 180000},
                   {"column": "milliseconds", "op": "lt", "value": 240000},
                   {"column": "track_id", "op": "in", "value": [1, 2, 1]}]
        prepared = self.prepare(query(filters=filters))
        self.assertEqual(prepared.parameters["filter_0"], 180000)
        self.assertEqual(prepared.parameters["filter_2"], [1, 2])
        self.assertEqual([spec.data_type for spec in prepared.parameter_specs[:3]], ["integer", "integer", "integer[]"])
        sql = canonical_sql(prepared.generation_request)
        self.assertIn("t.milliseconds >= :filter_0 AND t.milliseconds < :filter_1 AND t.track_id = ANY(:filter_2)", sql)
        for value in (True, 1.0, "1", 2147483648, -2147483649):
            self.assert_fault(query(filters=[{"column": "track_id", "op": "eq", "value": value}]))

    def test_decimal_filters_are_exact_bound_decimals_and_not_float(self):
        prepared = self.prepare(query(filters=[{"column": "unit_price", "op": "in", "value": ["0.99", Decimal("1.99")]}]))
        self.assertEqual(prepared.generation_request["filters"][0]["value"], ["0.99", "1.99"])
        self.assertEqual(prepared.parameters["filter_0"], [Decimal("0.99"), Decimal("1.99")])
        self.assertEqual(prepared.parameter_specs[0].data_type, "decimal[]")
        for value in (0.99, True, "0.001", "1E-1000000000", "1E+1000000000", "NaN", "Infinity", "100000000.00"):
            self.assert_fault(query(filters=[{"column": "unit_price", "op": "eq", "value": value}]))

    def test_timestamp_filters_preserve_naive_calendar_and_reject_offsets(self):
        prepared = self.prepare(query("employee", filters=[{"column": "hire_date", "op": "gte", "value": "2002-01-01"}]))
        self.assertEqual(prepared.parameters["filter_0"], datetime(2002, 1, 1))
        self.assertEqual(prepared.generation_request["filters"][0]["value"], "2002-01-01T00:00:00")
        for value in ("2002-01-01T00:00:00Z", datetime(2002, 1, 1, tzinfo=timezone.utc)):
            fault = self.assert_fault(query("employee", filters=[{"column": "hire_date", "op": "gte", "value": value}]),
                                      "AMBIGUOUS_TIME_SEMANTICS")
            self.assertEqual(fault.status, "needs_clarification")

    def test_text_patterns_escape_percent_underscore_and_backslash_literally(self):
        prepared = self.prepare(query(filters=[{"column": "name", "op": "contains", "value": "a%_\\'音乐"},
                                               {"column": "composer", "op": "starts_with", "value": "B_"}]))
        self.assertEqual(prepared.parameters["filter_0"], "%a\\%\\_\\\\'音乐%")
        self.assertEqual(prepared.parameters["filter_1"], "B\\_%")
        sql = canonical_sql(prepared.generation_request)
        self.assertIn("LIKE :filter_0 ESCAPE '\\'", sql)
        self.assertNotIn("'音乐", sql)
        self.assertNotIn("B_", sql)

    def test_null_operations_have_no_binding_and_keep_filter_index_numbering(self):
        prepared = self.prepare(query(filters=[{"column": "composer", "op": "is_null", "value": None},
                                               {"column": "album_id", "op": "is_not_null"},
                                               {"column": "name", "op": "ne", "value": ""}]))
        self.assertEqual(set(prepared.parameters), {"filter_2", "fetch_limit", "page_offset"})
        self.assertNotIn("value", prepared.generation_request["filters"][0])
        sql = canonical_sql(prepared.generation_request)
        self.assertIn("t.composer IS NULL AND t.album_id IS NOT NULL AND t.name <> :filter_2", sql)
        self.assert_fault(query(filters=[{"column": "composer", "op": "is_null", "value": "not null"}]))

    def test_missing_values_clarify_and_nulls_in_arrays_do_not_mimic_equality(self):
        for condition in ({"column": "name", "op": "eq"}, {"column": "name", "op": "eq", "value": None}):
            fault = self.assert_fault(query(filters=[condition]), "MISSING_FILTER_VALUE")
            self.assertEqual(fault.status, "needs_clarification")
        for values in ([], [1, None], [1, True], [1] * 51, "1"):
            self.assert_fault(query(filters=[{"column": "track_id", "op": "in", "value": values}]))

    def test_filter_operations_are_limited_by_column_type(self):
        for column, op, value in (("name", "gte", "Z"), ("track_id", "contains", 1),
                                   ("unit_price", "starts_with", "0.9"), ("name", "regex", "a.*")):
            self.assert_fault(query(filters=[{"column": column, "op": op, "value": value}]), "UNSUPPORTED_FILTER")

    def test_malicious_text_is_parameter_data_and_never_enters_sql(self):
        value = "x'; DROP TABLE public.track; --"
        prepared = self.prepare(query(filters=[{"column": "name", "op": "eq", "value": value}]))
        self.assertEqual(prepared.parameters["filter_0"], value)
        self.assertNotIn(value, canonical_sql(prepared.generation_request, named=False))
        self.assert_fault(query(filters=[{"column": "name", "op": "eq", "value": "x\x00"}]))

    def test_normalized_requests_rebuild_identical_bindings_without_mutating_inputs(self):
        original = query("invoice", filters=[{"column": "invoice_date", "op": "gte", "value": "2024-01-01"},
                                            {"column": "total", "op": "ne", "value": "0"}])
        before = copy.deepcopy((original, self.snapshot, self.config))
        prepared = self.prepare(original)
        rebuilt = prepare_detail_request(prepared.generation_request, prepared.schema_context)
        self.assertEqual(rebuilt, prepared)
        self.assertEqual((original, self.snapshot, self.config), before)
        json.dumps(prepared.generation_request, allow_nan=False)

    def test_changed_task_projection_source_or_incomplete_sort_cannot_rebuild(self):
        prepared = self.prepare(query(columns=["name"], filters=[{"column": "unit_price", "op": "eq", "value": "0.99"}]))
        variants = ({"task": "sum"}, {"source_id": "sales_db"}, {"table": "public.album"},
                    {"columns": "all"}, {"order_by": []}, {"filters": [{"column": "unit_price", "op": "eq", "value": 0.99}]})
        for fields in variants:
            with self.subTest(fields=fields), self.assertRaises(ServiceFault):
                prepare_detail_request({**prepared.generation_request, **fields}, prepared.schema_context)

    def test_missing_or_inconsistent_live_columns_primary_keys_and_types_are_blocked(self):
        modifications = (
            lambda s: s["columns"].append(copy.deepcopy(next(row for row in s["columns"] if row["table_name"] == "track"))),
            lambda s: next(row for row in s["columns"] if row["table_name"] == "track").update(data_type="jsonb"),
            lambda s: next(row for row in s["columns"] if row["table_name"] == "track").update(is_nullable=True),
            lambda s: s.update(keys=[row for row in s["keys"] if row["table_name"] != "track"]),
            lambda s: next(row for row in s["keys"] if row["table_name"] == "track").update(definition="PRIMARY KEY (track_id); DELETE FROM track"),
        )
        for modify in modifications:
            with self.subTest(modify=modify):
                self.snapshot = copy.deepcopy(self.raw)
                modify(self.snapshot)
                self.assert_fault(query(), "METADATA_ERROR")

    def test_structural_fingerprint_changes_only_with_structure_not_capture_time(self):
        before = self.prepare().schema_context["schema_fingerprint_sha256"]
        self.snapshot["captured_at"] = "2030-01-01"
        self.assertEqual(self.prepare().schema_context["schema_fingerprint_sha256"], before)
        next(row for row in self.snapshot["columns"] if row["table_name"] == "track" and row["column_name"] == "name")["data_type"] = "character varying(201)"
        self.assertNotEqual(self.prepare().schema_context["schema_fingerprint_sha256"], before)

    def test_schema_context_rejects_type_mismatch_and_invalid_fingerprint(self):
        prepared = self.prepare()
        for modify in (lambda s: s.update(schema_fingerprint_sha256="invalid"),
                       lambda s: s["columns"][0].update(data_type="text"),
                       lambda s: s["columns"][0].update(physical_data_type=[]),
                       lambda s: s.update(primary_key=["not_a_column"])):
            changed = copy.deepcopy(prepared.schema_context)
            modify(changed)
            with self.assertRaises(ServiceFault) as caught:
                prepare_detail_request(prepared.generation_request, changed)
            self.assertEqual(caught.exception.code, "METADATA_ERROR")

    def test_mapper_keeps_session_ids_and_clear_failure_structure(self):
        mapper = DetailQueryMapper(self.config)
        ready = mapper.match(query(), self.snapshot, {"request_id": "detail-test"})
        self.assertEqual(ready["status"], "ready")
        self.assertEqual(ready["request_id"], "detail-test")
        self.assertEqual(ready["sql_generation_request"], ready["prepared_detail"].generation_request)
        for context in ({"request_id": 1}, [], {"max_rows": True}):
            failure = mapper.match(query(), self.snapshot, context)
            self.assertEqual(failure["status"], "error")
            self.assertFalse(failure["execution_attempted"])


class DetailContractTests(unittest.TestCase):
    def test_candidate_is_explicitly_unvalidated_and_serializes_parameter_names(self):
        candidate = DetailCandidate("SELECT t.name", "chinook_db", "public.track", ("fetch_limit",), "a" * 64)
        self.assertFalse(candidate.validated)
        self.assertEqual(candidate.as_dict()["parameter_names"], ["fetch_limit"])
        self.assertEqual(candidate.task, "table_detail")
        with self.assertRaises(FrozenInstanceError):
            candidate.sql = "DELETE FROM public.track"
        self.assertFalse(DetailValidationResult("passed").passed)

    def test_validated_package_copies_constructor_and_all_output_mutations(self):
        parameters = (("filter_0", [1, 2]), ("fetch_limit", 11), ("page_offset", 0))
        headers = ({"name": "track_id", "data_type": "integer", "nullable": False},)
        package = ValidatedDetailSQL("SQL", "candidate", "chinook_db", "public.track", "a" * 64,
                                     "detail-v1", "b" * 64, "c" * 64, 10, 5000, 0, parameters, headers)
        parameters[0][1].append(3)
        headers[0]["name"] = "altered"
        self.assertEqual(package.parameters["filter_0"], [1, 2])
        self.assertEqual(package.output_columns[0]["name"], "track_id")
        result = package.as_dict()
        result["parameters"]["filter_0"].append(4)
        result["output_columns"][0]["name"] = "changed"
        self.assertEqual(package.parameters["filter_0"], [1, 2])
        self.assertEqual(package.output_columns[0]["name"], "track_id")
        self.assertEqual(package.as_dict()["page"], {"size": 10, "offset": 0})
        self.assertTrue(DetailValidationResult("passed", package).passed)


if __name__ == "__main__":
    unittest.main(verbosity=2)
