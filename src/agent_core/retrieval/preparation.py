"""通用文档准备：显式清单、来源快照、可定位片段和本地关键词索引。"""

import argparse
import hashlib
import json
import re
import subprocess
import sys
from datetime import UTC, date, datetime
from pathlib import Path
from uuid import uuid4

from agent_core.retrieval.documents import PROJECT_ROOT, keywords
from agent_core.retrieval.parsing import SUPPORTED_TYPES, parse_file

MAX_INDEX_BYTES = 8 * 1024 * 1024
_SLUG = re.compile(r"[a-z][a-z0-9_]{0,63}")


def encoded(value: dict) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    ).encode("utf-8")


def sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def load_manifest(path: Path) -> dict:
    if path.stat().st_size > 1024 * 1024:
        raise ValueError("导入清单超过 1MB。")
    manifest = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(manifest, dict):
        raise ValueError("导入清单必须是 JSON 对象。")
    if manifest.get("schema_version") != 1:
        raise ValueError("导入清单 schema_version 必须为 1。")
    for field in ("collection_id", "data_domain"):
        if not isinstance(manifest.get(field), str) or not _SLUG.fullmatch(
            manifest[field]
        ):
            raise ValueError(f"{field} 必须为小写英文、数字或下划线，且以字母开头。")
    if manifest["collection_id"] == "legacy_sales_demo":
        raise ValueError("通用导入需使用独立集合，保留已验收的 legacy_sales_demo。")
    documents = manifest.get("documents")
    if not isinstance(documents, list) or not 1 <= len(documents) <= 50:
        raise ValueError("清单需列出 1 至 50 份文件。")
    seen = set()
    for document in documents:
        if not isinstance(document, dict):
            raise ValueError("documents 项必须是对象。")
        for field in ("file", "doc_id", "title", "version", "source"):
            if not isinstance(document.get(field), str) or not document[field].strip():
                raise ValueError(f"每份文档必须明确填写 {field}，不能猜测版本或来源。")
            if len(document[field]) > 300:
                raise ValueError(f"{field} 过长。")
        identity = (document["doc_id"].casefold(), document["version"].casefold())
        if identity in seen:
            raise ValueError("同一文档编号和版本不能重复。")
        seen.add(identity)
        dates = []
        for field in ("valid_from", "valid_until"):
            value = document.get(field)
            dates.append(date.fromisoformat(value) if value is not None else None)
        if dates[0] is not None and dates[1] is not None and dates[0] >= dates[1]:
            raise ValueError("适用日期的开始必须早于结束（结束不包含）。")
        review = document.get("ocr_review_sha256")
        if review is not None and (
            not isinstance(review, str) or not re.fullmatch(r"[a-f0-9]{64}", review)
        ):
            raise ValueError("复核标记需使用报告中的 parsed_sha256，未知时留 null。")
    return manifest


def _slices(text: str, maximum: int):
    start = 0
    while start < len(text):
        end = min(start + maximum, len(text))
        if end < len(text):
            split = max(text.rfind("\n", start, end), text.rfind("。", start, end))
            if split >= start + maximum // 2:
                end = split + 1
        segment = text[start:end]
        stripped = segment.strip()
        if stripped:
            offset = start + segment.index(stripped)
            yield offset, offset + len(stripped), stripped
        start = end


