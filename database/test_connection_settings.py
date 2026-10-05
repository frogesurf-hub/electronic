"""连接参数解析的离线测试；仅使用临时合成配置和假加解密。

不会读取 database/.local，不会使用真实密码，不会连接数据库。
"""
from __future__ import annotations

import base64
import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

if __package__:
    from . import connection_settings, sql_common
else:
    import connection_settings
    import sql_common


class ConnectionSettingsTests(unittest.TestCase):
    def setUp(self):
        guard = patch("psycopg.connect", side_effect=AssertionError("离线测试禁止连接真实数据库"))
        self.guard = guard.start()
        self.addCleanup(guard.stop)
        environment = patch.dict(os.environ, {}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        self.temporary = tempfile.TemporaryDirectory(prefix="sql-connection-test-")
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.config = {"_base_dir": str(self.base), "connection_file": "synthetic.json"}

    def tearDown(self):
        self.guard.assert_not_called()

    def write_profile(self, profile):
        (self.base / "synthetic.json").write_text(json.dumps(profile), encoding="utf-8")

    @staticmethod
    def profile():
        return {
            "host": "127.0.0.1", "port": 5432, "dbname": "synthetic_db",
            "user": "synthetic_reader", "sslmode": "disable",
            "password_dpapi": "SYNTHETIC_ENCRYPTED_TOKEN",
        }

    def test_complete_environment_takes_precedence_without_profile_or_decryption(self):
        values = {
            "PGHOST": "synthetic.example", "PGDATABASE": "synthetic_db",
            "PGUSER": "synthetic_reader", "PGPORT": "6543",
            "PGPASSWORD": "SYNTHETIC_PASSWORD", "PGSSLMODE": "require",
            "PGHOSTADDR": "192.0.2.1",
        }
        os.environ.update(values)
        before = dict(os.environ)
        with patch.object(Path, "read_text", side_effect=AssertionError("环境模式不应读取文件")), \
                patch.object(connection_settings, "unprotect_password") as decrypt:
            result = connection_settings.resolve_connection_parameters(self.config)
        self.assertEqual(result, {
            "host": "synthetic.example", "dbname": "synthetic_db", "user": "synthetic_reader",
            "port": 6543, "password": "SYNTHETIC_PASSWORD", "sslmode": "require",
            "hostaddr": "192.0.2.1",
        })
        self.assertEqual(dict(os.environ), before)
        decrypt.assert_not_called()

    def test_partial_environment_never_falls_back_to_saved_credentials(self):
        for values in ({"PGPORT": "5432"}, {"PGPASSWORD": "SYNTHETIC_PASSWORD"},
                       {"PGHOST": "127.0.0.1", "PGUSER": "synthetic_reader"}):
            with self.subTest(values=values), patch.dict(os.environ, values, clear=True), \
                    patch.object(Path, "read_text", side_effect=AssertionError("禁止来源混用")), \
                    patch.object(connection_settings, "unprotect_password") as decrypt:
                with self.assertRaises(connection_settings.ConnectionSettingsError) as caught:
                    connection_settings.resolve_connection_parameters(self.config)
                self.assertEqual(caught.exception.code, "DATABASE_NOT_CONFIGURED")
                self.assertNotIn("SYNTHETIC_PASSWORD", str(caught.exception))
                decrypt.assert_not_called()

    def test_missing_explicit_profile_reports_not_configured(self):
        with self.assertRaises(connection_settings.ConnectionSettingsError) as caught:
            connection_settings.resolve_connection_parameters(self.config)
        self.assertEqual(caught.exception.code, "DATABASE_NOT_CONFIGURED")

    def test_synthetic_profile_decrypts_and_keeps_environment_unchanged(self):
        profile = self.profile()
        self.write_profile(profile)
        before = copy.deepcopy(self.config)
        with patch.object(connection_settings, "unprotect_password",
                          return_value="SYNTHETIC_PASSWORD") as decrypt:
            result = connection_settings.resolve_connection_parameters(self.config)
        decrypt.assert_called_once_with("SYNTHETIC_ENCRYPTED_TOKEN")
        self.assertEqual(result, {
            "host": "127.0.0.1", "port": 5432, "dbname": "synthetic_db",
            "user": "synthetic_reader", "sslmode": "disable", "password": "SYNTHETIC_PASSWORD",
        })
        self.assertNotIn("password_dpapi", result)
        self.assertEqual(self.config, before)
        self.assertEqual(dict(os.environ), {})

    def test_malformed_profile_reports_configuration_error_without_secrets(self):
        malformed = [[], {}, {**self.profile(), "port": 0},
                     {**self.profile(), "port": True},
                     {**self.profile(), "sslmode": "unknown"}]
        for profile in malformed:
            with self.subTest(profile=profile):
                self.write_profile(profile)
                with patch.object(connection_settings, "unprotect_password") as decrypt:
                    with self.assertRaises(connection_settings.ConnectionSettingsError) as caught:
                        connection_settings.resolve_connection_parameters(self.config)
                self.assertEqual(caught.exception.code, "INVALID_CONFIGURATION")
                self.assertNotIn("SYNTHETIC_ENCRYPTED_TOKEN", str(caught.exception))
                decrypt.assert_not_called()
        (self.base / "synthetic.json").write_text("not valid json", encoding="utf-8")
        with self.assertRaises(connection_settings.ConnectionSettingsError) as caught:
            connection_settings.resolve_connection_parameters(self.config)
        self.assertEqual(caught.exception.code, "INVALID_CONFIGURATION")

    def test_protect_and_unprotect_use_utf8_base64_without_real_dpapi(self):
        with patch.object(connection_settings, "_dpapi", return_value=b"synthetic-encrypted") as dpapi:
            encoded = connection_settings.protect_password("合成密码")
        self.assertEqual(encoded, base64.b64encode(b"synthetic-encrypted").decode("ascii"))
        dpapi.assert_called_once_with("合成密码".encode("utf-8"), encrypt=True)
        with patch.object(connection_settings, "_dpapi", return_value="合成密码".encode("utf-8")) as dpapi:
            decoded = connection_settings.unprotect_password(encoded)
        self.assertEqual(decoded, "合成密码")
        dpapi.assert_called_once_with(b"synthetic-encrypted", encrypt=False)

    def test_bad_ciphertext_does_not_invoke_dpapi_or_expose_input(self):
        with patch.object(connection_settings, "_dpapi") as dpapi:
            for ciphertext in (None, "", "SYNTHETIC_INVALID_@TOKEN"):
                with self.subTest(ciphertext=ciphertext):
                    with self.assertRaises(connection_settings.ConnectionSettingsError) as caught:
                        connection_settings.unprotect_password(ciphertext)
                    self.assertEqual(caught.exception.code, "INVALID_CONFIGURATION")
                    self.assertNotIn("SYNTHETIC_INVALID_@TOKEN", str(caught.exception))
            dpapi.assert_not_called()

    def test_common_wrapper_translates_settings_error_to_service_fault(self):
        error = connection_settings.ConnectionSettingsError("DATABASE_NOT_CONFIGURED", "合成配置缺失")
        with patch.object(connection_settings, "resolve_connection_parameters", side_effect=error):
            with self.assertRaises(sql_common.ServiceFault) as caught:
                sql_common.resolve_connection_parameters(self.config)
        self.assertEqual(caught.exception.code, "DATABASE_NOT_CONFIGURED")
        self.assertEqual(str(caught.exception), "合成配置缺失")


if __name__ == "__main__":
    unittest.main(verbosity=2)
