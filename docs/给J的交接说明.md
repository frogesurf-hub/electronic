# 给 J 的交接说明

交接日期：2026-10-06。功能基线提交：`b1065a0`（Complete document import and cited QA workflow）。原开发分支为 `competition/topic8-demo`；用户将把该分支连同本交接说明推送到新仓库的 `main`：`https://github.com/frogesurf-hub/electronic.git`。

这份交接对应比赛查询原型。仓库仍保留原桌面伴侣的历史代码与说明，当前事实以本说明、`PROJECT_STATE.md` 顶部、`docs/COMMUNICATION_PROTOCOL.md` 为准。完整项目尚未完成，跨源融合没有接入。

## 1. 已完成与尚未完成

| 内容 | 当前结果 |
| --- | --- |
| 桌面与传输 | WPF → 本机 WebSocket → Python Agent → 共享模型 Provider → WPF，已跑通 |
| 公共理解与会话 | 提取任务、澄清缺失条件、补全指代；内存会话；桌面新建会话，已跑通 |
| 独立问数 | 复用 H 的 SQL 服务；当前公共入口开放 Chinook 发票销售额汇总及国家／城市／客户编号筛选 |
| 独立文档问答 | 多格式准备、关键词检索、范围过滤、逐字引用及独立依据核验，已跑通 |
| OCR 复核 | 扫描默认待复核；人工对照原件更正、绑定解析摘要后才进入可用检索 |
| 桌面结果 | 表格、实际 SQL、原文引用、条件和执行记录；PDF／图片页码、坐标、表格行、更正及原件路径 |
| 融合 | 理解能识别 `problem_type=fusion`，但公共编排返回 `SERVICE_NOT_READY`；没有融合服务注入点或已定稿的融合计划协议 |
| 评估 | 已有回归与有限人工案例；未完成完整模型效果评估 |

J 负责跨源融合。本轮不替 J 实现规划、依赖执行、实体映射、一致性核验和融合答案；这些的具体范围、接口和团队验收口径仍需确认。H 的 SQL／评估公共模块保留来源结构，不要为接入融合另写一套数据库执行器。

## 2. 先读这些文件

| 文件 | 用途 |
| --- | --- |
| `PROJECT_STATE.md` 顶部 | 最新状态、分工及验收结论；下方有大量历史记录 |
| `docs/目录导航.md` | 找代码与本机产物 |
| `docs/COMMUNICATION_PROTOCOL.md` 顶部比赛部分 | 已实现的消息、会话、响应状态和结果字段 |
| `docs/文档导入与准备.md` | 清单、OCR 更正／复核、校验、选用集合、完整操作流程 |
| `docs/10月7日Demo操作说明.md` | 原电脑的启动／退出示例；其中 F:、P: 和已准备环境不能直接照搬到 J 的电脑 |
| `docs/SQL接入方案.md`、`database/SQLService使用说明.md` | H 的服务边界和来源；早期步骤说明不是最新验收状态 |

## 3. 当前代码入口与可复用接口

正常链路：

```text
WPF → chat 消息 → WebSocketServer → Agent → BusinessFlow
  → UnderstandingService → 校验／必要时澄清
  → SQLGenerationService 或 DocumentQAService
  → 统一结果 → 原传输 → WPF
```

### 公共入口与模型

- `src/agent_core/main.py`：创建共享 Provider、Clock、活动文档检索、理解、SQL 和问答服务。Provider 的关闭由入口负责。
- `src/agent_core/core/business_flow.py`：统一会话、需求理解、支线路由和结果组装。融合目前在未就绪分支中；J 的服务接入需在这里新增明确依赖，再由 `main.py` 装配。
- `src/agent_core/understanding/service.py`：`Interpretation` 有 `problem_type`、SQL 条件、`qa_query`、`doc_ids`、`doc_titles`、`doc_version`、`doc_as_of`。当前结构没有 `fusion_plan`，不要假定已经存在。
- `src/agent_core/providers/`：共享 `LLMProvider.generate(LLMRequest) -> LLMResponse` 异步接口；消息结构为 `LLMMessage(role, content)`，结果正文为 `.content`。融合应通过该接口调用模型，避免各自读取密钥或重复维护厂商客户端。
- `src/agent_core/sessions/store.py`：会话只在内存；请求、会话、任务编号分别是 `request_id`、`session_id`、`task_id`。新建会话／重启后不能继续借用旧任务条件。

