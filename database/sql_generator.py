"""Chinook 发票汇总的受控参数及参考 SQL 构造。"""
from __future__ import annotations

if __package__:
    from .chinook_query import ChinookSQLGenerator
else:
    from chinook_query import ChinookSQLGenerator


class SQLGenerator(ChinookSQLGenerator):
    """保持 generate(request, limits) 接口，只生成 Chinook 查询。"""
