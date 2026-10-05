import asyncio
import json
import shutil
from datetime import date
from pathlib import Path

import pytest
from websockets.asyncio.client import connect
from websockets.asyncio.server import serve

from agent_core.communication import WebSocketServer
from agent_core.core.agent import Agent
from agent_core.core.business_flow import BusinessFlow
from agent_core.core.message import Message
from agent_core.providers import LLMResponse
from agent_core.retrieval import preparation
from agent_core.retrieval.documents import (
    COLLECTION_ID,
    PROJECT_ROOT,
    import_collection,
)
from agent_core.retrieval.qa_service import DocumentQAService
from agent_core.retrieval.search import ActiveDocumentSearch, PreparedDocumentSearch
from agent_core.retrieval.selection import activate_collection, active_collection_id
from agent_core.tests.fakes import FixedClock
from agent_core.tests.test_business_flow import NOW, FakeSQL, request
from agent_core.understanding.service import UnderstandingService


def make_collection(tmp_path, name="business_docs", text="检测计数为 17。", **metadata):
    source = tmp_path / ("input_" + name)
    source.mkdir(exist_ok=True)
    (source / "source.txt").write_text(text, encoding="utf-8")
    manifest = {
        "schema_version": 1,
        "collection_id": name,
        "data_domain": "business_docs",
        "documents": [
            {
                "file": "source.txt",
                "doc_id": "DOC-NEW",
                "title": "检测制度",
                "version": "v1",
                "source": "检测文件，不是业务依据",
                "valid_from": "2024-01-01",
                "valid_until": "2025-01-01",
                **metadata,
            }
        ],
    }
    path = source / "manifest.json"
    path.write_bytes(preparation.encoded(manifest))
    folder = tmp_path / "knowledge" / name
    preparation.import_documents(path, folder)
    return path, folder


def test_prepared_scope_filters_title_version_and_exclusive_dates(tmp_path):
    _, folder = make_collection(tmp_path)
    search = PreparedDocumentSearch(folder)
    evidence, cached = search.search("检测计数", doc_titles=["检测制度"])
    assert evidence and not cached
    assert search.search("检测计数", doc_titles=["检测制度"])[1]
    assert evidence[0]["collection_id"] == "business_docs"
    assert Path(evidence[0]["original_path"]).exists()
    assert evidence[0]["line_start"] == 1 and evidence[0]["page"] is None
    for options in [
        {"doc_titles": ["不存在的指定文档"]},
        {"doc_ids": ["DOC-E001"]},
        {"version": "v2"},
        {"as_of": date(2023, 12, 31)},
        {"as_of": date(2025, 1, 1)},
    ]:
        assert search.search("检测计数", **options)[0] == []
    assert search.search("检测计数", as_of=date(2024, 1, 1))[0]
    assert search.search("检测计数", as_of=date(2024, 12, 31))[0]
    _, folder = make_collection(tmp_path, "unknown_dates", valid_from=None)
    search = PreparedDocumentSearch(folder)
    assert search.search("检测计数")[0]
    assert not search.search("检测计数", as_of=date(2024, 1, 1))[0]


def test_active_switch_and_reimport_do_not_reuse_stale_evidence(tmp_path):
    path, folder = make_collection(tmp_path)
    root = folder.parent
    activate_collection("business_docs", root)
    search = ActiveDocumentSearch(root)
    assert "17" in search.search("检测计数")[0][0]["text"]
    assert search.search("检测计数")[1]
    _, other = make_collection(tmp_path, "other_docs", text="检测计数为 29。")
    activate_collection("other_docs", root)
    evidence, cached = search.search("检测计数")
    assert not cached and "29" in evidence[0]["text"]
    assert evidence[0]["collection_id"] == "other_docs"
    activate_collection("business_docs", root)
    (path.parent / "source.txt").write_text("检测计数为 41。", encoding="utf-8")
    preparation.import_documents(path, folder)
    evidence, cached = search.search("检测计数")
    assert not cached and "41" in evidence[0]["text"]
    assert active_collection_id(root) == "business_docs"
    assert other.exists()