def prepare(
    manifest_path: Path,
    *,
    max_chars: int = 1200,
    language: str = "zh-Hans-CN",
    ocr_mode: str = "auto",
) -> tuple[dict, dict[str, bytes]]:
    if not 100 <= max_chars <= 4000:
        raise ValueError("片段长度必须为 100 至 4000 字符。")
    if ocr_mode not in {"auto", "always", "never"}:
        raise ValueError("OCR 模式必须为 auto、always 或 never。")
    manifest_path = manifest_path.resolve(strict=True)
    manifest = load_manifest(manifest_path)
    root = manifest_path.parent
    documents, chunks, blobs = [], [], {}
    file_paths = set()
    total_bytes = 0
    for entry in manifest["documents"]:
        relative = Path(entry["file"])
        if relative.is_absolute():
            raise ValueError("清单 file 请使用清单目录内的相对路径。")
        path = (root / relative).resolve(strict=True)
        if not path.is_relative_to(root) or not path.is_file():
            raise ValueError("清单文件必须位于清单目录内。")
        if path in file_paths:
            raise ValueError("同一文件不能重复列入清单。")
        file_paths.add(path)
        if path.suffix.lower() not in SUPPORTED_TYPES:
            raise ValueError(f"暂不支持 {path.suffix} 文件：{path.name}")
        file_size = path.stat().st_size
        total_bytes += file_size
        if total_bytes > 100 * 1024 * 1024:
            raise ValueError("一批源文件合计最多 100MB，请分批导入。")
        if file_size > 25 * 1024 * 1024:
            raise ValueError(f"{path.name} 超过单文件 25MB 限制。")
        raw = path.read_bytes()
        digest = sha256(raw)
        pages = parse_file(
            path, title=entry["title"], language=language, ocr_mode=ocr_mode
        )
        if sha256(path.read_bytes()) != digest:
            raise ValueError("解析期间源文件发生变化，请重试。")
        if not pages or not any(
            b.get("text", "").strip() for p in pages for b in p["blocks"]
        ):
            raise ValueError(f"{path.name} 没有可读取的文字，未替换已有索引。")
        for page_number, page in enumerate(pages, 1):
            for block_number, block in enumerate(page["blocks"], 1):
                block["block_id"] = f"P{page_number}-B{block_number}"
        corrections = entry.get("corrections", {})
        if not isinstance(corrections, dict):
            raise ValueError("corrections 必须是以块编号为键的对象。")
        by_id = {b["block_id"]: b for p in pages for b in p["blocks"]}
        for block_id, correction in corrections.items():
            block = by_id.get(block_id)
            if block is None or not block.get("method", "").startswith("windows_ocr"):
                raise ValueError("更正必须指向本次解析中的 OCR 块编号。")
            if (
                not isinstance(correction, dict)
                or any(
                    not isinstance(correction.get(f), str) or not correction[f].strip()
                    for f in ("text", "note")
                )
                or len(correction["text"]) > 4000
            ):
                raise ValueError(
                    "OCR 更正需填写 text 和复核依据 note，文字最多 4000 字符。"
                )
            if block["kind"] == "table_row":
                raise ValueError("表格更正需先在原件修正后重新导入，不能只改行文字。")
            block["before_correction_text"] = block["text"]
            block["text"] = correction["text"].strip()
            block["correction_note"] = correction["note"].strip()
        metadata = {
            field: entry.get(field)
            for field in (
                "doc_id",
                "title",
                "version",
                "source",
                "valid_from",
                "valid_until",
            )
        }
        metadata["data_domain"] = manifest["data_domain"]
        parsed = {
            "schema_version": 1,
            "parser_version": 1,
            "source_sha256": digest,
            "format": path.suffix.lower(),
            "document_metadata": metadata,
            "pages": pages,
        }
        parsed_raw = encoded(parsed)
        parsed_digest = sha256(parsed_raw)
        warnings = [
            b["warning"] for p in pages for b in p["blocks"] if b["kind"] == "warning"
        ]
        ocr_pages = [
            p["number"]
            for p in pages
            if any(b.get("method", "").startswith("windows_ocr") for b in p["blocks"])
        ]
        requires_review = bool(ocr_pages or warnings)
        if ocr_pages:
            warnings.append("OCR 文字及扫描表格结构需对照原文复核；识别词坐标已保留。")
        reviewed = entry.get("ocr_review_sha256") == parsed_digest
        document = {
            "doc_id": entry["doc_id"],
            "title": entry["title"],
            "version": entry["version"],
            "source": entry["source"],
            "data_domain": manifest["data_domain"],
            "valid_from": entry.get("valid_from"),
            "valid_until": entry.get("valid_until"),
            "source_path": str(path),
            "source_sha256": digest,
            "parsed_sha256": parsed_digest,
            "format": path.suffix.lower(),
            "original_file": f"originals/{digest}{path.suffix.lower()}",
            "parsed_file": f"parsed/{parsed_digest}.json",
            "page_count": len(pages)
            if path.suffix.lower() not in {".txt", ".md"}
            else None,
            "ocr_pages": ocr_pages,
            "warnings": warnings,
            "review_sha256": entry.get("ocr_review_sha256"),
            "review_status": "reviewed"
            if reviewed
            else "pending_review"
            if requires_review
            else "not_required",
            "ready_for_retrieval": reviewed or not requires_review,
        }
        document_chunks = []
        for page in pages:
            for block in page["blocks"]:
                for start, end, text in _slices(block.get("text", ""), max_chars):
                    document_chunks.append(
                        {
                            "evidence_id": (
                                f"{entry['doc_id']}:{parsed_digest[:16]}:"
                                f"{block['block_id']}:C{start}-{end}"
                            ),
                            "doc_id": entry["doc_id"],
                            "title": entry["title"],
                            "version": entry["version"],
                            "collection_id": manifest["collection_id"],
                            "data_domain": manifest["data_domain"],
                            "source": entry["source"],
                            "source_sha256": digest,
                            "parsed_sha256": parsed_digest,
                            "section": block["section"],
                            "section_basis": block.get(
                                "section_basis", "document_title"
                            ),
                            "page": page["number"],
                            "bbox": block.get("bbox"),
                            "bbox_units": page["units"],
                            "line_start": block.get("line_start"),
                            "line_end": block.get("line_end"),
                            "block_id": block["block_id"],
                            "char_start": start,
                            "char_end": end,
                            "kind": block["kind"],
                            "method": block["method"],
                            "text": text,
                            "table_id": block.get("table_id"),
                            "row_number": block.get("row_number"),
                            "header_bbox": block.get("header_bbox"),
                            "ready_for_retrieval": document["ready_for_retrieval"],
                        }
                    )
        document["chunk_count"] = len(document_chunks)
        documents.append(document)
        chunks.extend(document_chunks)
        blobs[document["original_file"]] = raw
        blobs[document["parsed_file"]] = parsed_raw
    inverted: dict[str, list[str]] = {}
    for chunk in chunks:
        if chunk["ready_for_retrieval"]:
            for term in keywords(chunk["text"]):
                inverted.setdefault(term, []).append(chunk["evidence_id"])
    collection = {
        "schema_version": 2,
        "collection_id": manifest["collection_id"],
        "data_domain": manifest["data_domain"],
        "manifest_sha256": sha256(manifest_path.read_bytes()),
        "prepared_at": datetime.now(UTC).isoformat(),
        "index_kind": "keywords",
        "documents": documents,
        "chunks": chunks,
        "keyword_index": inverted,
    }
    if len(encoded(collection)) > MAX_INDEX_BYTES:
        raise ValueError("集合索引超过 8MB，请分成较小集合。")
    return collection, blobs


