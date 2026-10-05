"""Full-source Chinook detail audit with a demo Provider and real readonly DB."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from agent_core.sql.detail_example import DetailDemoProvider
from agent_core.sql.generation_service import SQLGenerationService
from database.readonly_connection import AsyncReadOnlyClient, run_database
from .chinook_detail_oracle import parse_full_chinook_sql, full_source_hash, detail_gold

ROOT = Path(__file__).resolve().parents[3]
SOURCE_SHA256 = "e3fde5c1a5b51a2a91429a702c9ca6e69ba56e6c7f5e112724d70c3d03db695e"
FULL_SHA256 = "df63721f72f482c5d66454900b4a1ee2be29df3a638c93f445193fa218885908"


async def capture_source(config, reference):
    """Bounded full-table snapshot, all table locks before the first SELECT."""
    tables = {}
    async def read():
        async with AsyncReadOnlyClient(config).transaction({
            "db_timeout_ms": 5000, "execution_timeout_ms": 15000,
            "_lock_chinook_schema_before_snapshot": True,
        }) as cursor:
            for table, info in reference["schema"].items():
                columns = [column["name"] for column in info["columns"]]
                # Identifiers come exclusively from the hash-pinned seed DDL.
                sql = "SELECT " + ", ".join(columns) + " FROM public." + table
                sql += " ORDER BY " + ", ".join(info["primary_key"]) + " LIMIT %s"
                limit = len(reference["tables"][table]) + 1
                await cursor.execute(sql, (limit,))
                rows = await cursor.fetchmany(limit)
                tables[table] = [{name: value.isoformat() if isinstance(value, datetime)
                                  else format(value, ".2f") if isinstance(value, Decimal) else value
                                  for name, value in row.items()} for row in rows]
    await run_database(read)
    return full_source_hash({**reference, "tables": tables})


def extra_cases():
    return [
        {"table": "public.track", "columns": ["track_id", "name", "unit_price"],
         "filters": [{"column": "unit_price", "op": "gte", "value": "1.99"}],
         "order_by": [{"column": "unit_price", "direction": "desc"}], "page": {"size": 11, "offset": 2}},
        {"table": "public.employee", "filters": [{"column": "reports_to", "op": "is_null"}]},
        {"table": "public.employee", "order_by": [{"column": "reports_to", "direction": "desc"}]},
        {"table": "public.invoice", "filters": [{"column": "invoice_date", "op": "gte", "value": "2024-01-01"},
                                                 {"column": "invoice_date", "op": "lt", "value": "2025-01-01"}],
         "order_by": [{"column": "total", "direction": "desc"}], "page": {"size": 17, "offset": 3}},
        {"table": "public.track", "filters": [{"column": "name", "op": "contains", "value": "%_\\'"}]},
        {"table": "public.track", "filters": [{"column": "name", "op": "starts_with", "value": "Love"}]},
        {"table": "public.customer", "filters": [{"column": "customer_id", "op": "in", "value": [1, 14, 55]}]},
        {"table": "public.genre", "page": {"size": 10, "offset": 1000}},
        {"table": "public.playlist_track", "order_by": [{"column": "track_id", "direction": "desc"}],
         "page": {"size": 23, "offset": 15}},
    ]


async def run(output=None):
    source = ROOT / "database/Chinook_PostgreSql.sql"
    if hashlib.sha256(source.read_bytes()).hexdigest() != SOURCE_SHA256:
        raise ValueError("Chinook source SQL hash mismatch")
    reference = parse_full_chinook_sql(source)
    if full_source_hash(reference) != FULL_SHA256:
        raise ValueError("Independent full-source hash mismatch")
    config = json.loads((ROOT / "database/source_config.json").read_text(encoding="utf-8-sig"))
    config["_base_dir"] = str(ROOT / "database")
    service = SQLGenerationService(DetailDemoProvider(), config=config)
    before = await capture_source(config, reference)
    records, failures = [], []

    async def verify(query, label):
        result = await service.query({"source_id": "chinook_db", "task": "table_detail", **query})
        gold = detail_gold(query, reference)
        correct = result.get("status") == "ok" and result.get("columns") == gold["columns"] and all(result.get(key) == gold[key]
                  for key in ("rows", "row_count", "page", "has_more", "next_offset"))
        record = {"case": label, "table": query["table"], "passed": correct,
                  "row_count": result.get("row_count"), "has_more": result.get("has_more"),
                  "next_offset": result.get("next_offset"), "execution_attempted": result.get("execution_attempted")}
        if not correct:
            record["error_code"] = result.get("error_code", "RESULT_MISMATCH")
            failures.append(label)
        records.append(record)
        return result, correct

    coverage = []
    if before == FULL_SHA256:
        for table, expected_rows in reference["tables"].items():
            total, offset, pages = 0, 0, 0
            while True:
                query = {"table": "public." + table, "columns": "all", "page": {"size": 1000, "offset": offset}}
                result, correct = await verify(query, table + "@" + str(offset))
                pages += 1
                if not correct:
                    break
                total += result["row_count"]
                if not result["has_more"]:
                    break
                if result["next_offset"] != offset + 1000:
                    failures.append(table + ":INVALID_NEXT_OFFSET")
                    break
                offset = result["next_offset"]
            coverage.append({"table": "public." + table, "columns": len(reference["schema"][table]["columns"]),
                             "rows": total, "expected_rows": len(expected_rows), "pages": pages,
                             "complete": total == len(expected_rows)})
        for index, query in enumerate(extra_cases()):
            await verify(query, "typed-filter-sort-page-" + str(index + 1))
    else:
        failures.append("SNAPSHOT_BEFORE_MISMATCH")
    after = await capture_source(config, reference)
    if after != before:
        failures.append("SNAPSHOT_CHANGED")
    code_files = sorted((ROOT / "src/agent_core").rglob("*.py")) + sorted(
        path for path in (ROOT / "database").glob("*.py") if not path.name.startswith("test_"))
    code_hash = hashlib.sha256(b"".join(str(path.relative_to(ROOT)).encode() + b"\0" + path.read_bytes()
                                     + b"\0" for path in code_files)).hexdigest()
    report = {"status": "complete" if not failures else "failed", "source_id": "chinook_db",
              "provider_origin": "demo", "execution_origin": "live", "real_model_quality_verified": False,
              "source_sql_sha256": SOURCE_SHA256, "reference_sha256": FULL_SHA256,
              "snapshot_before_sha256": before, "snapshot_after_sha256": after, "code_version": code_hash,
              "tables": coverage, "total_rows": sum(row["rows"] for row in coverage),
              "total_columns": sum(row["columns"] for row in coverage), "checks": records, "failures": failures}
    if output:
        destination = Path(output).resolve()
        if not destination.is_relative_to(ROOT):
            raise ValueError("Report output must be in the project")
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: report[key] for key in ("status", "total_rows", "total_columns", "failures")}, ensure_ascii=False))
    return 0 if not failures else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live-demo", action="store_true")
    parser.add_argument("--output")
    args = parser.parse_args()
    if not args.live_demo:
        parser.error("Specify --live-demo for the readonly database audit")
    return asyncio.run(run(args.output))


if __name__ == "__main__":
    raise SystemExit(main())
