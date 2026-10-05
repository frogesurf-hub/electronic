"""Independent full Chinook source oracle; SQL INSERT data is never executed.

This reference reads seed literals and DDL without importing the production
mapper, generator, validator, executor, Provider or database driver.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from decimal import Decimal
from functools import cmp_to_key
from pathlib import Path

from .contracts import EvaluationError
from .chinook_oracle import chinook_money_cents, format_chinook_cents

DETAIL_ORACLE_VERSION = "independent-chinook-full-detail-v1"
_INSERT = re.compile(r"^INSERT INTO ([a-z_]+) \(([^)]+)\) VALUES", re.MULTILINE)
_NUMBER = re.compile(r"NULL|-?\d+(?:\.\d+)?")


def parse_source_schema(text):
    schema = {}
    for match in re.finditer(r"^CREATE TABLE ([a-z_]+)\s*\((.*?)\);", text, re.MULTILINE | re.DOTALL):
        table, body = match.groups()
        columns = []
        for line in body.splitlines():
            column = re.fullmatch(r"\s*([a-z_]+) (INT|VARCHAR\(\d+\)|NUMERIC\(\d+,\d+\)|TIMESTAMP)( NOT NULL)?,?\s*", line)
            if column:
                name, dtype, required = column.groups()
                kind = "integer" if dtype == "INT" else "timestamp" if dtype == "TIMESTAMP" else "decimal" if dtype.startswith("NUMERIC") else "text"
                columns.append({"name": name, "data_type": kind, "nullable": required is None})
        pk = re.search(r"PRIMARY KEY\s*\(([^)]+)\)", body)
        if not columns or pk is None:
            raise EvaluationError("INVALID_SOURCE_SQL", "Missing source columns or primary key")
        keys = [name.strip() for name in pk[1].split(",")]
        if len(set(keys)) != len(keys) or any(name not in {c["name"] for c in columns} for name in keys):
            raise EvaluationError("INVALID_SOURCE_SQL", "Invalid source primary key")
        schema[table] = {"columns": columns, "primary_key": keys}
    if len(schema) != 11 or sum(len(item["columns"]) for item in schema.values()) != 64:
        raise EvaluationError("INVALID_SOURCE_SQL", "Expected the frozen 11-table/64-column source")
    return schema


def parse_full_chinook_sql(path) -> dict:
    """Read literal INSERT statements only; reject expressions and other values.

    DDL/psql commands are never executed. All 11 tables retain every source field.
    """
    try:
        text = Path(path).read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError) as error:
        raise EvaluationError("INVALID_SOURCE_SQL", "Cannot read UTF-8 Chinook source") from error
    schema = parse_source_schema(text)
    records = {table: [] for table in schema}

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
    for table, rows in records.items():
        if not rows:
            raise EvaluationError("INVALID_SOURCE_SQL", "Missing table seed: " + table)
        names = [column["name"] for column in schema[table]["columns"]]
        seen = set()
        for row in rows:
            if set(row) != set(names):
                raise EvaluationError("INVALID_SOURCE_SQL", "Seed fields do not match source DDL")
            for column in schema[table]["columns"]:
                name, kind, value = column["name"], column["data_type"], row[column["name"]]
                if value is None:
                    if not column["nullable"]:
                        raise EvaluationError("INVALID_SOURCE_SQL", "NULL in required source field")
                elif kind == "timestamp":
                    try:
                        row[name] = datetime.strptime(value, "%Y/%m/%d").isoformat()
                    except (TypeError, ValueError) as error:
                        raise EvaluationError("INVALID_SOURCE_SQL", "Invalid source calendar date") from error
                elif kind == "decimal":
                    row[name] = format_chinook_cents(chinook_money_cents(value))
                elif kind == "integer" and type(value) is not int:
                    raise EvaluationError("INVALID_SOURCE_SQL", "Invalid integer source field")
                elif kind == "text" and not isinstance(value, str):
                    raise EvaluationError("INVALID_SOURCE_SQL", "Invalid text source field")
            key = tuple(row[name] for name in schema[table]["primary_key"])
            if key in seen:
                raise EvaluationError("INVALID_SOURCE_SQL", "Duplicate source primary key")
            seen.add(key)
        rows.sort(key=lambda row: tuple(row[name] for name in schema[table]["primary_key"]))
    return {"version": DETAIL_ORACLE_VERSION, "schema": schema, "tables": records}


def full_source_hash(parsed):
    return hashlib.sha256(json.dumps(parsed, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def detail_gold(query, parsed):
    """Source reference page; text sorting here is Unicode, not PG collation.

    Live audit compares only integer/decimal/calendar ordering and primary keys.
    """
    table = query["table"].removeprefix("public.")
    schema = parsed["schema"][table]
    types = {column["name"]: column["data_type"] for column in schema["columns"]}
    columns = list(types) if query.get("columns", "all") == "all" else list(query["columns"])

    def typed(column, value):
        if value is None:
            return None
        kind = types[column]
        if kind == "decimal":
            return Decimal(value)
        if kind == "timestamp":
            return datetime.fromisoformat(value)
        return value

    def accept(row, item):
        name, op = item["column"], item["op"]
        actual = typed(name, row[name])
        if op == "is_null":
            return actual is None
        if op == "is_not_null":
            return actual is not None
        if actual is None:
            return False
        value = item["value"]
        if op == "in":
            return actual in [typed(name, v) for v in value]
        expected = typed(name, value)
        if op == "contains":
            return expected in actual
        if op == "starts_with":
            return actual.startswith(expected)
        return {"eq": lambda: actual == expected, "ne": lambda: actual != expected,
                "gt": lambda: actual > expected, "gte": lambda: actual >= expected,
                "lt": lambda: actual < expected, "lte": lambda: actual <= expected}[op]()

    order = list(query.get("order_by", []))
    ordered = {item["column"] for item in order}
    order += [{"column": name, "direction": "asc"} for name in schema["primary_key"] if name not in ordered]

    def compare(left, right):
        for item in order:
            name = item["column"]
            a, b = typed(name, left[name]), typed(name, right[name])
            if a is None or b is None:
                result = 0 if a is b else 1 if a is None else -1
            else:
                result = (a > b) - (a < b)
                if item["direction"] == "desc":
                    result = -result
            if result:
                return result
        return 0

    rows = sorted([row for row in parsed["tables"][table] if all(accept(row, item) for item in query.get("filters", []))], key=cmp_to_key(compare))
    page = query.get("page", {})
    size, offset = page.get("size", 100), page.get("offset", 0)
    selected = rows[offset:offset + size]
    has_more = offset + size < len(rows)
    indexed = {column["name"]: column for column in schema["columns"]}
    return {"columns": [indexed[name] for name in columns],
            "rows": [{name: row[name] for name in columns} for row in selected],
            "row_count": len(selected), "page": {"size": size, "offset": offset},
            "has_more": has_more, "next_offset": offset + size if has_more else None}
