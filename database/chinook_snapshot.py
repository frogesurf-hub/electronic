"""Chinook 的受控业务语义；物理结构及实体名单仍由数据库实时读取。"""
from __future__ import annotations

import copy
import hashlib
import json
import re
from collections import defaultdict
from datetime import datetime, timezone

if __package__:
    from .sql_common import ServiceFault
    from .query_profiles import chinook_amount_type
else:
    from sql_common import ServiceFault
    from query_profiles import chinook_amount_type


COUNTRY_ALIASES = {
    "美国": "USA", "美利坚": "USA", "United States": "USA",
    "加拿大": "Canada", "巴西": "Brazil", "英国": "United Kingdom",
    "德国": "Germany", "法国": "France", "印度": "India", "澳大利亚": "Australia",
    "葡萄牙": "Portugal", "捷克": "Czech Republic", "芬兰": "Finland",
    "挪威": "Norway", "瑞典": "Sweden", "西班牙": "Spain", "意大利": "Italy",
}


def _fingerprint(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":")).encode("utf-8")).hexdigest()


def _fields(columns, unit):
    physical = {f"{row['schema_name']}.{row['table_name']}.{row['column_name']}": row
                for row in columns}
    specs = (
        ("amount", "total", "销售金额", "numeric", ["sum"], unit, ["销售额", "金额"]),
        ("invoice_date", "invoice_date", "发票日期", "timestamp", ["gte", "lt"], None,
         ["发票时间", "销售时间"]),
        ("country", "billing_country", "账单国家", "text", ["eq", "in"], None,
         ["国家", "销售国家", "账单国家"]),
        ("city", "billing_city", "账单城市", "text", ["eq", "in"], None,
         ["城市", "销售城市", "账单城市"]),
        ("customer", "customer_id", "客户", "integer", ["eq", "in"], None, ["客户"]),
    )
    fields = []
    for field_id, column, label, kind, operations, amount_unit, aliases in specs:
        name = f"public.invoice.{column}"
        if name not in physical:
            raise ServiceFault("METADATA_ERROR", f"Chinook 字段不存在：{name}")
        actual = physical[name]["data_type"]
        valid = {
            "numeric": chinook_amount_type(actual),
            "timestamp": actual == "timestamp without time zone",
            "text": actual == "text" or bool(re.fullmatch(r"character varying(?:\(\d+\))?", actual)),
            "integer": actual == "integer",
        }[kind]
        if not valid:
            raise ServiceFault("METADATA_ERROR", f"Chinook 字段类型不符：{name} / {actual}")
        fields.append({
            "field_id": field_id, "schema_name": "public", "table_name": "invoice",
            "column_name": column, "business_name": label, "data_type": kind,
            "physical_data_type": actual, "physical_column": name,
            "unit": amount_unit, "allowed_ops": operations, "name_aliases": aliases,
            "description": ("源发票总金额；币种未声明" if field_id == "amount" else label),
        })
    return fields


def city_entities(data):
    """城市按发票账单原值列举；同名城市可同时归属多个国家。"""
    if not isinstance(data, dict) or not isinstance(data.get("cities"), list):
        raise ServiceFault("METADATA_ERROR", "缺少实时账单城市名单")
    countries_by_city = defaultdict(set)
    for row in data["cities"]:
        if not isinstance(row, dict) or not {"billing_country", "billing_city"}.issubset(row):
            raise ServiceFault("METADATA_ERROR", "账单城市名单格式异常")
        city, country = row["billing_city"], row["billing_country"]
        if city is None:
            continue
        if not isinstance(city, str) or not city.strip():
            raise ServiceFault("METADATA_ERROR", "账单城市名单包含无效城市")
        if country is not None and (not isinstance(country, str) or not country.strip()):
            raise ServiceFault("METADATA_ERROR", "账单城市名单包含无效国家")
        countries_by_city[city]
        if country is not None:
            countries_by_city[city].add(country)
    return [{"city": city, "billing_countries": sorted(countries)}
            for city, countries in sorted(countries_by_city.items())]


