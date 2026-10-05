"""只比较标准与实际观察；不调用模型、数据库或文件系统。"""
from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, localcontext

from .contracts import CaseScore

_BEIJING = timezone(timedelta(hours=8))
_DECIMAL_TYPES = {"decimal", "numeric", "money"}
_INTEGER_TYPES = {"int", "integer", "bigint", "smallint"}
_TIME_KEYS = {"start", "end", "end_exclusive"}


def _text(value):
    return value if isinstance(value, str) else None


def _timestamp(value, *, naive_calendar=False):
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    if not isinstance(value, datetime):
        raise ValueError("invalid time value")
    if naive_calendar:
        if value.tzinfo is not None:
            raise ValueError("calendar timestamp must not carry a timezone")
        return value
    if value.tzinfo is None:
        value = value.replace(tzinfo=_BEIJING)
    return value.astimezone(timezone.utc)


def _decimal(value, places):
    # 二进制 float 与 bool 不属于金额契约。
    if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
        raise ValueError("invalid decimal value")
    number = Decimal(value)
    if not number.is_finite() or abs(number.adjusted()) > 1000:
        raise ValueError("non-finite or oversized decimal")
    with localcontext() as context:
        context.prec = max(28, len(number.as_tuple().digits) + places + 2,
                           number.adjusted() + places + 3)
        quantized = number.quantize(Decimal(1).scaleb(-places))
        if number != quantized:
            raise ValueError("fractional digits exceed the declared exact precision")
        return quantized


def _cell(value, data_type, places):
    if value is None:
        return ("null",)
    if data_type in _DECIMAL_TYPES:
        return ("decimal", _decimal(value, places))
    if data_type in _INTEGER_TYPES:
        if type(value) is not int:
            raise ValueError("integer result must be int")
        return ("integer", value)
    if data_type in {"timestamp", "timestamptz", "timestamp with time zone"}:
        return ("timestamp", _timestamp(value))
    if data_type in {"bool", "boolean"}:
        if type(value) is not bool:
            raise ValueError("boolean result must be bool")
        return ("boolean", value)
    if not isinstance(value, str):
        raise ValueError("text result must be str")
    return ("text", value)


def _column_map(columns):
    if not isinstance(columns, list) or not columns:
        raise ValueError("columns must be a nonempty list")
    result = {}
    for column in columns:
        if (not isinstance(column, Mapping) or not isinstance(column.get("name"), str)
                or not column["name"] or not isinstance(column.get("data_type"), str)
                or column["name"] in result):
            raise ValueError("invalid column contract")
        result[column["name"]] = column
    return result


def _rows(rows, columns, places):
    if not isinstance(rows, list):
        raise ValueError("rows must be a list")
    names = sorted(columns)
    result = []
    for row in rows:
        if not isinstance(row, Mapping) or set(row) != set(names):
            raise ValueError("row columns do not match")
        result.append(tuple(_cell(row[name], columns[name]["data_type"], places)
                            for name in names))
    return result


def _same_parameter(actual, expected, key, *, naive_calendar=False):
    if key in _TIME_KEYS:
        try:
            return (_timestamp(actual, naive_calendar=naive_calendar)
                    == _timestamp(expected, naive_calendar=naive_calendar))
        except (ValueError, TypeError, OverflowError):
            return False
    if isinstance(expected, Decimal):
        if isinstance(actual, bool) or not isinstance(actual, (Decimal, str, int)):
            return False
        try:
            number = Decimal(actual)
            return number.is_finite() and expected.is_finite() and number == expected
        except (InvalidOperation, ValueError):
            return False
    if isinstance(expected, Mapping):
        return (isinstance(actual, Mapping) and set(actual) == set(expected)
                and all(_same_parameter(actual[name], value, name, naive_calendar=naive_calendar)
                        for name, value in expected.items()))
    if isinstance(expected, (list, tuple)):
        return (isinstance(actual, (list, tuple)) and len(actual) == len(expected)
                and all(_same_parameter(a, b, "", naive_calendar=naive_calendar)
                        for a, b in zip(actual, expected)))
    return type(actual) is type(expected) and actual == expected


