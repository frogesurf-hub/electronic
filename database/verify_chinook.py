"""真实 Chinook 只读验收：独立金额 oracle、映射分流与非破坏权限检查。

仅显式运行时连接本地数据库，不修改业务行；允许 source_config 尚未切换时
用 Chinook 配置覆盖进行验收。保存的报告不包含连接密码或加密密码。
"""
from __future__ import annotations

import copy
import asyncio
import json
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

if not __package__:
    # 直接运行脚本时也使用同一套包导入，避免异常类出现两份身份。
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    __package__ = "database"

if __package__:
    from .chinook_query import ChinookSchemaLinkingService, ChinookSQLGenerator
    from .enrich_schema_snapshot import enrich_snapshot
    from .metadata_reader import LiveMetadataReader
    from .artifact_io import _save_json
    from .setup_chinook import DATABASE, HOST, LOCAL, PORT, READER, ROOT, _profile, chinook_config, inspect_database, prepare_script
    from .sql_common import ReadOnlyClient, ServiceFault
    from .sql_service import SQLService
else:
    from chinook_query import ChinookSchemaLinkingService, ChinookSQLGenerator
    from enrich_schema_snapshot import enrich_snapshot
    from metadata_reader import LiveMetadataReader
    from artifact_io import _save_json
    from setup_chinook import DATABASE, HOST, LOCAL, PORT, READER, ROOT, _profile, chinook_config, inspect_database, prepare_script
    from sql_common import ReadOnlyClient, ServiceFault
    from sql_service import SQLService

LIMITS = {"max_rows": 100, "db_timeout_ms": 5000}


class ChinookVerificationError(Exception):
    pass


def query(filters=None):
    return {"source_id": "chinook_db", "metric_id": "sales_amount",
            "metric_definition_id": "chinook_sales_amount_v1",
            "time_range": {"start": "2024-01-01", "end_exclusive": "2025-01-01"},
            "filters": [] if filters is None else filters}


def _ensure(condition, message):
    if not condition:
        raise ChinookVerificationError(message)


def _clarification(service, label, request, code):
    result = service.query(request, {"request_id": "chinook-verify-" + label})
    _ensure(result["status"] == "needs_clarification", "应当澄清的查询没有进入澄清分支")
    _ensure(result["route_to"] == "clarification" and not result["execution_attempted"]
            and result["sql"] is None, "澄清分支错误地生成或执行了业务 SQL")
    _ensure(code in {issue["code"] for issue in result["issues"]}, "澄清理由与预期不一致")
    return {"case": label, "status": result["status"], "issue_codes": [issue["code"] for issue in result["issues"]],
            "execution_attempted": False}


