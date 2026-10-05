"""显式离线示例，不访问模型 API 或数据库。

从项目根目录、配置 PYTHONPATH=src 后运行：
python -B -m agent_core.sql.example --offline
python -B -m agent_core.sql.example --offline --validate
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from agent_core.providers import LLMResponse
from database.metadata_reader import FileMetadataReader

from .generation_service import SQLGenerationService


class OfflineExampleProvider:
    """只演示接口流转，输出不能用于报告真实模型效果。"""

    async def generate(self, request):
        payload = json.loads(request.messages[-1].content)
        if payload.get("schema", {}).get("source_id") == "chinook_db":
            # 固定演示语法；仅用准备模块提供的受控标识符和参数规格。
            predicates = payload["query_spec"]["predicates"]
            conditions = []
            for item in predicates:
                column, parameter, op = item["column"], item["parameter"], item["operation"]
                if column not in {"invoice_date", "billing_country", "billing_city", "customer_id"}:
                    raise ValueError("unsupported demo column")
                operator = {"eq": "=", "gte": ">=", "lt": "<", "in": "="}[op]
                right = f"ANY(:{parameter})" if op == "in" else f":{parameter}"
                conditions.append(f"i.{column} {operator} {right}")
            sql = ("SELECT COALESCE(SUM(i.total), 0) AS sales_amount FROM public.invoice i WHERE "
                   + " AND ".join(conditions) + " LIMIT :fetch_limit")
            return LLMResponse(json.dumps({"sql": sql}))
        raise ValueError("项目仅支持 Chinook")


async def run(*, validate=False):
    base = Path(__file__).resolve().parents[3] / "database"
    config = json.loads((base / "source_config.json").read_text(encoding="utf-8-sig"))
    service = SQLGenerationService(
        OfflineExampleProvider(), base_dir=base, metadata_reader=FileMetadataReader(base, config=config),
    )
    entrypoint = service.generate_and_validate if validate else service.generate
    result = await entrypoint({
        "question": "查询2024年美国发票销售额",
        "source_id": "chinook_db", "metric_id": "sales_amount",
        "metric_definition_id": "chinook_sales_amount_v1",
        "time_range": {"start": "2024-01-01", "end_exclusive": "2025-01-01"},
        "filters": [{"field_id": "country", "op": "eq", "value": "美国"}],
    }, {"request_id": "offline-example"})
    result["provider_mode"] = "offline_example_only"
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == ("validated" if validate else "generated") else 1


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="显式离线演示 SQL 生成接口")
    parser.add_argument("--offline", action="store_true", help="使用文件元数据和演示 Provider")
    parser.add_argument("--validate", action="store_true", help="候选生成后继续第二项 SQL 检验")
    args = parser.parse_args()
    if not args.offline:
        parser.error("请指定 --offline；真实模型请由应用注入 Provider")
    return asyncio.run(run(validate=args.validate))


if __name__ == "__main__":
    raise SystemExit(main())