def _aliases(data, fields, config):
    targets = {}
    for row in data["countries"]:
        country = row["country"]
        if not isinstance(country, str) or not country:
            raise ServiceFault("METADATA_ERROR", "国家名单包含无效值")
        targets[("country", country)] = {
            "target_type": "country", "target_id": country,
            "display_name": country, "context": {"attribution": "invoice.billing_country"},
        }
    for row in city_entities(data):
        city = row["city"]
        targets[("city", city)] = {
            "target_type": "city", "target_id": city, "display_name": city,
            "context": {"attribution": "invoice.billing_city",
                        "billing_countries": row["billing_countries"]},
        }
    for row in data["customers"]:
        identifier = row["customer_id"]
        if isinstance(identifier, bool) or not isinstance(identifier, int):
            raise ServiceFault("METADATA_ERROR", "Chinook 客户编号必须为整数")
        targets[("customer", identifier)] = {
            "target_type": "customer", "target_id": identifier,
            "display_name": row["full_name"], "context": {"support_rep_id": row["support_rep_id"]},
        }
    targets[("metric", "sales_amount")] = {
        "target_type": "metric", "target_id": "sales_amount", "display_name": "销售额", "context": {},
    }
    for field in fields:
        targets[("field", field["field_id"])] = {
            "target_type": "field", "target_id": field["field_id"],
            "display_name": field["business_name"],
            "context": {"physical_column": field["physical_column"]},
        }
    candidates = defaultdict(dict)
    aliases = []

    def add(text, kind, identifier):
        key = (kind, identifier)
        candidates[text][key] = targets[key]
        aliases.append({"alias_text": text, "target_type": kind, "target_id": identifier})

    for (kind, identifier), row in targets.items():
        add(row["display_name"], kind, identifier)
        add(str(identifier), kind, identifier)
    countries = {row["country"] for row in data["countries"]}
    configured = config.get("country_aliases", {})
    if not isinstance(configured, dict) or any(not isinstance(key, str) or not isinstance(value, str)
                                             for key, value in configured.items()):
        raise ServiceFault("METADATA_CONFIGURATION_ERROR", "country_aliases 应为别名到国家名称的字典")
    for text, country in {**COUNTRY_ALIASES, **configured}.items():
        if country in countries:
            add(text, "country", country)
    for text in ("销售额", "销售金额", "营业额", "sales_amount"):
        add(text, "metric", "sales_amount")
    for field in fields:
        for text in field["name_aliases"]:
            add(text, "field", field["field_id"])
    unique_aliases = {(row["alias_text"], row["target_type"], row["target_id"]): row for row in aliases}
    return list(unique_aliases.values()), [{
        "alias_text": text, "candidates": list(rows.values()),
        "resolution": {"filter_by_expected_target_type_first": True,
                       "zero_candidates": "clarify", "one_candidate": "resolve",
                       "multiple_candidates": "clarify", "allow_first_candidate_default": False},
    } for text, rows in sorted(candidates.items())]


