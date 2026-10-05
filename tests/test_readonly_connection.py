"""异步只读连接的离线测试；连接工厂和配置解析均为替身。"""
from __future__ import annotations

import asyncio
import contextvars
import copy
import sys
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from database import readonly_connection
from database.readonly_connection import AsyncReadOnlyClient, run_database
from database.sql_common import ServiceFault


IDENTITY = {
    "username": "reader", "session_username": "reader", "rolsuper": False,
    "rolbypassrls": False, "rolcreaterole": False, "transaction_read_only": "on",
}
QUERY = "SELECT 1 AS value"
CONNECTION_PARAMETERS = {
    "host": "127.0.0.1", "port": 65432, "dbname": "unused",
    "user": "reader", "sslmode": "disable",
}


class FakeDatabaseError(Exception):
    def __init__(self, sqlstate):
        super().__init__("private driver diagnostics")
        self.sqlstate = sqlstate


class FakeCursor:
    def __init__(self, connection):
        self.connection = connection
        self.rows = []
        self.description = None

    async def execute(self, sql, parameters=None):
        connection = self.connection
        connection.owner_thread = threading.current_thread()
        connection.calls.append((self, sql, copy.deepcopy(parameters)))
        self.description = (SimpleNamespace(name="control", type_code=25),)
        if "set_config('statement_timeout'" in sql:
            connection.budgets.append(tuple(map(int, parameters)))
            self.rows = [{"setting": "control"}]
        elif "pg_catalog.pg_roles" in sql:
            self.rows = [copy.deepcopy(connection.identity)]
        elif sql == QUERY:
            connection.query_started.set()
            if connection.block_query:
                await asyncio.Event().wait()
            if connection.query_delay:
                await asyncio.sleep(connection.query_delay)
            if connection.failure is not None:
                raise connection.failure
            self.rows = [{"value": 1}, {"value": 2}]
            self.description = (SimpleNamespace(name="value", type_code=23),)
        else:
            self.rows = []
        return self

    async def fetchone(self):
        return self.rows.pop(0) if self.rows else None

    async def fetchmany(self, size):
        rows, self.rows = self.rows[:size], self.rows[size:]
        return rows


class FakeConnection:
    def __init__(self, *, identity=None, failure=None, block_query=False,
                 query_delay=0, hold_close=False):
        self.identity = copy.deepcopy(IDENTITY if identity is None else identity)
        self.failure, self.block_query, self.query_delay = failure, block_query, query_delay
        self.calls, self.budgets, self.cursors = [], [], []
        self.connect_arguments = None
        self.owner_thread = None
        self.query_started, self.close_started, self.closed = (
            threading.Event(), threading.Event(), threading.Event(),
        )
        self.allow_close = threading.Event()
        if not hold_close:
            self.allow_close.set()

    async def connect(self, **kwargs):
        self.connect_arguments = kwargs
        return self

    def cursor(self):
        cursor = FakeCursor(self)
        self.cursors.append(cursor)
        return cursor

    async def close(self):
        self.close_started.set()
        while not self.allow_close.is_set():
            await asyncio.sleep(0.001)
        self.closed.set()


async def wait_event(event):
    async with asyncio.timeout(2):
        while not event.is_set():
            await asyncio.sleep(0.001)


async def operation(connection, limits=None):
    client = AsyncReadOnlyClient({"reader_role": "reader"}, connect=connection.connect)
    async with client.transaction({} if limits is None else limits) as cursor:
        await cursor.execute(QUERY)
        first = await cursor.fetchmany(1)
        second = await cursor.fetchone()
        return first, second, cursor.description, threading.current_thread().name


class NoDatabaseMixin:
    def setUp(self):
        super().setUp()
        resolver = patch.object(
            readonly_connection, "resolve_connection_parameters",
            side_effect=lambda config: dict(CONNECTION_PARAMETERS),
        )
        self.resolver = resolver.start()
        self.addCleanup(resolver.stop)
        real_connection = patch(
            "psycopg.AsyncConnection.connect",
            side_effect=AssertionError("tests must not open real database connections"),
        )
        self.real_connection = real_connection.start()
        self.addCleanup(real_connection.stop)


