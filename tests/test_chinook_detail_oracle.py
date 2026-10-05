"""Independent full-source reference regression; no model or DB access."""
import copy
import tempfile
import unittest
from pathlib import Path

from agent_core.evaluation.chinook_detail_example import FULL_SHA256, SOURCE_SHA256
from agent_core.evaluation.chinook_detail_oracle import detail_gold, full_source_hash, parse_full_chinook_sql
from agent_core.evaluation.contracts import EvaluationError

ROOT = Path(__file__).resolve().parents[1]


class FullSourceOracleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.reference = parse_full_chinook_sql(ROOT / "database/Chinook_PostgreSql.sql")

    def test_all_source_rows_fields_and_hash_are_frozen(self):
        self.assertEqual(len(self.reference["schema"]), 11)
        self.assertEqual(sum(len(v["columns"]) for v in self.reference["schema"].values()), 64)
        self.assertEqual(sum(len(v) for v in self.reference["tables"].values()), 15607)
        self.assertEqual(full_source_hash(self.reference), FULL_SHA256)

    def test_same_counts_with_changed_value_changes_full_hash(self):
        changed = copy.deepcopy(self.reference)
        changed["tables"]["track"][0]["name"] += " changed"
        self.assertNotEqual(full_source_hash(changed), FULL_SHA256)

    def test_all_pages_of_composite_primary_key_cover_source_once(self):
        rows = []
        offset = 0
        while True:
            page = detail_gold({"table": "playlist_track", "page": {"size": 1000, "offset": offset}}, self.reference)
            rows += page["rows"]
            if not page["has_more"]:
                self.assertIsNone(page["next_offset"])
                break
            offset = page["next_offset"]
        self.assertEqual(rows, self.reference["tables"]["playlist_track"])
        self.assertEqual(len({(r["playlist_id"], r["track_id"]) for r in rows}), 8715)

    def test_typed_null_sort_and_filter(self):
        page = detail_gold({"table": "employee", "order_by": [{"column": "reports_to", "direction": "desc"}]}, self.reference)
        self.assertIsNone(page["rows"][-1]["reports_to"])
        nulls = detail_gold({"table": "employee", "filters": [{"column": "reports_to", "op": "is_null"}]}, self.reference)
        self.assertEqual(nulls["row_count"], 1)
        self.assertIsNone(nulls["rows"][0]["reports_to"])

    def test_literal_text_search_does_not_interpret_like_wildcards(self):
        changed = copy.deepcopy(self.reference)
        changed["tables"]["genre"][0]["name"] = "literal %_\\' text"
        page = detail_gold({"table": "genre", "filters": [{"column": "name", "op": "contains", "value": "%_\\'"}]}, changed)
        self.assertEqual(page["row_count"], 1)

    def test_decimal_order_is_numeric_and_projection_order_is_retained(self):
        page = detail_gold({"table": "track", "columns": ["unit_price", "track_id"],
                            "filters": [{"column": "unit_price", "op": "gte", "value": "1.99"}],
                            "order_by": [{"column": "unit_price", "direction": "desc"}]}, self.reference)
        self.assertGreater(page["row_count"], 0)
        self.assertEqual([column["name"] for column in page["columns"]], ["unit_price", "track_id"])
        self.assertEqual(page["rows"][0]["unit_price"], "1.99")

    def test_half_open_naive_calendar_dates(self):
        page = detail_gold({"table": "invoice", "page": {"size": 1000}, "filters": [
            {"column": "invoice_date", "op": "gte", "value": "2024-01-01"},
            {"column": "invoice_date", "op": "lt", "value": "2025-01-01"}]}, self.reference)
        self.assertTrue(all("2024-" in row["invoice_date"] for row in page["rows"]))
        self.assertEqual(page["row_count"], 83)

    def test_empty_page_is_empty_without_next_offset(self):
        page = detail_gold({"table": "genre", "page": {"size": 10, "offset": 1000}}, self.reference)
        self.assertEqual(page["rows"], [])
        self.assertFalse(page["has_more"])
        self.assertIsNone(page["next_offset"])

    def test_seed_expressions_are_rejected_without_execution(self):
        text = (ROOT / "database/Chinook_PostgreSql.sql").read_text(encoding="utf-8-sig")
        import re
        text = re.sub(r"(INSERT INTO artist \([^)]+\) VALUES\s*\()\d+", r"\g<1>pg_sleep(1)", text, count=1)
        with tempfile.TemporaryDirectory(dir=ROOT / "tmp") as directory:
            path = Path(directory) / "source.sql"
            path.write_text(text, encoding="utf-8")
            with self.assertRaises(EvaluationError):
                parse_full_chinook_sql(path)


if __name__ == "__main__":
    unittest.main()
