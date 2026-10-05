"""Chinook 发票销售额的需求映射与受控 SQL 生成。

模块只处理已提取的结构化需求，不读取凭据、不连接数据库。Chinook
invoice_date 为无时区时间，比较保留原始日历值，不进行时区转换。
"""
from __future__ import annotations

import copy
import re
from datetime import datetime

if __package__:
    from .sql_common import ServiceFault, json_value
    from .chinook_snapshot import city_entities
    from .query_profiles import chinook_amount_type
else:
    from sql_common import ServiceFault, json_value
    from chinook_snapshot import city_entities
    from query_profiles import chinook_amount_type


SOURCE_ID = "chinook_db"
DEFINITION_ID = "chinook_sales_amount_v1"
TABLE = "public.invoice"
SESSION_KEYS = ("session_id", "task_id", "request_id", "step_id")
FIELD_ALIASES = {"country": "country", "billing_country": "country", "region": "country",
                 "city": "city", "billing_city": "city", "账单城市": "city", "城市": "city",
                 "customer": "customer"}
COUNTRY_ALIASES = {
    "美国": "USA", "美利坚": "USA", "United States": "USA", "United States of America": "USA",
    "加拿大": "Canada", "巴西": "Brazil", "德国": "Germany", "英国": "United Kingdom",
    "法国": "France", "印度": "India", "澳大利亚": "Australia", "葡萄牙": "Portugal",
    "捷克": "Czech Republic", "芬兰": "Finland", "挪威": "Norway", "瑞典": "Sweden",
    "西班牙": "Spain", "意大利": "Italy", "荷兰": "Netherlands", "波兰": "Poland",
    "丹麦": "Denmark",
}
AGGREGATION = {"operation": "sum", "column": "total", "alias": "sales_amount"}


def _missing(value):
    return value is None or (isinstance(value, str) and not value.strip())


def _calendar_time(value):
    if not isinstance(value, str) or not value.strip():
        raise ServiceFault("INVALID_QUERY", "时间必须为 ISO 日期或时间字符串")
    try:
        return datetime.fromisoformat(value.strip())
    except ValueError as error:
        raise ServiceFault("INVALID_QUERY", "时间格式不合法，请使用 ISO 日期或时间") from error


def _rows_index(rows, key, label):
    if not isinstance(rows, list):
        raise ServiceFault("METADATA_ERROR", f"缺少或损坏的{label}")
    result = {}
    for row in rows:
        if not isinstance(row, dict) or key not in row:
            raise ServiceFault("METADATA_ERROR", f"{label}缺少编号")
        identifier = row[key]
        if not isinstance(identifier, (str, int)) or isinstance(identifier, bool) or identifier in result:
            raise ServiceFault("METADATA_ERROR", f"{label}编号格式异常或重复")
        result[identifier] = row
    return result