class ReadOnlyConnectionTests(NoDatabaseMixin, unittest.IsolatedAsyncioTestCase):
    async def test_startup_readonly_search_path_and_no_commit(self):
        connection = FakeConnection()
        first, second, description, _ = await operation(connection)
        self.assertEqual(first, [{"value": 1}])
        self.assertEqual(second, {"value": 2})
        self.assertEqual(description[0].name, "value")
        self.assertEqual(len(connection.cursors), 2)
        self.assertTrue(connection.closed.is_set())
        arguments = connection.connect_arguments
        self.assertTrue(arguments["autocommit"])
        for setting in ("default_transaction_read_only=on", "statement_timeout=5000",
                        "lock_timeout=1000", "search_path=pg_catalog", "row_security=on",
                        "timezone=Asia/Shanghai"):
            self.assertIn(setting, arguments["options"])
        statements = [sql.strip() for _, sql, _ in connection.calls]
        self.assertEqual(statements[0], "BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY")
        self.assertFalse(any(sql.startswith("COMMIT") for sql in statements))
        self.real_connection.assert_not_called()

    async def test_control_cursor_preserves_business_results(self):
        connection = FakeConnection()
        first, second, description, _ = await operation(connection)
        business = next(cursor for cursor, sql, _ in connection.calls if sql == QUERY)
        setting_cursors = {id(cursor) for cursor, sql, _ in connection.calls if "set_config" in sql}
        self.assertNotIn(id(business), setting_cursors)
        self.assertEqual((first, second, description[0].name),
                         ([{"value": 1}], {"value": 2}, "value"))

    async def test_declared_database_rejects_wrong_connection_before_connect(self):
        connection = FakeConnection()
        client = AsyncReadOnlyClient({"reader_role": "reader", "database": "chinook"},
                                    connect=connection.connect)
        with self.assertRaises(ServiceFault) as caught:
            async with client.transaction({}):
                self.fail("wrong source must not be yielded")
        self.assertEqual(caught.exception.code, "DATABASE_SOURCE_MISMATCH")
        self.assertIsNone(connection.connect_arguments)

    async def test_server_database_must_match_even_when_connection_parameters_match(self):
        self.resolver.side_effect = lambda config: {**CONNECTION_PARAMETERS, "dbname": "chinook"}
        for actual in (None, "electronic_cup"):
            connection = FakeConnection(identity={**IDENTITY, "database_name": actual})
            client = AsyncReadOnlyClient({"reader_role": "reader", "database": "chinook"},
                                        connect=connection.connect)
            with self.assertRaises(ServiceFault) as caught:
                async with client.transaction({}):
                    self.fail("server source must be verified")
            self.assertEqual(caught.exception.code, "DATABASE_SOURCE_MISMATCH")
            self.assertFalse(connection.query_started.is_set())
            self.assertTrue(connection.closed.is_set())

    async def test_verified_chinook_source_yields_readonly_cursor(self):
        self.resolver.side_effect = lambda config: {**CONNECTION_PARAMETERS, "dbname": "chinook"}
        connection = FakeConnection(identity={**IDENTITY, "database_name": "chinook"})
        client = AsyncReadOnlyClient({"reader_role": "reader", "database": "chinook",
                                    "dataset": "chinook"}, connect=connection.connect)
        async with client.transaction({"_lock_invoice_before_snapshot": True}) as cursor:
            await cursor.execute(QUERY)
            self.assertEqual(await cursor.fetchone(), {"value": 1})
        self.assertIn("LOCK TABLE public.invoice IN ACCESS SHARE MODE",
                      [sql for _, sql, _ in connection.calls])
        self.assertTrue(connection.closed.is_set())

    async def test_lock_precedes_first_select_only_with_internal_flag(self):
        connection = FakeConnection()
        await operation(connection, {"_lock_invoice_before_snapshot": True})
        statements = [sql.strip() for _, sql, _ in connection.calls]
        self.assertEqual(statements[1], "LOCK TABLE public.invoice IN ACCESS SHARE MODE")
        self.assertLess(1, next(index for index, sql in enumerate(statements)
                               if sql.startswith("SELECT")))
        ordinary = FakeConnection()
        await operation(ordinary)
        self.assertFalse(any(sql.startswith("LOCK TABLE") for _, sql, _ in ordinary.calls))

    async def test_rejects_privileged_different_or_non_readonly_identity(self):
        cases = (("username", "other"), ("session_username", "other"),
                 ("rolsuper", True), ("rolbypassrls", True), ("rolcreaterole", True),
                 ("transaction_read_only", "off"), ("rolsuper", None))
        for key, value in cases:
            with self.subTest(key=key, value=value):
                connection = FakeConnection(identity={**IDENTITY, key: value})
                with self.assertRaises(ServiceFault) as caught:
                    await operation(connection)
                self.assertEqual(caught.exception.code, "INVALID_DATABASE_ROLE")
                self.assertFalse(connection.query_started.is_set())
                self.assertTrue(connection.closed.is_set())

    async def test_limits_are_capped_and_statement_budget_shrinks(self):
        connection = FakeConnection()
        native_timeout = asyncio.timeout
        with patch.object(readonly_connection.asyncio, "timeout", wraps=native_timeout) as timeout:
            await operation(connection, {"execution_timeout_ms": 100000,
                                         "connect_timeout_ms": 100000,
                                         "db_timeout_ms": 100000, "lock_timeout_ms": 100000})
        self.assertEqual([call.args[0] for call in timeout.call_args_list], [15, 5])
        self.assertEqual(connection.connect_arguments["connect_timeout"], 5)
        self.assertTrue(all(0 < lock <= statement <= 5000 and lock <= 1000
                            for statement, lock in connection.budgets))
        shrinking = FakeConnection(query_delay=0.02)
        await operation(shrinking, {"execution_timeout_ms": 250, "db_timeout_ms": 250})
        self.assertLess(shrinking.budgets[-1][0], shrinking.budgets[0][0])

    async def test_connect_timeout(self):
        async def connect(**kwargs):
            await asyncio.sleep(1)
            raise AssertionError("expired connection factory must not finish")
        client = AsyncReadOnlyClient({"reader_role": "reader"}, connect=connect)
        with self.assertRaises(ServiceFault) as caught:
            async with client.transaction({"connect_timeout_ms": 10}):
                self.fail("connection must time out before yielding")
        self.assertEqual(caught.exception.code, "CONNECTION_TIMEOUT")

    async def test_transaction_timeout_closes_connection(self):
        connection = FakeConnection(block_query=True)
        with self.assertRaises(ServiceFault) as caught:
            await operation(connection, {"execution_timeout_ms": 25})
        self.assertEqual(caught.exception.code, "QUERY_TIMEOUT")
        self.assertTrue(connection.closed.is_set())

    async def test_consumer_wait_is_in_total_budget(self):
        connection = FakeConnection()
        client = AsyncReadOnlyClient({"reader_role": "reader"}, connect=connection.connect)
        with self.assertRaises(ServiceFault) as caught:
            async with client.transaction({"execution_timeout_ms": 25}) as cursor:
                await cursor.execute(QUERY)
                await asyncio.sleep(1)
        self.assertEqual(caught.exception.code, "QUERY_TIMEOUT")
        self.assertTrue(connection.closed.is_set())

    async def test_cancel_and_repeat_cancel_wait_for_close(self):
        connection = FakeConnection(block_query=True, hold_close=True)
        task = asyncio.create_task(operation(connection))
        try:
            await wait_event(connection.query_started)
            task.cancel()
            await wait_event(connection.close_started)
            task.cancel()
            await asyncio.sleep(0)
            self.assertFalse(task.done())
            self.assertFalse(connection.closed.is_set())
        finally:
            connection.allow_close.set()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertTrue(connection.closed.is_set())

    async def test_sqlstate_mapping_hides_driver_diagnostics(self):
        cases = (("57014", "QUERY_TIMEOUT"), ("55P03", "LOCK_TIMEOUT"),
                 ("42501", "DATABASE_PERMISSION_DENIED"),
                 ("28P01", "DATABASE_AUTHENTICATION_FAILED"),
                 ("28000", "DATABASE_AUTHENTICATION_FAILED"),
                 ("08001", "DATABASE_CONNECTION_FAILED"), ("XX000", "DATABASE_ERROR"))
        for state, code in cases:
            with self.subTest(state=state):
                connection = FakeConnection(failure=FakeDatabaseError(state))
                with self.assertRaises(ServiceFault) as caught:
                    await operation(connection)
                self.assertEqual(caught.exception.code, code)
                self.assertNotIn("private", str(caught.exception))
                self.assertTrue(connection.closed.is_set())