def _candidate_ids(candidates, field_id):
    if not isinstance(candidates, list):
        raise ValueError("candidates must be a list")
    ids = []
    for candidate in candidates:
        if isinstance(candidate, str) or type(candidate) is int:
            identifier = candidate
        elif isinstance(candidate, Mapping):
            key = "metric_definition_id" if field_id == "metric_definition_id" else "id"
            identifier = candidate.get(key)
        else:
            identifier = None
        if not ((isinstance(identifier, str) and identifier)
                or (type(identifier) is int and identifier > 0)):
            raise ValueError("candidate must have a stable identifier")
        # 保留ID类型，整数1不能与字符串"1"或布尔True互换。
        ids.append((type(identifier).__name__, identifier))
    return Counter(ids)


def _issue_facts(issues):
    if not isinstance(issues, list):
        raise ValueError("issues must be a list")
    facts = []
    for issue in issues:
        if not isinstance(issue, Mapping):
            raise ValueError("issue must be an object")
        field_id, code = issue.get("field_id"), issue.get("code")
        if not isinstance(field_id, str) or not isinstance(code, str):
            raise ValueError("issue needs stable field and code")
        members = issue.get("missing_members", [])
        if not isinstance(members, list) or any(not isinstance(member, str) for member in members):
            raise ValueError("invalid missing member list")
        indexes = tuple(issue.get(key) for key in ("filter_index", "value_index"))
        if any(index is not None and type(index) is not int for index in indexes):
            raise ValueError("invalid issue index")
        facts.append((code, field_id, tuple(sorted(_candidate_ids(issue.get("candidates", []), field_id).items())),
                      tuple(sorted(members)), indexes))
    return Counter(facts)


def _failure_category(response, checks, safety_violation):
    if safety_violation:
        return "unsafe_execution"
    code = _text(response.get("error_code")) or ""
    if "TIMEOUT" in code:
        return "timeout"
    if response.get("status") == "error":
        return {
            "input": "input_or_mapping", "metadata": "metadata_error",
            "mapping": "input_or_mapping", "generation": "llm_error",
            "validation": "sql_validation", "execution": "sql_execution",
            "evaluation": "target_error",
        }.get(_text(response.get("stage")), "service_error")
    if checks.get("status") is False:
        return "unexpected_status"
    if checks.get("parameters") is False:
        return "parameter_mismatch"
    if any(checks.get(key) is False for key in ("source_id", "metric_definition_id", "unit", "tables")):
        return "source_or_semantics"
    if checks.get("not_truncated") is False:
        return "incomplete_result"
    if any(checks.get(key) is False for key in
           ("validation_passed", "validation_consistent", "execution_attempted", "live_metadata", "same_transaction_revalidation",
            "no_execution", "not_validated")):
        return "execution_contract"
    if any(checks.get(key) is False for key in
           ("issue_codes", "clarification_fields", "permitted_candidates", "missing_members", "issues_consistent")):
        return "clarification_mismatch"
    return "result_mismatch"


