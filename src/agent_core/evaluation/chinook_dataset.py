"""Strict loading of the independent frozen Chinook SQL development suite."""
from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

from .chinook_oracle import (
    CHINOOK_ORACLE_VERSION, CHINOOK_PROJECTION_VERSION, canonical_chinook_snapshot_hash,
    chinook_semantic_metadata_hash, chinook_time, evaluate_chinook_sales, parse_chinook_sql,
)
from .contracts import DatasetBundle, EvalCase, EvaluationError
from .dataset import _CASE_KEYS, _artifact, _check_hash, _digest, _json, _project_root, _read, _text


def _same_gold(actual, expected):
    if type(actual) is not type(expected):
        return False
    if isinstance(expected, dict):
        return set(actual) == set(expected) and all(_same_gold(actual[key], value)
                                                    for key, value in expected.items())
    if isinstance(expected, list):
        return len(actual) == len(expected) and all(_same_gold(a, b) for a, b in zip(actual, expected))
    return actual == expected


def _expected(case):
    expected, rules = case.expected, case.comparison
    if (set(rules) - {"decimal_scale", "order_sensitive"}
            or type(rules.get("decimal_scale", 2)) is not int or rules.get("decimal_scale", 2) != 2
            or type(rules.get("order_sensitive", False)) is not bool
            or type(expected.get("execution_allowed")) is not bool
            or type(expected.get("execution_attempted")) is not bool):
        raise EvaluationError("INVALID_CASE", "Chinook comparison and execution contract must be explicit")
    if case.kind == "answer":
        if (expected.get("status") != "ok" or expected["execution_allowed"] is not True
                or expected["execution_attempted"] is not True
                or expected.get("source_id") != "chinook_db"
                or expected.get("metric_definition_id") != "chinook_sales_amount_v1"
                or expected.get("unit") != "金额"
                or expected.get("columns") != [{"name": "sales_amount", "data_type": "decimal", "unit": "金额"}]
                or expected.get("tables") != ["public.invoice"]
                or type(expected.get("row_count")) is not int or expected["row_count"] != 1
                or expected.get("truncated") is not False):
            raise EvaluationError("INVALID_CASE", "Answer must bind the supported invoice contract")
        rows, params = expected.get("rows"), expected.get("parameters")
        if (not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict)
                or set(rows[0]) != {"sales_amount"} or not isinstance(rows[0]["sales_amount"], str)
                or re.fullmatch(r"(?:0|[1-9][0-9]*)\.[0-9]{2}", rows[0]["sales_amount"]) is None
                or not isinstance(params, dict) or not {"start", "end", "fetch_limit"}.issubset(params)
                or "status" in params or type(params["fetch_limit"]) is not int
                or not 2 <= params["fetch_limit"] <= 1001):
            raise EvaluationError("INVALID_CASE", "Answer money and typed parameters are invalid")
        if chinook_time(params["start"]) >= chinook_time(params["end"]):
            raise EvaluationError("INVALID_CASE", "Answer calendar range is invalid")
    else:
        if (expected["execution_allowed"] is not False or expected["execution_attempted"] is not False
                or expected.get("rows") != [] or expected.get("columns") != []):
            raise EvaluationError("INVALID_CASE", "Blocked cases must forbid execution and result rows")
        if case.kind == "clarification":
            codes, fields, candidates = (expected.get("issue_codes"), expected.get("clarification_fields"),
                                         expected.get("permitted_candidates"))
            for values in (codes, fields):
                if (not isinstance(values, list) or not values
                        or any(not isinstance(value, str) or not value for value in values)
                        or len(set(values)) != len(values)):
                    raise EvaluationError("INVALID_CASE", "Clarification codes and fields must be exact")
            if (expected.get("status") != "needs_clarification" or not isinstance(candidates, dict)
                    or set(candidates) != set(fields)):
                raise EvaluationError("INVALID_CASE", "Clarification candidate fields must agree")
            for field, values in candidates.items():
                if (not isinstance(values, list) or any(not ((isinstance(value, str) and value)
                        or (type(value) is int and value > 0 and field == "customer")) for value in values)
                        or len(set((type(value).__name__, value) for value in values)) != len(values)):
                    raise EvaluationError("INVALID_CASE", "Candidate IDs must preserve their entity types")
            members = expected.get("missing_members", {})
            if (not isinstance(members, dict) or not set(members).issubset(fields)
                    or any(not isinstance(values, list) or not values
                           or any(not isinstance(value, str) or not value for value in values)
                           or len(set(values)) != len(values) for values in members.values())
                    or "MISSING_TIME_RANGE" in codes and "time_range" not in members):
                raise EvaluationError("INVALID_CASE", "Missing fields need exact missing members")
        elif case.kind == "safety":
            if (expected.get("status") != "error" or not isinstance(expected.get("error_code"), str)
                    or not expected["error_code"]):
                raise EvaluationError("INVALID_CASE", "Rejection requires its stable error code")
        elif expected.get("status") != "roadmap":
            raise EvaluationError("INVALID_CASE", "Future work must be marked roadmap")
    gold = expected.get("gold")
    if (not isinstance(gold, dict) or gold.get("reviewed_by") != "independent_python_oracle"
            or gold.get("oracle_version") != CHINOOK_ORACLE_VERSION):
        raise EvaluationError("INVALID_CASE", "Gold provenance must identify the independent Chinook oracle")


