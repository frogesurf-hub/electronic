"""以封闭的 PostgreSQL AST 规则检验 sales_amount_v1 候选，不访问数据库。

只有整条候选符合可信请求时，才用现有 SQLGenerator 生成等价驱动 SQL。
不通过的候选不会被修复、降级为模板或交给执行器。
"""
from __future__ import annotations

import copy
import hashlib
import re
from collections import Counter
from collections.abc import Mapping
from datetime import datetime

from .sql_common import ServiceFault, positive_limit
from .sql_contracts import SQLCandidate, ValidatedSQL, ValidationResult
from .sql_generator import SQLGenerator
from .query_profiles import query_profile, parameter_layout, compatible_type

POLICY_VERSION = "chinook-sql-validation-v1"
REQUEST_KEYS = {
    "source_id", "metric_definition_id", "table", "grain", "aggregation",
    "filters", "time_filter",
}
SESSION_KEYS = ("session_id", "task_id", "request_id", "step_id")


def _reject(code, message):
    raise ServiceFault(code, message)


def _same_binding(actual, expected):
    if isinstance(expected, datetime):
        if isinstance(actual, str):
            try:
                actual = datetime.fromisoformat(actual)
            except ValueError:
                return False
        if not isinstance(actual, datetime):
            return False
        return ((actual.utcoffset() is None) == (expected.utcoffset() is None)
                and actual == expected)
    if isinstance(expected, list):
        return (type(actual) is list and len(actual) == len(expected)
                and all(_same_binding(a, b) for a, b in zip(actual, expected)))
    return type(actual) is type(expected) and actual == expected


