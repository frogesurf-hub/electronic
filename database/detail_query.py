"""从受控 Chinook 结构准备单表明细；不连接数据库或接受自由 SQL。"""
from __future__ import annotations

import copy
import hashlib
import json
import re
from collections.abc import Mapping
from datetime import datetime
from decimal import Decimal, DecimalException, localcontext

from .detail_contracts import PreparedDetail
from .sql_common import ServiceFault, json_value
from .sql_contracts import ParameterSpec
from .query_profiles import chinook_amount_type

SOURCE_ID = "chinook_db"
TABLES = tuple("public." + name for name in (
    "album", "artist", "customer", "employee", "genre", "invoice", "invoice_line",
    "media_type", "playlist", "playlist_track", "track",
))
REQUEST_KEYS = {"task", "source_id", "table", "columns", "filters", "order_by", "page"}
OPS = {"eq", "ne", "in", "gt", "gte", "lt", "lte", "is_null", "is_not_null", "contains", "starts_with"}
NULL_OPS = {"is_null", "is_not_null"}
SESSION_KEYS = ("session_id", "task_id", "request_id", "step_id")
_NAME = re.compile(r"[a-z_][a-z_0-9]*\Z")


def _fault(code, message):
    raise ServiceFault(code, message)


def _name(value):
    if not isinstance(value, str) or _NAME.fullmatch(value) is None:
        _fault("INVALID_QUERY", "字段必须使用受控结构中的完整字段名称")
    return value


def _integer(value, lower, upper, label):
    if type(value) is not int or not lower <= value <= upper:
        _fault("INVALID_QUERY", f"{label}必须是 {lower} 至 {upper} 的整数")
    return value


def _kind(physical):
    if not isinstance(physical, str):
        _fault("METADATA_ERROR", "字段物理类型必须是字符串")
    if physical in {"integer", "int4"}:
        return "integer"
    if physical == "text" or re.fullmatch(r"character varying(?:\([1-9]\d*\))?", physical):
        return "text"
    if chinook_amount_type(physical):
        return "decimal"
    if physical in {"timestamp without time zone", "timestamp"}:
        return "timestamp"
    _fault("METADATA_ERROR", "Chinook 明细列类型不在已支持范围内")


def _value(value, column):
    kind = column["data_type"]
    if value is None:
        _fault("INVALID_QUERY", "NULL 筛选必须使用 is_null 或 is_not_null")
    if kind == "integer":
        return _integer(value, -2147483648, 2147483647, "整数筛选值")
    if kind == "text":
        if not isinstance(value, str) or "\x00" in value or len(value) > 4096:
            _fault("INVALID_QUERY", "文本筛选值须是最多 4096 字符且无 NUL 的字符串")
        return value
    if kind == "timestamp":
        if isinstance(value, datetime):
            result = value
        elif isinstance(value, str) and value.strip():
            try:
                result = datetime.fromisoformat(value.strip())
            except ValueError as error:
                raise ServiceFault("INVALID_QUERY", "时间筛选值必须是 ISO 日历时间") from error
        else:
            _fault("INVALID_QUERY", "时间筛选值必须是 ISO 日期或时间字符串")
        if result.tzinfo is not None:
            issue = {"code": "AMBIGUOUS_TIME_SEMANTICS", "field_id": column["name"],
                     "original_value": json_value(value), "reason": "源时间无时区，请确认不带偏移的日历时间", "candidates": []}
            raise ServiceFault("AMBIGUOUS_TIME_SEMANTICS", issue["reason"],
                               status="needs_clarification", issues=[issue])
        return result.isoformat()
    if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
        _fault("INVALID_QUERY", "金额筛选值须使用精确字符串、整数或 Decimal，不能使用浮点数")
    if isinstance(value, str) and (len(value) > 128 or not value.strip()):
        _fault("INVALID_QUERY", "金额筛选值格式异常")
    try:
        amount = Decimal(value)
        precision = int(re.fullmatch(r"numeric\(([1-9]\d*),\s*2\)", column["physical_data_type"])[1])
        with localcontext() as context:
            context.prec = max(28, precision + 4, len(amount.as_tuple().digits) + 4)
            if not amount.is_finite() or amount.copy_abs() >= Decimal(10) ** (precision - 2):
                _fault("INVALID_QUERY", "金额筛选值超出字段精度或不是有限数值")
            rounded = amount.quantize(Decimal("0.01"))
            if rounded != amount:
                _fault("INVALID_QUERY", "金额筛选值不能包含小于分的金额")
            return format(rounded, ".2f")
    except (DecimalException, ValueError, OverflowError) as error:
        raise ServiceFault("INVALID_QUERY", "金额筛选值格式异常") from error


