import json
import os
import subprocess
from pathlib import Path

import pytest

from agent_core.retrieval import preparation
from agent_core.retrieval.preparation import (
    import_documents,
    load_manifest,
    prepare,
    validate_collection,
)


def manifest_at(folder: Path, *, text="文件准备检测，保留来源和版本。") -> Path:
    folder.mkdir()
    (folder / "source.txt").write_text(text, encoding="utf-8")
    manifest = {
        "schema_version": 1,
        "collection_id": "test_documents",
        "data_domain": "test_domain",
        "documents": [
            {
                "file": "source.txt",
                "doc_id": "DOC-TEST",
                "title": "准备检测",
                "version": "v1",
                "source": "测试资料",
                "valid_from": None,
                "valid_until": None,
                "ocr_review_sha256": None,
            }
        ],
    }
    path = folder / "manifest.json"
    path.write_bytes(preparation.encoded(manifest))
    return path


def update_manifest(path, change):
    manifest = json.loads(path.read_text(encoding="utf-8"))
    change(manifest)
    path.write_bytes(preparation.encoded(manifest))


def test_long_line_chunks_keep_exact_original_character_positions(tmp_path):
    text = "正文不应被切丢。" * 400
    path = manifest_at(tmp_path / "input", text=text)
    collection, blobs = prepare(path, max_chars=100)
    assert "".join(c["text"] for c in collection["chunks"]) == text
    assert all(len(c["text"]) <= 100 for c in collection["chunks"])
    assert all(c["page"] is None and c["line_start"] == 1 for c in collection["chunks"])
    assert collection["documents"][0]["valid_from"] is None
    assert (tmp_path / "input" / "source.txt").read_text(encoding="utf-8") == text
    target = tmp_path / "test_documents"
    result = import_documents(path, target, max_chars=100)
    assert result["ready_document_count"] == 1
    assert validate_collection(target)["chunks"] == collection["chunks"]
    assert all((target / name).read_bytes() == raw for name, raw in blobs.items())


def test_reimport_is_stable_and_version_change_keeps_old_snapshots(tmp_path):
    path = manifest_at(tmp_path / "input")
    target = tmp_path / "test_documents"
    import_documents(path, target)
    old = validate_collection(target)
    import_documents(path, target)
    assert validate_collection(target)["chunks"] == old["chunks"]
    update_manifest(path, lambda m: m["documents"][0].update(version="v2"))
    import_documents(path, target)
    new = validate_collection(target)
    assert new["chunks"][0]["evidence_id"] != old["chunks"][0]["evidence_id"]
    assert (target / old["documents"][0]["parsed_file"]).exists()


def test_text_indentation_offsets_and_missing_chunks_are_checked(tmp_path):
    text = "    位置检测文字。  "
    path = manifest_at(tmp_path / "input", text=text)
    target = tmp_path / "test_documents"
    import_documents(path, target)
    collection = validate_collection(target)
    chunk = collection["chunks"][0]
    assert text[chunk["char_start"] : chunk["char_end"]] == chunk["text"]
    assert chunk["char_start"] == 4
    collection["chunks"] = []
    collection["keyword_index"] = {}
    collection["documents"][0]["chunk_count"] = 0
    (target / "index.json").write_bytes(preparation.encoded(collection))
    with pytest.raises(ValueError, match="遗漏"):
        validate_collection(target)


def test_same_document_multiple_versions_are_distinct_even_with_identical_text(
    tmp_path,
):
    path = manifest_at(tmp_path / "input")
    (path.parent / "v2.txt").write_bytes((path.parent / "source.txt").read_bytes())
    update_manifest(
        path,
        lambda m: m["documents"].append(
            {**m["documents"][0], "file": "v2.txt", "version": "v2"}
        ),
    )
    target = tmp_path / "test_documents"
    import_documents(path, target)
    collection = validate_collection(target)
    assert len({c["evidence_id"] for c in collection["chunks"]}) == 2
    assert {d["version"] for d in collection["documents"]} == {"v1", "v2"}


@pytest.mark.parametrize(
    "field,value",
    [
        ("version", ""),
        ("source", ""),
        ("file", "../outside.txt"),
        ("valid_from", "not-a-date"),
        ("ocr_review_sha256", "not-a-hash"),
    ],
)
def test_bad_manifest_does_not_replace_existing_index(tmp_path, field, value):
    path = manifest_at(tmp_path / "input")
    (tmp_path / "outside.txt").write_text("不应导入", encoding="utf-8")
    target = tmp_path / "test_documents"
    import_documents(path, target)
    before = (target / "index.json").read_bytes()
    update_manifest(path, lambda m: m["documents"][0].update({field: value}))
    with pytest.raises((ValueError, FileNotFoundError)):
        import_documents(path, target)
    assert (target / "index.json").read_bytes() == before