def test_cached_evidence_is_revalidated_against_original(tmp_path):
    _, folder = make_collection(tmp_path)
    search = PreparedDocumentSearch(folder)
    evidence, _ = search.search("检测计数")
    Path(evidence[0]["original_path"]).write_bytes(b"changed")
    with pytest.raises(ValueError):
        search.search("检测计数")


@pytest.mark.parametrize("bad_id", ["../outside", "business_docs/other", "UNKNOWN"])
def test_invalid_active_selection_fails_without_fallback(tmp_path, bad_id):
    _, folder = make_collection(tmp_path)
    root = folder.parent
    (root / "active.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "collection_id": bad_id,
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError):
        ActiveDocumentSearch(root).search("检测计数")


def test_unreviewed_ocr_is_not_searchable_or_activatable(tmp_path, monkeypatch):
    def fake_parse(*args, **kwargs):
        return [
            {
                "number": 1,
                "units": "pixels",
                "width": 100,
                "height": 100,
                "blocks": [
                    {
                        "kind": "ocr_line",
                        "method": "windows_ocr",
                        "text": "扫描检测计数为 99。",
                        "section": "检测制度",
                        "bbox": [0, 0, 100, 20],
                    }
                ],
            }
        ]

    monkeypatch.setattr(preparation, "parse_file", fake_parse)
    path, folder = make_collection(tmp_path)
    search = PreparedDocumentSearch(folder)
    assert search.search("检测计数")[0] == []
    assert search.catalog() == []
    with pytest.raises(ValueError, match="复核"):
        activate_collection("business_docs", folder.parent)
    assert not (folder.parent / "active.json").exists()
    report = json.loads((folder / "import-report.json").read_text("utf-8"))
    manifest = json.loads(path.read_text("utf-8"))
    manifest["documents"][0]["ocr_review_sha256"] = report["documents"][0][
        "parsed_sha256"
    ]
    path.write_bytes(preparation.encoded(manifest))
    preparation.import_documents(path, folder)
    activate_collection("business_docs", folder.parent)
    assert ActiveDocumentSearch(folder.parent).search("检测计数")[0]


def test_failed_switch_keeps_previous_selection_and_legacy_can_be_restored(tmp_path):
    _, folder = make_collection(tmp_path)
    root = folder.parent
    activate_collection("business_docs", root)
    before = (root / "active.json").read_bytes()
    with pytest.raises(FileNotFoundError):
        activate_collection("missing_docs", root)
    assert (root / "active.json").read_bytes() == before
    source = tmp_path / "legacy_input"
    source.mkdir()
    (source / "DOC-OLD.md").write_text(
        "# 旧检测文档\n版本：v1；适用时间：2023-01-01 起；来源：模拟演示。\n"
        "检测计数为 58。",
        encoding="utf-8",
    )
    import_collection(source, root / COLLECTION_ID)
    activate_collection(COLLECTION_ID, root)
    evidence, _ = ActiveDocumentSearch(root).search("检测计数")
    assert "58" in evidence[0]["text"] and evidence[0]["page"] is None


class EvidenceProvider:
    def __init__(self, *, quote="检测计数为 17。", foreign_reference=False):
        self.quote = quote
        self.foreign_reference = foreign_reference
        self.requests = []

    async def generate(self, request):
        self.requests.append(request)
        context = json.loads(request.messages[-1].content)
        if "current_datetime" in context:
            catalog = context["document_catalog"]
            assert catalog and context["document_catalog_available"]
            title = "文字与表格" if self.quote == "检测计数 | 17" else "检测制度"
            doc = next(d for d in catalog if d["title"] == title)
            response = {
                "problem_type": "qa",
                "qa_query": context["message"],
                "doc_ids": [doc["doc_id"]],
                "doc_titles": [title],
                "doc_version": doc["version"],
            }
        elif "evidence" in context:
            evidence = next(e for e in context["evidence"] if self.quote in e["text"])
            response = {
                "status": "ok",
                "claims": [
                    {
                        "text": "检测计数是 17。",
                        "references": [
                            {
                                "evidence_id": "old:wrong"
                                if self.foreign_reference
                                else evidence["evidence_id"],
                                "quote": self.quote,
                            }
                        ],
                    }
                ],
                "missing": "",
            }
        else:
            assert context["claims"][0]["quotes"] == [self.quote]
            assert context["document_scope"]["collection_id"] in {
                "business_docs",
                "import_detection",
            }
            response = {"supported": True}
        return LLMResponse(json.dumps(response, ensure_ascii=False))