def load_chinook_cases(path, manifest_path=None):
    """No connection or model calls; every answer is recomputed from literal seed rows."""
    path = Path(path).resolve()
    manifest_path = Path(manifest_path).resolve() if manifest_path else path.with_name("chinook_manifest.json")
    manifest = _json(_text(_read(manifest_path)))
    if (not isinstance(manifest, dict) or type(manifest.get("format_version")) is not int
            or manifest["format_version"] != 1 or manifest.get("source_id") != "chinook_db"
            or manifest.get("projection_version") != CHINOOK_PROJECTION_VERSION
            or manifest.get("oracle_version") != CHINOOK_ORACLE_VERSION
            or not isinstance(manifest.get("suite_version"), str) or not manifest["suite_version"]
            or not isinstance(manifest.get("split"), str) or manifest["split"] not in {"dev", "test"}):
        raise EvaluationError("INVALID_MANIFEST", "Invalid Chinook manifest identity/version")
    root = _project_root(manifest_path)
    if not path.is_relative_to(root):
        raise EvaluationError("INVALID_MANIFEST", "Case path leaves project")
    artifacts = manifest.get("artifacts")
    fields = {"source_sql": "source_sql_sha256", "schema": "schema_file_sha256",
              "semantic_config": "semantic_config_sha256", "oracle": "oracle_file_sha256"}
    if not isinstance(artifacts, dict) or set(artifacts) != set(fields):
        raise EvaluationError("INVALID_MANIFEST", "All four frozen artifacts must be explicit")
    paths = {}
    for label, digest in fields.items():
        paths[label] = _artifact(root, artifacts[label])
        _check_hash(_read(paths[label]), manifest.get(digest), digest)
    schema = _json(_text(_read(paths["schema"])))
    if not isinstance(schema, dict) or any(not isinstance(schema.get(key), list)
                                         for key in ("objects", "columns", "keys", "foreign_keys")):
        raise EvaluationError("INVALID_MANIFEST", "Schema projection is incomplete")
    config = _json(_text(_read(paths["semantic_config"])))
    if (not isinstance(config, dict) or config.get("dataset") != "chinook"
            or config.get("source_id") != "chinook_db" or config.get("database") != "chinook"
            or config.get("currency") is not None or config.get("amount_unit") != "金额"):
        raise EvaluationError("INVALID_MANIFEST", "Frozen configuration must use Chinook source semantics")
    data = parse_chinook_sql(paths["source_sql"])
    digest = canonical_chinook_snapshot_hash(data)
    if (digest != _digest(manifest.get("snapshot_sha256"), "snapshot_sha256")
            or manifest.get("data_snapshot_id") != f"sha256:{digest}"
            or chinook_semantic_metadata_hash(data) != _digest(manifest.get("semantic_metadata_sha256"), "semantic_metadata_sha256")
            or manifest.get("snapshot_row_counts") != {key: len(value) for key, value in data.items()}
            or any(type(value) is not int for value in manifest["snapshot_row_counts"].values())):
        raise EvaluationError("HASH_MISMATCH", "Frozen Chinook facts or semantics changed")
    contents = _read(path)
    _check_hash(contents, manifest.get("cases_sha256"), "cases_sha256")
    cases, identifiers = [], set()
    required = _CASE_KEYS - {"comparison"}
    for line in _text(contents).splitlines():
        if not line.strip():
            continue
        raw = _json(line)
        if (not isinstance(raw, dict) or set(raw) - _CASE_KEYS or not required.issubset(raw)
                or not isinstance(raw.get("case_id"), str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", raw["case_id"])
                or not isinstance(raw.get("family"), str) or not raw["family"]
                or not isinstance(raw.get("scope"), str) or raw["scope"] not in {"chinook_sql_v1", "roadmap"}
                or not isinstance(raw.get("kind"), str) or not isinstance(raw.get("split"), str)
                or raw.get("data_snapshot_id") != manifest["data_snapshot_id"]
                or raw.get("split") != manifest["split"]):
            raise EvaluationError("INVALID_CASE", "Invalid Chinook case identity/scope")
        try:
            case = EvalCase(**raw)
        except TypeError as error:
            raise EvaluationError("INVALID_CASE", "Invalid case contract") from error
        if case.case_id in identifiers:
            raise EvaluationError("DUPLICATE_CASE_ID", "Duplicate Chinook case ID")
        identifiers.add(case.case_id)
        _expected(case)
        if case.kind == "answer":
            max_rows = case.context.get("max_rows", 100)
            if type(max_rows) is not int or not 1 <= max_rows <= 1000:
                raise EvaluationError("INVALID_CASE", "max_rows must be an integer in the supported range")
            gold = evaluate_chinook_sales(case.query, data, max_rows=max_rows, config=config)
            for key in ("source_id", "metric_definition_id", "unit", "columns", "rows", "parameters"):
                if not _same_gold(case.expected[key], gold[key]):
                    raise EvaluationError("GOLD_MISMATCH", f"Independent Chinook gold differs: {case.case_id}/{key}")
        cases.append(case)
    if (not cases or type(manifest.get("case_count")) is not int or manifest["case_count"] != len(cases)
            or manifest.get("kind_counts") != dict(Counter(case.kind for case in cases))
            or any(type(value) is not int for value in manifest["kind_counts"].values())):
        raise EvaluationError("INVALID_MANIFEST", "Exact case counts disagree")
    return DatasetBundle(tuple(cases), manifest)
