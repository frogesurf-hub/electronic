"""运行示例：python -m database.query_example [--query-file 需求.json]。"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from uuid import uuid4

if __package__:
    from .sql_service import SQLService
else:
    from sql_service import SQLService


def main():
    parser = argparse.ArgumentParser(description="调用 SQLService 查询或返回澄清请求")
    parser.add_argument("--query-file", type=Path, help="结构化查询需求 JSON 文件")
    args = parser.parse_args()
    query = (json.loads(args.query_file.read_text(encoding="utf-8-sig")) if args.query_file else {
        "question": "查询2024年美国发票销售额",
        "source_id": "chinook_db",
        "metric_id": "sales_amount",
        "metric_definition_id": "chinook_sales_amount_v1",
        "time_range": {"start": "2024-01-01", "end_exclusive": "2025-01-01"},
        "filters": [{"field_id": "country", "op": "eq", "value": "美国"}],
    })
    result = SQLService().query(query, {"request_id": str(uuid4())})
    sys.stdout.reconfigure(encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] in {"ok", "needs_clarification"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
