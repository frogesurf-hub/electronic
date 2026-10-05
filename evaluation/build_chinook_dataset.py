"""Freeze the Chinook development suite offline; never execute source SQL or call a model."""
from __future__ import annotations

import hashlib
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agent_core.evaluation.chinook_oracle import (
    CHINOOK_ORACLE_VERSION, CHINOOK_PROJECTION_VERSION, canonical_chinook_snapshot_hash,
    chinook_semantic_metadata_hash, evaluate_chinook_sales, parse_chinook_sql,
)

SUITE_VERSION = "chinook-sql-v1-dev-1"
PERIOD = {"start": "2024-01-01", "end_exclusive": "2025-01-01"}


def query(question, filters=(), *, period=None):
    return {"question": question, "source_id": "chinook_db", "metric_definition_id": "chinook_sales_amount_v1",
            "time_range": dict(PERIOD if period is None else period),
            "filters": [{"field_id": field, "op": op, "value": value} for field, op, value in filters]}


def build_cases(data, config):
    snapshot_id = "sha256:" + canonical_chinook_snapshot_hash(data)
    cases = []

    def append(identifier, kind, family, query_value, expected, context=None, scope="chinook_sql_v1"):
        cases.append({"case_id": identifier, "kind": kind, "family": family, "scope": scope,
                      "query": query_value, "context": context or {}, "expected": expected,
                      "comparison": {"decimal_scale": 2, "order_sensitive": False},
                      "split": "dev", "data_snapshot_id": snapshot_id})

    positives = [
        ("A01", "annual_total", query("2024年发票总额"), {}),
        ("A02", "all_data", query("全部发票总额", period={"start": "2021-01-01", "end_exclusive": "2026-01-01"}), {}),
        ("A03", "country_eq", query("2024年美国发票总额", [("country", "eq", "美国")]), {}),
        ("A04", "country_in_alias_dedup", query("2024年美国与巴西发票总额", [("country", "in", ["USA", "美国", "Brazil"])]), {}),
        ("A05", "city_eq_case", query("2024年巴黎发票总额", [("city", "eq", "paris")]), {}),
        ("A06", "city_in", query("2024年巴黎和伦敦发票总额", [("billing_city", "in", ["Paris", "London"])]), {}),
        ("A07", "customer_integer", query("2024年客户1发票总额", [("customer", "eq", 1)]), {}),
        ("A08", "customer_string_id", query("2024年客户编号1发票总额", [("customer", "eq", "1")]), {}),
        ("A09", "customer_full_name", query("2024年客户完整姓名发票总额", [("customer", "eq", "Luís Gonçalves")]), {}),
        ("A10", "customer_in_dedup", query("2024年客户1与2发票总额", [("customer", "in", [1, 2, "1"])]), {}),
        ("A11", "country_city", query("2024年美国纽约发票总额", [("country", "eq", "USA"), ("city", "eq", "New York")]), {}),
        ("A12", "empty_intersection", query("2024年巴西且账单城市巴黎的发票总额", [("country", "eq", "Brazil"), ("city", "eq", "Paris")]), {}),
        ("A13", "start_inclusive", query("首日发票总额", period={"start": "2021-01-01T00:00:00", "end_exclusive": "2021-01-02T00:00:00"}), {}),
        ("A14", "end_exclusive", query("首日午夜之前不包含首日的发票总额", period={"start": "2020-12-31", "end_exclusive": "2021-01-01"}), {}),
        ("A15", "quarter", query("2024年第一季度发票总额", period={"start": "2024-01-01", "end_exclusive": "2024-04-01"}), {}),
        ("A16", "no_data", query("2099年发票总额无数据返回零", period={"start": "2099-01-01", "end_exclusive": "2100-01-01"}), {}),
        ("A17", "one_row_cap", query("2024年发票总额只需一行"), {"max_rows": 1}),
        ("A18", "national_literal_coercion", query("爱丁堡全部发票总额", [("city", "eq", "Edinburgh")],
                                      period={"start": "2021-01-01", "end_exclusive": "2026-01-01"}), {}),
    ]
    for identifier, family, value, context in positives:
        expected = evaluate_chinook_sales(value, data, max_rows=context.get("max_rows", 100), config=config)
        expected["tables"] = ["public.invoice"]
        append(identifier, "answer", family, value, expected, context)

    def blocked(identifier, kind, family, value, code, field=None, candidates=(), missing=None):
        expected = {"status": "needs_clarification" if kind == "clarification" else "error",
                    "route_to": "clarification" if kind == "clarification" else None,
                    "rows": [], "columns": [], "execution_allowed": False, "execution_attempted": False,
                    "gold": {"reviewed_by": "independent_python_oracle", "oracle_version": CHINOOK_ORACLE_VERSION,
                             "basis": "explicit_input_mapping_contract", "human_reviewed": False}}
        if kind == "clarification":
            expected.update(issue_codes=[code], clarification_fields=[field], permitted_candidates={field: list(candidates)})
            if missing:
                expected["missing_members"] = {field: list(missing)}
        else:
            expected["error_code"] = code
        append(identifier, kind, family, value, expected)

    value = query("发票总额尚未指定统计区间")
    del value["time_range"]
    blocked("C01", "clarification", "missing_time", value, "MISSING_TIME_RANGE", "time_range", missing=["start", "end_exclusive"])
    value = query("未指定发票口径")
    del value["metric_definition_id"]
    blocked("C02", "clarification", "missing_metric", value, "MISSING_METRIC_DEFINITION", "metric_definition_id", ["chinook_sales_amount_v1"])
    blocked("C03", "clarification", "unknown_customer", query("不存在客户", [("customer", "eq", 999)]), "UNKNOWN_ENTITY", "customer")
    blocked("C04", "clarification", "unknown_country", query("不存在国家", [("country", "eq", "火星")]), "UNKNOWN_ENTITY", "country")
    blocked("C05", "clarification", "unknown_city", query("不存在城市", [("city", "eq", "不存在城市")]), "UNKNOWN_ENTITY", "city")
    blocked("C06", "clarification", "empty_customer_in", query("客户列表尚未确定", [("customer", "in", [])]), "MISSING_FILTER_VALUE", "customer")
    blocked("C07", "clarification", "ambiguous_integer_candidates", query("客户Mark", [("customer", "eq", "Mark")]), "AMBIGUOUS_ENTITY", "customer", [14, 55])
    blocked("C08", "clarification", "aware_calendar_ambiguity", query("UTC偏移须确认日历时间", period={"start": "2024-01-01T00:00:00+08:00", "end_exclusive": "2025-01-01"}), "AMBIGUOUS_TIME_SEMANTICS", "time_range")

    value = query("未知数据源")
    value["source_id"] = "sales_db"
    blocked("S01", "safety", "wrong_source", value, "UNKNOWN_SOURCE")
    blocked("S02", "safety", "unsupported_employee", query("员工发票", [("employee", "eq", 1)]), "UNSUPPORTED_FILTER")
    value = query("分组排名未实现")
    value["group_by"] = ["country"]
    blocked("S03", "safety", "unsupported_grouping", value, "UNSUPPORTED_QUERY")
    blocked("S04", "safety", "inverted_period", query("反向区间", period={"start": "2025-01-01", "end_exclusive": "2024-01-01"}), "INVALID_QUERY")
    blocked("S05", "safety", "boolean_customer", query("客户错误类型", [("customer", "eq", True)]), "INVALID_QUERY")
    blocked("S06", "safety", "numeric_country", query("国家错误类型", [("country", "eq", 1)]), "INVALID_QUERY")
    value = query("错误口径")
    value["metric_definition_id"] = "sales_amount_v1"
    blocked("S07", "safety", "wrong_metric", value, "UNSUPPORTED_QUERY")
    blocked("S08", "safety", "duplicate_alias_field", query("重复城市字段", [("city", "eq", "Paris"), ("billing_city", "eq", "London")]), "INVALID_QUERY")
    blocked("S09", "safety", "in_non_array", query("客户数组错误", [("customer", "in", 1)]), "INVALID_QUERY")

    for identifier, family, text in (("R01", "track_ranking", "按曲目统计销量排名"),
                                      ("R02", "employee_join", "按员工统计发票总额"),
                                      ("R03", "cross_source", "跨数据库融合发票与客户知识")):
        append(identifier, "roadmap", family, query(text), {
            "status": "roadmap", "rows": [], "columns": [], "execution_allowed": False, "execution_attempted": False,
            "gold": {"reviewed_by": "independent_python_oracle", "oracle_version": CHINOOK_ORACLE_VERSION,
                     "basis": "future_scope; never included in measured denominator"}}, scope="roadmap")
    return cases