def validate_collection(folder: Path) -> dict:
    """验证二进制原件、解析快照和每条片段的页码／位置／内容绑定。"""
    path = folder / "index.json"
    if path.stat().st_size > MAX_INDEX_BYTES:
        raise ValueError("集合索引超过 8MB。")
    collection = json.loads(path.read_text(encoding="utf-8"))
    return _validate_data(collection, folder)


def _validate_data(collection: dict, folder: Path) -> dict:
    if collection.get("schema_version") != 2:
        raise ValueError("需要通用准备集合 schema_version=2。")
    chunks_by_id = {}
    docs = {}
    coverage: dict[tuple[str, str, str], list[tuple[int, int]]] = {}
    for doc in collection["documents"]:
        suffix = doc["format"]
        if suffix not in SUPPORTED_TYPES:
            raise ValueError("原文件格式无效。")
        for field in ("source_sha256", "parsed_sha256"):
            if not re.fullmatch(r"[a-f0-9]{64}", doc[field]):
                raise ValueError("快照摘要无效。")
        original_name = f"originals/{doc['source_sha256']}{suffix}"
        parsed_name = f"parsed/{doc['parsed_sha256']}.json"
        if doc["original_file"] != original_name or doc["parsed_file"] != parsed_name:
            raise ValueError("快照路径无效。")
        for name, digest in (
            (original_name, doc["source_sha256"]),
            (parsed_name, doc["parsed_sha256"]),
        ):
            if sha256((folder / name).read_bytes()) != digest:
                raise ValueError("原件或解析快照校验失败。")
        parsed = json.loads((folder / parsed_name).read_text(encoding="utf-8"))
        if parsed["source_sha256"] != doc["source_sha256"]:
            raise ValueError("解析快照与原件不对应。")
        for field, value in parsed["document_metadata"].items():
            if doc[field] != value:
                raise ValueError("文档元数据与解析快照不一致。")
        needs_review = any(
            b.get("method", "").startswith("windows_ocr") or b["kind"] == "warning"
            for p in parsed["pages"]
            for b in p["blocks"]
        )
        if doc["ready_for_retrieval"] != (
            not needs_review or doc.get("review_sha256") == doc["parsed_sha256"]
        ):
            raise ValueError("复核状态与解析内容不一致。")
        blocks = {b["block_id"]: (p, b) for p in parsed["pages"] for b in p["blocks"]}
        docs[(doc["doc_id"], doc["parsed_sha256"])] = (doc, blocks)
    for chunk in collection["chunks"]:
        doc, blocks = docs[(chunk["doc_id"], chunk["parsed_sha256"])]
        page, block = blocks[chunk["block_id"]]
        start, end = chunk["char_start"], chunk["char_end"]
        if (
            type(start) is not int
            or type(end) is not int
            or not 0 <= start < end <= len(block["text"])
        ):
            raise ValueError("片段字符位置无效。")
        if chunk["text"] != block["text"][start:end]:
            raise ValueError("片段不是解析原文。")
        for field in (
            "title",
            "version",
            "source",
            "source_sha256",
            "ready_for_retrieval",
            "data_domain",
        ):
            if chunk[field] != doc[field]:
                raise ValueError("片段与文档元数据不一致。")
        for field in ("section", "bbox", "kind", "method"):
            if chunk[field] != block.get(field):
                raise ValueError("片段与原文位置不一致。")
        if chunk["section_basis"] != block.get("section_basis", "document_title"):
            raise ValueError("章节来源不一致。")
        if chunk["page"] != page["number"] or chunk["bbox_units"] != page["units"]:
            raise ValueError("片段页码或坐标单位不一致。")
        for field in (
            "line_start",
            "line_end",
            "table_id",
            "row_number",
            "header_bbox",
        ):
            if chunk.get(field) != block.get(field):
                raise ValueError("片段行号或表格位置不一致。")
        identity = (
            f"{doc['doc_id']}:{doc['parsed_sha256'][:16]}:"
            f"{block['block_id']}:C{start}-{end}"
        )
        if chunk["evidence_id"] != identity or identity in chunks_by_id:
            raise ValueError("片段编号无效或重复。")
        if chunk["collection_id"] != collection["collection_id"]:
            raise ValueError("集合编号不一致。")
        chunks_by_id[identity] = chunk
        key = (chunk["doc_id"], chunk["parsed_sha256"], chunk["block_id"])
        coverage.setdefault(key, []).append((start, end))
    for (doc_id, parsed_hash), (doc, blocks) in docs.items():
        count = 0
        for block_id, (_, block) in blocks.items():
            spans = sorted(coverage.get((doc_id, parsed_hash, block_id), []))
            cursor = 0
            for start, end in spans:
                if start < cursor or block["text"][cursor:start].strip():
                    raise ValueError("片段有重复或遗漏正文。")
                cursor = end
            if block["text"][cursor:].strip():
                raise ValueError("片段遗漏正文。")
            count += len(spans)
        if count != doc["chunk_count"]:
            raise ValueError("片段数量与文档记录不一致。")
    expected: dict[str, list[str]] = {}
    for chunk in collection["chunks"]:
        if chunk["ready_for_retrieval"]:
            for word in keywords(chunk["text"]):
                expected.setdefault(word, []).append(chunk["evidence_id"])
    if expected != collection["keyword_index"]:
        raise ValueError("关键词索引与可检索片段不一致。")
    return collection