class _ASTChecker:
    def __init__(self, exp, sql, columns, tokens, profile):
        self.exp, self.sql, self.columns = exp, sql, columns
        self.profile = profile
        self.alias = None
        self.tokens_by_span = {(token.start, token.end): token for token in tokens}

    def args(self, node, allowed):
        if any(key not in allowed and value is not None and value is not False and value != []
               for key, value in node.args.items()):
            _reject("UNSUPPORTED_SQL_STRUCTURE", "SQL 节点含未允许的语法属性")

    def require(self, node, kind, allowed):
        if type(node) is not kind:
            _reject("UNSUPPORTED_SQL_STRUCTURE", "SQL 含未允许的表达式或查询结构")
        self.args(node, allowed)
        return node

    def unparen(self, node):
        while type(node) is self.exp.Paren:
            self.args(node, {"this"})
            node = node.this
        return node

    def identifier(self, node):
        self.require(node, self.exp.Identifier, {"this", "quoted"})
        name = node.this
        if not isinstance(name, str) or re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*", name) is None:
            _reject("UNSUPPORTED_SQL_STRUCTURE", "SQL 标识符格式不受支持")
        token = self.tokens_by_span.get((node.meta.get("start"), node.meta.get("end")))
        if token is None or token.token_type.name not in {"VAR", "IDENTIFIER"}:
            _reject("UNSUPPORTED_SQL_STRUCTURE", "SQL 标识符必须是普通名称或带引号的名称")
        return name if node.args.get("quoted") else name.lower()

    def column(self, node):
        node = self.require(self.unparen(node), self.exp.Column, {"this", "table", "db"})
        name = self.identifier(node.this)
        if name not in self.columns:
            _reject("COLUMN_NOT_ALLOWED", "SQL 使用了未允许或不存在的字段")
        qualifier = node.args.get("table")
        schema = node.args.get("db")
        if self.alias is not None:
            if schema is not None or (qualifier is not None and self.identifier(qualifier) != self.alias):
                _reject("COLUMN_NOT_ALLOWED", "SQL 字段限定符与表别名不一致")
        elif schema is not None:
            if (self.identifier(schema) != self.profile["schema"] or qualifier is None
                    or self.identifier(qualifier) != self.profile["table"]):
                _reject("COLUMN_NOT_ALLOWED", "SQL 字段限定符不属于允许的表")
        elif qualifier is not None and self.identifier(qualifier) != self.profile["table"]:
            _reject("COLUMN_NOT_ALLOWED", "SQL 字段限定符不属于允许的表")
        return name

    def placeholder(self, node):
        node = self.require(self.unparen(node), self.exp.Placeholder, {"this"})
        if not isinstance(node.this, str) or re.fullmatch(r"[a-z_][a-z_0-9]*", node.this) is None:
            _reject("PARAMETER_MISMATCH", "SQL 必须使用允许的命名占位符")
        return node.this

    def function(self, node, kind, name, allowed):
        self.require(self.unparen(node), kind, allowed)
        # SQLGlot 会把 NVL/IFNULL 等归一为 Coalesce。检查原函数 token，
        # 防止接受 PostgreSQL 中无效或含不同含义的函数后静默改写。
        start, end = node.meta.get("start"), node.meta.get("end")
        if (not isinstance(start, int) or not isinstance(end, int)
                or self.sql[start:end + 1].upper() != name):
            _reject("UNSUPPORTED_SQL_STRUCTURE", "SQL 函数名称不在允许范围内")

    def check(self, root, expected_predicates):
        e = self.exp
        if type(root) is not e.Select:
            _reject("SQL_NOT_READ_ONLY", "只允许一个受控 SELECT 查询")
        self.args(root, {"expressions", "from_", "where", "limit"})
        source = self.require(root.args.get("from_"), e.From, {"this"})
        table = self.require(source.this, e.Table, {"this", "db", "alias"})
        if (self.identifier(table.this) != self.profile["table"]
                or self.identifier(table.args.get("db")) != self.profile["schema"]):
            _reject("TABLE_NOT_ALLOWED", "SQL 只能查询当前指标允许的物理表")
        alias = table.args.get("alias")
        if alias is not None:
            self.require(alias, e.TableAlias, {"this"})
            self.alias = self.identifier(alias.this)

        expressions = root.args.get("expressions")
        if not isinstance(expressions, list) or len(expressions) != 1:
            _reject("SQL_SEMANTIC_MISMATCH", "SQL 必须且只能输出 sales_amount")
        projection = self.require(expressions[0], e.Alias, {"this", "alias"})
        if self.identifier(projection.args.get("alias")) != "sales_amount":
            _reject("SQL_SEMANTIC_MISMATCH", "汇总结果别名必须为 sales_amount")
        coalesce = self.unparen(projection.this)
        self.function(coalesce, e.Coalesce, "COALESCE", {"this", "expressions"})
        total = self.unparen(coalesce.this)
        self.function(total, e.Sum, "SUM", {"this"})
        if self.column(total.this) != self.profile["amount"]:
            _reject("SQL_SEMANTIC_MISMATCH", "汇总字段与确认的指标口径不一致")
        defaults = coalesce.args.get("expressions")
        if not isinstance(defaults, list) or len(defaults) != 1:
            _reject("SQL_SEMANTIC_MISMATCH", "空汇总结果必须只替换为数值零")
        zero = self.require(self.unparen(defaults[0]), e.Literal, {"this", "is_string"})
        if zero.args.get("is_string") or re.fullmatch(r"0+(?:\.0+)?", str(zero.this)) is None:
            _reject("SQL_SEMANTIC_MISMATCH", "空汇总结果必须替换为数值零")

        where = self.require(root.args.get("where"), e.Where, {"this"})
        pending, predicates = [where.this], []
        while pending:
            node = self.unparen(pending.pop())
            if type(node) is e.And:
                self.args(node, {"this", "expression"})
                pending.extend((node.this, node.expression))
                continue
            operation = {e.EQ: "eq", e.GTE: "gte", e.LT: "lt"}.get(type(node))
            if operation is None:
                _reject("SQL_SEMANTIC_MISMATCH", "条件必须为可信请求对应的 AND 谓词")
            self.args(node, {"this", "expression"})
            column = self.column(node.this)
            right = self.unparen(node.expression)
            if type(right) is e.Any:
                if operation != "eq":
                    _reject("SQL_SEMANTIC_MISMATCH", "数组筛选只允许 = ANY(:name)")
                self.args(right, {"this"})
                if type(right.this) is not e.Paren:
                    _reject("UNSUPPORTED_SQL_STRUCTURE", "ANY 必须包含带括号的命名参数")
                operation, right = "in", right.this
            predicates.append((column, operation, self.placeholder(right)))
        if Counter(predicates) != Counter(expected_predicates):
            _reject("SQL_SEMANTIC_MISMATCH", "SQL 条件与可信请求不一致或存在重复、缺失")

        limit = root.args.get("limit")
        if type(limit) is not e.Limit:
            _reject("RESULT_LIMIT_INVALID", "SQL 必须包含 LIMIT :fetch_limit")
        self.args(limit, {"expression"})
        if self.placeholder(limit.expression) != "fetch_limit":
            _reject("RESULT_LIMIT_INVALID", "结果上限必须使用可信 fetch_limit 参数")


