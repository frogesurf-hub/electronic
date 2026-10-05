import asyncio
import json
from datetime import date
from pathlib import Path

import pytest

from agent_core.core.business_flow import BusinessFlow
from agent_core.providers import LLMResponse, ProviderAuthenticationError
from agent_core.retrieval.documents import COLLECTION_ID, import_collection
from agent_core.retrieval.qa_service import DocumentQAService
from agent_core.retrieval.search import DocumentSearch
from agent_core.tests.fakes import FixedClock
from agent_core.tests.test_business_flow import NOW, FakeSQL, request
from agent_core.understanding.service import UnderstandingService

QUOTE = "张伟通过每周回访减少沟通偏差。"
TEXT = f"""# 张伟销售经验

版本：v1；适用时间：2023-01-01 起；来源：模拟演示。

## 回访

{QUOTE}
这只是虚构员工的经验，不说明冠军身份。
"""


@pytest.fixture
def search(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "DOC-E001.md").write_text(TEXT, encoding="utf-8")
    folder = tmp_path / "knowledge"
    import_collection(source, folder)
    return DocumentSearch(folder)


class QAProvider:
    def __init__(
        self,
        *,
        quote=QUOTE,
        evidence_id=None,
        supported=True,
        status="ok",
        missing=None,
    ):
        self.requests = []
        self.quote = quote
        self.evidence_id = evidence_id
        self.supported = supported
        self.status = status
        self.missing = missing

    async def generate(self, request):
        self.requests.append(request)
        context = json.loads(request.messages[-1].content)
        if "evidence" in context and "missing" in context:
            data = {"supported": self.supported}
        elif "evidence" in context:
            evidence = next(x for x in context["evidence"] if QUOTE in x["text"])
            data = {
                "status": self.status,
                "claims": []
                if self.status == "insufficient_evidence"
                else [
                    {
                        "text": "张伟通过每周回访减少沟通偏差。",
                        "references": [
                            {
                                "evidence_id": self.evidence_id
                                or evidence["evidence_id"],
                                "quote": self.quote,
                            }
                        ],
                    }
                ],
                "missing": self.missing
                if self.missing is not None
                else "没有证明冠军身份的资料。"
                if self.status == "partial"
                else "",
            }
        else:
            assert context["claims"][0]["quotes"] == [self.quote]
            data = {"supported": self.supported}
        return LLMResponse(json.dumps(data, ensure_ascii=False))


def test_retrieval_filters_version_date_scope_and_cache(search):
    evidence, reused = search.search("张伟沟通偏差")
    assert QUOTE in evidence[0]["text"] and not reused
    assert search.search("张伟沟通偏差")[1] is True
    assert search.search("张伟沟通偏差", version="v2")[0] == []
    assert search.search("张伟沟通偏差", doc_ids=["DOC-E002"])[0] == []
    assert search.search("张伟沟通偏差", as_of=date(2022, 1, 1))[0] == []
    assert search.search("张伟沟通偏差", as_of=date(2024, 1, 1))[0]


@pytest.mark.parametrize("change", ["text", "version", "section", "page", "snapshot"])
def test_tampered_evidence_or_snapshot_is_not_accepted(search, change):
    index = search.folder / "index.json"
    collection = json.loads(index.read_text(encoding="utf-8"))
    if change == "snapshot":
        digest = collection["documents"][0]["file_sha256"]
        (search.folder / "originals" / f"{digest}.md").write_text(
            "changed", encoding="utf-8"
        )
    else:
        collection["chunks"][1][change] = "changed"
        index.write_text(json.dumps(collection), encoding="utf-8")
    with pytest.raises(ValueError):
        search.search("张伟沟通偏差")


def test_answer_uses_real_quotes_and_independent_grounding_check(search):
    provider = QAProvider()
    service = DocumentQAService(provider, search)
    result = asyncio.run(service.answer("张伟如何减少沟通偏差？", request_id="qa-1"))
    assert result["status"] == "ok"
    assert "[1]" in result["message"] and "演示文档" in result["message"]
    assert result["citations"][0]["quote"] == QUOTE
    assert result["citations"][0]["version"] == "v1"
    assert result["citations"][0]["page"] is None
    assert result["source_id"] == COLLECTION_ID
    assert len(provider.requests) == 2
    assert [x["service"] for x in result["trace"]] == [
        "retrieval",
        "qa_generation",
        "citation_check",
        "grounding_check",
    ]