### H 的 SQL 服务

真实入口为 `src/agent_core/sql/generation_service.py` 的 `SQLGenerationService`，不是 `sql/service.py`。构造时传共享 Provider；异步调用 `query(query, context)`。

目前公共入口确认后传入的查询形状：

```json
{
  "source_id": "chinook_db",
  "metric_id": "sales_amount",
  "metric_definition_id": "chinook_sales_amount_v1",
  "time_range": {"start": "2024-01-01", "end_exclusive": "2025-01-01"},
  "filters": [{"field_id": "country", "op": "eq", "value": "USA"}]
}
```

`context` 带 `session_id/task_id/request_id/step_id`；限额和超时以 H 的服务实现为准。结果包括 `status`、`issues`、`source_id`、`metric_definition_id`、`columns`、`rows`、`sql`、`parameters`、`tables`、`truncated`、`execution_attempted` 等，先判断状态再使用数据。

`generate()` 只生成候选，`generate_and_validate()` 只表示检验；只有 `query()` 在检验和执行成功后返回的结果才能作为真实查询结果。当前公共入口不开放利润、排名、分组、任意 JOIN、明细或自由 SQL，不能因底层存在其他模块便视为已接通。融合若依赖新查询类型，要先与 H 对齐并扩展公共理解及校验。

### 文档检索与问答

- `retrieval/parsing.py`、`preparation.py`：离线文件准备；解析不在每轮问答中重新执行。
- `retrieval/selection.py`：验证并选用活动集合，配置在 `.local/knowledge/active.json`。
- `retrieval/search.py`：`ActiveDocumentSearch` 每轮读取活动集合，兼容旧 schema 1 和准备集合 schema 2。
- `retrieval/qa_service.py`：`DocumentQAService` 使用共享 Provider 和检索对象。

检索接口是同步的：

```python
evidence, reused = search.search(
    question,
    doc_ids=[...],
    doc_titles=[...],
    version=version_or_none,
    as_of=date_or_none,
)
```

`as_of` 是 Python `date`；过滤先于关键词打分。最多返回 4 个片段，`reused` 表示缓存复用，不能当成“答案已核验”。缓存绑定已验证集合／内容／范围；每次仍校验原件和解析快照。一次只查当前选用集合，配置或快照损坏会报错，不回退其他集合。

问答接口是异步的：

```python
result = await qa.answer(
    original_message,
    query=resolved_question,
    doc_ids=doc_ids,
    doc_titles=doc_titles,
    version=doc_version,
    as_of=doc_as_of,
    request_id=request_id,
)
```

结果带 `status/message/citations/evidence/source_id/issues/trace`；失败时还可能有安全错误码 `code`。`evidence` 是检索结果，`citations` 才是该次答案通过逐字摘录与依据核验的引用。融合若直接使用 `search.search()`，仍需自行核验最终结论，不能把命中片段等同于已核验答案。

引用须保留 `evidence_id/doc_id/title/version/source/collection_id/section/quote` 及原有位置字段：文本 `line_start/line_end`；PDF／图片 `page/bbox/bbox_units`；表格 `table_id/row_number/header_bbox`；OCR `review_status/correction_note`；还有 `original_path` 与摘要。不要重编编号、伪造页码或丢弃来源。

## 4. 融合接入时要先对齐的事项