def test_prepared_qa_propagates_source_and_citation_then_rejects_foreign_ids(tmp_path):
    _, folder = make_collection(tmp_path)
    for foreign in [False, True]:
        provider = EvidenceProvider(foreign_reference=foreign)
        result = asyncio.run(
            DocumentQAService(provider, PreparedDocumentSearch(folder)).answer(
                "根据检测制度查询检测计数。",
                doc_titles=["检测制度"],
                request_id="new-qa",
            )
        )
        assert result["source_id"] == "business_docs"
        if foreign:
            assert result["status"] == "error" and result["citations"] == []
        else:
            assert result["status"] == "ok"
            assert result["citations"][0]["quote"] == provider.quote
            assert result["citations"][0]["original_path"]
            assert "依据导入文档" in result["message"]


@pytest.mark.parametrize(
    "day,unsupported_claim,expected",
    [
        ("2022-01-01", False, "NO_RELEVANT_EVIDENCE"),
        ("2024-01-01", False, "ok"),
        ("2025-01-01", False, "NO_RELEVANT_EVIDENCE"),
        ("2024-01-01", True, "ANSWER_GROUNDING_REJECTED"),
    ],
)
def test_explicit_applicability_reaches_retrieval_and_keeps_grounding(
    tmp_path, day, unsupported_claim, expected
):
    _, folder = make_collection(tmp_path, text="检测项 | 检测值\n检测计数 | 17")
    search = PreparedDocumentSearch(folder)

    class ScopeProvider:
        def __init__(self):
            self.calls = []

        async def generate(self, call):
            context = json.loads(call.messages[-1].content)
            self.calls.append(context)
            if "current_datetime" in context:
                doc = context["document_catalog"][0]
                # A model that mistakes catalogue dates for ambiguous requirements
                # must no longer receive applicability metadata at this stage.
                issues = ["无法确认适用版本"] if "applicable_time" in doc else []
                data = {
                    "problem_type": "qa",
                    "qa_query": context["message"],
                    "doc_ids": [doc["doc_id"]],
                    "doc_titles": [doc["title"]],
                    "doc_as_of": day,
                    "issues": issues,
                }
            else:
                assert context["document_scope"]["as_of"] == day
                assert context["retrieval_scope_verified"] is True
                if "evidence" in context:
                    row = next(
                        e for e in context["evidence"] if "检测计数 | 17" in e["text"]
                    )
                    assert row["valid_from"] == "2024-01-01"
                    assert row["valid_until"] == "2025-01-01"
                    data = {
                        "status": "ok",
                        "claims": [
                            {
                                "text": "在 2024-01-01 实施检测得到 17。"
                                if unsupported_claim
                                else "文档中的检测计数为 17。",
                                "references": [
                                    {
                                        "evidence_id": row["evidence_id"],
                                            "quote": "检测计数 | 17",
                                    }
                                ],
                            }
                        ],
                    }
                else:
                    assert context["claims"][0]["citation_contexts"][0]["valid_from"]
                    data = {
                        "supported": not unsupported_claim,
                        "reason_code": "unsupported_claim"
                        if unsupported_claim
                        else "unspecified",
                    }
            return LLMResponse(json.dumps(data, ensure_ascii=False))

    provider = ScopeProvider()
    sql = FakeSQL()
    flow = BusinessFlow(
        UnderstandingService(provider, document_catalog=search.catalog),
        sql,
        FixedClock(NOW),
        qa_service=DocumentQAService(provider, search),
    )
    result = asyncio.run(
        flow.process_message(
            request(f"依据在 {day} 适用的《检测制度》，检测计数是多少？")
        )
    ).payload
    assert result["document_scope"]["as_of"] == day
    assert not sql.calls
    if expected == "ok":
        assert result["status"] == "ok"
        assert "17" in result["message"] and result["citations"]
    else:
        assert result["status"] == "insufficient_evidence"
        assert result["issues"][0]["code"] == expected
        assert not result["citations"]
    assert len(provider.calls) == (1 if expected == "NO_RELEVANT_EVIDENCE" else 3)