def enrich_snapshot(snapshot, data, config):
    result = copy.deepcopy(snapshot)
    if config.get("timezone", "Asia/Shanghai") != "Asia/Shanghai":
        raise ServiceFault("METADATA_CONFIGURATION_ERROR", "当前 Chinook 时间契约使用 Asia/Shanghai 应用标签")
    objects = {f"{row['schema_name']}.{row['table_name']}" for row in snapshot["objects"]}
    fields = _fields(snapshot["columns"], config.get("amount_unit", "金额"))
    paths = [{
        "path_id": "invoice_customer", "edges": [["public.invoice.customer_id", "public.customer.customer_id"]],
        "cardinality_per_edge": "many_to_one", "preserves_grain": "invoice",
        "allowed_use": ["display", "filter"],
    }, {
        "path_id": "invoice_lines_track", "edges": [["public.invoice.invoice_id", "public.invoice_line.invoice_id"],
                                                     ["public.invoice_line.track_id", "public.track.track_id"]],
        "cardinalities": ["one_to_many", "many_to_one"], "result_grain": "invoice_line",
        "allowed_use": ["schema_description"],
        "invoice_metric_track_filter": "exists", "amount_expression": "unit_price * quantity",
    }, {
        "path_id": "customer_support_rep", "edges": [["public.customer.support_rep_id", "public.employee.employee_id"]],
        "cardinality_per_edge": "many_to_one", "allowed_use": ["schema_description"],
    }]
    physical = {f"{row['schema_name']}.{row['table_name']}.{row['column_name']}" for row in snapshot["columns"]}
    for path in paths:
        if any(column not in physical for edge in path["edges"] for column in edge):
            raise ServiceFault("METADATA_ERROR", f"Chinook 关联字段缺失：{path['path_id']}")
    definition = {
        "metric_definition_id": "chinook_sales_amount_v1", "metric_id": "sales_amount", "version": 1,
        "metric_name": "销售额", "unit": config.get("amount_unit", "金额"), "currency": None,
        "definition_text": "按发票日期汇总发票 total；源数据库未声明币种，不附加支付状态或退款条件",
        "time_field": "invoice_date", "time_column": "public.invoice.invoice_date",
        "time_data_type": "timestamp without time zone", "fixed_filters": {},
        "aggregation_rule": "sum_invoice_total", "valid_from": "0001-01-01", "valid_to": None,
        "query_spec": {"source_id": config["source_id"], "from": "public.invoice", "grain": "invoice",
                       "filter": {}, "aggregate": "sum_invoice_total", "time_zone": "Asia/Shanghai",
                       "timestamp_semantics": "naive_calendar", "time_bounds": "start_inclusive_end_exclusive",
                       "refund_rule": None},
        "version_selection": {"basis": "source_configuration", "on_multiple_versions": "clarify"},
        "evidence_chunk_id": None,
    }
    aliases, candidates = _aliases(data, fields, config)
    result.update({
        "dataset": "chinook", "business_fields": fields, "metric_definitions": [definition],
        "city_entities": city_entities(data),
        "business_aliases": aliases, "alias_candidates": candidates,
        "condition_rules": {
            "tasks": {"metric_aggregate": {"required": ["metric_definition_id", "time_range"],
                                           "optional_filters": ["country", "city", "customer"],
                                           "missing_optional_filter": "all_authorized_objects"}},
            "time_range": {"required_members": ["start", "end_exclusive"], "timezone": "Asia/Shanghai",
                           "require_start_before_end": True, "timestamp_semantics": "naive_calendar",
                           "source_timezone_declared": False, "timezone_conversion_applied": False},
        },
        "join_semantics": {"grains": {"public.invoice": ["invoice_id"], "public.invoice_line": ["invoice_line_id"]},
                           "historical_attribution": {"country": "public.invoice.billing_country",
                                                      "city": "public.invoice.billing_city"},
                           "allowed_paths": paths, "declared_foreign_keys": copy.deepcopy(snapshot["foreign_keys"]),
                           "unlisted_path_action": "block",
                           "aggregation_guards": ["do_not_sum_invoice_total_after_one_to_many_join", "use_exists_for_track_filter"]},
    })
    structural = {key: snapshot[key] for key in ("objects", "columns", "keys", "foreign_keys")}
    captured_at = snapshot.get("captured_at") if snapshot.get("metadata_mode") == "live" else None
    result["snapshot_metadata"] = {
        "snapshot_format_version": "3.0", "dataset": "chinook", "database_schema_version": None,
        "schema_fingerprint_sha256": _fingerprint(structural),
        "semantic_fingerprint_sha256": _fingerprint({"fields": fields, "definitions": [definition], "aliases": aliases}),
        "schema_read_date": snapshot.get("read_date"), "schema_captured_at": captured_at,
        "schema_capture_precision": "timestamp" if captured_at else "day",
        "enriched_at": datetime.now(timezone.utc).isoformat(), "semantic_source": "source_configuration",
        "semantic_database_consistency_verified": False, "semantic_physical_mapping_verified": True,
        "freshness_policy": {"max_age_seconds": 86400, "on_unknown_capture_time": "refresh_before_query",
                             "on_expired": "refresh_before_query", "on_schema_or_permission_change": "invalidate",
                             "enrichment_renews_schema_freshness": False},
    }
    scopes = {}
    for name, key in (("business_query", "query_table_allowlist"), ("semantic_lookup", "metadata_tables"),
                      ("evidence_lookup", "evidence_tables")):
        declared = list(config[key])
        scopes[name] = {"configured_allowlist": declared,
                        "present_in_snapshot": [table for table in declared if table in objects],
                        "missing_from_snapshot": [table for table in declared if table not in objects]}
    result["access_scope"] = {
        "source_id": config["source_id"], "reader_role": config["reader_role"], "allowed_operations": ["SELECT"],
        "scopes": scopes, "live_permissions_verified": snapshot.get("permissions_verified", False),
        "permissions_checked_at": captured_at, "requires_live_grant_and_rls_check": True,
        "statement_timeout_ms": config["statement_timeout_ms"], "max_result_rows": config["max_result_rows"],
        "unknown_table_action": "block",
    }
    return result
