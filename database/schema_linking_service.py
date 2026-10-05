"""Chinook 指标请求映射接口。"""
from __future__ import annotations

if __package__:
    from .chinook_query import ChinookSchemaLinkingService
else:
    from chinook_query import ChinookSchemaLinkingService


class SchemaLinkingService(ChinookSchemaLinkingService):
    """统一接口仅接受 Chinook 数据源，返回 ready/needs_clarification/error。"""
