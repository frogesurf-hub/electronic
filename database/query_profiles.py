"""受控数据集的物理映射；标识符不能由用户或模型自由指定。"""
from __future__ import annotations

import re

if __package__:
    from .sql_common import ServiceFault
else:
    from sql_common import ServiceFault


PROFILES = {
    "chinook_db": {
        "metric_definition_id": "chinook_sales_amount_v1", "schema": "public", "table": "invoice",
        "amount": "total", "time": "invoice_date", "time_type": "timestamp",
        "grain": "invoice", "unit": "金额", "fixed": None,
    },
}


def query_profile(request):
    profile = PROFILES.get(request.get("source_id"))
    if profile is None:
        raise ServiceFault("UNKNOWN_SOURCE", "不支持的数据源")
    if (request.get("metric_definition_id") != profile["metric_definition_id"]
            or request.get("table") != f'{profile["schema"]}.{profile["table"]}'):
        raise ServiceFault("UNSUPPORTED_QUERY", "数据源、指标口径与物理表映射不一致")
    return profile


def parameter_layout(request):
    profile = query_profile(request)
    specs = []
    specs.extend([
        ("start", profile["time_type"], profile["time"], "gte"),
        ("end", profile["time_type"], profile["time"], "lt"),
        ("fetch_limit", "integer", None, "limit"),
    ])
    for index, item in enumerate(request["filters"]):
        dtype = "integer" if profile["table"] == "invoice" and item["column"] == "customer_id" else "text"
        specs.append((f"filter_{index}", dtype + ("[]" if item["op"] == "in" else ""), item["column"], item["op"]))
    return specs


def chinook_amount_type(data_type):
    """当前金额输出保留两位小数，物理字段也必须明确声明 scale=2。"""
    if not isinstance(data_type, str):
        return False
    match = re.fullmatch(r"numeric\(([1-9]\d*),\s*2\)", data_type)
    return match is not None and len(match[1]) <= 4 and int(match[1]) <= 1000


def compatible_type(profile, column, data_type):
    if column == profile["amount"]:
        return chinook_amount_type(data_type)
    if column == profile["time"]:
        return data_type in {"timestamp without time zone", "timestamp"}
    if profile["table"] == "invoice" and column == "customer_id":
        return data_type in {"integer", "int4"}
    if profile["table"] == "invoice" and column in {"billing_country", "billing_city"}:
        return isinstance(data_type, str) and re.fullmatch(r"(?:text|character varying(?:\(\d+\))?)", data_type) is not None
    return data_type == "text"


def output_columns(source_id):
    return [{"name": "sales_amount", "data_type": "decimal", "unit": PROFILES[source_id]["unit"]}]
