"""仅增强 Chinook 结构；默认读取实时只读数据库。"""
import argparse
import hashlib
import json
import sys
from pathlib import Path

if __package__:
    from .chinook_snapshot import enrich_snapshot as enrich_chinook
    from .sql_common import ServiceFault
else:
    from chinook_snapshot import enrich_snapshot as enrich_chinook
    from sql_common import ServiceFault

ROOT = Path(__file__).resolve().parent


def load(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def enrich_snapshot(snapshot, data, config):
    if not isinstance(config, dict) or config.get("source_id") != "chinook_db" or config.get("dataset") != "chinook":
        raise ServiceFault("METADATA_CONFIGURATION_ERROR", "项目仅支持 Chinook 数据源")
    return enrich_chinook(snapshot, data, config)


def main(argv=None):
    parser = argparse.ArgumentParser(description="输出增强结构快照；当前 Chinook 默认读取实时元数据。")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--live", action="store_true", help="读取当前数据源的实时结构、权限和实体名单")
    mode.add_argument("--file", action="store_true", help="使用当前数据源的 generated 离线文件")
    args = parser.parse_args(argv)
    snapshot_path = ROOT / "generated/chinook_schema_snapshot.json"
    data_path = ROOT / "generated/chinook_metadata.json"
    config_path = ROOT / "source_config.json"
    if __package__:
        from .metadata_reader import FileMetadataReader, LiveMetadataReader
        from .sql_common import ServiceFault
    else:
        from metadata_reader import FileMetadataReader, LiveMetadataReader
        from sql_common import ServiceFault

    try:
        config = load(config_path)
        live = not args.file
        if live:
            payload = LiveMetadataReader(config).read({"db_timeout_ms": config["statement_timeout_ms"]})
            result = enrich_snapshot(payload["snapshot"], payload["data"], config)
            semantic_source = payload.get("semantic_source", "database")
            result["snapshot_metadata"].update({
                "schema_captured_at": payload["captured_at"], "schema_capture_precision": "timestamp",
                "semantic_source": semantic_source,
                "semantic_database_consistency_verified": semantic_source == "database",
            })
            result["access_scope"].update({
                "live_permissions_verified": payload["permissions_verified"],
                "permissions_checked_at": payload["captured_at"],
            })
            hashes = {"source_config": sha256(config_path)}
        else:
            payload = FileMetadataReader(ROOT, config).read({})
            result = enrich_snapshot(payload["snapshot"], payload["data"], config)
            if config.get("dataset") == "chinook":
                snapshot_path = ROOT / "generated/chinook_schema_snapshot.json"
                data_path = ROOT / "generated/chinook_metadata.json"
            hashes = {"schema_snapshot": sha256(snapshot_path), "semantic_data": sha256(data_path),
                      "source_config": sha256(config_path)}
        result["snapshot_metadata"]["input_sha256"] = hashes
    except ServiceFault as error:
        result = {"status": "error", "error_code": error.code, "error_message": str(error)}
    except (OSError, ValueError, KeyError, TypeError):
        result = {"status": "error", "error_code": "METADATA_CONFIGURATION_ERROR",
                  "error_message": "结构快照或数据源配置读取失败，请核对当前配置和模式"}

    # 只打印，不写入或覆盖任何文件。
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if result.get("status") == "error" else 0


if __name__ == "__main__":
    raise SystemExit(main())
