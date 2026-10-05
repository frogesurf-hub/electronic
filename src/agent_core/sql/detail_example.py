"""Fixed demo Provider for Chinook detail SQL; it does not measure model quality.

Offline candidate example:
  python -B -m agent_core.sql.detail_example --table public.track --offline --validate-only
Default execution uses the existing dedicated read-only database identity.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
from pathlib import Path

from agent_core.providers import LLMResponse
from database.metadata_reader import FileMetadataReader

from .generation_service import SQLGenerationService

TABLES = {"public." + name for name in (
    "album", "artist", "customer", "employee", "genre", "invoice", "invoice_line",
    "media_type", "playlist", "playlist_track", "track")}


class DetailDemoProvider:
    """Build a candidate only from sanitized projection/predicates/order names."""
    async def generate(self, request):
        payload = json.loads(request.messages[-1].content)
        spec, schema = payload["query_spec"], payload["schema"]
        table = spec["table"]
        if spec.get("task") != "table_detail" or table not in TABLES or schema["table"] != table:
            raise ValueError("invalid demo detail source")
        columns = {row["name"] for row in schema["columns"]}
        if any(not isinstance(name, str) or not re.fullmatch(r"[a-z_][a-z_0-9]*", name) for name in columns):
            raise ValueError("invalid demo column")

        def column(name):
            if name not in columns:
                raise ValueError("undeclared demo column")
            return "d." + name

        def parameter(name):
            if name not in {item["name"] for item in payload["parameters"]} or not re.fullmatch(r"filter_[0-9]+", name):
                raise ValueError("invalid demo parameter")
            return ":" + name

        clauses = []
        symbols = {"eq": "=", "ne": "<>", "gt": ">", "gte": ">=", "lt": "<", "lte": "<="}
        for item in spec["predicates"]:
            name, operation = column(item["column"]), item["operation"]
            if operation in {"is_null", "is_not_null"}:
                clauses.append(name + (" IS NULL" if operation == "is_null" else " IS NOT NULL"))
            elif operation == "in":
                clauses.append(name + " = ANY(" + parameter(item["parameter"]) + ")")
            elif operation in {"contains", "starts_with"}:
                clauses.append(name + " LIKE " + parameter(item["parameter"]) + " ESCAPE '\\'")
            else:
                clauses.append(name + " " + symbols[operation] + " " + parameter(item["parameter"]))
        ordered = []
        for item in spec["order_by"]:
            if item["direction"] not in {"asc", "desc"} or item.get("nulls") != "last":
                raise ValueError("invalid demo order")
            ordered.append(column(item["column"]) + " " + item["direction"].upper() + " NULLS LAST")
        if not spec["columns"] or not ordered or spec.get("limit_parameter") != "fetch_limit" or spec.get("offset_parameter") != "page_offset":
            raise ValueError("invalid demo projection/page")
        sql = ("SELECT " + ", ".join(column(name) for name in spec["columns"]) + " FROM " + table + " AS d"
               + (" WHERE " + " AND ".join(clauses) if clauses else "")
               + " ORDER BY " + ", ".join(ordered) + " LIMIT :fetch_limit OFFSET :page_offset")
        return LLMResponse(json.dumps({"sql": sql}))


async def run(args):
    base = Path(__file__).resolve().parents[3] / "database"
    reader = FileMetadataReader(base) if args.offline else None
    service = SQLGenerationService(DetailDemoProvider(), base_dir=base, metadata_reader=reader)
    query = {"task": "table_detail", "source_id": "chinook_db", "table": args.table,
             "columns": "all" if args.columns is None else args.columns.split(","),
             "filters": json.loads(args.filters), "page": {"size": args.page_size, "offset": args.offset}}
    if args.generate_only:
        result = await service.generate(query)
    elif args.validate_only:
        result = await service.generate_and_validate(query)
    else:
        result = await service.query(query)
    result["provider_origin"] = "demo"
    result["model_quality_measured"] = False
    print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
    return 0 if result["status"] in {"ok", "generated", "validated"} else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--table", default="public.artist")
    parser.add_argument("--columns", help="comma-separated explicit columns; default is all")
    parser.add_argument("--filters", default="[]", help="JSON array of typed filters")
    parser.add_argument("--page-size", type=int, default=100)
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--offline", action="store_true", help="use explicit file metadata; generation/validation only")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--generate-only", action="store_true")
    group.add_argument("--validate-only", action="store_true")
    args = parser.parse_args()
    if args.offline and not (args.generate_only or args.validate_only):
        parser.error("--offline requires --generate-only or --validate-only")
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
