"""Check the imported SQL pipeline without a database or model API call."""

import asyncio
import json
import sys
from pathlib import Path

# The teammate's database package lives beside src in this source checkout.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from agent_core.sql.detail_example import DetailDemoProvider  # noqa: E402
from agent_core.sql.example import OfflineExampleProvider  # noqa: E402
from agent_core.sql.generation_service import SQLGenerationService  # noqa: E402
from database.metadata_reader import FileMetadataReader  # noqa: E402


async def main() -> int:
    database_dir = PROJECT_ROOT / "database"
    cases = (
        (
            "invoice_amount_2024_usa",
            OfflineExampleProvider(),
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
        ),
        (
            "track_details",
            DetailDemoProvider(),
            {
                "task": "table_detail",
                "source_id": "chinook_db",
                "table": "public.track",
                "columns": ["track_id", "name", "unit_price"],
                "filters": [],
                "page": {"size": 10, "offset": 0},
            },
        ),
    )
    examples = []
    for name, provider, query in cases:
        service = SQLGenerationService(
            provider,
            base_dir=database_dir,
            metadata_reader=FileMetadataReader(database_dir),
        )
        result = await service.generate_and_validate(query, {"request_id": name})
        passed = (
            result.get("status") == "validated"
            and result.get("validation_status") == "passed"
            and result.get("execution_attempted") is False
        )
        examples.append(
            {
                "case": name,
                "passed": passed,
                "status": result.get("status"),
                "validation_status": result.get("validation_status"),
                "execution_attempted": result.get("execution_attempted"),
                "sql": (result.get("candidate") or {}).get("sql"),
                "parameters": result.get("parameters"),
                "error_code": result.get("error_code"),
                "error_message": result.get("error_message"),
            }
        )

    passed = all(example["passed"] for example in examples)
    report = {
        "status": "passed" if passed else "failed",
        "provider_mode": "fixed_examples",
        "model_api_called": False,
        "model_quality_measured": False,
        "database_connected": False,
        "examples": examples,
    }
    report_text = json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False)
    report_path = PROJECT_ROOT / ".cache" / "sql-integration-check.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(report_text + "\n", encoding="utf-8")
    print(report_text)
    return 0 if passed else 1


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(asyncio.run(main()))
