"""Generate SQL with DeepSeek, validate it, and query the local Chinook database."""

import argparse
import asyncio
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from agent_core.config import get_settings  # noqa: E402
from agent_core.main import _create_provider  # noqa: E402
from agent_core.providers import LLMProviderError  # noqa: E402
from agent_core.sql.generation_service import SQLGenerationService  # noqa: E402


async def run() -> dict:
    settings = get_settings()
    provider = _create_provider(settings)
    try:
        service = SQLGenerationService(provider, base_dir=PROJECT_ROOT / "database")
        result = await service.query(
            {
                "source_id": "chinook_db",
                "metric_id": "sales_amount",
                "metric_definition_id": "chinook_sales_amount_v1",
                "time_range": {
                    "start": "2024-01-01",
                    "end_exclusive": "2025-01-01",
                },
                "filters": [{"field_id": "country", "op": "eq", "value": "美国"}],
            },
            {"request_id": "deepseek-live-sql-check"},
        )
        result.update(
            provider_mode="real_deepseek",
            model=settings.deepseek_model,
            input_mode="structured_query",
            expected_sales_amount="127.98",
            reference_match=(
                result.get("status") == "ok"
                and result.get("rows") == [{"sales_amount": "127.98"}]
            ),
        )
        return dict(result)
    finally:
        await provider.aclose()


def main() -> int:
    argparse.ArgumentParser(description=__doc__).parse_args()
    try:
        result = asyncio.run(run())
    except LLMProviderError as error:
        result = {
            "status": "error",
            "error_code": type(error).__name__,
            "error_message": str(error),
        }
    except KeyboardInterrupt:
        result = {"status": "cancelled"}
    except Exception as error:
        result = {
            "status": "error",
            "error_code": type(error).__name__,
            "error_message": "Check model configuration and local database readiness.",
        }
    report_text = json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False)
    report_path = PROJECT_ROOT / ".cache" / "sql-model-check.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(report_text + "\n", encoding="utf-8")
    print(report_text)
    return 0 if result.get("status") == "ok" and result.get("reference_match") else 1


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