class ChinookSchemaLinkingService:
    """返回 ready / needs_clarification / error，保留原有澄清交接结构。"""

    def match(self, query, snapshot, context=None, entities=None):
        context = {} if context is None else context
        session = {}
        try:
            if not isinstance(query, dict) or not isinstance(context, dict):
                raise ServiceFault("INVALID_QUERY", "query 和 context 必须为字典；请先完成需求结构化")
            if any(not isinstance(key, str) for key in query):
                raise ServiceFault("INVALID_QUERY", "查询参数名称必须为字符串")
            if "question" in query and not isinstance(query["question"], str):
                raise ServiceFault("INVALID_QUERY", "question 必须为字符串")
            for key in SESSION_KEYS:
                if key in context:
                    if not isinstance(context[key], str):
                        raise ServiceFault("INVALID_QUERY", f"{key} 必须为字符串")
                    session[key] = context[key]
            decision = self._match(query, snapshot, context, entities)
            decision.update(session)
            return decision
        except ServiceFault as error:
            return {"status": "error", "error_code": error.code, "error_message": str(error),
                    "issues": error.issues, "execution_attempted": False, **session}
        except (KeyError, TypeError, ValueError, AttributeError):
            return {"status": "error", "error_code": "METADATA_ERROR",
                    "error_message": "Chinook 元数据结构不完整或字段类型异常，请刷新元数据",
                    "issues": [], "execution_attempted": False, **session}

    @staticmethod
    def _metadata(snapshot):
        if not isinstance(snapshot, dict) or snapshot.get("dataset") != "chinook":
            raise ServiceFault("METADATA_ERROR", "需要 Chinook 的增强结构快照")
        scope = snapshot["access_scope"]
        if scope["source_id"] != SOURCE_ID:
            raise ServiceFault("METADATA_ERROR", "Chinook 快照数据源编号不一致")
        if "SELECT" not in scope["allowed_operations"]:
            raise ServiceFault("DATABASE_PERMISSION_DENIED", "元数据声明不允许 SELECT")
        access = scope["scopes"]["business_query"]
        if TABLE not in access["configured_allowlist"]:
            raise ServiceFault("TABLE_NOT_ALLOWED", "发票表不在查询白名单中")
        objects = {(row["schema_name"], row["table_name"]) for row in snapshot["objects"]}
        if TABLE in access["missing_from_snapshot"] or ("public", "invoice") not in objects:
            raise ServiceFault("METADATA_ERROR", "快照中缺少 public.invoice")
        fields = _rows_index(snapshot["business_fields"], "field_id", "业务字段")
        columns = {}
        for row in snapshot["columns"]:
            key = (row["schema_name"], row["table_name"], row["column_name"])
            if key in columns:
                raise ServiceFault("METADATA_ERROR", "快照物理字段重复")
            columns[key] = row["data_type"]
        required = (("amount", "total", "numeric", ["sum"]),
                    ("invoice_date", "invoice_date", "timestamp", ["gte", "lt"]),
                    ("country", "billing_country", "text", ["eq", "in"]),
                    ("city", "billing_city", "text", ["eq", "in"]),
                    ("customer", "customer_id", "integer", ["eq", "in"]))
        for field_id, column, semantic_type, operations in required:
            field = fields.get(field_id)
            if field is None:
                raise ServiceFault("METADATA_ERROR", f"缺少 Chinook 业务字段：{field_id}")
            key = (field["schema_name"], field["table_name"], field["column_name"])
            if (key != ("public", "invoice", column) or key not in columns
                    or field["data_type"] != semantic_type
                    or field["physical_column"] != f"public.invoice.{column}"
                    or not set(operations).issubset(field["allowed_ops"])):
                raise ServiceFault("METADATA_ERROR", f"Chinook 字段映射与查询实现不一致：{field_id}")
            physical = columns[key]
            valid = {
                "numeric": chinook_amount_type(physical),
                "timestamp": physical == "timestamp without time zone",
                "text": isinstance(physical, str) and (physical == "text" or bool(
                    re.fullmatch(r"character varying(?:\(\d+\))?", physical))),
                "integer": physical == "integer",
            }[semantic_type]
            if not valid or field.get("physical_data_type", physical) != physical:
                raise ServiceFault("METADATA_ERROR", f"Chinook 物理字段类型不一致：{field_id}")
        definitions = _rows_index(snapshot["metric_definitions"], "metric_definition_id", "指标口径")
        definition = definitions.get(DEFINITION_ID)
        if definition is None:
            raise ServiceFault("METADATA_ERROR", "缺少 Chinook 发票销售额口径")
        spec = definition["query_spec"]
        if (definition["metric_id"] != "sales_amount" or spec["source_id"] != SOURCE_ID
                or spec["from"] != TABLE or spec["grain"] != "invoice"
                or spec["aggregate"] != "sum_invoice_total" or spec["filter"] != {}
                or spec["timestamp_semantics"] != "naive_calendar"
                or spec["time_zone"] != "Asia/Shanghai"
                or spec["time_bounds"] != "start_inclusive_end_exclusive"
                or spec.get("refund_rule") is not None or definition["time_field"] != "invoice_date"
                or definition["time_column"] != "public.invoice.invoice_date"
                or definition["aggregation_rule"] != "sum_invoice_total"
                or definition["fixed_filters"] != {}):
            raise ServiceFault("METADATA_ERROR", "Chinook 发票口径已变化，需要更新查询实现")
        rules = snapshot["condition_rules"]
        if set(rules["tasks"]["metric_aggregate"]["required"]) != {"metric_definition_id", "time_range"}:
            raise ServiceFault("METADATA_ERROR", "Chinook 必填条件已变化")
        time_rule = rules["time_range"]
        if (set(time_rule["required_members"]) != {"start", "end_exclusive"}
                or time_rule["timezone"] != "Asia/Shanghai"
                or time_rule["timestamp_semantics"] != "naive_calendar"
                or time_rule["require_start_before_end"] is not True):
            raise ServiceFault("METADATA_ERROR", "Chinook 时间契约与查询实现不一致")
        return definition

    @staticmethod
    def _missing_filter(field_id, value):
        return {"code": "MISSING_FILTER_VALUE", "field_id": field_id, "original_value": value,
                "reason": {"country": "请补充账单国家筛选值", "city": "请补充账单城市筛选值",
                           "customer": "请补充客户筛选值"}[field_id],
                "candidates": []}

    def _country(self, value, data, snapshot):
        if _missing(value):
            return None, self._missing_filter("country", value)
        if not isinstance(value, str):
            raise ServiceFault("INVALID_QUERY", "账单国家必须为字符串")
        if not isinstance(data, dict) or "countries" not in data:
            raise ServiceFault("METADATA_ERROR", "缺少实时账单国家名单")
        countries = _rows_index(data["countries"], "country", "国家名单")
        if any(not isinstance(country, str) or not country for country in countries):
            raise ServiceFault("METADATA_ERROR", "国家名单包含无效值")
        text = value.strip().casefold()
        candidates = {country for country in countries if country.casefold() == text}
        for alias, target in COUNTRY_ALIASES.items():
            if alias.casefold() == text and target in countries:
                candidates.add(target)
        for alias in snapshot["business_aliases"]:
            if alias["target_type"] == "country" and alias["alias_text"].strip().casefold() == text:
                if alias["target_id"] not in countries:
                    raise ServiceFault("METADATA_ERROR", "国家别名指向不存在或不可访问的国家")
                candidates.add(alias["target_id"])
        if len(candidates) == 1:
            return next(iter(candidates)), None
        return None, {"code": "AMBIGUOUS_ENTITY" if candidates else "UNKNOWN_ENTITY",
                      "field_id": "country", "original_value": value,
                      "reason": "账单国家存在多个候选，请明确国家" if candidates
                                else "未找到对应账单国家，请补充或修正国家名称",
                      "candidates": [{"id": country, "name": country} for country in sorted(candidates)]}

    def _city(self, value, data, snapshot):
        if _missing(value):
            return None, self._missing_filter("city", value)
        if not isinstance(value, str):
            raise ServiceFault("INVALID_QUERY", "账单城市必须为字符串")
        records = {row["city"]: row for row in city_entities(data)}
        text = value.strip().casefold()
        candidates = {city for city in records if city.casefold() == text}
        for alias in snapshot["business_aliases"]:
            if alias["target_type"] == "city" and alias["alias_text"].strip().casefold() == text:
                if alias["target_id"] not in records:
                    raise ServiceFault("METADATA_ERROR", "城市别名指向不存在或不可访问的城市")
                candidates.add(alias["target_id"])
        if len(candidates) == 1:
            return next(iter(candidates)), None
        return None, {"code": "AMBIGUOUS_ENTITY" if candidates else "UNKNOWN_ENTITY",
                      "field_id": "city", "original_value": value,
                      "reason": "账单城市存在多个候选，请明确城市" if candidates
                                else "未找到对应账单城市，请补充或修正城市名称",
                      "candidates": [{"id": city, "name": city,
                                      "context": {"billing_countries": records[city]["billing_countries"]}}
                                     for city in sorted(candidates)]}

    def _customer(self, value, data, snapshot):
        if _missing(value):
            return None, self._missing_filter("customer", value)
        if isinstance(value, bool) or not isinstance(value, (str, int)):
            raise ServiceFault("INVALID_QUERY", "客户需提供整数编号或姓名")
        if isinstance(value, int) and value <= 0:
            raise ServiceFault("INVALID_QUERY", "客户编号必须为正整数")
        if not isinstance(data, dict) or "customers" not in data:
            raise ServiceFault("METADATA_ERROR", "缺少实时客户名单")
        records = _rows_index(data["customers"], "customer_id", "客户名单")
        if any(not isinstance(key, int) or key <= 0 for key in records):
            raise ServiceFault("METADATA_ERROR", "Chinook 客户编号必须为正整数")
        text = str(value).strip()
        candidates = {int(text)} & records.keys() if text.isascii() and text.isdecimal() else set()
        for identifier, row in records.items():
            names = (row["full_name"], row["first_name"], row["last_name"])
            if any(not isinstance(name, str) for name in names):
                raise ServiceFault("METADATA_ERROR", "客户姓名元数据格式异常")
            if any(text.casefold() == name.strip().casefold() for name in names):
                candidates.add(identifier)
        for alias in snapshot["business_aliases"]:
            if alias["target_type"] == "customer" and alias["alias_text"].strip().casefold() == text.casefold():
                target = alias["target_id"]
                if isinstance(target, bool) or not isinstance(target, int) or target not in records:
                    raise ServiceFault("METADATA_ERROR", "客户别名指向不存在或不可访问的客户")
                candidates.add(target)
        if len(candidates) == 1:
            return next(iter(candidates)), None
        options = [{"id": identifier, "name": records[identifier]["full_name"],
                    "context": {"support_rep_id": records[identifier].get("support_rep_id")}}
                   for identifier in sorted(candidates)]
        return None, {"code": "AMBIGUOUS_ENTITY" if candidates else "UNKNOWN_ENTITY",
                      "field_id": "customer", "original_value": value,
                      "reason": "客户姓名对应多个候选，请确认完整姓名或编号" if candidates
                                else "未找到对应客户，请补充或修正姓名、编号",
                      "candidates": options}

    def _match(self, query, snapshot, context, entities):
        allowed = {"question", "source_id", "metric_id", "metric_definition_id", "time_range", "filters"}
        unknown = set(query) - allowed
        if unknown:
            raise ServiceFault("UNSUPPORTED_QUERY", f"Chinook 发票汇总暂不支持这些参数：{sorted(unknown)}")
        definition = self._metadata(snapshot)
        if query.get("source_id") != SOURCE_ID:
            raise ServiceFault("UNKNOWN_SOURCE", "Chinook 查询的数据源必须为 chinook_db")
        issues = []
        confirmed = {"source_id": SOURCE_ID}
        metric = query.get("metric_id")
        if metric is not None:
            if not isinstance(metric, str) or not metric.strip():
                raise ServiceFault("INVALID_QUERY", "metric_id 必须为非空字符串")
            known = {"sales_amount", "销售额", "销售金额", "营业额", "发票销售额"}
            known.update(alias["alias_text"] for alias in snapshot["business_aliases"]
                         if alias["target_type"] == "metric" and alias["target_id"] == "sales_amount")
            if metric.strip() not in known:
                raise ServiceFault("UNSUPPORTED_QUERY", "Chinook 当前仅支持发票销售额汇总")
        definition_id = query.get("metric_definition_id")
        if _missing(definition_id):
            issues.append({"code": "MISSING_METRIC_DEFINITION", "field_id": "metric_definition_id",
                           "reason": "请确认按发票日期汇总 total 的销售额口径，不自动选择默认口径",
                           "candidates": [{"metric_id": "sales_amount", "metric_definition_id": DEFINITION_ID,
                                           "name": definition["metric_name"],
                                           "definition": definition["definition_text"]}]})
        elif not isinstance(definition_id, str):
            raise ServiceFault("INVALID_QUERY", "口径编号必须为字符串")
        elif definition_id != DEFINITION_ID:
            raise ServiceFault("UNSUPPORTED_QUERY", "Chinook 当前仅支持 chinook_sales_amount_v1")
        else:
            confirmed.update(metric_id="sales_amount", metric_definition_id=DEFINITION_ID)

        period = query.get("time_range")
        parsed = {}
        if period is not None and not isinstance(period, dict):
            raise ServiceFault("INVALID_QUERY", "time_range 必须为字典")
        if period is not None and set(period) - {"start", "end_exclusive"}:
            raise ServiceFault("INVALID_QUERY", "时间范围仅接受 start 和 end_exclusive")
        missing = []
        for key in ("start", "end_exclusive"):
            value = None if period is None else period.get(key)
            if _missing(value):
                missing.append(key)
                continue
            date_value = _calendar_time(value)
            if date_value.tzinfo is not None:
                issues.append({"code": "AMBIGUOUS_TIME_SEMANTICS", "field_id": "time_range",
                               "member": key, "original_value": value,
                               "reason": "Chinook 发票日期未声明时区；请确认日历时间并提供不带 UTC 偏移的日期或时间",
                               "candidates": []})
            else:
                parsed[key] = date_value
        if missing:
            issues.append({"code": "MISSING_TIME_RANGE", "field_id": "time_range",
                           "missing_members": missing, "reason": "请补充统计时间范围，包含开始、不包含结束",
                           "candidates": []})
        if parsed:
            confirmed["time_range"] = {key: value.isoformat() for key, value in parsed.items()}
        if len(parsed) == 2:
            if parsed["start"] >= parsed["end_exclusive"]:
                raise ServiceFault("INVALID_QUERY", "开始时间必须早于结束时间")
            try:
                valid_from = _calendar_time(definition["valid_from"]) if definition.get("valid_from") else None
                valid_to = _calendar_time(definition["valid_to"]) if definition.get("valid_to") else None
            except ServiceFault as error:
                raise ServiceFault("METADATA_ERROR", "Chinook 指标有效期格式异常") from error
            if ((valid_from and valid_from.tzinfo is not None) or (valid_to and valid_to.tzinfo is not None)
                    or (valid_from and valid_to and valid_from >= valid_to)):
                raise ServiceFault("METADATA_ERROR", "Chinook 指标有效期语义异常")
            if ((valid_from and parsed["start"] < valid_from)
                    or (valid_to and parsed["end_exclusive"] > valid_to)):
                issues.append({"code": "METRIC_VERSION_INCOMPATIBLE", "field_id": "metric_definition_id",
                               "reason": "指标口径不能覆盖整个统计期间，请确认时间或口径版本",
                               "candidates": [{"metric_definition_id": DEFINITION_ID,
                                               "valid_from": definition.get("valid_from"),
                                               "valid_to": definition.get("valid_to")}]})

        filters = query.get("filters", [])
        if not isinstance(filters, list):
            raise ServiceFault("INVALID_QUERY", "filters 必须为列表")
        seen, resolved_filters, confirmed_filters = set(), [], []
        for index, item in enumerate(filters):
            if not isinstance(item, dict) or set(item) != {"field_id", "op", "value"}:
                raise ServiceFault("INVALID_QUERY", "每个筛选条件需提供 field_id、op、value")
            field, op, value = item["field_id"], item["op"], item["value"]
            if not isinstance(field, str) or not isinstance(op, str):
                raise ServiceFault("INVALID_QUERY", "field_id 和 op 必须为字符串")
            if field not in FIELD_ALIASES:
                raise ServiceFault("UNSUPPORTED_FILTER", "Chinook 当前仅支持账单国家、账单城市与客户筛选；员工、曲目和类型未实现")
            field = FIELD_ALIASES[field]
            if field in seen:
                raise ServiceFault("INVALID_QUERY", "同一业务字段请合并为一个筛选条件")
            seen.add(field)
            if op not in {"eq", "in"}:
                raise ServiceFault("UNSUPPORTED_FILTER", "Chinook 当前筛选仅支持 eq 和 in")
            if op == "in":
                if not isinstance(value, list) or len(value) > 50:
                    raise ServiceFault("INVALID_QUERY", "in 的值需为包含 1 至 50 项的列表")
                if not value:
                    issue = self._missing_filter(field, value)
                    issue["filter_index"] = index
                    issues.append(issue)
                    continue
                values = value
            else:
                values = [value]
            identifiers, unresolved = [], False
            resolver = {"country": self._country, "city": self._city, "customer": self._customer}[field]
            for position, candidate in enumerate(values):
                identifier, issue = resolver(candidate, entities, snapshot)
                if issue:
                    issue["filter_index"] = index
                    if op == "in":
                        issue["value_index"] = position
                    issues.append(issue)
                    unresolved = True
                elif identifier not in identifiers:
                    identifiers.append(identifier)
            if not unresolved:
                selected = identifiers[0] if op == "eq" else identifiers
                resolved_filters.append({"column": {"country": "billing_country", "city": "billing_city",
                                                    "customer": "customer_id"}[field],
                                         "op": op, "value": selected})
                confirmed_filters.append({"field_id": field, "op": op, "value": selected})
        if confirmed_filters:
            confirmed["filters"] = confirmed_filters
        if issues:
            return {"status": "needs_clarification", "issues": issues,
                    "clarification_request": {"issues": issues, "confirmed_conditions": confirmed,
                                              "query": json_value(copy.deepcopy(query)),
                                              "context": json_value(copy.deepcopy(context))},
                    "execution_attempted": False}
        request = {"source_id": SOURCE_ID, "metric_definition_id": DEFINITION_ID,
                   "table": TABLE, "grain": "invoice", "aggregation": dict(AGGREGATION),
                   "filters": resolved_filters,
                   "time_filter": {"column": "invoice_date", "start": parsed["start"].isoformat(),
                                   "end_exclusive": parsed["end_exclusive"].isoformat(),
                                   "timezone": "Asia/Shanghai"}}
        return {"status": "ready", "sql_generation_request": request,
                "confirmed_conditions": confirmed, "execution_attempted": False}


