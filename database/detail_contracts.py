"""单表明细候选与内部执行包；与销售额汇总契约独立。"""
from __future__ import annotations

import copy
from dataclasses import asdict, dataclass, field
from typing import Any

from .sql_common import json_value
from .sql_contracts import ParameterSpec


@dataclass(frozen=True, slots=True)
class PreparedDetail:
    generation_request: dict[str, Any]
    schema_context: dict[str, Any]
    parameter_specs: tuple[ParameterSpec, ...]
    parameters: dict[str, Any]


@dataclass(frozen=True, slots=True)
class DetailCandidate:
    sql: str
    source_id: str
    table: str
    parameter_names: tuple[str, ...]
    schema_fingerprint_sha256: str | None
    session_id: str | None = None
    task_id: str | None = None
    request_id: str | None = None
    step_id: str | None = None
    task: str = field(default="table_detail", init=False)
    dialect: str = field(default="postgresql", init=False)
    placeholder_style: str = field(default="named", init=False)
    validated: bool = field(default=False, init=False)

    def as_dict(self):
        value = asdict(self)
        value["parameter_names"] = list(self.parameter_names)
        return value


@dataclass(frozen=True, slots=True)
class ValidatedDetailSQL:
    sql: str
    candidate_sql: str
    source_id: str
    table: str
    schema_fingerprint_sha256: str
    policy_version: str
    candidate_sha256: str
    sql_sha256: str
    max_rows: int
    db_timeout_ms: int
    page_offset: int
    _parameter_items: tuple[tuple[str, Any], ...] = field(repr=False)
    _output_columns: tuple[dict[str, Any], ...] = field(repr=False)
    session_id: str | None = None
    task_id: str | None = None
    request_id: str | None = None
    step_id: str | None = None
    task: str = field(default="table_detail", init=False)
    dialect: str = field(default="postgresql", init=False)
    placeholder_style: str = field(default="psycopg_named", init=False)
    validated: bool = field(default=True, init=False)

    def __post_init__(self):
        object.__setattr__(self, "_parameter_items", copy.deepcopy(self._parameter_items))
        object.__setattr__(self, "_output_columns", copy.deepcopy(self._output_columns))

    @property
    def parameters(self):
        return {key: list(value) if isinstance(value, tuple) else copy.deepcopy(value)
                for key, value in self._parameter_items}

    @property
    def output_columns(self):
        return [copy.deepcopy(value) for value in self._output_columns]

    def as_dict(self):
        value = asdict(self)
        del value["_parameter_items"], value["_output_columns"]
        value["parameters"] = json_value(self.parameters)
        value["output_columns"] = self.output_columns
        value["page"] = {"size": self.max_rows, "offset": self.page_offset}
        return value


@dataclass(frozen=True, slots=True)
class DetailValidationResult:
    status: str
    validated_sql: ValidatedDetailSQL | None = None
    error_code: str | None = None
    error_message: str | None = None

    @property
    def passed(self):
        return self.status == "passed" and self.validated_sql is not None

    def as_dict(self):
        return {"status": self.status, "passed": self.passed,
                "error_code": self.error_code, "error_message": self.error_message,
                "validated_sql": self.validated_sql.as_dict() if self.validated_sql else None}
