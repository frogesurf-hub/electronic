r"""安全导入用户提供的 Chinook SQL，并准备本地只读连接。

默认切换 source_config；--prepare-only 仅准备数据库和连接，不切换服务。
不会执行脚本中的 DROP DATABASE、CREATE DATABASE 或 psql \c 指令；
已有 chinook 数据库只验证并复用，导入失败保留数据供审查。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import secrets
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

if __package__:
    from .connection_settings import protect_password, unprotect_password
    from .artifact_io import _save_json
else:
    from connection_settings import protect_password, unprotect_password
    from artifact_io import _save_json

ROOT = Path(__file__).resolve().parent
SCRIPT = ROOT / "Chinook_PostgreSql.sql"
REVIEWED_SCRIPT_SHA256 = "e3fde5c1a5b51a2a91429a702c9ca6e69ba56e6c7f5e112724d70c3d03db695e"
LOCAL = ROOT / ".local"
BACKUP = ROOT / "migration" / "chinook"
HOST, PORT, DATABASE, READER = "127.0.0.1", 5432, "chinook", "chinook_reader"
COUNTS = {"artist": 275, "album": 347, "track": 3503, "customer": 59,
          "employee": 8, "genre": 25, "media_type": 5, "invoice": 412,
          "invoice_line": 2240, "playlist": 18, "playlist_track": 8715}
TABLES = tuple(sorted(COUNTS))


class ChinookSetupError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def split_statements(text):
    """仅按字符串与注释之外的分号拆分，不执行或解释 SQL。"""
    statements, buffer = [], []
    index, quote, block, line = 0, None, False, False
    while index < len(text):
        char = text[index]
        following = text[index:index + 2]
        if line:
            if char == "\n":
                line = False
                buffer.append("\n")
            index += 1
            continue
        if block:
            if following == "*/":
                block = False
                buffer.append(" ")
                index += 2
            else:
                index += 1
            continue
        if quote is not None:
            buffer.append(char)
            if char == quote:
                if index + 1 < len(text) and text[index + 1] == quote:
                    buffer.append(quote)
                    index += 2
                    continue
                quote = None
            index += 1
            continue
        if following == "--":
            line, index = True, index + 2
            continue
        if following == "/*":
            block, index = True, index + 2
            continue
        if char in ("'", '"'):
            quote = char
        if char == ";":
            statement = "".join(buffer).strip()
            if statement:
                statements.append(statement)
            buffer.clear()
        else:
            buffer.append(char)
        index += 1
    if quote is not None or block:
        raise ChinookSetupError("INVALID_SCRIPT", "Chinook 脚本包含未结束的字符串或注释")
    if "".join(buffer).strip():
        raise ChinookSetupError("INVALID_SCRIPT", "Chinook 脚本包含未终止的 SQL 指令")
    return statements


def _tuple_values(text):
    rows, current, values = [], [], []
    depth, quote, index = 0, False, 0
    while index < len(text):
        char = text[index]
        if quote:
            current.append(char)
            if char == "'":
                if index + 1 < len(text) and text[index + 1] == "'":
                    current.append("'")
                    index += 2
                    continue
                quote = False
        elif char == "'":
            quote = True
            current.append(char)
        elif char == "(":
            depth += 1
            if depth != 1:
                raise ChinookSetupError("INVALID_SCRIPT", "INSERT 使用了不支持的表达式")
        elif char == ")":
            depth -= 1
            if depth != 0:
                raise ChinookSetupError("INVALID_SCRIPT", "INSERT 元组格式异常")
            values.append("".join(current).strip())
            rows.append(values)
            current, values = [], []
        elif char == "," and depth == 1:
            values.append("".join(current).strip())
            current = []
        elif depth:
            current.append(char)
        elif char not in ", \t\r\n":
            raise ChinookSetupError("INVALID_SCRIPT", "INSERT 元组外包含不支持的内容")
        index += 1
    if depth or quote:
        raise ChinookSetupError("INVALID_SCRIPT", "INSERT 元组或字符串未结束")
    return rows


def _literal(token):
    if token.startswith("N'") or token.startswith("n'"):
        token = token[1:]
    if token.startswith("'") and token.endswith("'"):
        return token[1:-1].replace("''", "'")
    return token


def prepare_script(path=SCRIPT):
    """剥离固定三条库级指令，独立计算真实 INSERT 行数、日期和金额。"""
    raw = Path(path).read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if digest != REVIEWED_SCRIPT_SHA256:
        raise ChinookSetupError("UNREVIEWED_SCRIPT", "Chinook SQL 与已审核源文件不同，拒绝执行；请先独立审核新脚本")
    text = raw.decode("utf-8-sig")
    for pattern in (r"(?im)^\s*DROP DATABASE IF EXISTS chinook;\s*$",
                    r"(?im)^\s*CREATE DATABASE chinook;\s*$",
                    r"(?im)^\s*\\c chinook;?\s*$"):
        text, count = re.subn(pattern, "", text, count=1)
        if count != 1:
            raise ChinookSetupError("INVALID_SCRIPT", "脚本顶部库级指令与已审核 Chinook 格式不一致")
    statements = split_statements(text)
    counts = {table: 0 for table in TABLES}
    created, foreign_keys, total, invoice_dates = set(), 0, Decimal(0), []
    for statement in statements:
        create = re.match(r"CREATE TABLE ([a-z_]+)\s*\(", statement, re.IGNORECASE)
        insert = re.match(r"INSERT INTO ([a-z_]+)\s*\(([^)]+)\)\s+VALUES\s*(.*)$",
                          statement, re.IGNORECASE | re.DOTALL)
        if create:
            table = create.group(1).lower()
            if table not in COUNTS or table in created:
                raise ChinookSetupError("INVALID_SCRIPT", "脚本创建了未知或重复的表")
            created.add(table)
        elif insert:
            table = insert.group(1).lower()
            if table not in COUNTS:
                raise ChinookSetupError("INVALID_SCRIPT", "脚本插入了未知的表")
            columns = [value.strip() for value in insert.group(2).split(",")]
            rows = _tuple_values(insert.group(3))
            counts[table] += len(rows)
            if any(len(row) != len(columns) for row in rows):
                raise ChinookSetupError("INVALID_SCRIPT", "INSERT 字段与元组数量不一致")
            if table == "invoice":
                date_position, amount_position = columns.index("invoice_date"), columns.index("total")
                for row in rows:
                    invoice_dates.append(datetime.strptime(_literal(row[date_position]), "%Y/%m/%d"))
                    total += Decimal(_literal(row[amount_position]))
        elif re.match(r"ALTER TABLE [a-z_]+ ADD CONSTRAINT [a-z_]+\s+FOREIGN KEY\b",
                      statement, re.IGNORECASE):
            foreign_keys += 1
        elif re.match(r"CREATE INDEX [a-z_]+ ON [a-z_]+\s*\(", statement, re.IGNORECASE):
            pass
        else:
            raise ChinookSetupError("UNSAFE_SCRIPT", "脚本含未批准的指令，未执行任何导入")
    if created != set(TABLES) or counts != COUNTS or foreign_keys != 11 or not invoice_dates:
        raise ChinookSetupError("INVALID_SCRIPT", "独立解析得到的 Chinook 表、行数或外键与预期范围不一致")
    facts = {"row_counts": counts, "tables": len(created), "foreign_keys": foreign_keys,
             "invoice_date_min": min(invoice_dates).isoformat(),
             "invoice_date_max": max(invoice_dates).isoformat(),
             "invoice_total": format(total, ".2f"),
             "script_sha256": digest}
    return ";\n".join(statements) + ";\n", facts


def _profile(path, user, database):
    try:
        profile = json.loads(path.read_text(encoding="utf-8-sig"))
        if (profile.get("host") != HOST or profile.get("port") != PORT or profile.get("dbname") != database
                or profile.get("user") != user or profile.get("sslmode") != "disable"):
            raise ValueError("profile mismatch")
        password = unprotect_password(profile["password_dpapi"])
    except Exception as error:
        raise ChinookSetupError("INVALID_CONNECTION_PROFILE", "指定本地连接配置不存在、不可解密或与项目身份不符") from error
    return {"host": HOST, "port": PORT, "dbname": database, "user": user,
            "sslmode": "disable", "password": password}


def inspect_database(connection, expected):
    from psycopg import sql
    counts = {}
    with connection.cursor() as cursor:
        cursor.execute("SELECT c.relname AS table_name FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
                       "WHERE n.nspname='public' AND c.relkind IN ('r','p') ORDER BY c.relname")
        if [row["table_name"] for row in cursor.fetchall()] != list(TABLES):
            raise ChinookSetupError("EXISTING_DATABASE_MISMATCH", "chinook 已有数据库的表范围不符，拒绝覆盖数据")
        for table in TABLES:
            cursor.execute(sql.SQL("SELECT count(*) AS count FROM public.{}").format(sql.Identifier(table)))
            counts[table] = cursor.fetchone()["count"]
        cursor.execute("SELECT count(*) AS count FROM pg_constraint c JOIN pg_namespace n ON n.oid=c.connamespace "
                       "WHERE n.nspname='public' AND c.contype='f'")
        foreign_keys = cursor.fetchone()["count"]
        cursor.execute("SELECT min(invoice_date) AS min_date,max(invoice_date) AS max_date," 
                       "COALESCE(sum(total),0) AS total FROM public.invoice")
        invoice = cursor.fetchone()
    actual = {"row_counts": counts, "tables": len(counts), "foreign_keys": foreign_keys,
              "invoice_date_min": invoice["min_date"].isoformat(),
              "invoice_date_max": invoice["max_date"].isoformat(),
              "invoice_total": format(invoice["total"], ".2f")}
    if any(actual[key] != expected[key] for key in actual):
        raise ChinookSetupError("DATA_VERIFICATION_FAILED", "chinook 数据行数、外键、日期或总金额与独立脚本解析不一致；未覆盖数据")
    return actual


def chinook_config(previous):
    result = dict(previous)
    for key in ("project_ref", "project_name", "_base_dir"):
        result.pop(key, None)
    result.update(dataset="chinook", source_id="chinook_db", hosting="local_postgresql", database=DATABASE,
                  reader_role=READER, connection_file=".local/connection_chinook.json",
                  timezone="Asia/Shanghai", currency=None, amount_unit="金额",
                  query_table_allowlist=["public." + table for table in TABLES],
                  metadata_tables=[], evidence_tables=[],
                  note="Chinook 1.4.5 音乐商店数据库，连接已有本地 PostgreSQL 127.0.0.1:5432；"
                       "发票日期为无时区时间，金额不假定币种。应用使用独立只读角色。")
    return result


def setup(*, prepare_only=False):
    import psycopg
    from psycopg import sql
    from psycopg.rows import dict_row
    sanitized, expected = prepare_script()
    admin_parameters = _profile(LOCAL / "target_admin_connection.json", "postgres", "postgres")
    app_profile = LOCAL / "connection_chinook.json"
    reader_parameters = None
    created_role = False
    with psycopg.connect(**admin_parameters, autocommit=True, connect_timeout=10, row_factory=dict_row) as admin:
        with admin.cursor() as cursor:
            cursor.execute("SELECT inet_server_port() AS port,current_user AS username")
            identity = cursor.fetchone()
            if identity != {"port": PORT, "username": "postgres"}:
                raise ChinookSetupError("WRONG_DATABASE_SERVER", "连接未指向指定 5432 管理员身份")
            cursor.execute("SELECT rolname,rolsuper,rolbypassrls,rolcreatedb,rolcreaterole FROM pg_roles WHERE rolname=%s", (READER,))
            role = cursor.fetchone()
            if role:
                if not app_profile.is_file():
                    raise ChinookSetupError("READER_ROLE_COLLISION", "chinook_reader 已存在但没有本项目连接配置，拒绝改变现有角色")
                reader_parameters = _profile(app_profile, READER, DATABASE)
                if any(role[key] for key in ("rolsuper", "rolbypassrls", "rolcreatedb", "rolcreaterole")):
                    raise ChinookSetupError("INVALID_READER_ROLE", "现有 Chinook 查询角色具备过高权限，拒绝复用")
            cursor.execute("SELECT 1 FROM pg_database WHERE datname=%s", (DATABASE,))
            database_exists = cursor.fetchone() is not None
            if not database_exists:
                cursor.execute(sql.SQL("CREATE DATABASE {} ENCODING 'UTF8' TEMPLATE template0").format(sql.Identifier(DATABASE)))
    with psycopg.connect(**{**admin_parameters, "dbname": DATABASE}, autocommit=True,
                         connect_timeout=10, row_factory=dict_row) as connection:
        if not database_exists:
            with connection.transaction():
                connection.execute("SET LOCAL search_path=public,pg_catalog")
                connection.execute("SET LOCAL standard_conforming_strings=on")
                connection.execute(sanitized, prepare=False)
        actual = inspect_database(connection, expected)
        if reader_parameters is None:
            created_role = True
            password = secrets.token_urlsafe(48)
            encrypted = protect_password(password)
            with connection.transaction():
                connection.execute(sql.SQL("CREATE ROLE {} LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE NOINHERIT PASSWORD {}").format(
                    sql.Identifier(READER), sql.Literal(password)))
                for option, value in (("default_transaction_read_only", "on"), ("statement_timeout", "5s"),
                                      ("timezone", "Asia/Shanghai")):
                    connection.execute(sql.SQL("ALTER ROLE {} SET {} TO {}").format(
                        sql.Identifier(READER), sql.Identifier(option), sql.Literal(value)))
                connection.execute(sql.SQL("ALTER ROLE {} SET search_path TO public,pg_catalog").format(sql.Identifier(READER)))
            reader_parameters = {"host": HOST, "port": PORT, "dbname": DATABASE, "user": READER,
                                 "sslmode": "disable", "password": password}
        with connection.transaction():
            connection.execute("REVOKE CREATE ON SCHEMA public FROM PUBLIC")
            connection.execute(sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(sql.Identifier(DATABASE), sql.Identifier(READER)))
            connection.execute(sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(sql.Identifier(READER)))
            connection.execute(sql.SQL("GRANT SELECT ON {} TO {}").format(
                sql.SQL(", ").join(sql.Identifier("public", table) for table in TABLES), sql.Identifier(READER)))
            with connection.cursor() as cursor:
                cursor.execute("SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
                               "WHERE n.nspname='public' AND c.relkind IN ('r','p') AND "
                               "has_table_privilege(%s,c.oid,'INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')", (READER,))
                if cursor.fetchone():
                    raise ChinookSetupError("INVALID_READER_ROLE", "Chinook 查询角色具备业务写权限，未切换应用连接")
    # 专用账号实际登录读取；失败时不保存新应用配置或切换数据源。
    with psycopg.connect(**reader_parameters, autocommit=True, connect_timeout=10, row_factory=dict_row) as reader:
        with reader.transaction():
            reader.execute("SET TRANSACTION READ ONLY")
            inspect_database(reader, expected)
    if created_role:
        _save_json(app_profile, {"host": HOST, "port": PORT, "dbname": DATABASE, "user": READER,
                                 "sslmode": "disable", "password_dpapi": encrypted})
    previous = json.loads((ROOT / "source_config.json").read_text(encoding="utf-8-sig"))
    if not (BACKUP / "source_config.previous.json").is_file():
        _save_json(BACKUP / "source_config.previous.json", previous)
        _save_json(BACKUP / "connection_origin.previous.json", {
            key: previous.get(key) for key in ("source_id", "database", "reader_role", "connection_file", "hosting")})
    report = {"prepared_at": datetime.now(timezone.utc).isoformat(), "database": DATABASE,
              "host": HOST, "port": PORT, "reader_role": READER,
              "database_reused": database_exists, "source_config_switched": not prepare_only,
              "facts": actual, "script_sha256": expected["script_sha256"], "currency_assumed": None}
    _save_json(BACKUP / "setup_verification.json", report)
    if not prepare_only:
        _save_json(ROOT / "source_config.json", chinook_config(previous))
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description="安全准备本地 Chinook 只读数据库")
    parser.add_argument("--prepare-only", action="store_true", help="准备数据库和账号，不切换 source_config")
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    try:
        print(json.dumps(setup(prepare_only=args.prepare_only), ensure_ascii=False, indent=2))
        return 0
    except ChinookSetupError as error:
        output = {"status": "error", "error_code": error.code, "message": str(error)}
    except Exception:
        output = {"status": "error", "error_code": "CHINOOK_SETUP_FAILED",
                  "message": "Chinook 准备失败，已创建的数据保留供审查；未自动删除或覆盖数据库。"}
    print(json.dumps(output, ensure_ascii=False), file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
