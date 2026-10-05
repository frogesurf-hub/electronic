"""按可信明细请求封闭校验 PostgreSQL AST；不连接数据库。"""
from __future__ import annotations

import copy
import hashlib
import re
from collections import Counter
from collections.abc import Mapping

from .detail_contracts import DetailCandidate, DetailValidationResult, ValidatedDetailSQL
from .detail_query import canonical_sql, prepare_detail_request
from .sql_common import ServiceFault, positive_limit

POLICY_VERSION = "chinook-detail-validation-v1"
_SESSION_KEYS = ("session_id", "task_id", "request_id", "step_id")


def _reject(code, message):
    raise ServiceFault(code, message)


def _same_binding(actual, expected):
    if type(expected) is list:
        return (type(actual) is list and len(actual) == len(expected)
                and all(_same_binding(a, b) for a, b in zip(actual, expected)))
    return type(actual) is type(expected) and actual == expected


class _DetailAST:
    def __init__(self, exp, tokens, request, schema):
        self.e, self.request = exp, request
        self.schema_name, self.table_name = request["table"].split(".")
        self.columns = {column["name"] for column in schema["columns"]}
        self.alias = None
        self.tokens = {(token.start, token.end): token for token in tokens}

    def args(self, node, allowed):
        if any(key not in allowed and value is not None and value is not False and value != []
               for key, value in node.args.items()):
            _reject("UNSUPPORTED_SQL_STRUCTURE", "明细 SQL 含未允许的语法属性")

    def require(self, node, kind, allowed):
        if type(node) is not kind:
            _reject("UNSUPPORTED_SQL_STRUCTURE", "明细 SQL 含未允许的表达式")
        self.args(node, allowed)
        return node

    def unparen(self, node):
        while type(node) is self.e.Paren:
            self.args(node, {"this"})
            node = node.this
        return node

    def identifier(self, node):
        self.require(node, self.e.Identifier, {"this", "quoted"})
        name = node.this
        if not isinstance(name, str) or re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*", name) is None:
            _reject("UNSUPPORTED_SQL_STRUCTURE", "SQL 标识符格式不受支持")
        token = self.tokens.get((node.meta.get("start"), node.meta.get("end")))
        # NAME 是 Chinook 的真实列名；其余关键字不能成为隐式别名。
        if token is None or token.token_type.name not in {"VAR", "IDENTIFIER", "NAME"}:
            _reject("UNSUPPORTED_SQL_STRUCTURE", "SQL 标识符必须使用普通名称或引用名称")
        return name if node.args.get("quoted") else name.lower()

    def column(self, node):
        node = self.require(self.unparen(node), self.e.Column, {"this", "table", "db"})
        name = self.identifier(node.this)
        if name not in self.columns:
            _reject("COLUMN_NOT_ALLOWED", "明细 SQL 使用了未知字段")
        qualifier, schema = node.args.get("table"), node.args.get("db")
        if self.alias is not None:
            if schema is not None or (qualifier is not None and self.identifier(qualifier) != self.alias):
                _reject("COLUMN_NOT_ALLOWED", "字段限定符与目标表别名不一致")
        elif schema is not None:
            if (self.identifier(schema) != self.schema_name or qualifier is None
                    or self.identifier(qualifier) != self.table_name):
                _reject("COLUMN_NOT_ALLOWED", "字段限定符不属于目标表")
        elif qualifier is not None and self.identifier(qualifier) != self.table_name:
            _reject("COLUMN_NOT_ALLOWED", "字段限定符不属于目标表")
        return name

    def placeholder(self, node):
        node = self.require(self.unparen(node), self.e.Placeholder, {"this"})
        if not isinstance(node.this, str) or re.fullmatch(r"[a-z_][a-z_0-9]*", node.this) is None:
            _reject("PARAMETER_MISMATCH", "SQL 必须使用连续的命名占位符")
        return node.this

    def predicate(self, node):
        e, node = self.e, self.unparen(node)
        if type(node) is e.Not:
            self.args(node, {"this"})
            inner = self.require(self.unparen(node.this), e.Is, {"this", "expression", "negate"})
            self.require(self.unparen(inner.expression), e.Null, set())
            if inner.args.get("negate"):
                _reject("SQL_SEMANTIC_MISMATCH", "不允许重复否定条件")
            return self.column(inner.this), "is_not_null", None
        if type(node) is e.Is:
            self.args(node, {"this", "expression", "negate"})
            self.require(self.unparen(node.expression), e.Null, set())
            return self.column(node.this), "is_not_null" if node.args.get("negate") else "is_null", None
        if type(node) is e.Escape:
            self.args(node, {"this", "expression"})
            literal = self.require(self.unparen(node.expression), e.Literal, {"this", "is_string"})
            if literal.args.get("is_string") is not True or literal.this != "\\":
                _reject("SQL_SEMANTIC_MISMATCH", "LIKE 必须使用固定反斜线转义字符")
            like = self.require(self.unparen(node.this), e.Like, {"this", "expression"})
            return self.column(like.this), "like", self.placeholder(like.expression)
        operation = {e.EQ: "eq", e.NEQ: "ne", e.GT: "gt", e.GTE: "gte", e.LT: "lt", e.LTE: "lte"}.get(type(node))
        if operation is None:
            _reject("SQL_SEMANTIC_MISMATCH", "条件必须精确对应可信请求的 AND 谓词")
        self.args(node, {"this", "expression"})
        column, right = self.column(node.this), self.unparen(node.expression)
        if type(right) is e.Any:
            if operation != "eq":
                _reject("SQL_SEMANTIC_MISMATCH", "数组筛选必须使用 = ANY(:name)")
            self.args(right, {"this"})
            self.require(right.this, e.Paren, {"this"})
            operation, right = "in", right.this
        return column, operation, self.placeholder(right)

    def check(self, root):
        e, request = self.e, self.request
        self.require(root, e.Select, {"expressions", "from_", "where", "order", "limit", "offset"})
        source = self.require(root.args.get("from_"), e.From, {"this"})
        table = self.require(source.this, e.Table, {"this", "db", "alias"})
        if (self.identifier(table.this) != self.table_name
                or self.identifier(table.args.get("db")) != self.schema_name):
            _reject("TABLE_NOT_ALLOWED", "SQL 只能查询可信请求中的完整限定单表")
        alias = table.args.get("alias")
        if alias is not None:
            self.require(alias, e.TableAlias, {"this"})
            self.alias = self.identifier(alias.this)
        projections = []
        for expression in root.args.get("expressions") or []:
            expression = self.unparen(expression)
            if type(expression) is e.Alias:
                self.args(expression, {"this", "alias"})
                column = self.column(expression.this)
                if self.identifier(expression.args.get("alias")) != column:
                    _reject("SQL_SEMANTIC_MISMATCH", "明细投影不得改名")
            else:
                column = self.column(expression)
            projections.append(column)
        if projections != request["columns"]:
            _reject("SQL_SEMANTIC_MISMATCH", "投影字段及顺序与可信请求不一致")
        expected = []
        for index, item in enumerate(request["filters"]):
            op = "like" if item["op"] in {"contains", "starts_with"} else item["op"]
            expected.append((item["column"], op, None if op in {"is_null", "is_not_null"} else f"filter_{index}"))
        actual, where = [], root.args.get("where")
        if where is not None:
            self.require(where, e.Where, {"this"})
            pending = [where.this]
            while pending:
                node = self.unparen(pending.pop())
                if type(node) is e.And:
                    self.args(node, {"this", "expression"})
                    pending.extend((node.this, node.expression))
                else:
                    actual.append(self.predicate(node))
        if Counter(actual) != Counter(expected):
            _reject("SQL_SEMANTIC_MISMATCH", "SQL 条件存在缺失、重复或额外条件")
        order = self.require(root.args.get("order"), e.Order, {"expressions"})
        actual_order = []
        for item in order.expressions:
            self.require(item, e.Ordered, {"this", "desc", "nulls_first"})
            if item.args.get("nulls_first") is not False:
                _reject("SQL_SEMANTIC_MISMATCH", "每个排序字段必须使用 NULLS LAST 语义")
            actual_order.append({"column": self.column(item.this), "direction": "desc" if item.args.get("desc") else "asc"})
        if actual_order != request["order_by"]:
            _reject("SQL_SEMANTIC_MISMATCH", "排序必须与可信请求及完整主键顺序一致")
        for key, kind, name in (("limit", e.Limit, "fetch_limit"), ("offset", e.Offset, "page_offset")):
            item = self.require(root.args.get(key), kind, {"expression"})
            if self.placeholder(item.expression) != name:
                _reject("RESULT_LIMIT_INVALID", "分页必须使用可信 fetch_limit/page_offset 参数")


