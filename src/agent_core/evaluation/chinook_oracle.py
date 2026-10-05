"""Independent Chinook seed parser and integer-cents invoice oracle.

No production mapper, SQL generator, Provider, database driver or credential
reader is imported. SQL is data: only literal INSERT VALUES are parsed, never
executed. Invoice dates retain their naive calendar values.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from datetime import datetime
from decimal import Decimal, InvalidOperation, localcontext
from pathlib import Path

from .contracts import EvaluationError

CHINOOK_ORACLE_VERSION = "independent-chinook-invoice-cents-v1"
CHINOOK_PROJECTION_VERSION = "chinook-invoice-projection-v1"
CHINOOK_SNAPSHOT_TABLES = {
    "invoices": ("public.invoice", "invoice_id", (
        "invoice_id", "customer_id", "invoice_date", "billing_country", "billing_city", "total")),
    "customers": ("public.customer", "customer_id", (
        "customer_id", "first_name", "last_name")),
}
COUNTRY_ALIASES = {
    "美国": "USA", "美利坚": "USA", "United States": "USA", "United States of America": "USA",
    "加拿大": "Canada", "巴西": "Brazil", "德国": "Germany", "英国": "United Kingdom",
    "法国": "France", "印度": "India", "澳大利亚": "Australia", "葡萄牙": "Portugal",
    "捷克": "Czech Republic", "芬兰": "Finland", "挪威": "Norway", "瑞典": "Sweden",
    "西班牙": "Spain", "意大利": "Italy", "荷兰": "Netherlands", "波兰": "Poland", "丹麦": "Denmark",
}
FIELD_ALIASES = {"customer": "customer", "country": "country", "billing_country": "country",
                 "region": "country", "city": "city", "billing_city": "city",
                 "城市": "city", "账单城市": "city"}
_INSERT = re.compile(r"^INSERT INTO ([a-z_]+) \(([^)]+)\) VALUES", re.MULTILINE)
_NUMBER = re.compile(r"NULL|-?\d+(?:\.\d+)?")


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      allow_nan=False).encode("utf-8")


def _hash(value):
    return hashlib.sha256(_canonical(value)).hexdigest()


def chinook_time(value) -> datetime:
    if isinstance(value, datetime):
        result = value
    elif isinstance(value, str) and value.strip():
        try:
            result = datetime.fromisoformat(value.strip())
        except ValueError as error:
            raise EvaluationError("INVALID_ORACLE_INPUT", "Invoice dates must be ISO calendar timestamps") from error
    else:
        raise EvaluationError("INVALID_ORACLE_INPUT", "Invoice time must be a datetime or ISO string")
    if result.tzinfo is not None:
        raise EvaluationError("AMBIGUOUS_TIME_SEMANTICS", "Chinook timestamps must have no timezone")
    return result


def chinook_money_cents(value) -> int:
    if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
        raise EvaluationError("INVALID_SNAPSHOT", "Money must use exact transport types")
    try:
        amount = Decimal(value)
    except (InvalidOperation, ValueError) as error:
        raise EvaluationError("INVALID_SNAPSHOT", "Invalid invoice amount") from error
    if not amount.is_finite() or abs(amount) >= Decimal("100000000"):
        raise EvaluationError("INVALID_SNAPSHOT", "Amount is nonfinite or outside numeric(10,2)")
    with localcontext() as context:
        context.prec = max(28, len(amount.as_tuple().digits) + 2)
        cents = amount * 100
        if cents != cents.to_integral_value():
            raise EvaluationError("INVALID_SNAPSHOT", "Amount has fractional cents")
        return int(cents)


def format_chinook_cents(value: int) -> str:
    if type(value) is not int:
        raise EvaluationError("INVALID_ORACLE_INPUT", "Cents must be an integer")
    whole, fraction = divmod(abs(value), 100)
    return ("-" if value < 0 else "") + f"{whole}.{fraction:02d}"


def canonical_chinook_snapshot_payload(data: dict) -> dict:
    if not isinstance(data, Mapping):
        raise EvaluationError("INVALID_SNAPSHOT", "Chinook snapshot must be an object")
    tables = {}
    for key, (_, primary_key, columns) in CHINOOK_SNAPSHOT_TABLES.items():
        records = data.get(key)
        if not isinstance(records, list):
            raise EvaluationError("INVALID_SNAPSHOT", f"Missing Chinook table: {key}")
        rows, seen = [], set()
        for record in records:
            if not isinstance(record, Mapping) or not set(columns).issubset(record):
                raise EvaluationError("INVALID_SNAPSHOT", f"Missing projected columns: {key}")
            row = {}
            for column in columns:
                value = record[column]
                if column in {"invoice_id", "customer_id"}:
                    if type(value) is not int or not 1 <= value <= 2147483647:
                        raise EvaluationError("INVALID_SNAPSHOT", "Chinook IDs must be positive PostgreSQL integers")
                elif column == "invoice_date":
                    value = chinook_time(value).isoformat()
                elif column == "total":
                    value = format_chinook_cents(chinook_money_cents(value))
                elif value is None and column in {"billing_country", "billing_city"}:
                    pass
                elif not isinstance(value, str):
                    raise EvaluationError("INVALID_SNAPSHOT", f"Expected text: {key}.{column}")
                row[column] = value
            if row[primary_key] in seen:
                raise EvaluationError("INVALID_SNAPSHOT", f"Duplicate primary key: {key}")
            seen.add(row[primary_key])
            rows.append(row)
        tables[key] = sorted(rows, key=lambda row: row[primary_key])
    customers = {row["customer_id"] for row in tables["customers"]}
    if any(row["customer_id"] not in customers for row in tables["invoices"]):
        raise EvaluationError("INVALID_SNAPSHOT", "Invoice references a missing customer")
    return {"projection_version": CHINOOK_PROJECTION_VERSION, "tables": tables}


def canonical_chinook_snapshot_hash(data: dict) -> str:
    return _hash(canonical_chinook_snapshot_payload(data))


def chinook_semantic_metadata_hash(data: dict) -> str:
    tables = canonical_chinook_snapshot_payload(data)["tables"]
    return _hash({
        "oracle_version": CHINOOK_ORACLE_VERSION, "customers": tables["customers"],
        "countries": sorted({row["billing_country"] for row in tables["invoices"] if row["billing_country"]}),
        "cities": sorted({row["billing_city"] for row in tables["invoices"] if row["billing_city"]}),
        "billing_locations": sorted({(row["billing_country"], row["billing_city"])
                                     for row in tables["invoices"]}, key=lambda pair: (pair[0] or "", pair[1] or "")),
        "country_aliases": COUNTRY_ALIASES,
        "metric": {"source_id": "chinook_db", "metric_definition_id": "chinook_sales_amount_v1",
                   "table": "public.invoice", "aggregate": "sum(total)", "alias": "sales_amount",
                   "time_column": "invoice_date", "time_semantics": "naive_calendar",
                   "time_bounds": "start_inclusive_end_exclusive", "unit": "金额", "currency": None},
    })


def parse_chinook_sql(path) -> dict:
    """Read literal INSERT statements only; reject expressions and other values.

    DDL/psql commands are never executed. Only the explicitly defined invoice
    and customer projection survives into the returned frozen snapshot.
    """
    try:
        text = Path(path).read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError) as error:
        raise EvaluationError("INVALID_SOURCE_SQL", "Cannot read UTF-8 Chinook source") from error
    records = {"invoice": [], "customer": []}

    def skip(index):
        while index < len(text) and text[index].isspace():
            index += 1
        return index

    def quoted(index):
        national = text[index:index + 2] == "N'"
        if national:
            index += 1
        index += 1
        value = []
        while index < len(text):
            if text[index] == "'":
                if text[index:index + 2] == "''":
                    value.append("'")
                    index += 2
                    continue
                result = "".join(value)
                # PostgreSQL national literals have bpchar semantics: coercion
                # to the seed's varchar columns removes trailing ASCII spaces.
                return result.rstrip(" ") if national else result, index + 1
            value.append(text[index])
            index += 1
        raise EvaluationError("INVALID_SOURCE_SQL", "Unterminated seed string")

    for match in _INSERT.finditer(text):
        table, columns = match.group(1), [item.strip() for item in match.group(2).split(",")]
        if len(set(columns)) != len(columns) or any(not re.fullmatch(r"[a-z_]+", item) for item in columns):
            raise EvaluationError("INVALID_SOURCE_SQL", "Invalid INSERT columns")
        index = skip(match.end())
        while index < len(text) and text[index] == "(":
            index += 1
            values = []
            while True:
                index = skip(index)
                if text[index:index + 1] == "'" or text[index:index + 2] == "N'":
                    value, index = quoted(index)
                else:
                    token = _NUMBER.match(text, index)
                    if token is None:
                        raise EvaluationError("INVALID_SOURCE_SQL", "Seed contains a nonliteral value")
                    literal = token.group()
                    value = None if literal == "NULL" else Decimal(literal) if "." in literal else int(literal)
                    index = token.end()
                values.append(value)
                index = skip(index)
                if index >= len(text) or text[index] not in {",", ")"}:
                    raise EvaluationError("INVALID_SOURCE_SQL", "Invalid VALUES separator")
                if text[index] == ")":
                    index += 1
                    break
                index += 1
            if len(columns) != len(values):
                raise EvaluationError("INVALID_SOURCE_SQL", "VALUES width differs from columns")
            if table in records:
                records[table].append(dict(zip(columns, values)))
            index = skip(index)
            if index < len(text) and text[index] == ";":
                break
            if index >= len(text) or text[index] != ",":
                raise EvaluationError("INVALID_SOURCE_SQL", "INSERT must terminate with semicolon")
            index = skip(index + 1)
            if index >= len(text) or text[index] != "(":
                raise EvaluationError("INVALID_SOURCE_SQL", "Expected a literal row")
        else:
            raise EvaluationError("INVALID_SOURCE_SQL", "INSERT VALUES contains no literal rows")
    if not records["invoice"] or not records["customer"]:
        raise EvaluationError("INVALID_SOURCE_SQL", "Source lacks invoice/customer seed data")
    for row in records["invoice"]:
        value = row.get("invoice_date")
        try:
            row["invoice_date"] = datetime.strptime(value, "%Y/%m/%d").isoformat()
        except (TypeError, ValueError) as error:
            raise EvaluationError("INVALID_SOURCE_SQL", "Unexpected source invoice date literal") from error
    return canonical_chinook_snapshot_payload({"invoices": records["invoice"], "customers": records["customer"]})["tables"]


def resolve_chinook_entity(field: str, value, data: dict, config=None):
    """Independent resolver used for gold; no live/business implementation calls."""
    if field == "customer":
        if isinstance(value, bool) or not isinstance(value, (str, int)) or (isinstance(value, int) and value <= 0):
            raise EvaluationError("INVALID_ORACLE_INPUT", "Customer requires positive integer ID or name")
        text = str(value).strip()
        candidates = set()
        for row in data["customers"]:
            names = (row["first_name"], row["last_name"], row["first_name"] + " " + row["last_name"])
            if (text.isascii() and text.isdecimal() and int(text) == row["customer_id"]) or any(
                    text.casefold() == name.strip().casefold() for name in names):
                candidates.add(row["customer_id"])
    elif field in {"country", "city"}:
        if not isinstance(value, str) or not value.strip():
            raise EvaluationError("INVALID_ORACLE_INPUT", "Location requires nonempty text")
        column = "billing_country" if field == "country" else "billing_city"
        options = {row[column] for row in data["invoices"] if isinstance(row[column], str) and row[column]}
        text = value.strip().casefold()
        candidates = {option for option in options if option.strip().casefold() == text}
        if field == "country":
            aliases = {**COUNTRY_ALIASES, **(config or {}).get("country_aliases", {})}
            candidates.update(target for alias, target in aliases.items()
                              if alias.strip().casefold() == text and target in options)
    else:
        raise EvaluationError("INVALID_ORACLE_INPUT", "Unsupported entity field")
    if len(candidates) != 1:
        raise EvaluationError("AMBIGUOUS_ENTITY" if candidates else "UNKNOWN_ENTITY", "Entity must be unique")
    return next(iter(candidates))


def evaluate_chinook_sales(query: dict, data: dict, *, max_rows=100, config=None) -> dict:
    allowed = {"question", "source_id", "metric_id", "metric_definition_id", "time_range", "filters"}
    if (not isinstance(query, Mapping) or set(query) - allowed
            or ("question" in query and not isinstance(query["question"], str))
            or query.get("source_id") != "chinook_db"
            or query.get("metric_definition_id") != "chinook_sales_amount_v1"):
        raise EvaluationError("INVALID_ORACLE_INPUT", "Unsupported confirmed Chinook query")
    if "metric_id" in query and query["metric_id"] not in (
            "sales_amount", "销售额", "销售金额", "营业额", "发票销售额"):
        raise EvaluationError("INVALID_ORACLE_INPUT", "Unsupported invoice metric")
    if type(max_rows) is not int or not 1 <= max_rows <= 1000:
        raise EvaluationError("INVALID_ORACLE_INPUT", "Invalid max_rows")
    tables = canonical_chinook_snapshot_payload(data)["tables"]
    period = query.get("time_range")
    if not isinstance(period, Mapping) or set(period) != {"start", "end_exclusive"}:
        raise EvaluationError("INVALID_ORACLE_INPUT", "Exact half-open time boundaries are required")
    start, end = chinook_time(period["start"]), chinook_time(period["end_exclusive"])
    if start >= end:
        raise EvaluationError("INVALID_ORACLE_INPUT", "Invalid time range")
    parameters = {"start": start.isoformat(), "end": end.isoformat(), "fetch_limit": max_rows + 1}
    filters, seen, predicates = query.get("filters", []), set(), []
    if not isinstance(filters, list):
        raise EvaluationError("INVALID_ORACLE_INPUT", "Filters must be an array")
    for index, item in enumerate(filters):
        if not isinstance(item, Mapping) or set(item) != {"field_id", "op", "value"}:
            raise EvaluationError("INVALID_ORACLE_INPUT", "Invalid filter shape")
        field = FIELD_ALIASES.get(item["field_id"]) if isinstance(item["field_id"], str) else None
        if (field is None or field in seen or not isinstance(item["op"], str)
                or item["op"] not in {"eq", "in"}):
            raise EvaluationError("INVALID_ORACLE_INPUT", "Unsupported/duplicate filter")
        seen.add(field)
        values = item["value"] if item["op"] == "in" else [item["value"]]
        if not isinstance(values, list) or not 1 <= len(values) <= 50:
            raise EvaluationError("INVALID_ORACLE_INPUT", "in requires 1 to 50 values")
        resolved = list(dict.fromkeys(resolve_chinook_entity(field, value, tables, config) for value in values))
        parameters[f"filter_{index}"] = resolved if item["op"] == "in" else resolved[0]
        predicates.append(({"customer": "customer_id", "country": "billing_country", "city": "billing_city"}[field], resolved))
    total = sum(chinook_money_cents(row["total"]) for row in tables["invoices"]
                if start <= chinook_time(row["invoice_date"]) < end
                and all(row[column] in values for column, values in predicates))
    return {"status": "ok", "route_to": "answer", "source_id": "chinook_db",
            "metric_definition_id": "chinook_sales_amount_v1", "unit": "金额",
            "columns": [{"name": "sales_amount", "data_type": "decimal", "unit": "金额"}],
            "rows": [{"sales_amount": format_chinook_cents(total)}], "row_count": 1, "truncated": False,
            "parameters": parameters, "execution_allowed": True, "execution_attempted": True,
            "gold": {"reviewed_by": "independent_python_oracle", "oracle_version": CHINOOK_ORACLE_VERSION,
                     "arithmetic": "integer_cents", "timestamp_semantics": "naive_calendar", "currency": None}}
