import hashlib
import json
from pathlib import Path

import pytest

from agent_core.retrieval.documents import (
    COLLECTION_ID,
    import_collection,
    prepare_collection,
)

TEXT = """# 销售说明

版本：v1；适用时间：2023-01-01 起；来源：模拟演示。

## 口径

销售额按支付时间统计，不扣退款。
这是模拟文档，不是 Chinook 发票口径。
"""


def source_at(tmp_path, text=TEXT):
    folder = tmp_path / "source"
    folder.mkdir()
    (folder / "DOC-TEST.md").write_text(text, encoding="utf-8")
    return folder


def test_import_preserves_original_and_verifiable_section_citations(tmp_path):
    source = source_at(tmp_path)
    raw = (source / "DOC-TEST.md").read_bytes()
    report = import_collection(source, tmp_path / "target")
    assert report["status"] == "ok"
    assert report["document_count"] == 1 and report["chunk_count"] == 2
    collection = json.loads(Path(report["index_path"]).read_text(encoding="utf-8"))
    document = collection["documents"][0]
    snapshot = tmp_path / "target" / document["snapshot_file"]
    assert snapshot.read_bytes() == raw
    assert document["file_sha256"] == hashlib.sha256(raw).hexdigest()
    lines = raw.decode("utf-8").splitlines(keepends=True)
    for chunk in collection["chunks"]:
        assert (
            chunk["text"]
            == "".join(lines[chunk["line_start"] - 1 : chunk["line_end"]]).strip()
        )
        assert chunk["version"] == "v1" and chunk["page"] is None
        assert chunk["data_domain"] == COLLECTION_ID
    assert collection["chunks"][1]["section"] == "销售说明 / 口径"
    assert "退款" in collection["keyword_index"]
    assert (source / "DOC-TEST.md").read_bytes() == raw


def test_reimport_is_stable_and_new_version_keeps_old_snapshot(tmp_path):
    source = source_at(tmp_path)
    target = tmp_path / "target"
    first = import_collection(source, target)
    old = json.loads(Path(first["index_path"]).read_text(encoding="utf-8"))
    import_collection(source, target)
    unchanged = json.loads((target / "index.json").read_text(encoding="utf-8"))
    assert old["chunks"] == unchanged["chunks"]
    (source / "DOC-TEST.md").write_text(TEXT.replace("v1", "v2"), encoding="utf-8")
    import_collection(source, target)
    new = json.loads((target / "index.json").read_text(encoding="utf-8"))
    assert new["chunks"][0]["evidence_id"] != old["chunks"][0]["evidence_id"]
    assert new["documents"][0]["version"] == "v2"
    assert (target / old["documents"][0]["snapshot_file"]).exists()


def test_invalid_document_does_not_replace_existing_index(tmp_path):
    source = source_at(tmp_path)
    target = tmp_path / "target"
    import_collection(source, target)
    before = (target / "index.json").read_bytes()
    (source / "BAD.md").write_text("# 没有版本的文档", encoding="utf-8")
    with pytest.raises(ValueError, match="版本"):
        import_collection(source, target)
    assert (target / "index.json").read_bytes() == before


def test_long_section_is_split_without_losing_original_lines(tmp_path):
    source = source_at(tmp_path, TEXT + ("\n一段需要保留的原文。" * 50))
    collection, _ = prepare_collection(source, max_chars=100)
    lines = (source / "DOC-TEST.md").read_text(encoding="utf-8").splitlines()
    for i, line in enumerate(lines, start=1):
        if line.strip():
            assert any(
                chunk["line_start"] <= i <= chunk["line_end"]
                for chunk in collection["chunks"]
            )
    assert all(len(chunk["text"]) <= 100 for chunk in collection["chunks"])


def test_target_cannot_replace_source_folder(tmp_path):
    source = source_at(tmp_path)
    with pytest.raises(ValueError, match="分开"):
        import_collection(source, source)
    assert not (source / "index.json").exists()


def test_selected_team_demo_documents_have_metadata_and_usable_citations():
    root = Path(__file__).resolve().parents[3]
    collection, originals = prepare_collection(root / "database" / "documents")
    assert len(collection["documents"]) == 5
    assert len(collection["chunks"]) == 10
    assert len(originals) == 5
    assert all(x["version"] == "v1" for x in collection["documents"])
    assert all(x["source"] == "模拟演示" for x in collection["documents"])
    assert all(x["data_domain"] == COLLECTION_ID for x in collection["chunks"])