1. 确认一个可验收的混合问题、所需指标／文档及两侧实体映射。现有张伟／陈敏模拟员工文档不对应 Chinook 客户或员工，检测 PDF 的 17 也不是业务金额，不能直接拼接或据此解释 127.98。
2. 确认 J 的调用签名、计划结构、执行依赖、超时／取消、缺失／冲突和部分结果规则。建议单独模块，由 `BusinessFlow` 注入并路由；这是接入建议，不是已经实现的接口。
3. 保留外层 `id/type/timestamp/source/payload` 和 `chat` 请求。公共响应状态沿用 `ok/partial/needs_clarification/insufficient_evidence/error`，编号和实际执行 trace 沿用公共入口；新增融合字段先更新协议与前端约定。
4. SQL 的 `chinook_db` 与文档集合来源分别保留。现有单支线 `source_id` 不足以表达所有混合来源，融合多来源字段须另行设计，不能将混合结果统一标成其中一个来源。
5. 当前 WPF 能显示文字、单张结果表、单份 SQL 内容和引用列表；多步骤／多表／多 SQL 的展示没有实现，需要另行约定，不要只改后端就宣称前端支持。
6. 逐步验证：先用替身和固定样例检查规划／依赖／失败／无证据／冲突，再接真实 SQL 与文档；最终通过同一个 WPF 自然语言入口验收。继续保留 SQL 只读检验、引用连续性和独立依据核验。

文档适用日期开始包含、结束不包含；未知起始日不能满足明确适用日筛选。适用日只限定文档版本范围，不能证明事件发生于该日。理解阶段目录仅作身份匹配，不预判证据缺失；缺失应由实际检索和问答返回。明确但仅部分有依据的复合问题应保留全部子问题，按可支持内容返回部分结果。

## 5. J 拉取后如何启动

### 基础环境与最短文档问答路径

需要 Windows、Git、Python 3.11；安装脚本准备 Agent 依赖和 .NET 8，需联网。J 在自己的工作目录执行：

```powershell
git clone --branch main https://github.com/frogesurf-hub/electronic.git
Set-Location -LiteralPath .\electronic
powershell -ExecutionPolicy Bypass -File .\scripts\setup.ps1
```

安装脚本会从 `.env.example` 创建不存在的 `.env`。J 在本机填写自己的模型配置，至少包括 `DCA_DEEPSEEK_API_KEY`；不要提交 `.env`。首次搭建不能复用原电脑的加密连接配置或绝对路径。

先用仓库内的旧 Markdown 来源验证问答，不需要 PostgreSQL 或 PDF 环境：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\import-demo-documents.ps1
powershell -ExecutionPolicy Bypass -File .\scripts\start-core.ps1
```

没有活动选择文件时默认查 `legacy_sales_demo`。后端窗口保持运行，另一个 PowerShell 在同一仓库根目录执行：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\start-desktop.ps1
```

连接后提问“根据演示文档，张伟如何减少沟通偏差？”，检查原文引用。真实理解和问答需要模型联网；文档解析／OCR 不调用模型。

### 重建多格式检测集合

新仓库不包含原电脑 `.local/document-fixtures`、导入索引或 OCR 复核摘要。J 可建独立文档工具环境，不改变 Agent 的 `.venv`：

```powershell
py -3.11 -m venv .\.local\document-tools
& .\.local\document-tools\Scripts\python.exe -m pip install -r .\requirements-documents.txt
powershell -ExecutionPolicy Bypass -File .\scripts\setup-document-tools.ps1 -PythonPath .\.local\document-tools\Scripts\python.exe
& .\.local\document-tools\Scripts\python.exe .\scripts\create_document_fixtures.py --target .\.local\document-fixtures
powershell -ExecutionPolicy Bypass -File .\scripts\import-documents.ps1 -Manifest .\.local\document-fixtures\manifest.json
```

PDF／图片解析依赖按 `requirements-documents.txt` 安装；扫描另需 Windows PowerShell 5 与对应系统 OCR 语言，配置脚本会列出语言。不要假定 J 已安装原电脑的中文 OCR 包。生成器只写空目录，不覆盖已有资料。

新生成资料复核标记为空，首轮预期 3 可用、2 待复核；实际 OCR 文字与摘要可能因系统环境不同而变化。按 `docs/文档导入与准备.md` 对照原件、更正、重导入、复核并填写当前摘要；不能照抄原电脑的哈希，也不能未检查便清除复核状态。文字 PDF 可在扫描复核完成前单独测试。