def test_source_and_target_cannot_overlap_or_replace_demo_collection(tmp_path):
    path = manifest_at(tmp_path / "input")
    with pytest.raises(ValueError, match="分开"):
        import_documents(path, path.parent / "test_documents")
    update_manifest(path, lambda m: m.update(collection_id="legacy_sales_demo"))
    with pytest.raises(ValueError, match="独立集合"):
        load_manifest(path)


@pytest.mark.parametrize(
    "change", ["original", "parsed", "text", "page", "bbox", "keywords", "ready"]
)
def test_tampered_original_parse_or_index_is_rejected(tmp_path, change):
    path = manifest_at(tmp_path / "input")
    target = tmp_path / "test_documents"
    import_documents(path, target)
    collection = validate_collection(target)
    doc = collection["documents"][0]
    if change in {"original", "parsed"}:
        field = "original_file" if change == "original" else "parsed_file"
        (target / doc[field]).write_bytes(b"changed")
    else:
        if change == "keywords":
            collection["keyword_index"] = {}
        elif change == "ready":
            doc["ready_for_retrieval"] = False
        else:
            collection["chunks"][0][change] = "changed"
        (target / "index.json").write_bytes(preparation.encoded(collection))
    with pytest.raises(ValueError):
        validate_collection(target)


def test_ocr_review_binds_to_exact_parse_and_is_invalidated_on_version_change(
    tmp_path, monkeypatch
):
    def fake_parse(*args, **kwargs):
        return [
            {
                "number": 1,
                "width": 100,
                "height": 100,
                "units": "pixels",
                "blocks": [
                    {
                        "text": "扫描检测文字",
                        "kind": "ocr_line",
                        "method": "windows_ocr",
                        "bbox": [0, 0, 80, 20],
                        "section": "检测",
                    }
                ],
            }
        ]

    monkeypatch.setattr(preparation, "parse_file", fake_parse)
    path = manifest_at(tmp_path / "input")
    target = tmp_path / "test_documents"
    pending = import_documents(path, target)
    assert pending["pending_review_count"] == 1 and pending["keyword_count"] == 0
    digest = pending["documents"][0]["parsed_sha256"]
    update_manifest(path, lambda m: m["documents"][0].update(ocr_review_sha256=digest))
    reviewed = import_documents(path, target)
    assert reviewed["ready_document_count"] == 1 and reviewed["keyword_count"] > 0
    assert reviewed["documents"][0]["review_status"] == "reviewed"
    update_manifest(
        path,
        lambda m: m["documents"][0].update(
            corrections={
                "P1-B1": {"text": "复核后的扫描文字", "note": "对照检测原件逐字核对。"}
            }
        ),
    )
    corrected = import_documents(path, target)
    assert corrected["pending_review_count"] == 1
    assert corrected["documents"][0]["parsed_sha256"] != digest
    parsed = json.loads(
        (target / corrected["documents"][0]["parsed_file"]).read_text(encoding="utf-8")
    )
    block = parsed["pages"][0]["blocks"][0]
    assert block["text"] == "复核后的扫描文字"
    assert block["before_correction_text"] == "扫描检测文字"
    assert block["bbox"] == [0, 0, 80, 20]
    before = (target / "index.json").read_bytes()
    update_manifest(
        path,
        lambda m: m["documents"][0].update(
            corrections={"P9-B1": {"text": "更正", "note": "错误编号。"}}
        ),
    )
    with pytest.raises(ValueError, match="编号"):
        import_documents(path, target)
    assert (target / "index.json").read_bytes() == before
    update_manifest(path, lambda m: m["documents"][0].update(corrections={}))
    update_manifest(path, lambda m: m["documents"][0].update(version="v2"))
    assert import_documents(path, target)["pending_review_count"] == 1


def test_empty_or_failed_parse_keeps_previous_index(tmp_path, monkeypatch):
    path = manifest_at(tmp_path / "input")
    target = tmp_path / "test_documents"
    import_documents(path, target)
    before = (target / "index.json").read_bytes()
    monkeypatch.setattr(preparation, "parse_file", lambda *args, **kw: [])
    with pytest.raises(ValueError, match="文字"):
        import_documents(path, target)
    assert (target / "index.json").read_bytes() == before


