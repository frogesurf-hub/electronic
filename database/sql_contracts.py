"""SQL 生成与检验的数据契约；候选与通过检验的执行包分开保存。"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from .sql_common import json_value
from .query_profiles import output_columns


@dataclass(frozen=True, slots=True)
class ParameterSpec:
    name: str
    data_type: str
    column: str | None
    operation: str


@dataclass(frozen=True, slots=True)
class PreparedGeneration:
    generation_request: dict[str, Any]
    schema_context: dict[str, Any]
    parameter_specs: tuple[ParameterSpec, ...]
    parameters: dict[str, Any]


@dataclass(frozen=True, slots=True)
class SQLCandidate:
    sql: str
    source_id: str
    metric_definition_id: str
    parameter_names: tuple[str, ...]
    schema_fingerprint_sha256: str | None
    session_id: str | None = None
    task_id: str | None = None
    request_id: str | None = None
    step_id: str | None = None
    dialect: str = field(default="postgresql", init=False)
    placeholder_style: str = field(default="named", init=False)
    validated: bool = field(default=False, init=False)

    def as_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["parameter_names"] = list(self.parameter_names)
        return value


@dataclass(frozen=True, slots=True)
class ValidatedSQL:
    """供后续只读执行模块使用；参数每次读取均返回独立副本。

    validated 表示通过本模块策略，并不表示已经执行或数据库权限已验证。
    """

    sql: str
    candidate_sql: str
    source_id: str
    metric_definition_id: str
    schema_fingerprint_sha256: str
    policy_version: str
    candidate_sha256: str
    sql_sha256: str
    max_rows: int
    db_timeout_ms: int
    _parameter_items: tuple[tuple[str, Any], ...] = field(repr=False)
    session_id: str | None = None
    task_id: str | None = None
    request_id: str | None = None
    step_id: str | None = None
    dialect: str = field(default="postgresql", init=False)
    placeholder_style: str = field(default="psycopg_named", init=False)
    validated: bool = field(default=True, init=False)

    @property
    def parameters(self) -> dict[str, Any]:
        return {name: list(value) if isinstance(value, tuple) else value
                for name, value in self._parameter_items}

    def as_dict(self) -> dict[str, Any]:
        value = asdict(self)
        del value["_parameter_items"]
        value["parameters"] = json_value(self.parameters)
        value["output_columns"] = output_columns(self.source_id)
        return value


@dataclass(frozen=True, slots=True)
class ValidationResult:
    status: str
    validated_sql: ValidatedSQL | None = None
    error_code: str | None = None
    error_message: str | None = None

    @property
    def passed(self) -> bool:
        return self.status == "passed" and self.validated_sql is not None

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status, "passed": self.passed,
            "error_code": self.error_code, "error_message": self.error_message,
            "validated_sql": self.validated_sql.as_dict() if self.validated_sql else None,
        }