def test_no_relevant_evidence_never_calls_model(search):
    provider = QAProvider()
    result = asyncio.run(
        DocumentQAService(provider, search).answer(
            "火星轨道半径",
            request_id="qa-1",
        )
    )
    assert result["status"] == "insufficient_evidence"
    assert not provider.requests
    assert result["citations"] == []
    assert "火星轨道半径" in result["message"]
    assert result["issues"][0]["code"] == "NO_RELEVANT_EVIDENCE"


def test_filtered_no_hit_explains_actual_scope_without_claiming_global_absence(search):
    provider = QAProvider()
    result = asyncio.run(
        DocumentQAService(provider, search).answer(
            "那他呢？",
            query="张伟沟通偏差",
            doc_ids=["DOC-E001"],
            version="v2",
            as_of=date(2024, 1, 1),
            request_id="qa-scope",
        )
    )
    assert result["status"] == "insufficient_evidence"
    assert "待回答的问题：张伟沟通偏差" in result["message"]
    assert "文档编号 DOC-E001" in result["message"]
    assert "版本 v2" in result["message"]
    assert "适用日期 2024-01-01" in result["message"]
    assert "本次检索" in result["message"]
    assert not provider.requests
    # 同一知识库在匹配的版本下仍有证据，筛选无命中不代表全库不存在。
    assert search.search("张伟沟通偏差", version="v1")[0]


def test_specific_missing_evidence_is_checked_before_display(search):
    provider = QAProvider(status="insufficient_evidence", missing="张伟的手机号码")
    result = asyncio.run(
        DocumentQAService(provider, search).answer(
            "根据张伟的回访记录，他的手机号码是多少？", request_id="qa-missing"
        )
    )
    assert result["status"] == "insufficient_evidence"
    assert "尚缺依据的内容：张伟的手机号码" in result["message"]
    assert "来源和版本" in result["message"]
    assert result["issues"][0]["code"] == "MISSING_DOCUMENT_EVIDENCE"
    assert result["citations"] == []
    assert len(provider.requests) == 2
    check = json.loads(provider.requests[1].messages[-1].content)
    assert check["missing"] == "张伟的手机号码"
    assert check["evidence"] == result["evidence"]
    assert result["trace"][-1]["service"] == "missing_check"
    assert result["trace"][-1]["status"] == "ok"


@pytest.mark.parametrize(
    "missing",
    ["张伟的电话是 13800000000", "所有文档都没有联系方式", "张伟是销售冠军"],
)
def test_rejected_missing_explanation_is_not_displayed(search, missing):
    provider = QAProvider(
        status="insufficient_evidence", missing=missing, supported=False
    )
    result = asyncio.run(
        DocumentQAService(provider, search).answer(
            "根据张伟的回访记录，他的手机号码是多少？", request_id="qa-rejected-missing"
        )
    )
    assert result["status"] == "insufficient_evidence"
    assert missing not in result["message"]
    assert missing not in json.dumps(result, ensure_ascii=False)
    assert "待回答的问题：根据张伟的回访记录，他的手机号码是多少？" in result["message"]
    assert "尚缺依据的内容" not in result["message"]
    assert result["citations"] == []
    assert result["trace"][-1]["status"] == "rejected"


@pytest.mark.parametrize("failure", ["invalid_json", "provider", "timeout"])
def test_failed_missing_check_falls_back_to_scoped_insufficiency(search, failure):
    class MissingCheckFails(QAProvider):
        async def generate(self, request):
            context = json.loads(request.messages[-1].content)
            if "evidence" in context and "missing" in context:
                if failure == "provider":
                    raise ProviderAuthenticationError("secret-check-failure")
                if failure == "timeout":
                    raise TimeoutError("secret-check-failure")
                return LLMResponse("not-json-secret-check-failure")
            return await super().generate(request)

    provider = MissingCheckFails(
        status="insufficient_evidence", missing="未核验的缺失说明"
    )
    result = asyncio.run(
        DocumentQAService(provider, search).answer(
            "根据张伟的回访记录，他的手机号码是多少？", request_id="qa-missing-failure"
        )
    )
    assert result["status"] == "insufficient_evidence"
    assert "secret-check-failure" not in json.dumps(result)
    assert "未核验的缺失说明" not in json.dumps(result, ensure_ascii=False)
    assert "待回答的问题：根据张伟的回访记录，他的手机号码是多少？" in result["message"]
    assert result["citations"] == []
    assert result["trace"][-1]["service"] == "missing_check"
    assert result["trace"][-1]["status"] == "error"


