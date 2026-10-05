"""异步只读连接、共享预算和 Windows Selector 桥接；不记录连接凭据。"""
from __future__ import annotations

import asyncio
import concurrent.futures
import contextvars
import math
import sys
import threading
from collections.abc import Mapping
from contextlib import asynccontextmanager

from .sql_common import ServiceFault, positive_limit, resolve_connection_parameters, verify_database_target


_IDENTITY_SQL = """
SELECT current_user AS username, session_user AS session_username,
       current_database() AS database_name,
       r.rolsuper, r.rolbypassrls, r.rolcreaterole,
       current_setting('transaction_read_only') AS transaction_read_only
FROM pg_catalog.pg_roles AS r WHERE r.rolname = current_user
"""
_BUDGET_SQL = """
SELECT pg_catalog.set_config('statement_timeout', %s, true),
       pg_catalog.set_config('lock_timeout', %s, true)
"""
_LOCK_INVOICE_SQL = "LOCK TABLE public.invoice IN ACCESS SHARE MODE"


def _limits(value):
    if not isinstance(value, Mapping):
        raise ServiceFault("INVALID_QUERY", "数据库执行限制必须为映射")
    statement = positive_limit(value.get("db_timeout_ms", 5000), 5000, "db_timeout_ms")
    return {
        "db_timeout_ms": statement,
        "lock_timeout_ms": positive_limit(value.get("lock_timeout_ms", 1000),
                                          min(statement, 1000), "lock_timeout_ms"),
        "execution_timeout_ms": positive_limit(value.get("execution_timeout_ms", 10000),
                                               15000, "execution_timeout_ms"),
        "connect_timeout_ms": positive_limit(value.get("connect_timeout_ms", 3000),
                                             5000, "connect_timeout_ms"),
    }


def _remaining_ms(deadline):
    remaining = math.floor((deadline - asyncio.get_running_loop().time()) * 1000)
    if remaining <= 0:
        raise ServiceFault("QUERY_TIMEOUT", "数据库执行总预算已耗尽")
    return remaining


def _database_fault(error):
    state = getattr(error, "sqlstate", None)
    if state == "57014":
        return ServiceFault("QUERY_TIMEOUT", "数据库查询超时或已取消")
    if state == "55P03":
        return ServiceFault("LOCK_TIMEOUT", "数据库锁等待超过限制")
    if state == "42501":
        return ServiceFault("DATABASE_PERMISSION_DENIED", "数据库只读账号权限不足")
    if state in {"28P01", "28000"}:
        return ServiceFault("DATABASE_AUTHENTICATION_FAILED", "数据库身份验证失败")
    if isinstance(state, str) and state.startswith("08"):
        return ServiceFault("DATABASE_CONNECTION_FAILED", "数据库连接不可用")
    return ServiceFault("DATABASE_ERROR", "数据库连接或操作失败")


async def _finish_close(connection):
    """外层重复取消也不能取消连接关闭；关闭完成后仍传播取消。"""
    async def close_once():
        try:
            await connection.close()
        except BaseException as error:
            return error
        return None

    closing = asyncio.create_task(close_once())
    cancelled = False
    while not closing.done():
        try:
            await asyncio.shield(closing)
        except asyncio.CancelledError:
            cancelled = True
        except Exception:
            break
    error = closing.result()
    if cancelled:
        raise asyncio.CancelledError
    if error is not None:
        raise error


class _BudgetCursor:
    """设置超时使用独立 cursor，不能覆盖业务 cursor 的待取结果。"""

    def __init__(self, cursor, control, deadline, limits):
        self._cursor = cursor
        self._control = control
        self._deadline = deadline
        self._limits = limits

    @property
    def description(self):
        return self._cursor.description

    async def _before_operation(self):
        remaining = _remaining_ms(self._deadline)
        statement = min(self._limits["db_timeout_ms"], remaining)
        lock = min(self._limits["lock_timeout_ms"], statement)
        await self._control.execute(_BUDGET_SQL, (str(statement), str(lock)))
        _remaining_ms(self._deadline)

    async def execute(self, sql, params=None):
        if not isinstance(sql, str) or not sql.strip():
            raise ServiceFault("INVALID_QUERY", "数据库语句必须为非空字符串")
        await self._before_operation()
        await self._cursor.execute(sql, params)
        return self

    async def fetchone(self):
        await self._before_operation()
        return await self._cursor.fetchone()

    async def fetchmany(self, size):
        if type(size) is not int or size <= 0:
            raise ServiceFault("INVALID_QUERY", "读取行数必须为正整数")
        await self._before_operation()
        return await self._cursor.fetchmany(size)


