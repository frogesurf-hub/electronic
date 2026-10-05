"""Strict JSONL/manifest loading without a database connection or credentials."""
from __future__ import annotations

import hashlib
import json
import math
import re
from collections import Counter
from pathlib import Path, PureWindowsPath

from .contracts import DatasetBundle, EvalCase, EvaluationError
_HASH = re.compile(r"[a-f0-9]{64}\Z")
_ID = re.compile(r"[A-Za-z0-9_.-]{1,128}\Z")
_CASE_KEYS = {"case_id", "kind", "query", "context", "expected", "comparison", "scope",
              "family", "split", "data_snapshot_id"}


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise EvaluationError("INVALID_JSON", f"Duplicate JSON member: {key}")
        result[key] = value
    return result


def _reject_constant(value):
    raise EvaluationError("INVALID_JSON", f"Nonfinite JSON number: {value}")


def _finite(value):
    if isinstance(value, float) and not math.isfinite(value):
        raise EvaluationError("INVALID_JSON", "Nonfinite JSON number")
    if isinstance(value, dict):
        for item in value.values():
            _finite(item)
    elif isinstance(value, list):
        for item in value:
            _finite(item)


def _json(text):
    try:
        value = json.loads(text, object_pairs_hook=_object, parse_constant=_reject_constant)
    except (json.JSONDecodeError, RecursionError) as error:
        raise EvaluationError("INVALID_JSON", "Malformed evaluation JSON") from error
    try:
        _finite(value)
    except RecursionError as error:
        raise EvaluationError("INVALID_JSON", "Evaluation JSON nesting is too deep") from error
    return value


def _read(path):
    try:
        return path.read_bytes()
    except OSError as error:
        raise EvaluationError("ARTIFACT_NOT_FOUND", f"Cannot read evaluation artifact: {path.name}") from error


def _text(contents):
    try:
        return contents.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise EvaluationError("INVALID_JSON", "Evaluation artifacts must be UTF-8") from error


def _digest(value, name):
    if not isinstance(value, str) or not _HASH.fullmatch(value):
        raise EvaluationError("INVALID_MANIFEST", f"{name} must be a lowercase SHA-256 digest")
    return value


def _check_hash(contents, expected, name):
    if hashlib.sha256(contents).hexdigest() != _digest(expected, name):
        raise EvaluationError("HASH_MISMATCH", f"Evaluation artifact hash changed: {name}")


def _project_root(manifest_path):
    # The distributed layout is <root>/evaluation/datasets/manifest.json.
    # A portable test bundle may put manifest.json directly in its own root.
    parent = manifest_path.parent
    if parent.name == "datasets" and parent.parent.name == "evaluation":
        return parent.parent.parent.resolve()
    return parent.resolve()


def _artifact(root, value):
    if (not isinstance(value, str) or not value or Path(value).is_absolute()
            or PureWindowsPath(value).is_absolute()
            or PureWindowsPath(value).drive or ".." in PureWindowsPath(value).parts
            or "\x00" in value
            or any(part.lower() in {".local", ".env"} for part in PureWindowsPath(value).parts)):
        raise EvaluationError("INVALID_MANIFEST", "Artifact paths must be relative inside the project")
    path = (root / value).resolve()
    if not path.is_relative_to(root):
        raise EvaluationError("INVALID_MANIFEST", "Artifact path leaves the project")
    if any(part.lower() in {".local", ".env"} for part in path.relative_to(root).parts):
        raise EvaluationError("INVALID_MANIFEST", "Artifact resolves into a credential directory")
    return path


def load_cases(path, manifest_path=None):
    """项目入口仅加载已冻结的 Chinook 评估集。"""
    from .chinook_dataset import load_chinook_cases
    return load_chinook_cases(path, manifest_path)