@pytest.mark.parametrize(
    "options",
    [
        {"quote": "张伟是销售冠军。"},
        {"evidence_id": "invented-document"},
    ],
)
def test_fake_quote_or_citation_fails_before_grounding_call(search, options):
    provider = QAProvider(**options)
    result = asyncio.run(
        DocumentQAService(provider, search).answer(
            "张伟沟通偏差",
            request_id="qa-1",
        )
    )
    assert result["code"] == "INVALID_QA_RESPONSE"
    assert len(provider.requests) == 1
    assert result["citations"] == []
    assert "冠军" not in result["message"]


def test_rejected_grounding_does_not_show_claim_or_citations(search):
    provider = QAProvider(supported=False)
    result = asyncio.run(
        DocumentQAService(provider, search).answer(
            "张伟沟通偏差",
            request_id="qa-1",
        )
    )
    assert result["status"] == "insufficient_evidence"
    assert result["citations"] == []
    assert QUOTE not in result["message"]
    assert result["issues"][0]["code"] == "ANSWER_GROUNDING_REJECTED"


@pytest.mark.parametrize("status", ["partial", "insufficient_evidence"])
def test_partial_and_insufficient_results_preserve_status(search, status):
    provider = QAProvider(status=status)
    result = asyncio.run(
        DocumentQAService(provider, search).answer(
            "张伟沟通偏差",
            request_id="qa-1",
        )
    )
    assert result["status"] == status
    assert len(provider.requests) == (2 if status == "partial" else 1)
    assert bool(result["citations"]) == (status == "partial")


def test_provider_failure_is_safe(search):
    class FailingProvider:
        async def generate(self, request):
            raise ProviderAuthenticationError("secret-connection")

    result = asyncio.run(
        DocumentQAService(FailingProvider(), search).answer(
            "张伟沟通偏差",
            request_id="qa-1",
        )
    )
    assert result["code"] == "QA_PROVIDER_UNAVAILABLE"
    assert "secret-connection" not in json.dumps(result)


def test_business_flow_routes_qa_without_sql_or_sql_required_time(search):
    class SharedProvider(QAProvider):
        async def generate(self, request):
            context = json.loads(request.messages[-1].content)
            if "current_datetime" in context:
                return LLMResponse(
                    json.dumps(
                        {
                            "problem_type": "qa",
                            "qa_query": "张伟沟通偏差",
                            "doc_ids": ["DOC-E001"],
                            "doc_version": "v1",
                        }
                    )
                )
            return await super().generate(request)

    provider, sql = SharedProvider(), FakeSQL()
    flow = BusinessFlow(
        UnderstandingService(provider),
        sql,
        FixedClock(NOW),
        qa_service=DocumentQAService(provider, search),
    )
    message = request("根据演示文档，张伟如何减少沟通偏差？")
    result = asyncio.run(flow.process_message(message))
    assert result.payload["status"] == "ok"
    assert result.payload["problem_type"] == "qa"
    assert result.payload["request_id"] == message.id
    assert result.payload["citations"]
    assert result.payload["document_scope"]["doc_ids"] == ["DOC-E001"]
    assert result.payload["trace"][0]["service"] == "understanding"
    assert not sql.calls


