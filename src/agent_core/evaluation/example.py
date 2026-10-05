"""固定示例 Provider + 现有真实只读数据库的评估联调，不调用模型 API。"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
import uuid
from pathlib import Path

from agent_core.providers import LLMResponse
from agent_core.sql.generation_service import SQLGenerationService
from agent_core.sql.llm_sql_generator import PROMPT_VERSION

from .contracts import RunConfig, RunManifest
from .dataset import load_cases
from .reporting import summarize
from .runner import run_suite
from .snapshot import ROOT, SnapshotVerifier


class PredicateDemoProvider:
    """只根据受控谓词构造示例候选；不读取数据、gold 或 query 参数值。"""

    async def generate(self, request):
        payload = json.loads(request.messages[-1].content)
        spec, schema = payload["query_spec"], payload["schema"]
        table, amount, alias = ("public.invoice", "total", "i")
        if schema["table"] != table or schema["source_id"] != "chinook_db":
            raise ValueError("unsupported demo source")
        predicates = []
        columns = {"invoice_date", "billing_country", "billing_city", "customer_id"}
        for item in spec["predicates"]:
            column, operation, name = item["column"], item["operation"], item["parameter"]
            if column not in columns or not (name in {"start", "end"} or
                                            (name.startswith("filter_") and name[7:].isdigit())):
                raise ValueError("unsupported demo predicate")
            if operation == "in":
                predicates.append(f"{alias}.{column} = ANY(:{name})")
            else:
                symbol = {"eq": "=", "gte": ">=", "lt": "<"}[operation]
                predicates.append(f"{alias}.{column} {symbol} :{name}")
        sql = (f"SELECT COALESCE(SUM({alias}.{amount}), 0) AS sales_amount FROM {table} {alias} WHERE "
               + " AND ".join(predicates) + " LIMIT :fetch_limit")
        return LLMResponse(json.dumps({"sql": sql}))


class SourceDatasetMismatch(ValueError):
    """当前数据源不适用于支持的冻结评估集。"""


def _source():
    try:
        path = ROOT / "database/source_config.json"
        source = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError, TypeError) as error:
        raise SourceDatasetMismatch("无法确认当前数据源；请检查 database/source_config.json") from error
    if not isinstance(source, dict):
        raise SourceDatasetMismatch("数据源配置必须为对象")
    chinook = source.get("source_id") == "chinook_db" and source.get("dataset") == "chinook"
    if not chinook:
        raise SourceDatasetMismatch("项目仅支持 Chinook 冻结评估集")
    source["_base_dir"] = str(ROOT / "database")
    return source


async def evaluate_service(service, bundle, manifest, *, config=None, snapshot_verifier):
    """应用可注入自己的真实 Provider 服务与独立快照核验器。"""
    run = await run_suite(service, bundle.cases, manifest, config,
                          snapshot_verifier=snapshot_verifier)
    return summarize(run)


async def run(args):
    source = _source()
    from .chinook_snapshot import ChinookSnapshotVerifier
    bundle = load_cases(ROOT / "evaluation/datasets/chinook_sql_v1.jsonl",
                        ROOT / "evaluation/datasets/chinook_manifest.json")
    verifier_type = ChinookSnapshotVerifier
    service = SQLGenerationService(PredicateDemoProvider(), config=source)
    source_files = sorted((ROOT / "src/agent_core").rglob("*.py")) + sorted(
        path for path in (ROOT / "database").glob("*.py") if not path.name.startswith("test_"))
    code_hash = hashlib.sha256(b"".join(
        str(path.relative_to(ROOT)).encode("utf-8") + b"\0" + path.read_bytes() + b"\0"
        for path in source_files)).hexdigest()
    manifest = RunManifest(
        run_id=uuid.uuid4().hex, suite_version=bundle.manifest["suite_version"],
        data_snapshot_id=bundle.manifest["data_snapshot_id"], provider_origin="demo",
        execution_origin="live", prompt_version=PROMPT_VERSION,
        code_version=code_hash, versions=dict(bundle.manifest),
    )
    verifier = verifier_type(bundle.manifest, mode="live", config=service.config)
    report = await evaluate_service(service, bundle, manifest,
                                    config=RunConfig(repeats=args.repeats), snapshot_verifier=verifier)
    payload = report.as_dict()
    serialized = json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False)
    if args.output:
        output = Path(args.output).resolve()
        if not output.is_relative_to(ROOT):
            raise ValueError("示例报告只能保存到项目目录")
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(serialized + "\n", encoding="utf-8")
    # 终端只展示摘要；详细逐题记录可通过 --output 保存。
    summary = {key: payload.get(key) for key in
               ("run_id", "status", "quality_claim", "coverage", "metrics", "performance", "failure_counts")}
    print(json.dumps(summary, ensure_ascii=False, indent=2, allow_nan=False))
    return 0 if (payload.get("status") == "complete" and payload.get("measurement_status") == "scored"
                 and not payload.get("failure_counts") and payload.get("safety_violation_count") == 0) else 1


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live-demo", action="store_true", help="使用固定示例 Provider 与现有真实只读库")
    parser.add_argument("--output", help="报告路径（位于项目目录）")
    parser.add_argument("--repeats", type=int, default=1)
    args = parser.parse_args()
    try:
        _source()
    except SourceDatasetMismatch as error:
        parser.error(str(error))
    if not args.live_demo:
        parser.error("请显式指定 --live-demo；真实模型由应用通过 evaluate_service 注入")
    try:
        return asyncio.run(run(args))
    except SourceDatasetMismatch as error:
        parser.error(str(error))


if __name__ == "__main__":
    raise SystemExit(main())