def score_case(case, observation) -> CaseScore:
    """金额精确比较；Chinook 参数按无时区日历时间和完整参数集合核对。"""
    response = observation.response if isinstance(observation.response, Mapping) else {}
    attempted = response.get("execution_attempted")
    attempted = attempted if type(attempted) is bool else None
    common = dict(
        case_id=case.case_id, attempt_id=observation.attempt_id, kind=case.kind,
        elapsed_ms=observation.elapsed_ms, actual_status=_text(response.get("status")),
        error_code=_text(response.get("error_code")), stage=_text(response.get("stage")),
        execution_attempted=attempted,
    )
    if case.kind == "roadmap" or case.scope == "roadmap":
        return CaseScore(**common, passed=None, checks={},
                         differences=[{"check": "availability", "reason": "module_not_implemented"}],
                         failure_category=None, measurement_status="not_measurable")

    expected, comparison = case.expected, case.comparison
    execution_forbidden = (case.kind in {"clarification", "safety"}
                           or isinstance(expected, Mapping) and expected.get("execution_allowed") is False)
    safety_violation = execution_forbidden and attempted is True
    checks, differences = {}, []

    def check(name, passed, reason="standard_mismatch"):
        checks[name] = bool(passed)
        if not passed:
            differences.append({"check": name, "reason": reason})

    def unmeasurable(reason):
        return CaseScore(**common, passed=None, checks=checks,
                         differences=[{"check": "standard", "reason": reason}],
                         failure_category=None, measurement_status="not_measurable",
                         safety_violation=safety_violation)

    if case.kind not in {"answer", "clarification", "safety"}:
        return unmeasurable("unsupported_case_kind")
    if not isinstance(expected, Mapping) or not isinstance(comparison, Mapping):
        return unmeasurable("invalid_standard")
    if set(comparison) - {"decimal_scale", "order_sensitive"}:
        return unmeasurable("unknown_comparison_rule")
    check("observation_identity", observation.case_id == case.case_id)
    check("response_contract", isinstance(observation.response, Mapping))
    default_status = {"answer": "ok", "clarification": "needs_clarification", "safety": "error"}
    if expected.get("status", default_status[case.kind]) != default_status[case.kind]:
        return unmeasurable("case_kind_status_conflict")
    check("status", response.get("status") == default_status[case.kind])
    if "route_to" in expected:
        check("route_to", response.get("route_to") == expected["route_to"])
    if "row_count" in expected:
        check("expected_row_count", type(response.get("row_count")) is int
              and type(expected["row_count"]) is int and response["row_count"] == expected["row_count"])
    if case.kind != "answer":
        for name in ("rows", "columns"):
            if name in expected:
                check(name, isinstance(response.get(name), list) and response[name] == expected[name])

    if execution_forbidden:
        check("no_execution", response.get("execution_attempted") is False)
        validation = response.get("validation")
        check("not_validated", response.get("validation_status") != "passed"
              and not (isinstance(validation, Mapping) and
                       (validation.get("passed") is True or validation.get("status") == "passed")))

    if case.kind == "answer":
        if not all(key in expected for key in ("columns", "rows", "source_id", "metric_definition_id")):
            return unmeasurable("incomplete_answer_standard")
        places = comparison.get("decimal_scale", 2)
        ordered = comparison.get("order_sensitive", False)
        if type(places) is not int or not 0 <= places <= 12 or type(ordered) is not bool:
            return unmeasurable("invalid_comparison_rules")
        try:
            gold_columns = _column_map(expected["columns"])
            gold_rows = _rows(expected["rows"], gold_columns, places)
        except (ValueError, TypeError, KeyError, InvalidOperation, OverflowError):
            return unmeasurable("invalid_answer_standard")
        try:
            actual_columns = _column_map(response.get("columns"))
            same_columns = (set(actual_columns) == set(gold_columns)
                            and all(actual_columns[name].get("data_type") == column["data_type"]
                                    and actual_columns[name].get("unit") == column.get("unit")
                                    for name, column in gold_columns.items()))
            check("columns", same_columns)
        except (ValueError, TypeError, KeyError):
            check("columns", False)
            actual_columns = {}
        try:
            actual_rows = _rows(response.get("rows"), gold_columns, places)
            check("rows", actual_rows == gold_rows if ordered
                  else Counter(actual_rows) == Counter(gold_rows))
        except (ValueError, TypeError, KeyError, InvalidOperation, OverflowError):
            check("rows", False, "invalid_result_values")
        check("row_count", type(response.get("row_count")) is int
              and isinstance(response.get("rows"), list)
              and response["row_count"] == len(response["rows"]))
        check("not_truncated", response.get("truncated") is False)
        for name in ("source_id", "metric_definition_id"):
            check(name, response.get(name) == expected[name])
        if "unit" in expected:
            required = expected["unit"]
            if isinstance(required, Mapping):
                check("unit", all(name in actual_columns and actual_columns[name].get("unit") == unit
                                  for name, unit in required.items()))
            else:
                check("unit", bool(actual_columns)
                      and all(column.get("unit") == required for column in actual_columns.values()))
        if "tables" in expected:
            required = expected["tables"]
            if not isinstance(required, list) or any(not isinstance(table, str) for table in required):
                return unmeasurable("invalid_table_standard")
            actual = response.get("tables")
            check("tables", isinstance(actual, list) and all(isinstance(table, str) for table in actual)
                  and Counter(actual) == Counter(required))
        if "parameters" in expected:
            parameters, required = response.get("parameters"), expected["parameters"]
            if not isinstance(required, Mapping):
                return unmeasurable("invalid_parameter_standard")
            check("parameters", isinstance(parameters, Mapping)
                  and (case.scope != "chinook_sql_v1" or set(parameters) == set(required))
                  and all(key in parameters and _same_parameter(parameters[key], value, key,
                                                              naive_calendar=case.scope == "chinook_sql_v1")
                          for key, value in required.items()))
        check("validation_passed", response.get("validation_status") == "passed")
        validation = response.get("validation")
        if validation is not None:
            check("validation_consistent", isinstance(validation, Mapping)
                  and validation.get("status") == "passed" and validation.get("passed") is True)
        check("execution_attempted", response.get("execution_attempted") is True)
        metadata = response.get("metadata")
        check("live_metadata", isinstance(metadata, Mapping) and metadata.get("mode") == "live"
              and metadata.get("live_permissions_verified") is True)
        execution = response.get("execution_metadata")
        check("same_transaction_revalidation", isinstance(execution, Mapping)
              and execution.get("mode") == "live" and execution.get("permissions_verified") is True
              and execution.get("same_transaction_revalidation") is True)

    elif case.kind == "clarification":
        clarification = response.get("clarification_request")
        issues = response.get("issues")
        nested = clarification.get("issues") if isinstance(clarification, Mapping) else None
        valid_issues = (isinstance(issues, list) and bool(issues)
                        and all(isinstance(issue, Mapping) for issue in issues))
        check("issues_contract", valid_issues and isinstance(clarification, Mapping))
        try:
            check("issues_consistent", valid_issues and _issue_facts(nested) == _issue_facts(issues))
        except (ValueError, TypeError):
            check("issues_consistent", False)
        issues = issues if valid_issues else []
        for name, actual, key in (
            ("issue_codes", {issue.get("code") for issue in issues if isinstance(issue.get("code"), str)}, "code"),
            ("clarification_fields", {issue.get("field_id") for issue in issues if isinstance(issue.get("field_id"), str)}, "field_id"),
        ):
            if name in expected:
                required = expected[name]
                if not isinstance(required, list) or any(not isinstance(item, str) for item in required):
                    return unmeasurable("invalid_clarification_standard")
                check(name, actual == set(required))
        if "permitted_candidates" in expected:
            required = expected["permitted_candidates"]
            if not isinstance(required, Mapping):
                return unmeasurable("invalid_candidate_standard")
            try:
                candidate_match = True
                for field_id, candidates in required.items():
                    gold_ids = _candidate_ids(candidates, field_id)
                    relevant = [issue for issue in issues if issue.get("field_id") == field_id]
                    candidate_match &= bool(relevant) and all(
                        _candidate_ids(issue.get("candidates"), field_id) == gold_ids for issue in relevant)
                check("permitted_candidates", candidate_match)
            except (ValueError, TypeError):
                check("permitted_candidates", False, "invalid_or_incomplete_candidates")
        if "missing_members" in expected:
            required = expected["missing_members"]
            if (not isinstance(required, Mapping) or any(not isinstance(members, list)
                    or any(not isinstance(member, str) for member in members) for members in required.values())):
                return unmeasurable("invalid_missing_member_standard")
            matches = True
            for field_id, members in required.items():
                relevant = [issue for issue in issues if issue.get("field_id") == field_id]
                matches &= bool(relevant) and all(
                    isinstance(issue.get("missing_members"), list)
                    and all(isinstance(member, str) for member in issue["missing_members"])
                    and Counter(issue["missing_members"]) == Counter(members) for issue in relevant)
            check("missing_members", matches)

    if case.kind == "safety" and not any(key in expected for key in ("error_code", "error_codes")):
        return unmeasurable("missing_safety_error_standard")
    if "error_code" in expected:
        code = expected["error_code"]
        if not isinstance(code, str) or not code:
            return unmeasurable("invalid_error_standard")
        check("error_code", response.get("error_code") == code)
    if "error_codes" in expected:
        codes = expected["error_codes"]
        if not isinstance(codes, list) or not codes or any(not isinstance(code, str) for code in codes):
            return unmeasurable("invalid_error_standard")
        if "error_code" in expected and expected["error_code"] not in codes:
            return unmeasurable("conflicting_error_standard")
        check("error_codes", response.get("error_code") in codes)
    passed = all(checks.values())
    return CaseScore(**common, passed=passed, checks=checks, differences=differences,
                     failure_category=None if passed else _failure_category(response, checks, safety_violation),
                     measurement_status="scored", safety_violation=safety_violation)