def test_explicit_multipart_qa_preserves_answerable_claim_and_missing_part(search):
    question = "根据张伟的回访记录，他如何减少沟通偏差？他的手机号码是多少？"

    class MultipartProvider(QAProvider):
        async def generate(self, request):
            context = json.loads(request.messages[-1].content)
            if "current_datetime" in context:
                return LLMResponse(
                    json.dumps(
                        {
                            "problem_type": "qa",
                            "qa_query": question,
                            "issues": [],
                        },
                        ensure_ascii=False,
                    )
                )
            if "evidence" in context:
                assert context["question"] == question
                assert "沟通偏差" in context["resolved_query"]
                assert "手机号码" in context["resolved_query"]
            return await super().generate(request)

    provider = MultipartProvider(
        status="partial", missing="张伟的手机号码缺少原文依据。"
    )
    sql = FakeSQL()
    flow = BusinessFlow(
        UnderstandingService(provider),
        sql,
        FixedClock(NOW),
        qa_service=DocumentQAService(provider, search),
    )
    response = asyncio.run(flow.process_message(request(question)))
    assert response.type == "response"
    assert response.payload["status"] == "partial"
    assert QUOTE in response.payload["message"]
    assert "资料未覆盖：张伟的手机号码缺少原文依据。" in response.payload["message"]
    assert response.payload["citations"][0]["quote"] == QUOTE
    assert response.payload["citations"][0]["doc_id"] == "DOC-E001"
    assert response.payload["issues"] == []
    assert [step["service"] for step in response.payload["trace"]] == [
        "understanding",
        "retrieval",
        "qa_generation",
        "citation_check",
        "grounding_check",
    ]
    assert not sql.calls


def test_genuine_qa_requirement_ambiguity_still_clarifies_before_retrieval(search):
    class AmbiguousProvider(QAProvider):
        async def generate(self, request):
            self.requests.append(request)
            return LLMResponse(
                json.dumps(
                    {
                        "problem_type": "qa",
                        "issues": ["请确认“他”指张伟还是陈敏。"],
                    },
                    ensure_ascii=False,
                )
            )

    provider, sql = AmbiguousProvider(), FakeSQL()
    flow = BusinessFlow(
        UnderstandingService(provider),
        sql,
        FixedClock(NOW),
        qa_service=DocumentQAService(provider, search),
    )
    response = asyncio.run(
        flow.process_message(request("根据演示文档，他如何减少沟通偏差？"))
    )
    assert response.payload["status"] == "needs_clarification"
    assert response.payload["issues"][0]["code"] == "AMBIGUOUS_REQUIREMENT"
    assert response.payload["citations"] == []
    assert len(provider.requests) == 1
    assert len(response.payload["trace"]) == 1
    assert not sql.calls


def test_name_clarification_resumes_full_qa_with_original_citation_context(tmp_path):
    root = Path(__file__).resolve().parents[3]
    import_collection(root / "database" / "documents", tmp_path / "knowledge")
    search = DocumentSearch(tmp_path / "knowledge")
    first_question = "根据演示文档，他是怎么提升合作效率的？"
    resolved_question = "根据演示文档，陈敏是怎么提升合作效率的？"
    method = "陈敏通过渠道伙伴共同制定方案、明确交付责任和季度复盘提升合作效率。"
    pronoun_quote = "她保留方案版本和客户反馈，使承诺与验收标准可以追溯。"
    named_claim = "陈敏保留方案版本和客户反馈，使承诺与验收标准可以追溯。"

    class ResumedQAProvider:
        def __init__(self):
            self.requests = []

        async def generate(self, request):
            self.requests.append(request)
            context = json.loads(request.messages[-1].content)
            if "current_datetime" in context:
                if context["message"] == first_question:
                    return LLMResponse(
                        json.dumps(
                            {
                                "problem_type": "qa",
                                "qa_query": first_question,
                                "issues": ["请确认指哪位员工。"],
                            },
                            ensure_ascii=False,
                        )
                    )
                assert context["message"] == "陈敏"
                assert context["pending_clarification"] is True
                assert context["previous"]["qa_query"] == first_question
                assert context["history"][0]["content"] == first_question
                return LLMResponse(
                    json.dumps(
                        {
                            "problem_type": "qa",
                            "continues_task": True,
                            "qa_query": resolved_question,
                            "issues": [],
                        },
                        ensure_ascii=False,
                    )
                )
            assert context["question"] == resolved_question
            assert context["original_message"] == "陈敏"
            if "evidence" in context:
                chunk = next(
                    row for row in context["evidence"] if method in row["text"]
                )
                return LLMResponse(
                    json.dumps(
                        {
                            "status": "ok",
                            "claims": [
                                {
                                    "text": method,
                                    "references": [
                                        {
                                            "evidence_id": chunk["evidence_id"],
                                            "quote": method,
                                        }
                                    ],
                                },
                                {
                                "text": named_claim,
                                    "references": [
                                        {
                                            "evidence_id": chunk["evidence_id"],
                                            "quote": pronoun_quote,
                                        }
                                    ],
                                },
                            ],
                            "missing": "",
                        },
                        ensure_ascii=False,
                    )
                )
            claim = context["claims"][1]
            assert claim["quotes"] == [pronoun_quote]
            source = claim["citation_contexts"][0]
            assert source["doc_id"] == "DOC-E002"
            assert source["title"] == "陈敏销售经验分享"
            assert source["quote"] == pronoun_quote
            assert method in source["text"] and pronoun_quote in source["text"]
            return LLMResponse('{"supported":true}')

    provider, sql = ResumedQAProvider(), FakeSQL()
    flow = BusinessFlow(
        UnderstandingService(provider),
        sql,
        FixedClock(NOW),
        qa_service=DocumentQAService(provider, search),
    )

    async def scenario():
        first = await flow.process_message(request(first_question))
        assert first.payload["status"] == "needs_clarification"
        assert len(provider.requests) == 1
        second = await flow.process_message(
            request("陈敏", task=first.payload["task_id"])
        )
        assert second.payload["status"] == "ok"
        assert second.payload["task_id"] == first.payload["task_id"]
        assert second.payload["document_scope"]["query"] == resolved_question
        assert len(second.payload["citations"]) == 2
        assert all(row["doc_id"] == "DOC-E002" for row in second.payload["citations"])
        assert method in second.payload["message"]
        assert not sql.calls

    asyncio.run(scenario())