class DetailSQLValidator:
    def __init__(self, *, max_sql_chars=8192, max_ast_nodes=1024, max_ast_depth=96):
        if any(type(value) is not int or value <= 0 for value in (max_sql_chars, max_ast_nodes, max_ast_depth)):
            raise ValueError("SQL 长度、节点数及深度限制必须为正整数")
        self.max_sql_chars, self.max_ast_nodes, self.max_ast_depth = max_sql_chars, max_ast_nodes, max_ast_depth

    def validate(self, candidate, generation_request, schema_context, trusted_parameters, limits=None):
        try:
            packet = self._validate(candidate, generation_request, schema_context, trusted_parameters,
                                    {} if limits is None else limits)
            return DetailValidationResult("passed", validated_sql=packet)
        except ServiceFault as error:
            return DetailValidationResult("rejected", error_code=error.code, error_message=str(error))
        except (KeyError, TypeError, ValueError, AttributeError, RecursionError):
            return DetailValidationResult("rejected", error_code="INVALID_VALIDATION_INPUT", error_message="明细 SQL 检验输入结构不合法")

    def _validate(self, candidate, request, schema, parameters, limits):
        if type(candidate) is not DetailCandidate or not isinstance(candidate.sql, str) or not candidate.sql.strip() or "\x00" in candidate.sql:
            _reject("INVALID_CANDIDATE", "必须提供有效的明细 SQL 候选对象")
        if len(candidate.sql) > self.max_sql_chars:
            _reject("SQL_TOO_LARGE", "SQL 超过长度限制")
        if not all(isinstance(item, Mapping) for item in (request, schema, parameters, limits)):
            _reject("INVALID_VALIDATION_INPUT", "请求、结构、参数和限制必须为映射")
        request, schema = copy.deepcopy(dict(request)), copy.deepcopy(dict(schema))
        prepared = prepare_detail_request(request, schema, limits=limits)
        if prepared.generation_request != request:
            _reject("INVALID_GENERATION_REQUEST", "SQL 检验必须使用已确认的标准明细请求")
        if (candidate.task != "table_detail" or candidate.source_id != request["source_id"] or candidate.table != request["table"]
                or candidate.dialect != "postgresql" or candidate.placeholder_style != "named" or candidate.validated is not False):
            _reject("CANDIDATE_CONTEXT_MISMATCH", "候选数据源、表或格式与请求不一致")
        fingerprint = schema.get("schema_fingerprint_sha256")
        if not isinstance(fingerprint, str) or re.fullmatch(r"[a-f0-9]{64}", fingerprint) is None:
            _reject("METADATA_ERROR", "缺少有效的数据库结构指纹")
        if candidate.schema_fingerprint_sha256 != fingerprint:
            _reject("CANDIDATE_CONTEXT_MISMATCH", "候选与当前结构指纹不一致")
        if any(getattr(candidate, key) is not None and not isinstance(getattr(candidate, key), str) for key in _SESSION_KEYS):
            _reject("INVALID_CANDIDATE", "上下文编号必须为字符串")
        expected = prepared.parameters
        if (type(candidate.parameter_names) is not tuple or any(type(name) is not str for name in candidate.parameter_names)
                or len(set(candidate.parameter_names)) != len(candidate.parameter_names)
                or set(candidate.parameter_names) != set(expected) or set(parameters) != set(expected)):
            _reject("PARAMETER_MISMATCH", "候选声明及可信参数集合必须完全匹配")
        if any(not _same_binding(parameters[name], value) for name, value in expected.items()):
            _reject("PARAMETER_VALUE_MISMATCH", "可信参数的类型或值与请求不一致")
        root, exp, tokens = self._parse(candidate.sql)
        _DetailAST(exp, tokens, request, schema).check(root)
        sql = canonical_sql(request, named=False)
        indexed = {column["name"]: column for column in schema["columns"]}
        outputs = tuple({key: indexed[name][key] for key in ("name", "data_type", "nullable")} for name in request["columns"])
        return ValidatedDetailSQL(
            sql=sql, candidate_sql=candidate.sql, source_id=candidate.source_id, table=candidate.table,
            schema_fingerprint_sha256=fingerprint, policy_version=POLICY_VERSION,
            candidate_sha256=hashlib.sha256(candidate.sql.encode("utf-8")).hexdigest(),
            sql_sha256=hashlib.sha256(sql.encode("utf-8")).hexdigest(),
            max_rows=request["page"]["size"], page_offset=request["page"]["offset"],
            db_timeout_ms=positive_limit(limits.get("db_timeout_ms", 5000), 5000, "db_timeout_ms"),
            _parameter_items=tuple((name, tuple(value) if type(value) is list else value) for name, value in expected.items()),
            _output_columns=outputs, **{key: getattr(candidate, key) for key in _SESSION_KEYS},
        )

    def _parse(self, sql):
        try:
            from sqlglot import Dialect, ErrorLevel, exp, parse
            from sqlglot.errors import SqlglotError
        except ImportError as error:
            raise ServiceFault("PARSER_NOT_INSTALLED", "请安装 database/requirements.txt 中的 sqlglot") from error
        try:
            tokens = Dialect.get_or_raise("postgres").tokenize(sql)
            if not tokens or tokens[0].token_type.name != "SELECT":
                _reject("SQL_NOT_READ_ONLY", "明细只允许单个 SELECT")
            semicolons = [i for i, token in enumerate(tokens) if token.token_type.name == "SEMICOLON"]
            if semicolons and semicolons != [len(tokens) - 1]:
                _reject("MULTIPLE_STATEMENTS", "只允许一个完整 SQL 语句")
            allowed = {"SELECT", "FROM", "ALIAS", "WHERE", "AND", "LIMIT", "OFFSET", "ORDER_BY", "ASC", "DESC",
                       "ANY", "IS", "NOT", "NULL", "LIKE", "ESCAPE", "VAR", "IDENTIFIER", "NAME", "STRING",
                       "EQ", "NEQ", "GT", "GTE", "LT", "LTE", "COLON", "DOT", "L_PAREN", "R_PAREN", "COMMA", "SEMICOLON"}
            spelling = {"EQ": {"="}, "NEQ": {"!=", "<>"}, "GT": {">"}, "GTE": {">="}, "LT": {"<"}, "LTE": {"<="}, "AND": {"AND"}}
            depth = 0
            for index, token in enumerate(tokens):
                kind = token.token_type.name
                if kind not in allowed or (kind in spelling and token.text.upper() not in spelling[kind]):
                    _reject("UNSUPPORTED_SQL_STRUCTURE", "SQL 包含未允许的 token 或运算符")
                if kind == "L_PAREN":
                    depth += 1
                    if depth > self.max_ast_depth:
                        _reject("SQL_TOO_COMPLEX", "SQL 括号嵌套超过限制")
                elif kind == "R_PAREN":
                    depth -= 1
                elif kind == "COLON":
                    name = tokens[index + 1] if index + 1 < len(tokens) else None
                    if (name is None or name.start != token.end + 1 or name.token_type.name != "VAR"
                            or re.fullmatch(r"[a-z_][a-z_0-9]*", name.text) is None or sql[name.start:name.end + 1] != name.text):
                        _reject("PARAMETER_MISMATCH", "占位符必须使用连续且未引用的 :name")
            roots = parse(sql, read="postgres", error_level=ErrorLevel.RAISE)
        except ServiceFault:
            raise
        except (SqlglotError, ValueError, RecursionError) as error:
            raise ServiceFault("SQL_PARSE_ERROR", "明细 SQL 无法按 PostgreSQL 语法解析") from error
        if len(roots) != 1 or roots[0] is None:
            _reject("MULTIPLE_STATEMENTS", "只允许一个完整 SQL 语句")
        pending, count = [(roots[0], 1)], 0
        while pending:
            node, depth = pending.pop()
            count += 1
            if count > self.max_ast_nodes or depth > self.max_ast_depth:
                _reject("SQL_TOO_COMPLEX", "SQL 语法树超过大小或深度限制")
            pending.extend((child, depth + 1) for child in node.iter_expressions())
        return roots[0], exp, tokens