class AsyncReadOnlyClient:
    """专用账号、只读事务及连接和消费阶段共用的总执行预算。"""

    def __init__(self, config=None, *, connect=None):
        if config is not None and not isinstance(config, Mapping):
            raise ValueError("数据库配置必须为映射")
        if connect is not None and not callable(connect):
            raise ValueError("connect 必须为可调用的异步连接工厂")
        self.config = {} if config is None else dict(config)
        self._connect = connect

    @asynccontextmanager
    async def transaction(self, limits):
        budget = _limits(limits)
        expected_role = self.config.get("reader_role", "chinook_reader")
        if not isinstance(expected_role, str) or not expected_role.strip():
            raise ServiceFault("INVALID_CONFIGURATION", "缺少有效的专用只读角色名称")
        connection = None
        failed = False
        deadline = asyncio.get_running_loop().time() + budget["execution_timeout_ms"] / 1000
        try:
            async with asyncio.timeout(budget["execution_timeout_ms"] / 1000):
                try:
                    from psycopg import AsyncConnection
                    from psycopg.rows import dict_row
                except ImportError as error:
                    raise ServiceFault("DRIVER_NOT_INSTALLED", "请安装 psycopg 3") from error
                parameters = resolve_connection_parameters(self.config)
                verify_database_target(self.config, parameters.get("dbname"))
                host = parameters["host"]
                sslmode = parameters.get("sslmode", "require")
                if sslmode not in {"require", "verify-ca", "verify-full"} and (
                    host not in {"localhost", "127.0.0.1", "::1"}
                    or sslmode not in {"disable", "allow", "prefer"}
                ):
                    raise ServiceFault("INVALID_CONFIGURATION", "远程数据库连接必须启用 TLS")
                statement = min(budget["db_timeout_ms"], _remaining_ms(deadline))
                lock = min(budget["lock_timeout_ms"], statement)
                parameters = dict(parameters)
                parameters.update(
                    options=("-c default_transaction_read_only=on "
                             f"-c statement_timeout={statement} -c lock_timeout={lock} "
                             "-c search_path=pg_catalog -c row_security=on -c standard_conforming_strings=on "
                             "-c timezone=Asia/Shanghai"),
                    connect_timeout=max(2, math.ceil(budget["connect_timeout_ms"] / 1000)),
                    row_factory=dict_row,
                    autocommit=True,
                )
                factory = self._connect if self._connect is not None else AsyncConnection.connect
                connect_seconds = min(budget["connect_timeout_ms"], _remaining_ms(deadline)) / 1000
                try:
                    async with asyncio.timeout(connect_seconds) as connecting:
                        connection = await factory(**parameters)
                    if connecting.expired():
                        raise ServiceFault("CONNECTION_TIMEOUT", "数据库连接超过时间限制")
                except TimeoutError as error:
                    raise ServiceFault("CONNECTION_TIMEOUT", "数据库连接超过时间限制") from error
                _remaining_ms(deadline)
                control = connection.cursor()
                await control.execute("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY")
                if limits.get("_lock_chinook_schema_before_snapshot") is True:
                    if self.config.get("dataset") != "chinook":
                        raise ServiceFault("INVALID_CONFIGURATION", "完整结构锁仅适用于 Chinook")
                    _remaining_ms(deadline)
                    await control.execute(
                        "LOCK TABLE public.album, public.artist, public.customer, public.employee, "
                        "public.genre, public.invoice, public.invoice_line, public.media_type, "
                        "public.playlist, public.playlist_track, public.track IN ACCESS SHARE MODE")
                elif limits.get("_lock_invoice_before_snapshot") is True:
                    # 锁先于身份和预算 SELECT，防止 RR 快照建立后才阻止并发 DDL。
                    _remaining_ms(deadline)
                    await control.execute(_LOCK_INVOICE_SQL)
                cursor = _BudgetCursor(connection.cursor(), control, deadline, budget)
                await cursor.execute(_IDENTITY_SQL)
                identity = await cursor.fetchone()
                verify_database_target(self.config, identity.get("database_name")
                                       if isinstance(identity, Mapping) else None)
                if (not isinstance(identity, Mapping)
                        or identity.get("username") != expected_role
                        or identity.get("session_username") != expected_role
                        or any(identity.get(flag) is not False
                               for flag in ("rolsuper", "rolbypassrls", "rolcreaterole"))
                        or identity.get("transaction_read_only") != "on"):
                    raise ServiceFault("INVALID_DATABASE_ROLE", "请使用配置指定的非特权只读登录账号")
                _remaining_ms(deadline)
                yield cursor
                _remaining_ms(deadline)
        except asyncio.CancelledError:
            failed = True
            raise
        except ServiceFault:
            failed = True
            raise
        except TimeoutError as error:
            failed = True
            raise ServiceFault("QUERY_TIMEOUT", "数据库执行总预算已耗尽") from error
        except Exception as error:
            failed = True
            raise _database_fault(error) from error
        except BaseException:
            failed = True
            raise
        finally:
            if connection is not None:
                try:
                    # 不使用 connection 上下文，不提交事务；关闭会终止只读事务。
                    await _finish_close(connection)
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    if not failed:
                        raise _database_fault(error) from error


