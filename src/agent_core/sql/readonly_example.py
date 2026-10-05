"""用固定示例 Provider 联调现有真实数据库的只读执行；不调用模型 API。"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys

from .example import OfflineExampleProvider
from .generation_service import SQLGenerationService


async def run(args):
    service = SQLGenerationService(OfflineExampleProvider())
    result = await service.query({
        "source_id": "chinook_db", "metric_id": "sales_amount",
        "metric_definition_id": "chinook_sales_amount_v1",
        "time_range": {"start": "2024-01-01", "end_exclusive": "2025-01-01"},
        "filters": [{"field_id": "country", "op": "eq", "value": "美国"}],
    }, {
        "request_id": "readonly-live-example", "max_rows": args.max_rows,
        "db_timeout_ms": args.db_timeout_ms, "execution_timeout_ms": args.execution_timeout_ms,
        "request_timeout_ms": args.request_timeout_ms,
    })
    result.update(provider_mode="fixed_example_provider", model_api_called=False)
    print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
    return 0 if result["status"] == "ok" else 1


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--max-rows", type=int, default=100)
    parser.add_argument("--db-timeout-ms", type=int, default=5000)
    parser.add_argument("--execution-timeout-ms", type=int, default=10000)
    parser.add_argument("--request-timeout-ms", type=int, default=30000)
    return asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
