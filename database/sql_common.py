"""SQL 流程共用的异常、时间处理和只读数据库连接。"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import date, datetime
from decimal import Decimal


class ServiceFault(Exception):
    def __init__(self, code, message, *, status="error", issues=None,
                 execution_attempted=False):
        super().__init__(message)
        self.code = code
        self.status = status
        self.issues = issues or []
        self.execution_attempted = execution_attempted


def json_value(value):
    if isinstance(value, Decimal):
        return format(value, ".2f")
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_value(item) for item in value]
    return value


def positive_limit(value, cap, name):
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ServiceFault("INVALID_QUERY", f"{name} 必须为正整数")
    return min(value, cap)


def resolve_connection_parameters(config=None):
    if __package__:
        from .connection_settings import ConnectionSettingsError, resolve_connection_parameters as resolve
    else:
        from connection_settings import ConnectionSettingsError, resolve_connection_parameters as resolve
    try:
        return resolve(config)
    except ConnectionSettingsError as error:
        raise ServiceFault(error.code, str(error)) from error


def verify_database_target(config, database_name):
    """配置声明的源库必须与解析参数和服务器实际库名一致。"""
    if "database" not in config:
        return
    expected = config["database"]
    if not isinstance(expected, str) or not expected.strip():
        raise ServiceFault("INVALID_CONFIGURATION", "数据源必须声明有效的目标数据库")
    if database_name != expected:
        raise ServiceFault("DATABASE_SOURCE_MISMATCH", "实际数据库与数据源配置不一致")


class ReadOnlyClient:
    """所有实时元数据与业务查询都使用独立只读身份。"""

    def __init__(self, config=None):
        self.config = config or {}

    @contextmanager
    def transaction(self, limits):
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError as error:
            raise ServiceFault("DRIVER_NOT_INSTALLED", "请安装 psycopg 3") from error

        parameters = resolve_connection_parameters(self.config)
        verify_database_target(self.config, parameters.get("dbname"))
        host = parameters["host"]
        loopback = host in {"localhost", "127.0.0.1", "::1"}
        sslmode = parameters.get("sslmode", "require")
        if sslmode not in {"require", "verify-ca", "verify-full"}:
            if not loopback or sslmode not in {"disable", "allow", "prefer"}:
                raise ServiceFault("INVALID_CONFIGURATION", "远程数据库连接必须启用 TLS")
        try:
            with psycopg.connect(**parameters, connect_timeout=10,
                                 row_factory=dict_row) as connection:
                with connection.cursor() as cursor:
                    cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
                    cursor.execute("SET LOCAL standard_conforming_strings = on")
                    cursor.execute("SELECT set_config('statement_timeout', %s, true)",
                                   (str(limits["db_timeout_ms"]),))
                    cursor.execute("SELECT set_config('lock_timeout', %s, true)",
                                   (str(positive_limit(limits.get("lock_timeout_ms", 1000),
                                                       min(limits["db_timeout_ms"], 1000),
                                                       "lock_timeout_ms")),))
                    cursor.execute("SELECT set_config('TimeZone', %s, true)",
                                   (self.config.get("timezone", "Asia/Shanghai"),))
                    cursor.execute("SELECT current_user AS username, current_database() AS database_name, rolsuper, rolbypassrls "
                                   "FROM pg_roles WHERE rolname = current_user")
                    identity = cursor.fetchone()
                    verify_database_target(self.config, identity.get("database_name") if identity else None)
                    if (not identity or identity["username"] != self.config.get(
                            "reader_role", "chinook_reader") or identity["rolsuper"]
                            or identity["rolbypassrls"]):
                        raise ServiceFault("INVALID_DATABASE_ROLE", "请使用配置指定的专用只读登录账号")
                    yield cursor
        except ServiceFault:
            raise
        except psycopg.Error as error:
            code = ("QUERY_TIMEOUT" if error.sqlstate == "57014"
                    else "LOCK_TIMEOUT" if error.sqlstate == "55P03"
                    else "DATABASE_PERMISSION_DENIED" if error.sqlstate == "42501"
                    else "DATABASE_AUTHENTICATION_FAILED" if error.sqlstate in {"28P01", "28000"}
                    else "DATABASE_ERROR")
            raise ServiceFault(code, "数据库连接或执行失败，请检查本地连接配置和权限") from error
