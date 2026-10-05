"""导入模拟 Markdown 文档，保留可核验的原文和引用位置。"""

import argparse
import hashlib
import json
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

PROJECT_ROOT = Path(__file__).resolve().parents[3]
COLLECTION_ID = "legacy_sales_demo"
_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
_VERSION = re.compile(r"版本[：:]\s*([^；;\r\n]+)")
_VALID_FROM = re.compile(r"适用时间[：:]\s*([^；;\r\n]+)")
_SOURCE = re.compile(r"来源[：:]\s*([^；;\r\n]+)")


def keywords(text: str) -> list[str]:
    """中文双字词加英文单词；只用于小型演示集合的关键词索引。"""
    words = set(re.findall(r"[a-z0-9_]+", text.casefold()))
    for run in re.findall(r"[\u4e00-\u9fff]+", text):
        if len(run) == 1:
            words.add(run)
        else:
            words.update(run[i : i + 2] for i in range(len(run) - 1))
    return sorted(words)


def _metadata(pattern: re.Pattern[str], text: str, name: str) -> str:
    match = pattern.search(text)
    if match is None:
        raise ValueError(f"文档缺少{name}，请先补充文档头部信息。")
    return match.group(1).strip().rstrip("。")


def _chunks(text: str, *, document: dict, max_chars: int) -> list[dict]:
    lines = text.splitlines(keepends=True)
    chunks: list[dict] = []
    heading_path: list[tuple[int, str]] = []
    start = 0
    length = 0
    section = document["title"]

    def emit(end: int) -> None:
        excerpt = "".join(lines[start:end]).strip()
        if not excerpt:
            return
        chunks.append(
            {
                "evidence_id": (
                    f"{document['doc_id']}:{document['file_sha256'][:12]}:"
                    f"L{start + 1}-{end}"
                ),
                "doc_id": document["doc_id"],
                "title": document["title"],
                "version": document["version"],
                "collection_id": COLLECTION_ID,
                "data_domain": COLLECTION_ID,
                "source": document["source"],
                "applicable_time": document["applicable_time"],
                "source_path": document["source_path"],
                "file_sha256": document["file_sha256"],
                "section": section,
                "page": None,
                "line_start": start + 1,
                "line_end": end,
                "text": excerpt,
            }
        )

    for i, line in enumerate(lines):
        if len(line) > max_chars:
            raise ValueError(f"{document['doc_id']} 第 {i + 1} 行过长，请先分行。")
        heading = _HEADING.match(line.strip())
        if heading:
            emit(i)
            start, length = i, 0
            level, title = len(heading.group(1)), heading.group(2)
            heading_path = [(n, t) for n, t in heading_path if n < level]
            heading_path.append((level, title))
            section = " / ".join(t for _, t in heading_path)
        elif length + len(line) > max_chars:
            emit(i)
            start, length = i, 0
        length += len(line)
    emit(len(lines))
    return chunks


def prepare_collection(
    source_dir: Path, *, max_chars: int = 1200
) -> tuple[dict, dict[str, bytes]]:
    if not 100 <= max_chars <= 4000:
        raise ValueError("片段上限必须在 100 至 4000 字符之间。")
    source_dir = source_dir.resolve(strict=True)
    if not source_dir.is_dir():
        raise ValueError("导入源必须是文件夹。")
    files = sorted(source_dir.glob("*.md"))
    if not files:
        raise ValueError("目录中没有 Markdown 文档。")
    if len(files) > 100:
        raise ValueError("本阶段最多导入 100 份 Markdown 文档。")
    documents = []
    chunks = []
    originals = {}
    for path in files:
        if path.stat().st_size > 2 * 1024 * 1024:
            raise ValueError(f"{path.name} 超过本阶段单文件 2MB 限制。")
        raw = path.read_bytes()
        text = raw.decode("utf-8-sig")
        headings = [
            m.group(2)
            for line in text.splitlines()
            if (m := _HEADING.match(line.strip()))
        ]
        digest = hashlib.sha256(raw).hexdigest()
        document = {
            "doc_id": path.stem,
            "title": headings[0] if headings else path.stem,
            "version": _metadata(_VERSION, text, "版本"),
            "source": _metadata(_SOURCE, text, "来源"),
            "applicable_time": _metadata(_VALID_FROM, text, "适用时间"),
            "source_path": str(path.resolve()),
            "file_sha256": digest,
            "snapshot_file": f"originals/{digest}.md",
        }
        prepared = _chunks(text, document=document, max_chars=max_chars)
        if not prepared:
            raise ValueError(f"{path.name} 没有可导入内容。")
        document["chunk_count"] = len(prepared)
        documents.append(document)
        chunks.extend(prepared)
        originals[digest] = raw
    inverted: dict[str, list[str]] = {}
    for chunk in chunks:
        for term in keywords(chunk["text"]):
            inverted.setdefault(term, []).append(chunk["evidence_id"])
    return (
        {
            "schema_version": 1,
            "collection_id": COLLECTION_ID,
            "data_domain": COLLECTION_ID,
            "description": "旧模拟业务演示文档；不作为 Chinook SQL 口径或实体依据。",
            "prepared_at": datetime.now(UTC).isoformat(),
            "documents": documents,
            "chunks": chunks,
            "keyword_index": inverted,
        },
        originals,
    )


def import_collection(source_dir: Path, target_dir: Path) -> dict:
    """先完整校验，再保存原文快照并原子替换集合索引。"""
    collection, originals = prepare_collection(source_dir)
    target_dir = target_dir.resolve()
    source_root = source_dir.resolve()
    if target_dir == source_root or source_root.is_relative_to(target_dir):
        raise ValueError("导入产物必须与源文档目录分开。")
    snapshot_dir = target_dir / "originals"
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    for digest, raw in originals.items():
        snapshot = snapshot_dir / f"{digest}.md"
        if snapshot.exists():
            if hashlib.sha256(snapshot.read_bytes()).hexdigest() != digest:
                raise ValueError("已保存的原文快照损坏，请检查本机知识库。")
        else:
            snapshot.write_bytes(raw)
    index = target_dir / "index.json"
    temporary = target_dir / f".index-{uuid4().hex}.tmp"
    try:
        temporary.write_text(
            json.dumps(collection, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.replace(index)
    finally:
        temporary.unlink(missing_ok=True)
    return summary(collection, index=index)


def summary(collection: dict, *, index: Path | None = None) -> dict:
    return {
        "status": "ok",
        "collection_id": collection["collection_id"],
        "document_count": len(collection["documents"]),
        "chunk_count": len(collection["chunks"]),
        "keyword_count": len(collection["keyword_index"]),
        "index_path": str(index) if index else None,
        "documents": [
            {
                "doc_id": x["doc_id"],
                "title": x["title"],
                "version": x["version"],
                "chunk_count": x["chunk_count"],
            }
            for x in collection["documents"]
        ],
        "note": collection["description"],
    }


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()
    try:
        source = PROJECT_ROOT / "database" / "documents"
        target = PROJECT_ROOT / ".local" / "knowledge" / COLLECTION_ID
        if args.check_only:
            result = summary(prepare_collection(source)[0])
            result["mode"] = "check_only"
        else:
            result = import_collection(source, target)
            result["mode"] = "import"
    except (OSError, ValueError) as error:
        result = {"status": "error", "message": str(error)}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
