"""按连接来源读取参数；本地密码使用当前 Windows 用户的 DPAPI。

读取、解密仅发生在显式调用时。环境配置是独立连接来源，不与保存的
主机或密码混用，也不会修改进程或系统环境变量。
"""
from __future__ import annotations

import base64
import binascii
import ctypes
import json
import os
from ctypes import wintypes
from pathlib import Path


class ConnectionSettingsError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD),
                ("pbData", ctypes.POINTER(ctypes.c_ubyte))]


def _dpapi(data, *, encrypt):
    if os.name != "nt":
        raise ConnectionSettingsError(
            "INVALID_CONFIGURATION", "本地加密密码只能由 Windows 当前用户读取或保存")
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.LocalFree.argtypes = [wintypes.HLOCAL]
    kernel32.LocalFree.restype = wintypes.HLOCAL
    function = crypt32.CryptProtectData if encrypt else crypt32.CryptUnprotectData
    function.argtypes = [
        ctypes.POINTER(_DataBlob),
        wintypes.LPCWSTR if encrypt else ctypes.POINTER(wintypes.LPWSTR),
        ctypes.POINTER(_DataBlob), wintypes.LPVOID, wintypes.LPVOID,
        wintypes.DWORD, ctypes.POINTER(_DataBlob),
    ]
    function.restype = wintypes.BOOL

    # buffer 必须存活至 API 返回；长度不包含 create_string_buffer 的结尾 NUL。
    buffer = ctypes.create_string_buffer(data)
    input_blob = _DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    output_blob = _DataBlob()
    try:
        success = function(ctypes.byref(input_blob), None, None, None, None,
                           0x1, ctypes.byref(output_blob))  # CRYPTPROTECT_UI_FORBIDDEN
        if not success:
            raise ConnectionSettingsError(
                "INVALID_CONFIGURATION", "本地密码加密或解密失败，请由同一 Windows 用户重新配置")
        return ctypes.string_at(output_blob.pbData, output_blob.cbData)
    finally:
        if output_blob.pbData:
            kernel32.LocalFree(ctypes.cast(output_blob.pbData, wintypes.HLOCAL))


def protect_password(password):
    """UTF-8 明文 → 当前用户 DPAPI 密文 → base64 ASCII。"""
    if not isinstance(password, str):
        raise ConnectionSettingsError("INVALID_CONFIGURATION", "密码必须为字符串")
    try:
        protected = _dpapi(password.encode("utf-8"), encrypt=True)
        return base64.b64encode(protected).decode("ascii")
    except ConnectionSettingsError:
        raise
    except (OSError, ValueError, UnicodeError, ctypes.ArgumentError) as error:
        raise ConnectionSettingsError("INVALID_CONFIGURATION", "本地密码加密失败") from error


def unprotect_password(password_dpapi):
    """解密 protect_password() 生成的 base64，不支持其他密文格式。"""
    if not isinstance(password_dpapi, str) or not password_dpapi:
        raise ConnectionSettingsError("INVALID_CONFIGURATION", "本地加密密码格式无效")
    try:
        encrypted = base64.b64decode(password_dpapi, validate=True)
        return _dpapi(encrypted, encrypt=False).decode("utf-8")
    except ConnectionSettingsError:
        raise
    except (binascii.Error, OSError, ValueError, UnicodeError, ctypes.ArgumentError) as error:
        raise ConnectionSettingsError("INVALID_CONFIGURATION", "本地加密密码损坏或无法解密") from error


_ENVIRONMENT_CONNECTION_KEYS = (
    "PGHOST", "PGPORT", "PGDATABASE", "PGUSER", "PGPASSWORD", "PGSSLMODE",
)
_SSLMODES = {"disable", "allow", "prefer", "require", "verify-ca", "verify-full"}


def _validate_parameters(parameters):
    for key in ("host", "dbname", "user"):
        if not isinstance(parameters.get(key), str) or not parameters[key].strip():
            raise ConnectionSettingsError("INVALID_CONFIGURATION", "数据库主机、库名和账号必须为非空字符串")
    port = parameters.get("port", 5432)
    if isinstance(port, bool) or not isinstance(port, (str, int)):
        raise ConnectionSettingsError("INVALID_CONFIGURATION", "数据库端口必须为有效整数")
    try:
        port = int(port)
    except ValueError as error:
        raise ConnectionSettingsError("INVALID_CONFIGURATION", "数据库端口必须为有效整数") from error
    if not 1 <= port <= 65535:
        raise ConnectionSettingsError("INVALID_CONFIGURATION", "数据库端口超出有效范围")
    parameters["port"] = port
    if not isinstance(parameters.get("sslmode"), str) or parameters["sslmode"] not in _SSLMODES:
        raise ConnectionSettingsError("INVALID_CONFIGURATION", "数据库 SSL 配置无效")
    if "hostaddr" in parameters and (
            not isinstance(parameters["hostaddr"], str) or not parameters["hostaddr"].strip()):
        raise ConnectionSettingsError("INVALID_CONFIGURATION", "数据库 hostaddr 配置无效")
    return parameters


def resolve_connection_parameters(config=None):
    """解析一组独立连接参数，不连接数据库，不修改环境变量。

    任一 PG 连接环境变量存在时，完整使用环境模式；环境缺少必需成员
    不会回退到本地配置。该模式不读取或解密本地保存的密码。
    """
    if config is not None and not isinstance(config, dict):
        raise ConnectionSettingsError("INVALID_CONFIGURATION", "连接配置必须为字典")
    config = {} if config is None else config
    if any(name in os.environ for name in _ENVIRONMENT_CONNECTION_KEYS):
        if not all(os.environ.get(name, "").strip() for name in ("PGHOST", "PGDATABASE", "PGUSER")):
            raise ConnectionSettingsError(
                "DATABASE_NOT_CONFIGURED", "环境连接配置需完整提供 PGHOST、PGDATABASE、PGUSER")
        parameters = {
            "host": os.environ["PGHOST"], "dbname": os.environ["PGDATABASE"],
            "user": os.environ["PGUSER"], "port": os.environ.get("PGPORT", "5432"),
            "sslmode": os.environ.get("PGSSLMODE", "require"),
        }
        if "PGPASSWORD" in os.environ:
            parameters["password"] = os.environ["PGPASSWORD"]
        if os.environ.get("PGHOSTADDR"):
            parameters["hostaddr"] = os.environ["PGHOSTADDR"]
        return _validate_parameters(parameters)

    try:
        base = Path(config.get("_base_dir", Path(__file__).resolve().parent))
        path = Path(config.get("connection_file", ".local/connection_chinook.json"))
        path = path if path.is_absolute() else base / path
        profile = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError as error:
        raise ConnectionSettingsError(
            "DATABASE_NOT_CONFIGURED", "未找到本地连接配置，请配置 PG 环境变量或保存本地连接") from error
    except (OSError, ValueError, TypeError) as error:
        raise ConnectionSettingsError("INVALID_CONFIGURATION", "本地连接文件无法读取或格式无效") from error
    if not isinstance(profile, dict) or not all(
            key in profile for key in ("host", "port", "dbname", "user", "sslmode", "password_dpapi")):
        raise ConnectionSettingsError("INVALID_CONFIGURATION", "本地连接文件缺少必需配置")
    parameters = {key: profile[key] for key in ("host", "port", "dbname", "user", "sslmode")}
    if "hostaddr" in profile:
        parameters["hostaddr"] = profile["hostaddr"]
    parameters = _validate_parameters(parameters)
    parameters["password"] = unprotect_password(profile["password_dpapi"])
    return parameters