def test_missing_catalog_does_not_block_sql_and_unknown_title_does_not_call_qa_model(
    tmp_path,
):
    from agent_core.tests.test_business_flow import READY, SequenceProvider

    def unavailable_catalog():
        raise FileNotFoundError("missing")

    provider = SequenceProvider(READY)
    sql = FakeSQL()
    flow = BusinessFlow(
        UnderstandingService(provider, document_catalog=unavailable_catalog),
        sql,
        FixedClock(NOW),
    )
    result = asyncio.run(flow.process_message(request()))
    assert result.payload["status"] == "ok" and len(sql.calls) == 1
    assert not json.loads(provider.requests[0].messages[-1].content)[
        "document_catalog_available"
    ]
    _, folder = make_collection(tmp_path)
    provider = EvidenceProvider()
    result = asyncio.run(
        DocumentQAService(provider, PreparedDocumentSearch(folder)).answer(
            "检测计数", doc_titles=["不存在的文档"], request_id="unknown-title"
        )
    )
    assert result["status"] == "insufficient_evidence" and not provider.requests
    assert "不存在的文档" in result["message"]


def test_real_pdf_and_reviewed_scan_locations_survive_search(tmp_path):
    source = PROJECT_ROOT / ".local" / "knowledge" / "import_detection"
    if not source.exists():
        pytest.skip("尚未生成本机 PDF/OCR 检测集合")
    folder = tmp_path / "knowledge" / "import_detection"
    shutil.copytree(source, folder)
    search = PreparedDocumentSearch(folder)
    table, _ = search.search("检测计数", doc_titles=["文字与表格"])
    assert any("检测计数 | 17" in e["text"] and e["page"] == 2 for e in table)
    for title in ["扫描文件", "扫描图片"]:
        evidence, _ = search.search("检查识别文字和引用位置", doc_titles=[title])
        corrected = next(e for e in evidence if e["correction_note"])
        assert corrected["page"] == 1 and corrected["review_status"] == "reviewed"
        assert corrected["bbox"] and Path(corrected["original_path"]).exists()


def test_websocket_business_flow_returns_pdf_table_citation(tmp_path):
    source = PROJECT_ROOT / ".local" / "knowledge" / "import_detection"
    if not source.exists():
        pytest.skip("尚未生成本机 PDF/OCR 检测集合")
    root = tmp_path / "knowledge"
    shutil.copytree(source, root / "import_detection")
    activate_collection("import_detection", root)

    async def scenario():
        search = ActiveDocumentSearch(root)
        provider = EvidenceProvider(quote="检测计数 | 17")
        sql = FakeSQL()
        clock = FixedClock(NOW)
        flow = BusinessFlow(
            UnderstandingService(provider, document_catalog=search.catalog),
            sql,
            clock,
            qa_service=DocumentQAService(provider, search),
        )
        agent = Agent(
            provider=provider, name="Query Agent", clock=clock, business_flow=flow
        )
        server = WebSocketServer("127.0.0.1", 0, agent)
        async with serve(server.handle_connection, "127.0.0.1", 0) as listener:
            port = listener.sockets[0].getsockname()[1]
            async with connect(f"ws://127.0.0.1:{port}", proxy=None) as client:
                await client.send(
                    request("依据《文字与表格》，检测计数是多少？").to_json()
                )
                response = Message.from_json(await client.recv())
        assert response.type == "response" and response.payload["status"] == "ok"
        citation = response.payload["citations"][0]
        assert citation["page"] == 2 and citation["row_number"] == 3
        assert citation["collection_id"] == "import_detection"
        assert citation["bbox"] and citation["header_bbox"]
        assert response.payload["document_scope"]["doc_titles"] == ["文字与表格"]
        assert not sql.calls
        assert len(provider.requests) == 3

    asyncio.run(scenario())