class ChinookSQLGenerator:
    """仅将已明确的 Chinook 发票汇总请求转为参数化 SQL。"""

    def generate(self, request, limits):
        expected = {"source_id", "metric_definition_id", "table", "grain", "aggregation", "filters", "time_filter"}
        if not isinstance(request, dict) or set(request) != expected:
            raise ServiceFault("METADATA_ERROR", "Chinook SQL 生成请求契约不完整或包含额外字段")
        if (request["source_id"] != SOURCE_ID or request["metric_definition_id"] != DEFINITION_ID
                or request["table"] != TABLE or request["grain"] != "invoice"
                or request["aggregation"] != AGGREGATION):
            raise ServiceFault("UNSUPPORTED_QUERY", "SQL 生成器仅支持 Chinook 发票 total 销售额汇总")
        time_filter = request["time_filter"]
        required_time = {"column", "start", "end_exclusive", "timezone"}
        if (not isinstance(time_filter, dict) or set(time_filter) != required_time
                or time_filter["column"] != "invoice_date" or time_filter["timezone"] != "Asia/Shanghai"):
            raise ServiceFault("METADATA_ERROR", "Chinook SQL 请求时间契约不一致")
        start, end = _calendar_time(time_filter["start"]), _calendar_time(time_filter["end_exclusive"])
        if start.tzinfo is not None or end.tzinfo is not None:
            raise ServiceFault("INVALID_QUERY", "Chinook SQL 时间参数不能带 UTC 偏移；必须先确认日历时间语义")
        if start >= end:
            raise ServiceFault("INVALID_QUERY", "开始时间必须早于结束时间")
        if (not isinstance(limits, dict) or isinstance(limits.get("max_rows"), bool)
                or not isinstance(limits.get("max_rows"), int) or not 1 <= limits["max_rows"] <= 1000):
            raise ServiceFault("INVALID_QUERY", "max_rows 必须为 1 至 1000 的整数")
        conditions = ["i.invoice_date >= %(start)s", "i.invoice_date < %(end)s"]
        parameters = {"start": start, "end": end, "fetch_limit": limits["max_rows"] + 1}
        filters = request["filters"]
        if not isinstance(filters, list):
            raise ServiceFault("METADATA_ERROR", "Chinook SQL 生成请求缺少筛选列表")
        seen = set()
        for index, item in enumerate(filters):
            if not isinstance(item, dict) or set(item) != {"column", "op", "value"}:
                raise ServiceFault("METADATA_ERROR", "Chinook SQL 筛选格式不合法")
            column, op, value = item["column"], item["op"], item["value"]
            if (not isinstance(column, str) or not isinstance(op, str)
                    or column not in {"billing_country", "billing_city", "customer_id"}
                    or column in seen or op not in {"eq", "in"}):
                raise ServiceFault("UNSUPPORTED_FILTER", "Chinook SQL 请求包含不支持的字段或操作")
            seen.add(column)
            values = [value] if op == "eq" else value
            if not isinstance(values, list) or not 1 <= len(values) <= 50:
                raise ServiceFault("INVALID_QUERY", "筛选需包含 1 至 50 项有效值")
            if column in {"billing_country", "billing_city"}:
                valid = all(isinstance(v, str) and bool(v.strip()) for v in values)
            else:
                valid = all(isinstance(v, int) and not isinstance(v, bool) and v > 0 for v in values)
            if not valid:
                raise ServiceFault("INVALID_QUERY", "国家和城市应为非空字符串，客户编号应为正整数")
            name = f"filter_{index}"
            conditions.append(f"i.{column} = %({name})s" if op == "eq"
                              else f"i.{column} = ANY(%({name})s)")
            parameters[name] = value
        sql = ("SELECT COALESCE(SUM(i.total), 0) AS sales_amount FROM public.invoice i WHERE "
               + " AND ".join(conditions) + " LIMIT %(fetch_limit)s")
        return sql, parameters
