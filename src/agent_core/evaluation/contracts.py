"""离线评估的数据契约；标准答案永不作为被评估服务的输入。"""
from __future__ import annotations

import copy
from dataclasses import asdict, dataclass, field
from typing import Any


class EvaluationError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class EvalCase:
    case_id: str
    kind: str
    query: dict[str, Any]
    context: dict[str, Any]
    expected: dict[str, Any]
    comparison: dict[str, Any] = field(default_factory=dict)
    scope: str = "sql_v1"
    family: str = ""
    split: str = "dev"
    data_snapshot_id: str = ""

    def __post_init__(self):
        if not isinstance(self.case_id, str) or not self.case_id:
            raise EvaluationError("INVALID_CASE", "case_id 必须为非空字符串")
        if self.kind not in {"answer", "clarification", "safety", "roadmap"}:
            raise EvaluationError("INVALID_CASE", "未知的测试类型")
        if any(not isinstance(value, dict) for value in
               (self.query, self.context, self.expected, self.comparison)):
            raise EvaluationError("INVALID_CASE", "用例输入、标准和比较规则必须为对象")
        if self.split not in {"dev", "test"}:
            raise EvaluationError("INVALID_CASE", "split 必须为 dev 或 test")
        if (self.kind == "roadmap") != (self.scope == "roadmap"):
            raise EvaluationError("INVALID_CASE", "未实现用例必须同时标记 kind/scope=roadmap")


@dataclass(frozen=True, slots=True)
class DatasetBundle:
    cases: tuple[EvalCase, ...]
    manifest: dict[str, Any]


@dataclass(frozen=True, slots=True)
class RunManifest:
    run_id: str
    suite_version: str
    data_snapshot_id: str
    provider_origin: str
    execution_origin: str
    model_id: str | None = None
    prompt_version: str | None = None
    code_version: str | None = None
    versions: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if any(not isinstance(value, str) or not value for value in
               (self.run_id, self.suite_version, self.data_snapshot_id)):
            raise EvaluationError("INVALID_MANIFEST", "运行、用例集和快照版本必须明确")
        if self.provider_origin not in {"real", "fake", "demo", "replay"}:
            raise EvaluationError("INVALID_MANIFEST", "必须声明 Provider 来源")
        if self.execution_origin not in {"live", "mock", "replay"}:
            raise EvaluationError("INVALID_MANIFEST", "必须声明执行来源")
        if self.provider_origin == "real" and (not isinstance(self.model_id, str) or not self.model_id):
            raise EvaluationError("INVALID_MANIFEST", "真实模型评估必须记录 model_id")
        if not isinstance(self.versions, dict):
            raise EvaluationError("INVALID_MANIFEST", "versions 必须为对象")

    def as_dict(self):
        return copy.deepcopy(asdict(self))


@dataclass(frozen=True, slots=True)
class RunConfig:
    timeout_ms: int = 60000
    repeats: int = 1
    split: str = "dev"

    def __post_init__(self):
        for key, value, cap in (("timeout_ms", self.timeout_ms, 120000),
                                ("repeats", self.repeats, 10)):
            if type(value) is not int or not 1 <= value <= cap:
                raise EvaluationError("INVALID_CONFIG", f"{key} 超出正整数范围")
        if self.split not in {"dev", "test", "all"}:
            raise EvaluationError("INVALID_CONFIG", "split 必须为 dev、test 或 all")


@dataclass(frozen=True, slots=True)
class Observation:
    case_id: str
    attempt_id: int
    response: dict[str, Any]
    elapsed_ms: float
    provider_origin: str
    execution_origin: str


@dataclass(frozen=True, slots=True)
class CaseScore:
    case_id: str
    attempt_id: int
    kind: str
    passed: bool | None
    checks: dict[str, bool]
    differences: list[dict[str, Any]]
    failure_category: str | None
    measurement_status: str
    elapsed_ms: float
    actual_status: str | None
    error_code: str | None
    stage: str | None
    execution_attempted: bool | None
    safety_violation: bool = False

    def as_dict(self):
        return copy.deepcopy(asdict(self))


@dataclass(slots=True)
class EvalRun:
    manifest: RunManifest
    cases: tuple[EvalCase, ...]
    observations: list[Observation] = field(default_factory=list)
    status: str = "complete"
    planned_attempts: int = 0
    preconditions: dict[str, Any] = field(default_factory=dict)
    error_code: str | None = None


@dataclass(frozen=True, slots=True)
class EvalReport:
    payload: dict[str, Any]

    def as_dict(self):
        return copy.deepcopy(self.payload)