def _pattern(value, operation):
    escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return ("%" if operation == "contains" else "") + escaped + "%"


def _schema(snapshot, table):
    if not isinstance(snapshot, Mapping) or any(not isinstance(snapshot.get(key), list)
                                               for key in ("objects", "columns", "keys", "foreign_keys")):
        _fault("METADATA_ERROR", "明细查询需要完整的实时结构快照")
    schema, name = table.split(".")
    try:
        objects = [row for row in snapshot["objects"] if row["schema_name"] == schema and row["table_name"] == name]
        if len(objects) != 1 or objects[0]["object_type"] != "table":
            _fault("METADATA_ERROR", "目标表缺失、重复或不是已授权的表")
        rows = [row for row in snapshot["columns"] if row["schema_name"] == schema and row["table_name"] == name]
        if not rows or len(rows) > 64:
            _fault("METADATA_ERROR", "目标表字段结构缺失或超过受控上限")
        positions, names, columns = set(), set(), []
        for row in sorted(rows, key=lambda row: row["ordinal_position"]):
            position, column = row["ordinal_position"], row["column_name"]
            if (type(position) is not int or position <= 0 or position in positions
                    or not isinstance(column, str) or _NAME.fullmatch(column) is None or column in names
                    or type(row["is_nullable"]) is not bool):
                _fault("METADATA_ERROR", "目标表字段名称、顺序或 NULL 约束异常")
            positions.add(position)
            names.add(column)
            columns.append({"name": column, "data_type": _kind(row["data_type"]),
                            "physical_data_type": row["data_type"], "nullable": row["is_nullable"]})
        keys = [row for row in snapshot["keys"] if row["schema_name"] == schema
                and row["table_name"] == name and row["constraint_type"] == "primary_key"]
        match = re.fullmatch(r"PRIMARY KEY\s*\(\s*([a-z_][a-z_0-9]*(?:\s*,\s*[a-z_][a-z_0-9]*)*)\s*\)",
                             keys[0]["definition"]) if len(keys) == 1 else None
        if match is None:
            _fault("METADATA_ERROR", "目标表必须有明确且唯一的完整主键")
        primary_key = [item.strip() for item in match[1].split(",")]
        if (len(set(primary_key)) != len(primary_key) or not set(primary_key).issubset(names)
                or any(row["nullable"] for row in columns if row["name"] in primary_key)):
            _fault("METADATA_ERROR", "目标表主键缺失、重复或允许 NULL")
        structural = {key: snapshot[key] for key in ("objects", "columns", "keys", "foreign_keys")}
        fingerprint = hashlib.sha256(json.dumps(structural, ensure_ascii=False, sort_keys=True,
                                                separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()
    except ServiceFault:
        raise
    except (KeyError, TypeError, ValueError, AttributeError, RecursionError) as error:
        raise ServiceFault("METADATA_ERROR", "明细表结构格式异常") from error
    return {"source_id": SOURCE_ID, "table": table, "columns": columns,
            "primary_key": primary_key, "schema_fingerprint_sha256": fingerprint}


def _schema_index(schema):
    if (not isinstance(schema, Mapping) or set(schema) != {"source_id", "table", "columns", "primary_key", "schema_fingerprint_sha256"}
            or schema.get("source_id") != SOURCE_ID or schema.get("table") not in TABLES
            or not isinstance(schema.get("columns"), list) or not 1 <= len(schema["columns"]) <= 64
            or not isinstance(schema.get("primary_key"), list) or not schema["primary_key"]
            or not isinstance(schema.get("schema_fingerprint_sha256"), str)
            or re.fullmatch(r"[a-f0-9]{64}", schema["schema_fingerprint_sha256"]) is None):
        _fault("METADATA_ERROR", "明细结构上下文不完整或来源不正确")
    columns = {}
    for row in schema["columns"]:
        if (not isinstance(row, Mapping) or set(row) != {"name", "data_type", "physical_data_type", "nullable"}
                or not isinstance(row.get("name"), str) or _NAME.fullmatch(row["name"]) is None
                or row["name"] in columns or type(row.get("nullable")) is not bool
                or _kind(row.get("physical_data_type")) != row.get("data_type")):
            _fault("METADATA_ERROR", "明细结构字段缺失、重复或类型不一致")
        columns[row["name"]] = dict(row)
    pk = schema["primary_key"]
    if (any(not isinstance(name, str) or name not in columns or columns[name]["nullable"] for name in pk)
            or len(set(pk)) != len(pk)):
        _fault("METADATA_ERROR", "明细主键声明不合法")
    return columns


def _normalize(query, schema, size_cap):
    columns = _schema_index(schema)
    selection = query.get("columns", "all")
    if selection == "all":
        selection = list(columns)
    elif (not isinstance(selection, list) or not selection or len(selection) > 64
          or any(not isinstance(name, str) or name not in columns for name in selection)
          or len(set(selection)) != len(selection)):
        _fault("COLUMN_NOT_ALLOWED", "投影须为 all 或该表不重复的字段名称列表")
    filters = query.get("filters", [])
    if not isinstance(filters, list) or len(filters) > 64:
        _fault("INVALID_QUERY", "筛选须是最多 64 项的列表")
    normalized = []
    for index, item in enumerate(filters):
        if (not isinstance(item, Mapping) or set(item) - {"column", "op", "value"}
                or not {"column", "op"}.issubset(item)
                or not isinstance(item["column"], str) or item["column"] not in columns
                or not isinstance(item["op"], str) or item["op"] not in OPS):
            _fault("UNSUPPORTED_FILTER", "筛选字段、操作或格式不在允许范围内")
        name, operation = item["column"], item["op"]
        field = columns[name]
        condition = {"column": name, "op": operation}
        if operation in NULL_OPS:
            if "value" in item and item["value"] is not None:
                _fault("INVALID_QUERY", "NULL 操作不接受非 NULL 筛选值")
        else:
            if "value" not in item or item["value"] is None:
                issue = {"code": "MISSING_FILTER_VALUE", "field_id": name, "filter_index": index,
                         "original_value": item.get("value"), "reason": "请补充该字段的筛选值", "candidates": []}
                raise ServiceFault("MISSING_FILTER_VALUE", issue["reason"], status="needs_clarification", issues=[issue])
            if operation in {"gt", "gte", "lt", "lte"} and field["data_type"] == "text":
                _fault("UNSUPPORTED_FILTER", "文本字段不支持范围比较")
            if operation in {"contains", "starts_with"} and field["data_type"] != "text":
                _fault("UNSUPPORTED_FILTER", "contains/starts_with 仅适用于文本字段")
            if operation == "in":
                values = item["value"]
                if not isinstance(values, list) or not 1 <= len(values) <= 50:
                    _fault("INVALID_QUERY", "in 筛选须包含 1 至 50 个同类型值")
                resolved = []
                for value in values:
                    converted = _value(value, field)
                    if converted not in resolved:
                        resolved.append(converted)
                condition["value"] = resolved
            else:
                condition["value"] = _value(item["value"], field)
        normalized.append(condition)
    order = query.get("order_by", [])
    if not isinstance(order, list) or len(order) > len(columns):
        _fault("INVALID_QUERY", "排序须是目标表字段的列表")
    ordered, seen = [], set()
    for item in order:
        if (not isinstance(item, Mapping) or set(item) != {"column", "direction"}
                or not isinstance(item["column"], str) or item["column"] not in columns or item["column"] in seen
                or not isinstance(item["direction"], str) or item["direction"].lower() not in {"asc", "desc"}):
            _fault("INVALID_QUERY", "排序字段或方向不合法；同一字段不能重复排序")
        seen.add(item["column"])
        ordered.append({"column": item["column"], "direction": item["direction"].lower()})
    ordered.extend({"column": name, "direction": "asc"} for name in schema["primary_key"] if name not in seen)
    page = query.get("page", {})
    if not isinstance(page, Mapping) or set(page) - {"size", "offset"}:
        _fault("INVALID_QUERY", "page 仅接受 size 和 offset")
    size = min(_integer(page.get("size", 100), 1, 1000, "page.size"), size_cap)
    offset = _integer(page.get("offset", 0), 0, 1_000_000, "page.offset")
    return {"task": "table_detail", "source_id": SOURCE_ID, "table": schema["table"],
            "columns": list(selection), "filters": normalized, "order_by": ordered,
            "page": {"size": size, "offset": offset}}


def _cap(limits, default=1000):
    if limits is None:
        return default
    if not isinstance(limits, Mapping):
        _fault("INVALID_QUERY", "limits 必须是映射")
    return min(default, _integer(limits.get("max_rows", default), 1, 1000, "max_rows"))


def prepare_detail_request(request, schema_context, limits=None):
    """重建已规范请求和可信参数；供生成、检验及执行前复核共用。"""
    if (not isinstance(request, Mapping) or set(request) != REQUEST_KEYS
            or request.get("task") != "table_detail" or request.get("source_id") != SOURCE_ID
            or not isinstance(schema_context, Mapping) or request.get("table") != schema_context.get("table")):
        _fault("INVALID_GENERATION_REQUEST", "明细生成请求来源或任务不一致")
    schema = copy.deepcopy(dict(schema_context))
    normalized = _normalize(request, schema, _cap(limits))
    if normalized != dict(request):
        _fault("INVALID_GENERATION_REQUEST", "明细生成请求必须保留规范字段、排序、参数值和分页")
    indexed = _schema_index(schema)
    parameters, specs = {}, []
    for index, item in enumerate(normalized["filters"]):
        operation = item["op"]
        if operation in NULL_OPS:
            continue
        name, field = f"filter_{index}", indexed[item["column"]]
        dtype = field["data_type"]
        values = item["value"] if operation == "in" else [item["value"]]
        values = [datetime.fromisoformat(value) if dtype == "timestamp" else Decimal(value) if dtype == "decimal" else value
                  for value in values]
        parameters[name] = values if operation == "in" else _pattern(values[0], operation) if operation in {"contains", "starts_with"} else values[0]
        specs.append(ParameterSpec(name, dtype + ("[]" if operation == "in" else ""), item["column"], operation))
    parameters.update(fetch_limit=normalized["page"]["size"] + 1, page_offset=normalized["page"]["offset"])
    specs.extend((ParameterSpec("fetch_limit", "integer", None, "limit"), ParameterSpec("page_offset", "integer", None, "offset")))
    return PreparedDetail(normalized, schema, tuple(specs), parameters)


def prepare_detail(query, snapshot, config, limits=None):
    """把公开明细 query 和原始结构转换为明确的单表请求；不访问模型或数据库。"""
    if (not isinstance(query, Mapping) or set(query) - (REQUEST_KEYS | {"question"})
            or query.get("task") != "table_detail"
            or ("question" in query and not isinstance(query["question"], str))):
        _fault("INVALID_QUERY", "明细查询须提供 table_detail 任务及允许的结构化字段")
    if (not isinstance(config, Mapping) or config.get("source_id") != SOURCE_ID
            or config.get("dataset") != "chinook" or config.get("database") != "chinook"):
        _fault("INVALID_CONFIGURATION", "明细查询仅支持 Chinook 数据源")
    if query.get("source_id", SOURCE_ID) != SOURCE_ID:
        _fault("UNKNOWN_SOURCE", "明细查询仅支持 chinook_db")
    allowed = config.get("query_table_allowlist")
    if (not isinstance(allowed, list) or not allowed or any(table not in TABLES for table in allowed)
            or len(set(allowed)) != len(allowed)):
        _fault("INVALID_CONFIGURATION", "明细表白名单必须是受控 Chinook 表")
    table = query.get("table")
    if table is None or (isinstance(table, str) and not table.strip()):
        issue = {"code": "MISSING_TABLE", "field_id": "table", "original_value": table,
                 "reason": "请指定要查询的 Chinook 表", "candidates": [{"id": name, "name": name} for name in allowed]}
        raise ServiceFault("MISSING_TABLE", issue["reason"], status="needs_clarification", issues=[issue])
    if not isinstance(table, str):
        _fault("INVALID_QUERY", "table 必须是明确的表名称")
    table = table.strip()
    table = table if "." in table else "public." + table
    if table not in allowed:
        _fault("TABLE_NOT_ALLOWED", "目标表未在 Chinook 明细白名单中获准")
    cap = _cap(limits, _integer(config.get("max_result_rows", 1000), 1, 1000, "max_result_rows"))
    schema = _schema(snapshot, table)
    request = _normalize(query, schema, cap)
    return prepare_detail_request(request, schema, {"max_rows": cap})


reprepare_detail = prepare_detail


def output_columns(request, schema_context):
    indexed = _schema_index(schema_context)
    return [{"name": name, "data_type": indexed[name]["data_type"], "nullable": indexed[name]["nullable"]}
            for name in request["columns"]]


def canonical_sql(request, *, named=True):
    """仅为 prepare_detail_request 接受的内部请求构建规范 SQL。"""
    if (not isinstance(request, Mapping) or set(request) != REQUEST_KEYS
            or request.get("task") != "table_detail" or request.get("source_id") != SOURCE_ID
            or request.get("table") not in TABLES or type(named) is not bool):
        _fault("INVALID_GENERATION_REQUEST", "无法为不合法的明细请求构建 SQL")
    placeholder = (lambda name: ":" + name) if named else (lambda name: "%(" + name + ")s")
    projection = ", ".join("t." + _name(name) for name in request["columns"])
    predicates = []
    symbols = {"eq": "=", "ne": "<>", "gt": ">", "gte": ">=", "lt": "<", "lte": "<="}
    for index, item in enumerate(request["filters"]):
        column, operation = "t." + _name(item["column"]), item["op"]
        binding = placeholder(f"filter_{index}")
        if operation in symbols:
            predicates.append(f"{column} {symbols[operation]} {binding}")
        elif operation == "in":
            predicates.append(f"{column} = ANY({binding})")
        elif operation in NULL_OPS:
            predicates.append(f"{column} IS {'NOT ' if operation == 'is_not_null' else ''}NULL")
        elif operation in {"contains", "starts_with"}:
            predicates.append(f"{column} LIKE {binding} ESCAPE '\\'")
        else:
            _fault("UNSUPPORTED_FILTER", "无法构建未允许的明细筛选")
    order = []
    for item in request["order_by"]:
        if item["direction"] not in {"asc", "desc"}:
            _fault("INVALID_GENERATION_REQUEST", "无法构建未允许的排序方向")
        order.append(f"t.{_name(item['column'])} {item['direction'].upper()} NULLS LAST")
    if not projection or not order:
        _fault("INVALID_GENERATION_REQUEST", "明细投影和稳定排序不能为空")
    return (f"SELECT {projection} FROM {request['table']} AS t"
            + (" WHERE " + " AND ".join(predicates) if predicates else "")
            + " ORDER BY " + ", ".join(order)
            + " LIMIT " + placeholder("fetch_limit") + " OFFSET " + placeholder("page_offset"))


class DetailQueryMapper:
    def __init__(self, config):
        self.config = copy.deepcopy(dict(config))

    def match(self, query, snapshot, context=None):
        context = {} if context is None else context
        session = {}
        try:
            if not isinstance(context, Mapping):
                _fault("INVALID_QUERY", "context 必须为映射")
            for key in SESSION_KEYS:
                if key in context:
                    if not isinstance(context[key], str):
                        _fault("INVALID_QUERY", "关联编号必须为字符串")
                    session[key] = context[key]
            prepared = prepare_detail(query, snapshot, self.config, context)
            return {"status": "ready", "prepared_detail": prepared,
                    "sql_generation_request": copy.deepcopy(prepared.generation_request), "execution_attempted": False, **session}
        except ServiceFault as error:
            result = {"status": error.status, "error_code": error.code, "error_message": str(error),
                      "issues": error.issues, "execution_attempted": False, **session}
            if error.status == "needs_clarification":
                result["clarification_request"] = {"query": json_value(copy.deepcopy(query)),
                                                   "context": json_value(copy.deepcopy(context)), "issues": copy.deepcopy(error.issues)}
            return result