def main():
    target = ROOT / "evaluation/datasets"
    target.mkdir(parents=True, exist_ok=True)
    schema = json.loads((ROOT / "database/generated/chinook_schema_snapshot.json").read_text(encoding="utf-8-sig"))
    config = json.loads((ROOT / "database/source_config.json").read_text(encoding="utf-8-sig"))
    if config.get("source_id") != "chinook_db" or config.get("dataset") != "chinook":
        raise ValueError("Default source must be Chinook before freezing the development suite")
    for name, contents in (("chinook_schema.json", schema), ("chinook_source.json", config)):
        (target / name).write_text(json.dumps(contents, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    data = parse_chinook_sql(ROOT / "database/Chinook_PostgreSql.sql")
    cases = build_cases(data, config)
    case_path = target / "chinook_sql_v1.jsonl"
    case_path.write_text("".join(json.dumps(case, ensure_ascii=False, sort_keys=True, allow_nan=False) + "\n" for case in cases), encoding="utf-8")
    artifacts = {"source_sql": "database/Chinook_PostgreSql.sql", "schema": "evaluation/datasets/chinook_schema.json",
                 "semantic_config": "evaluation/datasets/chinook_source.json", "oracle": "src/agent_core/evaluation/chinook_oracle.py"}
    hashes = {"source_sql": "source_sql_sha256", "schema": "schema_file_sha256", "semantic_config": "semantic_config_sha256", "oracle": "oracle_file_sha256"}
    structural = {key: schema[key] for key in ("objects", "columns", "keys", "foreign_keys")}
    manifest = {"format_version": 1, "source_id": "chinook_db", "suite_version": SUITE_VERSION, "split": "dev",
                "projection_version": CHINOOK_PROJECTION_VERSION, "oracle_version": CHINOOK_ORACLE_VERSION,
                "snapshot_sha256": canonical_chinook_snapshot_hash(data), "semantic_metadata_sha256": chinook_semantic_metadata_hash(data),
                "snapshot_row_counts": {key: len(rows) for key, rows in data.items()},
                "schema_fingerprint_sha256": hashlib.sha256(json.dumps(structural, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest(),
                "case_count": len(cases), "kind_counts": dict(Counter(case["kind"] for case in cases)), "artifacts": artifacts,
                "gold_review": {"human_reviewed": False, "method": "independent_literal_parser_integer_cents"},
                "evaluation_level": "structured_sql_service", "held_out": False,
                "coverage_note": "Development suite for invoice/customer/billing location aggregation; SQL attack/timeout/cancel covered by regression tests."}
    manifest["data_snapshot_id"] = "sha256:" + manifest["snapshot_sha256"]
    for label, field in hashes.items():
        manifest[field] = hashlib.sha256((ROOT / artifacts[label]).read_bytes()).hexdigest()
    manifest["cases_sha256"] = hashlib.sha256(case_path.read_bytes()).hexdigest()
    (target / "chinook_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    print(json.dumps({"case_count": len(cases), "kind_counts": manifest["kind_counts"], "snapshot_sha256": manifest["snapshot_sha256"]}))


if __name__ == "__main__":
    main()