def report(collection: dict, *, folder: Path | None = None) -> dict:
    return {
        "status": "prepared",
        "collection_id": collection["collection_id"],
        "index_path": str(folder / "index.json") if folder else None,
        "document_count": len(collection["documents"]),
        "chunk_count": len(collection["chunks"]),
        "ready_document_count": sum(
            d["ready_for_retrieval"] for d in collection["documents"]
        ),
        "pending_review_count": sum(
            not d["ready_for_retrieval"] for d in collection["documents"]
        ),
        "keyword_count": len(collection["keyword_index"]),
        "documents": collection["documents"],
    }


def write_preview(collection: dict, folder: Path) -> None:
    lines = ["# 文档导入预览", "", "用于检查解析与位置；OCR 内容须对照原件复核。", ""]
    for doc in collection["documents"]:
        original = (folder / doc["original_file"]).resolve().as_posix()
        parsed_path = folder / doc["parsed_file"]
        parsed = json.loads(parsed_path.read_text(encoding="utf-8"))
        lines.extend(
            [
                f"## {doc['title']} · {doc['version']}",
                "",
                f"编号：{doc['doc_id']}；来源：{doc['source']}",
                "",
                f"[查看原件](<{original}>)",
                "",
                f"状态：{doc['review_status']}；片段数：{doc['chunk_count']}",
                "",
                f"解析摘要（复核后填写到清单）：`{doc['parsed_sha256']}`",
                "",
            ]
        )
        for warning in doc["warnings"]:
            lines.extend([f"提示：{warning}", ""])
        for page in parsed["pages"]:
            label = (
                f"第 {page['number']} 页"
                if page["number"] is not None
                else "文本原文件行号"
            )
            lines.extend([f"### {label}", ""])
            for block in page["blocks"]:
                if not block.get("text"):
                    continue
                position = (
                    f"第 {block['line_start']} 行"
                    if block.get("line_start")
                    else f"坐标 {block.get('bbox')}（{page['units']}）"
                )
                lines.extend(
                    [f"{block['block_id']} · {block['kind']} · {position}", ""]
                )
                # 缩进代码块保留文字，不让导入内容在预览中成为命令或链接。
                lines.extend("    " + line for line in block["text"].splitlines())
                lines.append("")
                if block.get("before_correction_text"):
                    lines.extend(
                        [
                            "更正前 OCR：",
                            "",
                            "    " + block["before_correction_text"],
                            "",
                            "复核依据：",
                            "",
                            "    " + block["correction_note"],
                            "",
                        ]
                    )
    (folder / "preview.md").write_text("\n".join(lines), encoding="utf-8")


