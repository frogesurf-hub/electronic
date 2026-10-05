"""有界、可取消的串行评估；只向服务传 query/context。"""
from __future__ import annotations

import asyncio
import copy
import time
from collections.abc import Mapping

from .contracts import EvalRun, EvaluationError, Observation, RunConfig


class EvaluationRunner:
    """外部取消传播，last_run 保留已完成记录并标记 interrupted。"""

    def __init__(self):
        self.last_run = None

    async def run_suite(self, service, cases, manifest, config=None, *, snapshot_verifier=None):
        config = config or RunConfig()
        selected = tuple(copy.deepcopy(case) for case in cases
                         if config.split == "all" or case.split == config.split)
        ids = [case.case_id for case in selected]
        if len(set(ids)) != len(ids):
            raise EvaluationError("DUPLICATE_CASE_ID", "测试编号重复")
        if any(case.data_snapshot_id != manifest.data_snapshot_id for case in selected):
            raise EvaluationError("SNAPSHOT_MISMATCH", "用例与运行快照版本不一致")
        if not callable(getattr(service, "query", None)):
            raise EvaluationError("INVALID_TARGET", "被评估对象必须提供异步 query 接口")
        run = EvalRun(manifest=copy.deepcopy(manifest), cases=selected,
                      planned_attempts=sum(case.kind != "roadmap" for case in selected) * config.repeats)
        self.last_run = run
        try:
            initial = await self._verify(snapshot_verifier, manifest, config)
            run.preconditions["initial"] = initial
            if initial.get("verified") is not True:
                run.status, run.error_code = "invalid", "SNAPSHOT_NOT_VERIFIED"
                run.preconditions["verified"] = False
                return run
            for case in selected:
                if case.kind == "roadmap":
                    continue
                for attempt_id in range(1, config.repeats + 1):
                    # expected / comparison / gold SQL 永不传入目标服务。
                    query, context = copy.deepcopy(case.query), copy.deepcopy(case.context)
                    context.setdefault("session_id", f"eval:{manifest.run_id}")
                    context.setdefault("task_id", case.case_id)
                    context["request_id"] = f"{manifest.run_id}:{case.case_id}:{attempt_id}"
                    context["step_id"] = str(attempt_id)
                    started = time.perf_counter()
                    try:
                        async with asyncio.timeout(config.timeout_ms / 1000):
                            response = await service.query(query, context)
                        if not isinstance(response, Mapping):
                            raise TypeError("target response must be a mapping")
                        response = copy.deepcopy(dict(response))
                    except asyncio.CancelledError:
                        raise
                    except TimeoutError:
                        response = {"status": "error", "stage": "evaluation",
                                    "error_code": "EVALUATION_TIMEOUT", "execution_attempted": None}
                    except Exception:
                        # 不记录底层异常文本，避免连接配置或 Provider 密钥进入报告。
                        response = {"status": "error", "stage": "evaluation",
                                    "error_code": "TARGET_ERROR", "execution_attempted": None}
                    run.observations.append(Observation(
                        case.case_id, attempt_id, response,
                        round((time.perf_counter() - started) * 1000, 3),
                        manifest.provider_origin, manifest.execution_origin,
                    ))
                    baseline = initial.get("schema_fingerprint_sha256")
                    if isinstance(baseline, str):
                        for key in ("metadata", "execution_metadata"):
                            metadata = response.get(key)
                            if (isinstance(metadata, Mapping)
                                    and isinstance(metadata.get("schema_fingerprint_sha256"), str)
                                    and metadata["schema_fingerprint_sha256"] != baseline):
                                run.preconditions["schema_changed_during_run"] = True
            final = await self._verify(snapshot_verifier, manifest, config)
            run.preconditions["final"] = final
            run.preconditions["verified"] = final.get("verified") is True
            if run.preconditions.get("schema_changed_during_run") is True:
                run.preconditions["verified"] = False
            if run.preconditions["verified"] is not True:
                run.status, run.error_code = "invalid", "SNAPSHOT_CHANGED"
            return run
        except asyncio.CancelledError:
            run.status, run.error_code = "interrupted", "RUN_CANCELLED"
            run.preconditions["verified"] = False
            raise

    @staticmethod
    async def _verify(verifier, manifest, config):
        if verifier is None:
            return {"verified": False, "error_code": "SNAPSHOT_VERIFIER_REQUIRED"}
        try:
            async with asyncio.timeout(config.timeout_ms / 1000):
                result = await verifier(copy.deepcopy(manifest))
            if not isinstance(result, Mapping):
                return {"verified": False, "error_code": "INVALID_SNAPSHOT_VERIFICATION"}
            return copy.deepcopy(dict(result))
        except asyncio.CancelledError:
            raise
        except Exception:
            return {"verified": False, "error_code": "SNAPSHOT_VERIFICATION_FAILED"}


async def run_suite(service, cases, manifest, config=None, *, snapshot_verifier=None):
    return await EvaluationRunner().run_suite(service, cases, manifest, config,
                                            snapshot_verifier=snapshot_verifier)
