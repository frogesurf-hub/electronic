"""读取实时 PostgreSQL 结构及业务元数据；离线文件读取须显式选择。"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

if __package__:
    from .sql_common import ReadOnlyClient, ServiceFault, json_value
else:
    from sql_common import ReadOnlyClient, ServiceFault, json_value


MAX_METADATA_ROWS = 10_000
CHINOOK_TABLES = tuple("public." + table for table in (
    "album", "artist", "customer", "employee", "genre", "invoice", "invoice_line",
    "media_type", "playlist", "playlist_track", "track",
))
_CHINOOK_DATA_QUERIES = {
    "countries": "SELECT DISTINCT billing_country AS country FROM public.invoice "
                 "WHERE billing_country IS NOT NULL ORDER BY billing_country LIMIT %s",
    "cities": "SELECT DISTINCT billing_city, billing_country FROM public.invoice "
              "WHERE billing_city IS NOT NULL "
              "ORDER BY billing_country, billing_city LIMIT %s",
    "customers": "SELECT customer_id, first_name, last_name, support_rep_id, "
                 "concat_ws(' ', first_name, last_name) AS full_name "
                 "FROM public.customer ORDER BY customer_id LIMIT %s",
    "employees": "SELECT employee_id, first_name, last_name, title, "
                 "concat_ws(' ', first_name, last_name) AS full_name "
                 "FROM public.employee ORDER BY employee_id LIMIT %s",
}

_PERMISSIONS_SQL = """
SELECT requested.object_name,
       n.oid IS NOT NULL AS schema_exists,
       c.oid IS NOT NULL AS object_exists,
       CASE WHEN n.oid IS NULL THEN false
            ELSE has_schema_privilege(n.oid, 'USAGE') END AS schema_usage,
       CASE WHEN c.oid IS NULL THEN false
            ELSE has_table_privilege(c.oid, 'SELECT') END AS table_select,
       COALESCE(c.relrowsecurity, false) AS rls_enabled,
       CASE WHEN c.oid IS NULL OR c.relkind NOT IN ('r', 'p') THEN false
            ELSE row_security_active(c.oid) END AS rls_active
FROM unnest(%s::text[]) AS requested(object_name)
LEFT JOIN pg_namespace n
  ON n.nspname = split_part(requested.object_name, '.', 1)
LEFT JOIN pg_class c
  ON c.relnamespace = n.oid
 AND c.relname = split_part(requested.object_name, '.', 2)
 AND c.relkind IN ('r', 'p', 'v', 'm')
ORDER BY requested.object_name
LIMIT %s
"""

_OBJECTS_SQL = """
SELECT n.nspname AS schema_name, c.relname AS table_name,
       CASE c.relkind WHEN 'v' THEN 'view' WHEN 'm' THEN 'materialized_view'
            ELSE 'table' END AS object_type,
       obj_description(c.oid, 'pg_class') AS description
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname || '.' || c.relname = ANY(%s::text[])
  AND c.relkind IN ('r', 'p', 'v', 'm')
ORDER BY n.nspname, c.relname
LIMIT %s
"""

_COLUMNS_SQL = """
SELECT n.nspname AS schema_name, c.relname AS table_name,
       a.attnum AS ordinal_position, a.attname AS column_name,
       format_type(a.atttypid, a.atttypmod) AS data_type,
       NOT a.attnotnull AS is_nullable,
       a.attgenerated <> '' AS is_generated,
       pg_get_expr(d.adbin, d.adrelid) AS default_or_generation_expression,
       col_description(c.oid, a.attnum) AS description
FROM pg_attribute a
JOIN pg_class c ON c.oid = a.attrelid
JOIN pg_namespace n ON n.oid = c.relnamespace
LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
WHERE n.nspname || '.' || c.relname = ANY(%s::text[])
  AND c.relkind IN ('r', 'p', 'v', 'm')
  AND a.attnum > 0 AND NOT a.attisdropped
