"""离线 SQL 服务评估；通过公开 query 接口复用现有数据库链路。"""
from .contracts import (
    DatasetBundle, EvalCase, EvalReport, EvalRun, EvaluationError,
    Observation, RunConfig, RunManifest,
)
from .dataset import load_cases
from .runner import EvaluationRunner, run_suite
from .scoring import score_case
from .reporting import summarize

__all__ = ["DatasetBundle", "EvalCase", "EvalReport", "EvalRun", "EvaluationError",
           "Observation", "RunConfig", "RunManifest", "load_cases", "EvaluationRunner",
           "run_suite", "score_case", "summarize"]
