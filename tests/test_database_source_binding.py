"""同步读路径不能将错误库标记为当前 Chinook 数据源。"""
import unittest
from unittest.mock import MagicMock, patch

from database.sql_common import ReadOnlyClient, ServiceFault


class SourceBindingTests(unittest.TestCase):
    def setUp(self):
        self.config = {"database": "chinook", "reader_role": "chinook_reader"}
        self.parameters = {"host": "127.0.0.1", "dbname": "chinook", "user": "chinook_reader",
                           "port": 5432, "sslmode": "disable"}

    def test_wrong_resolved_source_never_connects(self):
        with patch("database.sql_common.resolve_connection_parameters",
                   return_value={**self.parameters, "dbname": "electronic_cup"}), \
             patch("psycopg.connect") as connect:
            with self.assertRaises(ServiceFault) as caught:
                with ReadOnlyClient(self.config).transaction({"db_timeout_ms": 5000}):
                    self.fail("source mismatch must fail")
            self.assertEqual(caught.exception.code, "DATABASE_SOURCE_MISMATCH")
            connect.assert_not_called()

    def test_actual_source_checked_and_lock_timeout_configured(self):
        for actual in ("chinook", "electronic_cup", None):
            with self.subTest(actual=actual):
                connection, cursor = MagicMock(), MagicMock()
                connection.cursor.return_value.__enter__.return_value = cursor
                cursor.fetchone.return_value = {"username": "chinook_reader", "database_name": actual,
                                                "rolsuper": False, "rolbypassrls": False}
                with patch("database.sql_common.resolve_connection_parameters", return_value=self.parameters), \
                     patch("psycopg.connect") as connect:
                    connect.return_value.__enter__.return_value = connection
                    if actual == "chinook":
                        with ReadOnlyClient(self.config).transaction({"db_timeout_ms": 5000}) as result:
                            self.assertIs(result, cursor)
                    else:
                        with self.assertRaises(ServiceFault) as caught:
                            with ReadOnlyClient(self.config).transaction({"db_timeout_ms": 5000}):
                                self.fail("server database must match")
                        self.assertEqual(caught.exception.code, "DATABASE_SOURCE_MISMATCH")
                    cursor.execute.assert_any_call("SELECT set_config('lock_timeout', %s, true)", ("1000",))