ORDER BY n.nspname, c.relname, a.attnum
LIMIT %s
"""

_KEYS_SQL = """
SELECT n.nspname AS schema_name, c.relname AS table_name,
       con.conname AS constraint_name,
       CASE con.contype WHEN 'p' THEN 'primary_key'
            WHEN 'u' THEN 'unique' END AS constraint_type,
       pg_get_constraintdef(con.oid) AS definition
FROM pg_constraint con
JOIN pg_class c ON c.oid = con.conrelid
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE con.contype IN ('p', 'u')
  AND n.nspname || '.' || c.relname = ANY(%s::text[])
ORDER BY n.nspname, c.relname, con.contype, con.conname
LIMIT %s
"""

_FOREIGN_KEYS_SQL = """
SELECT con.conname AS constraint_name,
       ns.nspname AS source_schema, src.relname AS source_table,
       sa.attname AS source_column,
       nt.nspname AS target_schema, tgt.relname AS target_table,
       ta.attname AS target_column, k.ord AS column_position,
       pg_get_constraintdef(con.oid) AS definition
FROM pg_constraint con
JOIN pg_class src ON src.oid = con.conrelid
JOIN pg_namespace ns ON ns.oid = src.relnamespace
JOIN pg_class tgt ON tgt.oid = con.confrelid
JOIN pg_namespace nt ON nt.oid = tgt.relnamespace
CROSS JOIN LATERAL unnest(con.conkey, con.confkey)
    WITH ORDINALITY AS k(source_attnum, target_attnum, ord)
JOIN pg_attribute sa ON sa.attrelid = src.oid AND sa.attnum = k.source_attnum
JOIN pg_attribute ta ON ta.attrelid = tgt.oid AND ta.attnum = k.target_attnum
WHERE con.contype = 'f'
  AND ns.nspname || '.' || src.relname = ANY(%s::text[])
  AND nt.nspname || '.' || tgt.relname = ANY(%s::text[])