class LoopBridgeTests(NoDatabaseMixin, unittest.TestCase):
    def test_selector_uses_calling_thread(self):
        connection = FakeConnection()
        with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
            result = runner.run(run_database(lambda: operation(connection)))
        self.assertEqual(result[3], threading.current_thread().name)
        self.assertTrue(connection.closed.is_set())

    @unittest.skipUnless(sys.platform == "win32", "requires Windows Proactor")
    def test_proactor_uses_selector_thread_without_policy_change(self):
        connection = FakeConnection()
        request_context = contextvars.ContextVar("readonly-test-request", default=None)

        async def factory():
            self.assertIsInstance(asyncio.get_running_loop(), asyncio.SelectorEventLoop)
            self.assertEqual(request_context.get(), "request-123")
            return await operation(connection)

        async def scenario():
            token = request_context.set("request-123")
            try:
                return await run_database(factory)
            finally:
                request_context.reset(token)

        with patch.object(asyncio, "set_event_loop_policy") as change_policy:
            with asyncio.Runner(loop_factory=asyncio.ProactorEventLoop) as runner:
                result = runner.run(scenario())
            change_policy.assert_not_called()
        self.assertEqual(result[3], "query-agent-selector")
        self.assertTrue(connection.closed.is_set())
        connection.owner_thread.join(timeout=1)
        self.assertFalse(connection.owner_thread.is_alive())

    @unittest.skipUnless(sys.platform == "win32", "requires Windows Proactor")
    def test_proactor_cancel_waits_for_actual_worker_cleanup(self):
        connection = FakeConnection(block_query=True, hold_close=True)

        async def scenario():
            task = asyncio.create_task(run_database(lambda: operation(connection)))
            try:
                await wait_event(connection.query_started)
                task.cancel()
                await wait_event(connection.close_started)
                task.cancel()
                await asyncio.sleep(0)
                self.assertFalse(task.done())
                self.assertFalse(connection.closed.is_set())
            finally:
                connection.allow_close.set()
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
            self.assertTrue(connection.closed.is_set())

        with asyncio.Runner(loop_factory=asyncio.ProactorEventLoop) as runner:
            runner.run(scenario())
        connection.owner_thread.join(timeout=1)
        self.assertFalse(connection.owner_thread.is_alive())


if __name__ == "__main__":
    unittest.main()