class SQLValidator:
    """检验第一项 SQLCandidate，返回 passed/rejected；全程无数据库调用。"""

    def __init__(self, *, max_sql_chars=8192, max_ast_nodes=512, max_ast_depth=64):
        for value in (max_sql_chars, max_ast_nodes, max_ast_depth):
            if type(value) is not int or value <= 0:
                raise ValueError("SQL 检验长度、节点数和深度限制必须为正整数")
        self.max_sql_chars, self.max_ast_nodes, self.max_ast_depth = (
            max_sql_chars, max_ast_nodes, max_ast_depth,
        )

    def validate(self, candidate, generation_request, schema_context,
                 trusted_parameters, limits=None) -> ValidationResult:
        try:
            artifact = self._validate(candidate, generation_request, schema_context,
                                      trusted_parameters, {} if limits is None else limits)
            return ValidationResult("passed", validated_sql=artifact)
        except ServiceFault as error:
            return ValidationResult("rejected", error_code=error.code, error_message=str(error))
        except (KeyError, TypeError, ValueError, AttributeError, RecursionError):
            return ValidationResult("rejected", error_code="INVALID_VALIDATION_INPUT",
                                    error_message="SQL 检验输入结构不合法")

    def _validate(self, candidate, generation_request, schema_context, trusted_parameters, limits):
        if not isinstance(candidate, SQLCandidate) or not isinstance(candidate.sql, str) or not candidate.sql.strip():
            _reject("INVALID_CANDIDATE", "必须提供第一项产生的非空 SQLCandidate")
        if "\x00" in candidate.sql:
            _reject("INVALID_CANDIDATE", "候选 SQL 包含非法字符")
        if len(candidate.sql) > self.max_sql_chars:
            _reject("SQL_TOO_LARGE", "候选 SQL 超过长度限制")
        if not all(isinstance(item, Mapping) for item in (generation_request, schema_context, trusted_parameters, limits)):
            _reject("INVALID_VALIDATION_INPUT", "请求、结构上下文、参数和限制必须为映射")
        request, schema = copy.deepcopy(dict(generation_request)), copy.deepcopy(dict(schema_context))
        if (set(request) != REQUEST_KEYS
                or not isinstance(request.get("time_filter"), dict)
                or set(request["time_filter"]) != {"column", "start", "end_exclusive", "timezone"}):
            _reject("INVALID_GENERATION_REQUEST", "生成请求不符合当前销售额接口")
        max_rows = positive_limit(limits.get("max_rows", 100), 1000, "max_rows")
        db_timeout = positive_limit(limits.get("db_timeout_ms", 5000), 5000, "db_timeout_ms")
        canonical_sql, parameters = SQLGenerator().generate(request, {"max_rows": max_rows})
        profile = query_profile(request)
        if (candidate.source_id != request["source_id"]
                or candidate.metric_definition_id != request["metric_definition_id"]
                or candidate.dialect != "postgresql" or candidate.placeholder_style != "named"
                or candidate.validated is not False):
            _reject("CANDIDATE_CONTEXT_MISMATCH", "候选 SQL 的来源、口径或格式与请求不一致")
        for key in SESSION_KEYS:
            if getattr(candidate, key) is not None and not isinstance(getattr(candidate, key), str):
                _reject("INVALID_CANDIDATE", "候选 SQL 的上下文编号必须为字符串")
        if (not isinstance(candidate.parameter_names, tuple)
                or not all(isinstance(name, str) for name in candidate.parameter_names)
                or len(set(candidate.parameter_names)) != len(candidate.parameter_names)
                or set(candidate.parameter_names) != set(parameters)
                or set(trusted_parameters) != set(parameters)):
            _reject("PARAMETER_MISMATCH", "候选声明或服务端参数集合与可信请求不一致")
        if not all(_same_binding(trusted_parameters[name], value) for name, value in parameters.items()):
            _reject("PARAMETER_VALUE_MISMATCH", "服务端参数类型或值与可信请求不一致")

        predicates = [(column, operation, name) for name, _, column, operation in parameter_layout(request) if column]
        columns = {profile["amount"], *(column for column, _, _ in predicates)}
        if any(schema.get(key) != request[key] for key in ("source_id", "table", "metric_definition_id")):
            _reject("METADATA_ERROR", "结构上下文与生成请求不一致")
        fingerprint = schema.get("schema_fingerprint_sha256")
        if not isinstance(fingerprint, str) or re.fullmatch(r"[a-f0-9]{64}", fingerprint) is None:
            _reject("METADATA_ERROR", "SQL 检验必须提供有效的结构指纹")
        if candidate.schema_fingerprint_sha256 != fingerprint:
            _reject("CANDIDATE_CONTEXT_MISMATCH", "候选 SQL 与当前结构指纹不一致")
        rows = schema.get("columns")
        if not isinstance(rows, list):
            _reject("METADATA_ERROR", "缺少允许查询的字段结构")
        indexed = {}
        for row in rows:
            if (not isinstance(row, Mapping) or not isinstance(row.get("name"), str)
                    or row["name"] in indexed or not isinstance(row.get("data_type"), str)):
                _reject("METADATA_ERROR", "结构字段缺失、重复或类型不合法")
            indexed[row["name"]] = row["data_type"]
        if any(not compatible_type(profile, name, indexed.get(name)) for name in columns):
            _reject("METADATA_ERROR", "查询字段缺失或类型与销售额口径不兼容")

        root, exp, tokens = self._parse(candidate.sql)
        _ASTChecker(exp, candidate.sql, columns, tokens, profile).check(root, predicates)
        return ValidatedSQL(
            sql=canonical_sql, candidate_sql=candidate.sql,
            source_id=candidate.source_id, metric_definition_id=candidate.metric_definition_id,
            schema_fingerprint_sha256=fingerprint,
            policy_version="chinook-sql-validation-v1" if request["source_id"] == "chinook_db" else POLICY_VERSION,
            candidate_sha256=hashlib.sha256(candidate.sql.encode("utf-8")).hexdigest(),
            sql_sha256=hashlib.sha256(canonical_sql.encode("utf-8")).hexdigest(),
            max_rows=max_rows, db_timeout_ms=db_timeout,
            _parameter_items=tuple((name, tuple(value) if isinstance(value, list) else value)
                                   for name, value in parameters.items()),
            **{key: getattr(candidate, key) for key in SESSION_KEYS},
        )

    def _parse(self, sql):
        try:
            from sqlglot import Dialect, ErrorLevel, exp, parse
            from sqlglot.errors import SqlglotError
        except ImportError as error:
            raise ServiceFault("PARSER_NOT_INSTALLED", "请安装 database/requirements.txt 中的 sqlglot") from error
        try:
            tokens = Dialect.get_or_raise("postgres").tokenize(sql)
            semicolons = [index for index, token in enumerate(tokens)
                          if token.token_type.name == "SEMICOLON"]
            if semicolons and semicolons != [len(tokens) - 1]:
                _reject("MULTIPLE_STATEMENTS", "候选 SQL 必须且只能包含一个完整语句")
            if not tokens or tokens[0].token_type.name != "SELECT":
                _reject("SQL_NOT_READ_ONLY", "只允许一个受控 SELECT 查询")
            allowed_tokens = {
                "SELECT", "FROM", "ALIAS", "WHERE", "AND", "LIMIT", "ANY", "ALL",
                "VAR", "IDENTIFIER", "END", "NUMBER", "EQ", "GTE", "LT",
                "COLON", "DOT", "L_PAREN", "R_PAREN", "COMMA", "SEMICOLON",
            }
            if any(token.token_type.name not in allowed_tokens for token in tokens):
                _reject("UNSUPPORTED_SQL_STRUCTURE", "SQL 包含当前查询语法未允许的 token")
            depth = 0
            for index, token in enumerate(tokens):
                kind = token.token_type.name
                if kind == "L_PAREN":
                    depth += 1
                    if depth > self.max_ast_depth:
                        _reject("SQL_TOO_COMPLEX", "SQL 括号嵌套超过深度限制")
                elif kind == "R_PAREN":
                    depth -= 1
                elif kind == "COLON":
                    name = tokens[index + 1] if index + 1 < len(tokens) else None
                    if (name is None or name.start != token.end + 1
                            or re.fullmatch(r"[a-z_][a-z_0-9]*", name.text) is None
                            or sql[name.start:name.end + 1] != name.text):
                        _reject("PARAMETER_MISMATCH", "占位符必须为连续且未加引号的 :name")
            # 检验 SQLGlot 会合并的非 PostgreSQL 运算符拼写。
            spelling = {"EQ": "=", "GTE": ">=", "LT": "<", "AND": "AND"}
            if any(token.token_type.name in spelling and token.text.upper() != spelling[token.token_type.name]
                   for token in tokens):
                _reject("UNSUPPORTED_SQL_STRUCTURE", "SQL 运算符拼写不符合 PostgreSQL 查询接口")
            roots = parse(sql, read="postgres", error_level=ErrorLevel.RAISE)
        except ServiceFault:
            raise
        except (SqlglotError, ValueError, RecursionError) as error:
            raise ServiceFault("SQL_PARSE_ERROR", "候选 SQL 无法按 PostgreSQL 查询语法解析") from error
        # 可接受一个正常末尾分号；额外空语句也拒绝。
        if len(roots) != 1 or roots[0] is None:
            _reject("MULTIPLE_STATEMENTS", "候选 SQL 必须且只能包含一个完整语句")
        root = roots[0]
        pending, count = [(root, 1)], 0
        while pending:
            node, depth = pending.pop()
            count += 1
            if count > self.max_ast_nodes or depth > self.max_ast_depth:
                _reject("SQL_TOO_COMPLEX", "SQL 语法树超过节点数或深度限制")
            pending.extend((child, depth + 1) for child in node.iter_expressions())
        return root, exp, tokens
