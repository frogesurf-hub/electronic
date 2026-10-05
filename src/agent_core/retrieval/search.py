"""本机模拟文档关键词检索；验证引用能回到导入时的原文快照。"""

import hashlib
import json
import math
import re
from collections import OrderedDict
from copy import deepcopy
from datetime import date
from pathlib import Path

from agent_core.retrieval.documents import (
    _HEADING,
    _SOURCE,
    _VALID_FROM,
    _VERSION,
    COLLECTION_ID,
    PROJECT_ROOT,
    _metadata,
    keywords,
)


class DocumentSearch:
    def __init__(self, folder: Path | None = None) -> None:
        self.folder = (
            folder
            if folder is not None
            else PROJECT_ROOT / ".local" / "knowledge" / COLLECTION_ID
        )
        self._cache: OrderedDict[tuple, list[dict]] = OrderedDict()

    def _load(self) -> tuple[dict, str]:
        index = self.folder / "index.json"
        if index.stat().st_size > 8 * 1024 * 1024:
            raise ValueError("Knowledge index is too large")
        raw = index.read_bytes()
        collection = json.loads(raw)
        if (
            collection["schema_version"] != 1
            or collection["collection_id"] != COLLECTION_ID
            or collection["data_domain"] != COLLECTION_ID
        ):
            raise ValueError("Unexpected knowledge collection")
        docs = {}
        for doc in collection["documents"]:
            digest = doc["file_sha256"]
            if not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest):
                raise ValueError("Invalid original digest")
            snapshot = self.folder / "originals" / f"{digest}.md"
            if snapshot.stat().st_size > 2 * 1024 * 1024:
                raise ValueError("Original document is too large")
            original = snapshot.read_bytes()
            if hashlib.sha256(original).hexdigest() != digest:
                raise ValueError("Original snapshot was changed")
            text = original.decode("utf-8-sig")
            headings = [
                match.group(2)
                for line in text.splitlines()
                if (match := _HEADING.match(line.strip()))
            ]
            if doc["title"] != (headings[0] if headings else doc["doc_id"]):
                raise ValueError("Document title does not match original")
            for field, pattern in (
                ("version", _VERSION),
                ("source", _SOURCE),
                ("applicable_time", _VALID_FROM),
            ):
                if doc[field] != _metadata(pattern, text, field):
                    raise ValueError("Document metadata does not match original")
            if doc["doc_id"] in docs:
                raise ValueError("Duplicate document ID")
            docs[doc["doc_id"]] = (doc, text.splitlines(keepends=True))
        evidence_ids = set()
        for chunk in collection["chunks"]:
            doc, lines = docs[chunk["doc_id"]]
            start, end = chunk["line_start"], chunk["line_end"]
            if (
                type(start) is not int
                or type(end) is not int
                or not 1 <= start <= end <= len(lines)
            ):
                raise ValueError("Invalid original location")
            expected_id = f"{doc['doc_id']}:{doc['file_sha256'][:12]}:L{start}-{end}"
            if chunk["evidence_id"] != expected_id or expected_id in evidence_ids:
                raise ValueError("Invalid evidence ID")
            evidence_ids.add(expected_id)
            if chunk["text"] != "".join(lines[start - 1 : end]).strip():
                raise ValueError("Evidence is not original text")
            heading_path: list[tuple[int, str]] = []
            for line in lines[:start]:
                match = _HEADING.match(line.strip())
                if match:
                    level = len(match.group(1))
                    heading_path = [(n, t) for n, t in heading_path if n < level]
                    heading_path.append((level, match.group(2)))
            expected_section = " / ".join(t for _, t in heading_path) or doc["title"]
            if chunk["section"] != expected_section:
                raise ValueError("Evidence section does not match original")
            if chunk["page"] is not None:
                raise ValueError("Markdown evidence cannot claim a page number")
            if (
                chunk["data_domain"] != COLLECTION_ID
                or chunk["collection_id"] != COLLECTION_ID
            ):
                raise ValueError("Evidence domain mismatch")
            for field in (
                "file_sha256",
                "version",
                "source",
                "applicable_time",
                "title",
            ):
                if chunk[field] != doc[field]:
                    raise ValueError("Evidence metadata mismatch")
        return collection, hashlib.sha256(raw).hexdigest()

    def search(
        self,
        question: str,
        *,
        doc_ids: list[str] | None = None,
        version: str | None = None,
        as_of: date | None = None,
    ) -> tuple[list[dict], bool]:
        collection, fingerprint = self._load()
        selected = tuple(sorted(doc_ids or []))
        key = (fingerprint, question.strip().casefold(), selected, version, as_of)
        if key in self._cache:
            self._cache.move_to_end(key)
            return deepcopy(self._cache[key]), True
        query_terms = set(keywords(question))
        terms = [set(keywords(row["text"])) for row in collection["chunks"]]
        frequencies = {
            term: sum(term in words for words in terms) for term in query_terms
        }
        scored = []
        for chunk in collection["chunks"]:
            if selected and chunk["doc_id"] not in selected:
                continue
            if version is not None and chunk["version"] != version:
                continue
            if as_of is not None:
                match = re.fullmatch(
                    r"(\d{4}-\d{2}-\d{2})\s*起", chunk["applicable_time"]
                )
                if match is None or as_of < date.fromisoformat(match.group(1)):
                    continue
            overlap = query_terms.intersection(keywords(chunk["text"]))
            # 使用实际文本计算匹配，不能让损坏的倒排索引伪造相关性。
            if len(overlap) < 2:
                continue
            total = len(collection["chunks"])
            score = sum(
                math.log(1 + total / (1 + frequencies[term])) for term in overlap
            )
            scored.append((score, chunk["evidence_id"], chunk))
        scored.sort(key=lambda x: (-x[0], x[1]))
        evidence = [deepcopy(row[2]) for row in scored[:4]]
        self._cache[key] = evidence
        if len(self._cache) > 32:
            self._cache.popitem(last=False)
        return deepcopy(evidence), False