完成复核后：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\import-documents.ps1 -Verify .\.local\knowledge\import_detection
powershell -ExecutionPolicy Bypass -File .\scripts\import-documents.ps1 -UseCollection import_detection
```

不同机器的片段数量和 OCR 内容需以本机校验为准。未来业务资料建议独立放 `.local/business-input`，配独立清单／集合；把文件放进目录不会自动导入。再次导入按完整清单重建当前集合，不是增量追加。

### 问数环境

Git 中有 Chinook 固定 SQL 源文件和 H 的程序，但没有原电脑的 PostgreSQL 安装、实际集群、只读角色密码及 Windows DPAPI 配置。J 需要自行准备本机 PostgreSQL 18、独立项目集群和只读连接，再按数据库说明导入、核对数据和启动服务。`scripts/setup-chinook.ps1` 是已准备项目数据库上的导入入口，不会替 J 下载／初始化整个 PostgreSQL 环境。

先用 `scripts/check-sql.ps1` 做离线 SQL 接线复查；实际数据库准备好后再跑 `scripts/check-model-sql.ps1` 和自然语言桌面问题。不能在没有数据库时将连接失败误判为融合失败。

## 6. 已验收案例与复查入口

原电脑用户六项实际文档问答均通过：

| 问题范围 | 实际结果 |
| --- | --- |
| 《文字与表格》test-v1 的检测计数 | 17；第 2 页、P2-T1 表格第 3 行、PDF 坐标、来源与原件路径 |
| 《扫描图片》检查文字和引用位置那句话 | “检查识别文字和引用位置”；第 1 页、像素位置、OCR 已复核与更正记录 |
| 只用 test-v2 | 资料不足，保留指定版本，未借用 v1 |
| 只用《不存在的文件》 | 资料不足，保留标题范围，未扩大到其他文档 |
| 依据 2022-01-01 适用的《文字与表格》 | 资料不足，直接检索过滤而非澄清 |
| 依据 2024-01-01 适用的《文字与表格》 | 17，恢复正确页码与表格引用 |

日期首次验收失败后修补了理解目录边界与 QA 日期／表格校验说明，重启后的两题复验通过。此结论只覆盖上述有限案例，不代表复杂业务资料或完整模型质量评估。

问数原电脑已验收 2024 年美国发票销售额 127.98，以及补充时间后的同一结果；口径为 `SUM(invoice.total)`，币种未指定，不推断利润或退款。

可在准备好 Agent 环境后执行以下离线回归（最终日期修补时 97 项通过）：

```powershell
& .\.venv\Scripts\python.exe -m pytest src/agent_core/tests/test_prepared_retrieval.py src/agent_core/tests/test_document_qa.py src/agent_core/tests/test_business_flow.py src/agent_core/tests/test_main.py -q
powershell -ExecutionPolicy Bypass -File .\scripts\check-sql.ps1
```

依赖本机 PDF／扫描产物的用例在集合未重建时会跳过，要看测试的 skipped 数量，不能将跳过当作通过。准备模块检查入口为 `src/agent_core/tests/test_document_preparation.py`；桌面基础验证入口为 `scripts/verify.ps1`，引用离线检查项目为 `tools/CitationDisplayCheck/`。历史记录还包含 139 项相关回归、中文管道修补的准备模块 21 项和 WPF 编译／引用检查，不是本次在 J 电脑上重新运行的结果。

## 7. Git 交接范围

源码、脚本、清单格式、测试、Chinook 固定资产与说明随仓库交接。`.env`、`.venv/`、`.tools/`、`.local/`、`.cache/`、`logs/` 和本机加密凭据均不交接；生成器和导入脚本用于在 J 电脑重建本地产物。

用户推送完成后，J 拉取 `main`，记录实际提交号再开展接入。融合交付应说明新增接口／字段、来源与实体对应、缺失／冲突行为、替身和真实验收结果；不要以固定模型替身的成功代替实际模型或数据库验收。
