"""选择本机问答集合；配置只保存集合编号，不能指向任意外部目录。"""

import json
import re
from pathlib import Path
from uuid import uuid4

from agent_core.retrieval.documents import COLLECTION_ID, PROJECT_ROOT

KNOWLEDGE_ROOT = PROJECT_ROOT / ".local" / "knowledge"


def collection_folder(root: Path, collection_id: str) -> Path:
    if not isinstance(collection_id, str) or not re.fullmatch(
        r"[a-z][a-z0-9_]{0,63}", collection_id
    ):
        raise ValueError("问答集合编号无效。")
    root = root.resolve()
    folder = (root / collection_id).resolve()
    if not folder.is_relative_to(root) or folder == root:
        raise ValueError("问答集合必须位于本机知识库目录内。")
    return folder


def active_collection_id(root: Path = KNOWLEDGE_ROOT) -> str:
    config = root / "active.json"
    if not config.exists():
        return COLLECTION_ID
    if config.stat().st_size > 4096:
        raise ValueError("问答集合配置过大。")
    data = json.loads(config.read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict) or data.get("schema_version") != 1:
        raise ValueError("问答集合配置无效。")
    collection_id = data["collection_id"]
    if not isinstance(collection_id, str):
        raise ValueError("问答集合编号必须是文字。")
    collection_folder(root, collection_id)
    return collection_id


def activate_collection(collection_id: str, root: Path = KNOWLEDGE_ROOT) -> None:
    folder = collection_folder(root, collection_id)
    if collection_id == COLLECTION_ID:
        from agent_core.retrieval.search import DocumentSearch

        DocumentSearch(folder)._load()
    else:
        from agent_core.retrieval.preparation import validate_collection

        collection = validate_collection(folder)
        if collection["collection_id"] != collection_id:
            raise ValueError("集合编号与目录不一致。")
        if not any(d["ready_for_retrieval"] for d in collection["documents"]):
            raise ValueError("集合尚无复核可用的资料，未切换问答集合。")
    root.mkdir(parents=True, exist_ok=True)
    temporary = root / f".active-{uuid4().hex}.tmp"
    try:
        temporary.write_text(
            json.dumps({"schema_version": 1, "collection_id": collection_id}) + "\n",
            encoding="utf-8",
        )
        temporary.replace(root / "active.json")
    finally:
        temporary.unlink(missing_ok=True)