def verify(config=None):
    import psycopg
    from psycopg.rows import dict_row
    current = json.loads((ROOT / "source_config.json").read_text(encoding="utf-8-sig"))
    config = chinook_config(current) if config is None else dict(config)
    config["_base_dir"] = str(ROOT)
    _, expected = prepare_script()
    administrator = _profile(LOCAL / "target_admin_connection.json", "postgres", "postgres")
    service = SQLService(ROOT, config=config)
    cases = [
        ("2024_usa", [{"field_id": "country", "op": "eq", "value": "美国"}], "billing_country=%s", ("USA",)),
        ("country_in", [{"field_id": "country", "op": "in", "value": ["美国", "加拿大"]}],
         "billing_country=ANY(%s)", (["USA", "Canada"],)),
        ("customer_eq", [{"field_id": "customer", "op": "eq", "value": 1}], "customer_id=%s", (1,)),
        ("customer_in", [{"field_id": "customer", "op": "in", "value": [1, 2]}], "customer_id=ANY(%s)", ([1, 2],)),
    ]
    business_results = []
    usa_amount = None
    with psycopg.connect(**{**administrator, "dbname": DATABASE}, autocommit=True,
                         connect_timeout=10, row_factory=dict_row) as admin:
        facts = inspect_database(admin, expected)
        with admin.transaction():
            admin.execute("SET TRANSACTION READ ONLY")
            with admin.cursor() as cursor:
                for label, filters, condition, values in cases:
                    # Oracle 直接使用源字段，没有调用映射或 SQL 生成器。
                    cursor.execute("SELECT COALESCE(SUM(total),0) AS amount FROM public.invoice "
                                   "WHERE invoice_date >= %s AND invoice_date < %s AND " + condition,
                                   (datetime(2024, 1, 1), datetime(2025, 1, 1), *values))
                    amount = cursor.fetchone()["amount"]
                    if label == "2024_usa":
                        usa_amount = amount
                    result = service.query(query(filters), {"request_id": "chinook-verify-" + label})
                    _ensure(result["status"] == "ok" and result["execution_attempted"], "Chinook 真实查询未执行成功")
                    _ensure(result["metadata"]["mode"] == "live", "真实验收使用了离线元数据")
                    _ensure(Decimal(result["rows"][0]["sales_amount"]) == amount, "SQLService 金额与独立 oracle 不一致")
                    _ensure("payment_status" not in result["sql"], "Chinook 错误使用了旧销售库支付条件")
                    _ensure(all(column.get("unit") != "元" for column in result["columns"]), "Chinook 金额错误假定为人民币")
                    business_results.append({"case": label, "actual": format(amount, ".2f"), "matches_oracle": True,
                                             "execution_attempted": True})
    from agent_core.sql.example import OfflineExampleProvider
    from agent_core.sql.generation_service import SQLGenerationService
    asynchronous = asyncio.run(SQLGenerationService(
        OfflineExampleProvider(), base_dir=ROOT, config=config,
    ).query(query([{"field_id": "country", "op": "eq", "value": "美国"}]),
            {"request_id": "chinook-verify-async-fixed-provider"}))
    _ensure(asynchronous["status"] == "ok" and asynchronous["execution_attempted"],
            "固定 Provider 的异步真实查询未执行成功")
    _ensure(asynchronous["validation_status"] == "passed" and
            Decimal(asynchronous["rows"][0]["sales_amount"]) == usa_amount,
            "异步查询没有通过 SQL 检验或与独立 oracle 不一致")
    asynchronous_report = {"status": "ok", "validation_status": "passed", "execution_attempted": True,
                           "rows": asynchronous["rows"], "matches_oracle": True,
                           "provider_mode": "fixed_example_only", "no_model_api_called": True}
    clarification_checks = []
    missing_time = query()
    missing_time.pop("time_range")
    clarification_checks.append(_clarification(service, "missing_time", missing_time, "MISSING_TIME_RANGE"))
    for value in (None, "", "  "):
        clarification_checks.append(_clarification(service, "empty_country", query([
            {"field_id": "country", "op": "eq", "value": value}]), "MISSING_FILTER_VALUE"))
    clarification_checks.append(_clarification(service, "unknown_country", query([
        {"field_id": "country", "op": "eq", "value": "不存在的国家"}]), "UNKNOWN_ENTITY"))
    unknown_city = service.query(query([
        {"field_id": "city", "op": "eq", "value": "不存在的城市"}]),
        {"request_id": "chinook-verify-unknown-city"})
    _ensure(unknown_city["status"] == "needs_clarification" and not unknown_city["execution_attempted"]
            and unknown_city["sql"] is None, "未知城市没有进入澄清分支或错误执行了业务查询")
    clarification_checks.append({"case": "unknown_city", "status": "needs_clarification",
                                 "issue_codes": [row["code"] for row in unknown_city["issues"]],
                                 "execution_attempted": False})
    offset = query()
    offset["time_range"]["start"] = "2024-01-01T00:00:00+08:00"
    clarification_checks.append(_clarification(service, "offset_time", offset, "AMBIGUOUS_TIME_SEMANTICS"))
    injection = "USA'; DROP TABLE public.invoice; --"
    clarification_checks.append(_clarification(service, "injection_country", query([
        {"field_id": "country", "op": "eq", "value": injection}]), "UNKNOWN_ENTITY"))

    payload = LiveMetadataReader(config).read(LIMITS)
    snapshot = enrich_snapshot(payload["snapshot"], payload["data"], config)
    original_customer = payload["data"]["customers"][0]
    duplicate = copy.deepcopy(original_customer)
    duplicate["customer_id"] = max(row["customer_id"] for row in payload["data"]["customers"]) + 1
    fixture = copy.deepcopy(payload)
    fixture["data"]["customers"].append(duplicate)

    class FixtureReader:
        def read(self, limits):
            return copy.deepcopy(fixture)

    def forbidden_executor(*args):
        raise ChinookVerificationError("同名客户模拟分支错误执行了业务查询")

    fixture_service = SQLService(ROOT, config=config, metadata_reader=FixtureReader(), executor=forbidden_executor)
    same_name = _clarification(fixture_service, "same_name_fixture", query([
        {"field_id": "customer", "op": "eq", "value": original_customer["full_name"]}]), "AMBIGUOUS_ENTITY")
    same_name["fixture_only"] = True
    clarification_checks.append(same_name)

    readonly_blocked = False
    try:
        with ReadOnlyClient(config).transaction(LIMITS) as cursor:
            cursor.execute("UPDATE public.invoice SET total=total WHERE false")
    except ServiceFault as error:
        sqlstate = getattr(error.__cause__, "sqlstate", None)
        _ensure(sqlstate in {"25006", "42501"}, "只读测试失败的原因不是只读或权限约束")
        readonly_blocked = True
    _ensure(readonly_blocked, "Chinook 查询角色未阻止 UPDATE")

    mapped = ChinookSchemaLinkingService().match(query([
        {"field_id": "country", "op": "eq", "value": "美国"}]), snapshot, entities=payload["data"])
    request = mapped["sql_generation_request"]
    request["filters"][0]["value"] = injection
    sql, parameters = ChinookSQLGenerator().generate(request, LIMITS)
    _ensure(injection not in sql and injection in parameters.values(), "筛选值没有使用绑定参数")
    with ReadOnlyClient(config).transaction(LIMITS) as cursor:
        cursor.execute(sql, parameters)
        _ensure(cursor.fetchone()["sales_amount"] == 0, "参数化注入测试返回了意外匹配")

    report = {"verified_at": datetime.now(timezone.utc).isoformat(), "dataset": "chinook",
              "source_id": "chinook_db", "database": DATABASE, "host": HOST, "port": PORT,
              "reader_role": READER, "facts": facts, "real_business_queries": business_results,
              "async_fixed_provider_query": asynchronous_report, "no_model_api_called": True,
              "clarification_checks": clarification_checks, "readonly_update_blocked": readonly_blocked,
              "parameterized_injection_query_returned_zero": True, "currency_assumed": None,
              "source_config_switched": current.get("dataset") == "chinook"}
    _save_json(ROOT / "generated" / "chinook_verification.json", report)
    return report


def main():
    source_path = str(ROOT.parent / "src")
    if source_path not in sys.path:
        sys.path.insert(0, source_path)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    try:
        print(json.dumps(verify(), ensure_ascii=False, indent=2))
        return 0
    except ChinookVerificationError as error:
        message = str(error)
    except Exception:
        message = "Chinook 真实验收未通过，请检查数据库连接、域模块和权限；未输出凭据或异常回溯。"
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")
    print(json.dumps({"status": "error", "error_code": "CHINOOK_VERIFICATION_FAILED", "message": message}, ensure_ascii=False), file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