def import_documents(manifest_path: Path, target: Path, **options) -> dict:
    collection, blobs = prepare(manifest_path, **options)
    target = target.resolve()
    root = manifest_path.resolve().parent
    if target == root or target.is_relative_to(root) or root.is_relative_to(target):
        raise ValueError("产物目录必须与资料目录分开。")
    if target.name != collection["collection_id"]:
        raise ValueError("产物目录末级名称必须与 collection_id 一致。")
    for name, raw in blobs.items():
        path = target / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            if path.read_bytes() != raw:
                raise ValueError("已有原件或解析快照损坏，请先检查。")
        else:
            temporary = path.with_name(f".{uuid4().hex}.tmp")
            try:
                temporary.write_bytes(raw)
                temporary.replace(path)
            finally:
                temporary.unlink(missing_ok=True)
    # 完整验证所有原件、解析产物与索引后，才替换当前集合入口。
    _validate_data(collection, target)
    temporary = target / f".index-{uuid4().hex}.tmp"
    try:
        temporary.write_bytes(encoded(collection))
        temporary.replace(target / "index.json")
    finally:
        temporary.unlink(missing_ok=True)
    result = report(validate_collection(target), folder=target)
    (target / "import-report.json").write_bytes(encoded(result))
    write_preview(collection, target)
    return result


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--target", type=Path)
    parser.add_argument("--verify", type=Path)
    parser.add_argument("--activate", action="store_true")
    parser.add_argument("--activate-collection")
    parser.add_argument("--check-only", action="store_true")
    parser.add_argument("--ocr", choices=["auto", "always", "never"], default="auto")
    parser.add_argument("--language", default="zh-Hans-CN")
    parser.add_argument("--max-chars", type=int, default=1200)
    args = parser.parse_args()
    try:
        if args.activate and (
            args.check_only or args.verify or args.activate_collection
        ):
            raise ValueError("--activate 只能与实际文档导入一起使用。")
        if args.activate_collection:
            from agent_core.retrieval.selection import (
                KNOWLEDGE_ROOT,
                activate_collection,
            )

            activate_collection(args.activate_collection)
            result = {
                "status": "active",
                "collection_id": args.activate_collection,
                "config_path": str(KNOWLEDGE_ROOT / "active.json"),
            }
        elif args.verify:
            result = report(validate_collection(args.verify), folder=args.verify)
            result["status"] = "verified"
        else:
            if args.manifest is None:
                raise ValueError("请指定 --manifest 导入清单。")
            options = {
                "ocr_mode": args.ocr,
                "language": args.language,
                "max_chars": args.max_chars,
            }
            if args.check_only:
                result = report(prepare(args.manifest, **options)[0])
                result["mode"] = "check_only"
            else:
                manifest = load_manifest(args.manifest)
                target = (
                    args.target
                    or PROJECT_ROOT / ".local" / "knowledge" / manifest["collection_id"]
                )
                result = import_documents(args.manifest, target, **options)
                result["mode"] = "import"
                if args.activate:
                    from agent_core.retrieval.selection import (
                        KNOWLEDGE_ROOT,
                        activate_collection,
                    )

                    if target.resolve().parent != KNOWLEDGE_ROOT.resolve():
                        raise ValueError("选用集合需导入到本机 .local/knowledge 目录。")
                    activate_collection(result["collection_id"])
                    result["active_collection_id"] = result["collection_id"]
    except (
        OSError,
        ValueError,
        KeyError,
        TypeError,
        ImportError,
        subprocess.SubprocessError,
    ) as error:
        result = {"status": "error", "message": str(error)}
    # ASCII JSON survives Windows PowerShell 5 native-process code-page decoding.
    # Stored reports and snapshots remain UTF-8; JSON readers recover Unicode.
    print(json.dumps(result, ensure_ascii=True, indent=2))
    return 1 if result["status"] == "error" else 0


if __name__ == "__main__":
    raise SystemExit(main())