ORDER BY ns.nspname, src.relname, con.conname, k.ord
LIMIT %s
"""


def _fetch(cursor, sql, parameters, label):
    cursor.execute(sql, parameters)
    rows = cursor.fetchmany(MAX_METADATA_ROWS + 1)
    if len(rows) > MAX_METADATA_ROWS:
        raise ServiceFault("METADATA_LIMIT_EXCEEDED", f"{label} 超过元数据读取上限 "
                           f"{MAX_METADATA_ROWS} 行，请缩小范围或实现分页读取")
    return json_value(rows)


async def _fetch_async(cursor, sql, parameters, label):
    await cursor.execute(sql, parameters)
    rows = await cursor.fetchmany(MAX_METADATA_ROWS + 1)
    if len(rows) > MAX_METADATA_ROWS:
        raise ServiceFault("METADATA_LIMIT_EXCEEDED", f"{label} 超过元数据读取上限")
    return json_value(rows)


def _summary(objects, columns, keys, foreign_keys):
    types = {(row["schema_name"], row["table_name"]): row["object_type"]
             for row in objects}
    return {
        "tables": sum(kind == "table" for kind in types.values()),
        "table_columns": sum(types[(row["schema_name"], row["table_name"])] == "table"
                             for row in columns),
        "views": sum(kind in {"view", "materialized_view"} for kind in types.values()),
        "view_columns": sum(types[(row["schema_name"], row["table_name"])] != "table"
                            for row in columns),
        "foreign_keys": len({(row["source_schema"], row["source_table"],
                               row["constraint_name"]) for row in foreign_keys}),
        "primary_keys": sum(row["constraint_type"] == "primary_key" for row in keys),
        "unique_constraints": sum(row["constraint_type"] == "unique" for row in keys),
    }


class LiveMetadataReader:
    """每次 read 均访问数据库，失败时不使用本地演示数据替代。"""

    def __init__(self, config, client=None, *, async_client=None):
        self.config = config
        self._declared_objects()
        self.client = client if client is not None else ReadOnlyClient(config)
        self.async_client = async_client

    def _declared_objects(self):
        names = []
        if self.config.get("source_id") != "chinook_db" or self.config.get("dataset") != "chinook":
            raise ServiceFault("METADATA_CONFIGURATION_ERROR", "项目仅支持 Chinook 数据源")
        for key in ("query_table_allowlist", "metadata_tables", "evidence_tables"):
            declared = self.config.get(key)
            if not isinstance(declared, list) or any(
                not isinstance(name, str) or name not in CHINOOK_TABLES
                for name in declared
            ):
                raise ServiceFault("METADATA_CONFIGURATION_ERROR", f"{key} 必须是明确的表白名单")
            if key != "query_table_allowlist" and declared:
                raise ServiceFault("METADATA_CONFIGURATION_ERROR", "Chinook 语义来自受控数据源配置，"
                                   "metadata_tables 和 evidence_tables 应为空")
            names.extend(declared)
        names = sorted(set(names))
        if not names or len(names) > MAX_METADATA_ROWS:
            raise ServiceFault("METADATA_CONFIGURATION_ERROR", "元数据对象范围为空或过大")
        required = set(CHINOOK_TABLES)
        missing = sorted(required - set(names))
        if missing:
            raise ServiceFault("METADATA_CONFIGURATION_ERROR", "缺少增强快照所需元数据表："
                               + "、".join(missing))
        return names

    def _data_queries(self):
        return _CHINOOK_DATA_QUERIES

    def read(self, limits):
        names = self._declared_objects()
        cap = MAX_METADATA_ROWS + 1
        with self.client.transaction(limits) as cursor:
            # pg_get_constraintdef 会按 search_path 省略可见 schema；固定路径使
            # 同步、异步和不同登录默认设置得到相同的完整结构及指纹。
            cursor.execute("SET LOCAL search_path = pg_catalog")
            permissions = _fetch(cursor, _PERMISSIONS_SQL, (names, cap), "对象权限")
            missing = [row["object_name"] for row in permissions
                       if not row["schema_exists"] or not row["object_exists"]]
            if missing:
                raise ServiceFault("METADATA_OBJECT_MISSING", "数据库缺少配置声明的对象："
                                   + "、".join(missing))
            denied = [row["object_name"] for row in permissions
                      if not row["schema_usage"] or not row["table_select"]]
            if denied:
                raise ServiceFault("DATABASE_PERMISSION_DENIED", "缺少 schema USAGE 或表 SELECT 权限："
                                   + "、".join(denied))

            objects = _fetch(cursor, _OBJECTS_SQL, (names, cap), "数据库对象")
            columns = _fetch(cursor, _COLUMNS_SQL, (names, cap), "数据库字段")
            keys = _fetch(cursor, _KEYS_SQL, (names, cap), "主键和唯一约束")
            foreign_keys = _fetch(cursor, _FOREIGN_KEYS_SQL, (names, names, cap), "外键字段")
            data = {}
            for key, sql in self._data_queries().items():
                # SQL 来自受控适配器；只读取解析条件所需实体，不加载事实表全量行。
                data[key] = _fetch(cursor, sql, (cap,), key)

        return self._pack(permissions, objects, columns, keys, foreign_keys, data)

    async def read_async(self, limits):
        """带事务总预算及可取消驱动的实时入口，兼容 Windows 事件循环。"""
        if __package__:
            from .readonly_connection import AsyncReadOnlyClient, run_database
        else:
            from readonly_connection import AsyncReadOnlyClient, run_database

        async def read():
            client = self.async_client if self.async_client is not None else AsyncReadOnlyClient(self.config)
            async with client.transaction(limits) as cursor:
                return await self.aread_in_transaction(cursor)

        return await run_database(read)

    async def aread_in_transaction(self, cursor):
        """使用调用方的同一只读事务，刷新物理结构、权限和业务字典。"""
        names, cap = self._declared_objects(), MAX_METADATA_ROWS + 1
        await cursor.execute("SET LOCAL search_path = pg_catalog")
        permissions = await _fetch_async(cursor, _PERMISSIONS_SQL, (names, cap), "对象权限")
        if (len(permissions) != len(names)
                or {row["object_name"] for row in permissions} != set(names)):
            raise ServiceFault("METADATA_ERROR", "实时对象权限结果不完整")
        if any(not row["schema_exists"] or not row["object_exists"] for row in permissions):
            raise ServiceFault("METADATA_OBJECT_MISSING", "数据库缺少声明的查询或元数据对象")
        if any(not row["schema_usage"] or not row["table_select"] for row in permissions):
            raise ServiceFault("DATABASE_PERMISSION_DENIED", "缺少 schema USAGE 或表 SELECT 权限")
        objects = await _fetch_async(cursor, _OBJECTS_SQL, (names, cap), "数据库对象")
        columns = await _fetch_async(cursor, _COLUMNS_SQL, (names, cap), "数据库字段")
        keys = await _fetch_async(cursor, _KEYS_SQL, (names, cap), "主键和唯一约束")
        foreign_keys = await _fetch_async(cursor, _FOREIGN_KEYS_SQL, (names, names, cap), "外键字段")
        data = {}
        for key, sql in self._data_queries().items():
            data[key] = await _fetch_async(cursor, sql, (cap,), key)
        return self._pack(permissions, objects, columns, keys, foreign_keys, data)

    def _pack(self, permissions, objects, columns, keys, foreign_keys, data):
        captured_at = datetime.now(timezone.utc).isoformat()
        snapshot = {
            "project_ref": self.config.get("project_ref"),
            "read_date": captured_at[:10],
            "captured_at": captured_at,
            "reader_role": self.config.get("reader_role"),
            "summary": _summary(objects, columns, keys, foreign_keys),
            "objects": objects,
            "columns": columns,
            "keys": keys,
            "foreign_keys": foreign_keys,
            "permissions": permissions,
            "metadata_mode": "live",
            "permissions_verified": True,
        }
        semantic_source = "source_configuration"
        return {"snapshot": snapshot, "data": data, "mode": "live",
                "permissions_verified": True, "captured_at": captured_at,
                "semantic_source": semantic_source}


class FileMetadataReader:
    """显式离线开发入口；不把文件读取标记成实时数据库校验。"""

    def __init__(self, base_dir, config=None):
        self.base_dir = Path(base_dir)
        if config is not None and not isinstance(config, dict):
            raise ServiceFault("METADATA_CONFIGURATION_ERROR", "离线读取配置必须为字典")
        self.config = config if config is not None else json.loads((self.base_dir / "source_config.json").read_text(encoding="utf-8-sig"))
        if self.config.get("source_id") != "chinook_db" or self.config.get("dataset") != "chinook":
            raise ServiceFault("METADATA_CONFIGURATION_ERROR", "项目仅支持 Chinook 数据源")

    def read(self, limits):
        del limits  # 保持与实时读取器一致的接口，不执行数据库语句。
        snapshot_name = "chinook_schema_snapshot.json"
        data_name = "chinook_metadata.json"
        try:
            snapshot = json.loads((self.base_dir / "generated" / snapshot_name)
                                  .read_text(encoding="utf-8-sig"))
            source_data = json.loads((self.base_dir / "generated" / data_name)
                                     .read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError) as error:
            raise ServiceFault("METADATA_FILE_ERROR", "本地结构快照或业务元数据文件读取失败") from error
        try:
            data = {key: source_data[key] for key in
                    _CHINOOK_DATA_QUERIES}
            batches = {**data, **{key: snapshot[key] for key in
                                  ("objects", "columns", "keys", "foreign_keys")}}
            if any(not isinstance(rows, list) for rows in batches.values()):
                raise TypeError("metadata batches must be arrays")
        except (KeyError, TypeError) as error:
            raise ServiceFault("METADATA_FILE_ERROR", "本地元数据缺少所需数组") from error
        for label, rows in batches.items():
            if len(rows) > MAX_METADATA_ROWS:
                raise ServiceFault("METADATA_LIMIT_EXCEEDED", f"{label} 超过元数据读取上限 "
                                   f"{MAX_METADATA_ROWS} 行")
        snapshot["metadata_mode"] = "file"
        snapshot["permissions_verified"] = False
        snapshot["captured_at"] = None
        return {"snapshot": snapshot, "data": data, "mode": "file",
                "permissions_verified": False, "captured_at": None,
                "semantic_source": "source_configuration"}