def test_missing_check_uses_full_question_after_name_supplement(search):
    provider = QAProvider(status="insufficient_evidence", missing="张伟的手机号码")
    question = "根据张伟的回访记录，他的手机号码是多少？"
    result = asyncio.run(
        DocumentQAService(provider, search).answer(
            "张伟",
            query=question,
            request_id="qa-supplement-missing",
        )
    )
    assert result["status"] == "insufficient_evidence"
    assert len(provider.requests) == 2
    for call in provider.requests:
        context = json.loads(call.messages[-1].content)
        assert context["question"] == question
        assert context["original_message"] == "张伟"
    assert result["citations"] == []


@pytest.mark.parametrize(
    "reason", ["unsupported_claim", "unresolved_reference", "question_mismatch"]
)
def test_grounding_rejection_still_hides_answer_and_records_bounded_reason(
    search, reason
):
    class RejectedProvider(QAProvider):
        async def generate(self, request):
            context = json.loads(request.messages[-1].content)
            if "claims" in context:
                assert context["claims"][0]["citation_contexts"][0]["quote"] == QUOTE
                return LLMResponse(
                    json.dumps({"supported": False, "reason_code": reason})
                )
            return await super().generate(request)

    result = asyncio.run(
        DocumentQAService(RejectedProvider(), search).answer(
            "张伟",
            query="张伟如何减少沟通偏差？",
            request_id="qa-bounded-reason",
        )
    )
    assert result["status"] == "insufficient_evidence"
    assert result["citations"] == []
    assert QUOTE not in result["message"]
    assert result["trace"][-1]["reason_code"] == reason


def test_partial_claims_and_missing_text_are_hidden_when_grounding_rejects(search):
    provider = QAProvider(
        status="partial", missing="编造的手机号码 13800000000", supported=False
    )
    result = asyncio.run(
        DocumentQAService(provider, search).answer(
            "张伟如何减少沟通偏差？他的手机号码是多少？",
            request_id="qa-partial-rejected",
        )
    )
    assert result["status"] == "insufficient_evidence"
    assert result["citations"] == []
    assert "13800000000" not in result["message"]
    assert QUOTE not in result["message"]
    assert result["issues"][0]["code"] == "ANSWER_GROUNDING_REJECTED"


def test_current_imported_demo_corpus_retrieves_appropriate_originals():
    root = Path(__file__).resolve().parents[3]
    search = DocumentSearch(root / ".local" / "knowledge" / COLLECTION_ID)
    if not (search.folder / "index.json").exists():
        pytest.skip("本机尚未导入演示资料")
    assert search.search("张伟如何减少沟通偏差")[0][0]["doc_id"] == "DOC-E001"
    assert search.search("陈敏如何渠道合作复盘")[0][0]["doc_id"] == "DOC-E002"
    assert search.search("历史退款截止时间")[0][0]["doc_id"] == "DOC-REFUNDS"