def test_windows_powershell_nested_json_pipeline_preserves_chinese_metadata(tmp_path):
    root = Path(__file__).resolve().parents[3]
    if os.name != "nt" or not (root / ".local/document-import-runtime.json").exists():
        pytest.skip("需已配置的 Windows 文档入口")
    manifest = manifest_at(tmp_path / "input")
    target = tmp_path / "test_documents"
    import_documents(manifest, target)
    target_argument = "'" + str(target).replace("'", "''") + "'"
    manifest_argument = "'" + str(manifest).replace("'", "''") + "'"
    script = f"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::GetEncoding(936)
$arguments = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File',
    '.\\scripts\\import-documents.ps1', '-Verify', {target_argument})
$result = powershell.exe @arguments | ConvertFrom-Json
if ($LASTEXITCODE -ne 0) {{ throw 'Worker failed' }}
$manifest = Get-Content -LiteralPath {manifest_argument} -Raw -Encoding UTF8 |
    ConvertFrom-Json
foreach ($field in @('title', 'source', 'version', 'doc_id')) {{
    if ($result.documents[0].$field -ne $manifest.documents[0].$field) {{
        throw 'Metadata was changed by output decoding'
    }}
}}
$result | Select-Object status,document_count,ready_document_count | ConvertTo-Json
"""
    run = subprocess.run(
        ["powershell.exe", "-NoProfile", "-Command", script],
        cwd=root,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
    )
    assert run.returncode == 0, run.stdout + run.stderr
    summary = json.loads(run.stdout)
    assert summary["status"] == "verified"
    assert summary["document_count"] == summary["ready_document_count"] == 1


def test_generated_pdf_table_scan_and_image_parse_with_real_local_tools(tmp_path):
    root = Path(__file__).resolve().parents[3]
    runtime = root / ".local" / "document-import-runtime.json"
    if not runtime.exists():
        pytest.skip("尚未配置可选本地 PDF/OCR 工具")
    python = json.loads(runtime.read_text(encoding="utf-8-sig"))["python"]
    source = tmp_path / "fixtures"
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    generated = subprocess.run(
        [
            python,
            str(root / "scripts" / "create_document_fixtures.py"),
            "--target",
            str(source),
        ],
        capture_output=True,
        encoding="utf-8",
        timeout=30,
        env=env,
    )
    assert generated.returncode == 0, generated.stderr
    target = tmp_path / "import_detection"
    run = subprocess.run(
        [
            python,
            str(root / "scripts" / "prepare_documents.py"),
            "--manifest",
            str(source / "manifest.json"),
            "--target",
            str(target),
        ],
        capture_output=True,
        encoding="utf-8",
        timeout=120,
        env=env,
    )
    assert run.returncode == 0, run.stdout + run.stderr
    collection = validate_collection(target)
    assert len(collection["documents"]) == 5
    assert sum(not d["ready_for_retrieval"] for d in collection["documents"]) == 2
    by_doc = {d["doc_id"]: d for d in collection["documents"]}
    pdf_doc = by_doc["IMPORT-CHECK-003"]
    pdf_chunks = [c for c in collection["chunks"] if c["doc_id"] == pdf_doc["doc_id"]]
    assert any("PDF-READY-303" in c["text"] and c["page"] == 1 for c in pdf_chunks)
    assert any(
        "TABLE-404" in c["text"]
        and c["kind"] == "table_row"
        and c["page"] == 2
        and c["header_bbox"] is not None
        for c in pdf_chunks
    )
    for identity in ["IMPORT-CHECK-004", "IMPORT-CHECK-005"]:
        chunks = [c for c in collection["chunks"] if c["doc_id"] == identity]
        assert any("SCAN-505" in c["text"] for c in chunks)
        assert any("OCR-606" in c["text"] for c in chunks)
        assert all(
            c["bbox"] is not None and c["method"].startswith("windows_ocr")
            for c in chunks
        )
        assert any(
            "检测项 | 检测值\n扫描表格 | OCR-606" == c["text"]
            and c["kind"] == "table_row"
            and c["header_bbox"] is not None
            for c in chunks
        )
        assert not any(
            c["evidence_id"] in ids
            for c in chunks
            for ids in collection["keyword_index"].values()
        )
    for doc in collection["documents"]:
        assert (target / doc["original_file"]).read_bytes() == Path(
            doc["source_path"]
        ).read_bytes()