async def _drain_worker(future):
    """调用方取消后等待工作线程已关闭连接和事件循环，不转发重复取消。"""
    while not future.done():
        try:
            await asyncio.shield(future)
        except asyncio.CancelledError:
            continue
        except BaseException:
            break
    if future.done():
        try:
            future.result()
        except BaseException:
            pass


async def run_database(factory):
    """Proactor 上使用私有 Selector 线程；其它循环直接执行异步工厂。"""
    if not callable(factory):
        raise ValueError("factory 必须为返回 awaitable 的可调用对象")
    caller_loop = asyncio.get_running_loop()
    proactor = getattr(asyncio, "ProactorEventLoop", ())
    if sys.platform != "win32" or not isinstance(caller_loop, proactor):
        return await factory()

    finished = concurrent.futures.Future()
    lock = threading.Lock()
    state = {"loop": None, "task": None, "cancel_requested": False}
    inherited_context = contextvars.copy_context()

    async def invoke():
        return await factory()

    def worker():
        loop = None
        result, failure = None, None
        try:
            loop = asyncio.SelectorEventLoop()
            asyncio.set_event_loop(loop)
            task = loop.create_task(invoke())
            with lock:
                state["loop"], state["task"] = loop, task
                cancel_requested = state["cancel_requested"]
            if cancel_requested:
                task.cancel()
            result = loop.run_until_complete(task)
        except BaseException as error:
            failure = error
        finally:
            if loop is not None:
                try:
                    loop.run_until_complete(loop.shutdown_asyncgens())
                    loop.run_until_complete(loop.shutdown_default_executor())
                except BaseException as error:
                    if failure is None:
                        failure = error
                finally:
                    try:
                        asyncio.set_event_loop(None)
                        loop.close()
                    except BaseException as error:
                        if failure is None:
                            failure = error
            # 只有私有循环和连接清理结束，调用方才会收到结果或取消。
            # 将异常作为私有结果传递，避免 shield 在调用方取消后记录底层异常。
            finished.set_result((result, failure))

    thread = threading.Thread(target=lambda: inherited_context.run(worker),
                              name="query-agent-selector", daemon=True)
    thread.start()
    completion = asyncio.wrap_future(finished, loop=caller_loop)
    try:
        result, failure = await asyncio.shield(completion)
        if failure is not None:
            raise failure
        return result
    except asyncio.CancelledError:
        with lock:
            state["cancel_requested"] = True
            worker_loop, task = state["loop"], state["task"]
        if worker_loop is not None and task is not None:
            try:
                worker_loop.call_soon_threadsafe(task.cancel)
            except RuntimeError:
                # 线程可能刚完成关闭；completion 仍会在清理之后完成。
                pass
        await _drain_worker(completion)
        raise
