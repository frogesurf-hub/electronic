# Desktop Companion Agent - Project State

> 比赛分支：`competition/topic8-demo`，基线 `30c6bbd`。截止计划：10 月 7 日当天最小 Demo，10 月 20 日交付版，10 月 25 日初赛截止。当前用户要求逐步指导其操作，不能自行推进下一阶段。

> 最新状态（2026-10-05）：原助手 WPF/WebSocket/真实 DeepSeek 对话已验收；队员 SQL 模块导入 93 个文件并离线验证（107 项测试、244 子检查、49 项原助手相关测试）；导入保护脚本 9 项回归测试通过。用户已部署 PostgreSQL 18.6、导入 Chinook 11 表/15,607 行、建立 chinook_reader，只读固定查询和真实 DeepSeek SQL 查询均返回 127.98。后续公共理解／澄清／会话及 WPF 问数接线完成，57 项针对性测试和编译通过；用户已验收完整自然语言问题以及“美国销售额是多少？→追问时间→仅补充2024年→127.98”两条真实链路。此结论限于一个已知查询案例，不代表完整模型效果评估。5 份模拟文档已导入；引用问答接线完成，58 项针对性检查与编译通过，用户已验收张伟经验问答与原文引用，资料不足案例也已通过用户验收；融合仍未接入。

> 本机数据库：`.tools/pgsql` 保存程序，`.local/postgres/data` 保存数据；中文路径初始化失败已用用户创建的 `subst P:` 项目别名绕过，PostgreSQL 命令从 `P:\` 运行。管理员及只读连接采用本机 Windows DPAPI 加密配置，助手不读取或打印密码。用户已保存 Git 检查点 `daf908b`（Integrate SQL pipeline and verify live DeepSeek query）。恢复上下文先读本页最新分工、`docs/比赛迁移记录.md` 和 `docs/目录导航.md`；以下 Phase 3 内容仅为原助手历史。

## 比赛架构与当前工作范围（2026-10-05 确认）

- 统一依据：`F:/01_Projects/School/竞赛/电子杯/框架/项目架构与运行说明.md` 及整体、问答、问数、跨源融合四张运行图。说明中的橙色“你负责”指 SQL 交付方 H，不能按当前用户理解；蓝色也不代表当前用户承担所有模块。
- 用户明确负责：WPF 前端、共享 LLM 接入、公共理解／语义校验／澄清／会话，以及此前明确的文档问答运行。统一 Agent 调度、业务结果协议与前端收发是这些模块的接线范围。
- H：数据库与 SQL 服务；架构说明将全项目效果评估分配给 SQL 交付方，沿用此约定。用户认为文档导入／解析／检索应该由 H 负责，但其归属及接口尚未确认，不能写成已经交付。现有包没有可调用的文档检索服务；拥有 PostgreSQL 不等于已有文档知识库。
- J：跨源融合；其具体交付范围尚未明确。当前用户提供公共 LLM 能力、共享请求／结果接口和 WPF 展示，不自行实现融合规划、依赖执行、实体关联及一致性检查。
- 正常信息流：WPF → 现有消息与 WebSocket → 统一 Agent → 公共理解与语义校验 → 需要时澄清并保存任务 → 已确认需求 → SQL／问答／J 的融合服务 → 统一结果组装 → 原传输链路 → WPF 展示。不得把直接提交 SQL 参数的联调示例替代公共理解与澄清流程。
- 问答运行接收确认后的问题和带来源／位置／版本的原文证据，按图处理证据复用、检索请求、证据是否充足、证据提示词、LLM 答案、引用核验以及不足／冲突结果。检索实现的负责人需明确；对接文档检索时不能用 SQL 测试结果或模型自行生成的文字充当文档证据。
- SQL 接入使用已有异步 `SQLGenerationService.query(query, context)`，保留其只读校验和执行边界，不再另写 SQL 生成、校验或数据库执行。共享 Provider 可复用；SQL 提示词仍在 H 的服务内，融合提示词归 J 的流程。
- 消息外层沿用 `id/type/timestamp/source/payload`，保留 `payload.message` 兼容文字显示；业务结果区分成功、澄清、证据不足、部分结果及错误，使用 session/task/request/step 标识关联实际执行记录。
- 随后用户授权继续断点，并希望今天跑通文档导入。本阶段先完成公共入口与 WPF 问数接线：新增 understanding/service.py、sessions/store.py、core/business_flow.py；生产入口注入统一业务流程，沿用现有 Provider、Clock、WebSocket 和 H 的 SQL 服务，比赛业务请求不使用旧陪伴角色提示词。保留旧助手组件供后续整理，不扩展其功能。
- 当前仅开放发票销售额汇总，必填时间、指标和筛选校验后才调用 SQL。澄清保存会话／任务，原服务返回的澄清也统一转交；分组、排名、跨表、明细等超出首阶段范围的要求显式拒绝，不静默降级。问答与融合只识别类别并返回 SERVICE_NOT_READY，不能当作已交付功能。
- WPF 新增 session_id／澄清 task_id，等待回复期间禁用重复发送，并显示结果表格、实际 SQL、条件及实际服务调用记录。会话最多 128 个、每会话保留 6 轮对话，内存存储，重启／容量淘汰后需要重新提问。
- 本阶段 57 项公共入口及原 Agent／主入口／Provider 错误／WebSocket 针对性测试通过；修改的 Python 文件 Ruff 检查通过，7 个运行文件 mypy 通过；WPF 编译通过（0 警告、0 错误，输出在 .cache/wpf-build，未启动或关闭用户界面）。尚未调用真实模型验收自然语言，用户原数据库和后台进程未被修改／重启。
- 当前停在人工运行验收前：指导用户先结束旧后端再运行 scripts/start-core.ps1，然后关闭旧桌面并运行 scripts/start-desktop.ps1；新会话先问“美国销售额是多少？”，应追问时间，再回答“2024 年”，应查询 127.98 并展示 SQL。完成这一步后继续今天的文档导入及问答对接，不自行完成后续阶段。工作区修改尚未提交，不自动操作受限 .git。
- 最新人工验收：用户已重启后端与新版 WPF；“美国销售额是多少？”返回时间澄清，模型 HTTP 200。补充“2024”／“2024年”均返回数据库连接超时。诊断发现没有 postgres 进程／5432 监听，pg_isready 无响应；server.log 在 16:54:15 记录 0xC000013A 终止、fast shutdown 和 ^C，与停止旧后端时间一致，推断为共享控制台 Ctrl+C 连带关闭 PostgreSQL。下一步由用户通过 Start-Process pg_ctl（WindowStyle Hidden，新控制台，不用 NoNewWindow）重新启动现有集群，再用 pg_isready 检查；无需重新导入数据或修改连接时限。恢复后发送完整“2024 年美国销售额是多少？”验收，仍不能宣称真实自然语言 SQL 查询已通过。

- 最新人工验收完成：独立恢复 PostgreSQL 后，用户先发送完整“2024 年美国销售额是多少？”，返回 127.98、正确时间区间及口径；随后再次问“美国销售额是多少？”，系统没有自动继承已完成任务的时间，而是追问。用户仅补充“2024年”，截图再次显示 127.98 和查询成功。因此完整问题及澄清恢复的已知案例均已通过；数据库保持独立控制台运行。
- 当前断点改为文档导入：用户明确选择先用 H 的 5 份模拟 Markdown 跑通独立导入与问答。文件采用旧订单／支付／退款口径和 EMP-001 等虚构实体，不能覆盖 Chinook 发票口径或用于同实体融合。统一标记 collection_id/data_domain=legacy_sales_demo，不修改 H 的原文件或 SQL 模块，不创建新的业务制度。
- 已准备 src/agent_core/retrieval/documents.py 与 scripts/import-demo-documents.ps1：读取 UTF-8 Markdown，按章节／长度切片，保存版本、来源、适用时间、章节与实际行号，按原文件 SHA-256 保存内容快照，创建关键词索引。产物为 .local/knowledge/legacy_sales_demo/index.json 和 originals/<sha256>.md，本机忽略目录；写入前完成资料校验，索引原子替换，重复导入片段编号保持稳定，新版本保留旧快照。
- 用户已运行 import-demo-documents.ps1，截图确认 status=ok、mode=import、5 文档、10 片段、334 关键词，索引写入 .local/knowledge/legacy_sales_demo/index.json。原文与章节／行号／版本已就绪；仍只处理 Markdown，PDF／扫描 OCR 未实现。
- 下一小步代码已完成：retrieval/search.py 提供关键词检索、文档／版本／适用日期过滤和同索引／查询／范围的证据缓存；读取时核对原文 SHA-256、片段文本、标题、章节、版本和行号，禁止编造 Markdown 页码。retrieval/qa_service.py 复用共享 Provider 生成逐条带原文引用的答案，先核验引用编号与逐字摘录，再独立调用模型核对结论依据；支持资料不足和部分结果，未通过核验的结论不展示。
- 公共理解新增 qa_query/doc_ids/doc_version/doc_as_of，并通过统一 BusinessFlow 调用问答支线；问答不强制 SQL 指标／时间，也不访问 SQL 数据库。WPF 新增“原文引用”页签，显示文档名、版本、章节／行号、原文与模拟来源，执行记录显示实际检索、答案生成和核验调用；J 的融合仍未接入。
- 本阶段 58 项针对性检查通过（问答／检索、导入、公共编排、主入口），包含用户实际导入集合的只读检索检查；Ruff、8 个运行文件 mypy 通过，WPF 编译 0 警告／0 错误。测试没有真实模型调用，模型核验不保证完整忠实度，目前不发布质量分数。
- 用户已验收真实文档问答案例“根据演示文档，张伟是如何减少沟通偏差的？”：WPF 显示查询成功，两条结论附原文引用，文档为《张伟销售经验分享》v1，章节“客户需求与交付”、第 5–10 行，来源为 legacy_sales_demo。此结果仅证明这一案例的问答与引用展示链路跑通。
- 用户已验收资料不足案例“根据演示文档，张伟的手机号码是多少？”：系统说明现有演示文档不足以回答、请补充资料，WPF 显示“资料不足”，原文引用栏为空，没有编造号码。该阶段已由用户保存 Git 检查点 b5d2ceb（Add unified clarification and document QA with citations），当时工作区干净。
- 用户关闭再启动数据库、后端与 WPF 后，完整自然语言问数仍返回 127.98，文档问答仍显示正确原文引用，未重新导入数据或索引。随后追问“那陈敏是怎么提升合作效率的？”成功沿用文档问答上下文、切换为《陈敏销售经验分享》v1 的两条原文证据。上述结论限于已知案例，不代表完整多轮效果评估。
- 当前小步：WPF 新增“新建会话”，更换 session_id 并清除待澄清 task_id、聊天、输入、表格、SQL、引用及执行记录，保持连接；等待回复时禁用，旧会话回复不再进入当前会话。仅修改桌面层 5 个文件，未修改后端或 H/J 模块。WPF 编译 0 警告、0 错误；用户已重开前端并验收：新建会话后 Connected 保持，聊天已清空，只输入“2024年”返回当前支持范围提示、状态“未能完成”，没有沿用旧的美国销售额任务，也没有返回 127.98。本小步待用户保存 Git 检查点。

> Context checkpoint: Phase 3 completed on 2026-09-13.
>
> This file is the primary context-recovery document for future development sessions. If chat context is lost, read this file first, then `docs/PHASE_3_CHECKPOINT.md`, `ARCHITECTURE.md`, `ROADMAP.md`, and the relevant subsystem design / ADR documents.

## 1. Current Status

Current phase status:

- Phase 0 - Engineering Foundation / Cross-Language Vertical Slice: **Complete**
- Phase 1 - LLM Provider / Original AI MVP: **Complete**
- Phase 2 - Event System: **Complete**
- Phase 3 - Character System: **Complete**
- Next roadmap phase: **Phase 4 - Memory System**

Current implementation baseline before the Phase 3 final-documentation commit:

```text
a172a43 Update Agent integration test fixtures
```

Phase 3 added a structured Character System, runtime temporal context, and provider-neutral prompt/context composition without replacing the verified Provider/WebSocket/EventBus runtime.

The verified real vertical slice is now:

```text
WPF UI
  -> MainWindowViewModel
  -> AgentClientService
  -> IAgentConnection
  -> WebSocketAgentConnection
  -> JSON / AgentMessage
  -> WebSocket
  -> Python WebSocketServer
  -> Message
  -> Agent.process_message()
  -> Clock.now()
  -> TemporalContext
  -> active CharacterDefinition
  -> PromptContextComposer
  -> LLMRequest
  -> LLMProvider
  -> DeepSeekProvider
  -> DeepSeek API
  -> LLMResponse
  -> protocol response
  -> WPF UI
```

Alongside that request/response path, the Python runtime continues to own the in-process EventBus foundation introduced in Phase 2.

## 2. What Phase 3 Proved

Phase 3 proved that stable Character identity and runtime temporal truth can participate in the real LLM request path without collapsing Provider, Event, factual-state, or future Memory boundaries.

Verified properties:

- Character definition is explicit domain data rather than an ad-hoc prompt blob.
- TOML is a human-editable external serialization format, not the Character domain model.
- the default built-in Character is `Aria`.
- runtime Character selection belongs to the composition root.
- external Character definition directories and active Character IDs are configurable.
- Character does not own Memory.
- Character does not own Internal State.
- Character intent does not grant permission or tool authority.
- Character fiction does not override mathematics, science, runtime facts, user facts, or tool results.
- `Clock` is the sole runtime source of current date/time truth.
- `TemporalContext` is a derived per-request snapshot.
- `PromptContextComposer` consumes prepared context and produces a provider-neutral `LLMRequest`.
- Composer does not load Character, query Clock, query Memory, call Provider, publish Events, permission-check, or execute tools.
- Agent receives Character / Composer / Clock through explicit dependency injection.
- Provider error mapping remains unchanged.
- unsupported Desktop messages are still rejected before Provider execution.
- Character loading failure does not create Provider or EventBus resources.
- the real WPF -> Python -> DeepSeek -> Python -> WPF path works with Character and Temporal Context active.
- manual runtime acceptance confirmed Aria identity/style, mathematically correct `sqrt(2)` reasoning, correct runtime date/weekday, and factual-reality priority over Character fiction.
- deliberate Provider authentication failure and sensitive-log review remained safe after Character integration.

Phase 3 deliberately did **not** implement Memory persistence, User Profile learning, Internal State, Scheduler, Perception, Situation, Attention, Behavior, Permission, Tools, Voice, Avatar, dynamic Character switching UI, Character hot reload, or a Desktop Event Bridge.

## 3. Current Implemented Architecture

### 3.1 Python Agent Core

Entry point:

```text
src/agent_core/main.py
```

Current composition:

```text
main.py
  -> get_settings()
  -> setup_logging()
  -> load / resolve active Character
  -> create PromptContextComposer
  -> create SystemClock
  -> create DeepSeekProvider
  -> establish Provider cleanup boundary
  -> create EventBus(queue_capacity=Settings)
  -> create Agent(provider, character, composer, clock)
  -> create WebSocketServer(agent)
  -> await EventBus.start()
  -> await WebSocketServer.run()
  -> finally await EventBus.close()
  -> finally await Provider.aclose()
```

Implemented modules now include:

```text
src/agent_core/
├── characters/
│   ├── definitions/
│   │   └── aria.toml
│   ├── errors.py
│   ├── loader.py
│   └── models.py
├── communication/
│   └── websocket_server.py
├── composition/
│   └── composer.py
├── config/
│   └── settings.py
├── core/
│   ├── agent.py
│   └── message.py
├── events/
│   ├── __init__.py
│   ├── base.py
│   ├── bus.py
│   ├── errors.py
│   └── models.py
├── observability/
│   └── logging.py
├── providers/
│   ├── __init__.py
│   ├── base.py
│   ├── deepseek.py
│   ├── errors.py
│   └── models.py
├── temporal/
│   ├── clock.py
│   └── models.py
├── tests/
└── main.py
```

Implemented behavior includes:

- typed runtime settings via `pydantic-settings`
- `.env` / environment-variable support with `DCA_` prefix
- secrets represented with `SecretStr`
- `pyproject.toml` as the canonical Python dependency source
- OpenAI Python SDK isolated inside the DeepSeek adapter boundary
- console + rotating-file logging
- local WebSocket server
- JSON message parse / serialize
- malformed-input isolation
- async provider-neutral `LLMProvider` contract
- DeepSeek OpenAI-compatible Chat Completions adapter
- provider-neutral error hierarchy / safe Desktop error mapping
- explicit Provider timeout / zero SDK retries / asyncio cancellation propagation
- immutable Runtime Event foundation
- asynchronous bounded in-process EventBus
- exact-type Event routing
- fail-fast overload admission
- explicit EventBus lifecycle / graceful drain
- subscriber concurrency + ordinary failure isolation
- safe Event lifecycle observability
- structured `CharacterDefinition` domain
- human-editable TOML Character definitions
- built-in default Character `Aria`
- runtime active-Character selection through Settings
- explicit `Clock` / `SystemClock` temporal authority
- derived per-request `TemporalContext`
- provider-neutral `PromptContextComposer`
- runtime rules that preserve factual truth over Character fiction
- explicit Agent injection of Character / Composer / Clock
- composition-root ownership of Character selection, EventBus, and Provider lifetimes

### 3.2 Current DeepSeek Baseline

Configuration:

```text
DCA_MODEL_PROVIDER=deepseek
DCA_DEEPSEEK_API_KEY=<local secret>
DCA_DEEPSEEK_MODEL=deepseek-flash
DCA_DEEPSEEK_TIMEOUT_SECONDS=60
DCA_DEEPSEEK_THINKING_ENABLED=false
```

Provider implementation baseline:

```text
client: openai.AsyncOpenAI
base URL: https://api.deepseek.com
API style: OpenAI-compatible Chat Completions
streaming: false
thinking: explicitly disabled by default
application timeout: 60 seconds by default
SDK max_retries: 0
```

`Agent` does not import `AsyncOpenAI`, DeepSeek HTTP status types, or vendor SDK exceptions.

### 3.3 Provider Contract

Agent-facing contract:

```python
async def generate(request: LLMRequest) -> LLMResponse
```

Provider-neutral models:

```text
LLMMessage
LLMRequest
LLMResponse
LLMRole
```

Current supported roles:

```text
system
user
assistant
```

Provider error boundary:

```text
LLMProviderError
├── ProviderConfigurationError
├── ProviderAuthenticationError
├── ProviderQuotaError
├── ProviderRateLimitError
├── ProviderTimeoutError
├── ProviderConnectionError
├── ProviderRequestError
├── ProviderUnavailableError
└── ProviderResponseError
```

Phase 1-3 runtime performs zero automatic Provider retries.

### 3.4 Event Contract

Base Runtime Event:

```text
RuntimeEvent
├── source: str
├── event_id: UUID
├── occurred_at: timezone-aware UTC datetime
├── correlation_id: UUID | None
└── causation_id: UUID | None
```

Contract properties:

- frozen dataclass
- slot-based
- keyword-only construction
- default `event_id` generated with UUID
- default occurrence time generated in UTC
- aware timestamps normalized to UTC
- naive timestamps rejected
- blank `source` rejected
- Event-specific payload fields are added by concrete Event subclasses

Public capability boundary:

```python
class EventPublisher(Protocol):
    async def publish(self, event: RuntimeEvent) -> None: ...
```

Subscriber boundary is an asynchronous callable typed to one Runtime Event type.

### 3.5 EventBus Baseline

Phase 2 EventBus semantics:

```text
scope: in-process Python runtime
execution: async
queue: bounded in-memory queue
routing: exact concrete Event type
subscription registration: static before running
publish result: queue admission only
queue-full behavior: fail fast with EventBusFullError
ordering: queue admission order
subscriber execution for one Event: concurrent
cross-Event execution: next Event waits until current handlers settle
ordinary handler failure: isolated
persistence: none
wildcards: none
priority: none
retry: none
multiple dispatcher workers: none
restart policy: none
```

Lifecycle:

```text
NEW
  -> RUNNING
  -> CLOSING
  -> CLOSED
```

Important lifecycle properties:

- publication is accepted only while the bus is RUNNING
- `close()` drains already accepted Events
- repeated close is safe under the accepted Phase 2 semantics
- close from NEW is supported
- terminal cleanup leaves no intended live EventBus tasks
- cancellation remains distinct from ordinary handler failure

### 3.6 Event Observability

EventBus logs diagnostic metadata such as:

- lifecycle transition
- Event type
- Event ID
- sanitized source
- handler identity where useful
- handler / dispatch duration where useful
- exception type for failure diagnostics

Default Event logs must not include:

- `repr(event)`
- full Event payloads
- subscriber exception messages
- dispatcher exception messages
- arbitrary sensitive payload content

Tests explicitly verify known sensitive markers do not appear in captured Event System logs.

### 3.7 C# WPF Desktop

Current Desktop layering remains:

```text
App.xaml.cs                 Composition Root
      |
      v
MainWindow                  Presentation view
      |
      v
MainWindowViewModel         Presentation state / interaction
      |
      v
AgentClientService          Application-level Agent client behavior
      |
      v
IAgentConnection            Transport abstraction
      |
      v
WebSocketAgentConnection    WebSocket + JSON transport
      |
      v
AgentMessage                Protocol model
```

The independent Desktop receive loop remains an intentional long-term capability for future proactive messages.

Phases 2 and 3 did not modify C# source.

## 4. Current Protocol

Transport:

```text
ws://127.0.0.1:8765
```

Implemented Desktop protocol message behavior:

- `chat`
- `response`
- `error`

Provider error payloads contain stable safe `code` / `message` fields.

Documented but not implemented end-to-end:

- Desktop `event`
- `permission_request`
- `tool_request`
- `tool_response`
- memory / emotion / avatar events

Important distinction:

> Python Runtime Events are an internal in-process architecture boundary. They are not automatically Desktop protocol messages.

A Desktop Event Bridge remains deferred until a concrete later-phase requirement exists.

## 5. Configuration and Dependency Baseline

Python configuration currently includes:

```text
DCA_ENVIRONMENT
DCA_RUNTIME_MODE
DCA_WEBSOCKET_HOST
DCA_WEBSOCKET_PORT
DCA_EVENT_BUS_QUEUE_CAPACITY
DCA_CHARACTER_DEFINITIONS_DIR
DCA_ACTIVE_CHARACTER_ID
DCA_MODEL_PROVIDER
DCA_DEEPSEEK_API_KEY
DCA_DEEPSEEK_MODEL
DCA_DEEPSEEK_TIMEOUT_SECONDS
DCA_DEEPSEEK_THINKING_ENABLED
DCA_LOG_LEVEL
```

EventBus queue capacity:

```text
default: 256
constraint: > 0
role: operational configuration, not an architecture constant
```

Important:

- real `.env` files are ignored and must never be committed
- API keys remain local secrets
- `pyproject.toml` is the canonical Python dependency source
- `src/agent_core/requirements.txt` remains legacy duplicate metadata and must not receive new dependencies
- Phases 2 and 3 added no third-party dependency

## 6. Validation Baseline

### Phase 1 checkpoint

Phase 1 final acceptance on 2026-09-09:

```text
66 pytest tests passed
Ruff passed
mypy passed on 28 source files
```

Real acceptance included ConnectionProbe, WPF, DeepSeek, deliberate authentication failure, and sensitive-log inspection.

### Phase 2 checkpoint

Phase 2 final project-owner acceptance on 2026-09-12:

```text
python -m pytest -q
-> 104 passed in 4.73s

python -m ruff check .
-> All checks passed!

python -m mypy src
-> Success: no issues found in 38 source files
```

C# build:

```text
N/A for Phase 2 final acceptance because Phase 2 changed no C# source.
```

Manual runtime regression:

- Python Agent Core started normally.
- EventBus logged `event_bus_started queue_capacity=256`.
- WPF connected successfully.
- two consecutive WPF messages received real DeepSeek responses.
- Python observed two successful DeepSeek HTTP 200 responses.
- EventBus shutdown logged `event_bus_closing` and `event_bus_closed`.
- the Phase 1 vertical slice remained operational after Phase 2 integration.

Known existing shutdown debt observed during manual acceptance:

- closing WPF may produce `websockets.exceptions.ConnectionClosedError: no close frame received or sent` on the Python server side.
- this is an existing Desktop/WebSocket shutdown-hardening issue and is not an EventBus shutdown failure.

### Phase 3 checkpoint

Phase 3 final project-owner acceptance on 2026-09-13:

```text
python -m pytest -q
-> 145 passed in 4.99s

python -m ruff check .
-> All checks passed!

python -m mypy src
-> Success: no issues found in 51 source files

git diff --check
-> clean
```

C# build:

```text
N/A for Phase 3 final acceptance because Phase 3 changed no C# source.
```

Manual runtime acceptance:

- Python Agent Core started successfully.
- WPF connected successfully.
- default Character identified herself as `Aria`.
- ordinary conversation showed visible Character style without blocking task-oriented use.
- a real `sqrt(2)` irrationality proof remained mathematically correct.
- runtime temporal context produced `2026-09-13`, Sunday.
- the model explicitly preferred verifiable mathematical / scientific reality over conflicting Character settings.
- real DeepSeek-backed responses returned through WPF.
- deliberate Provider authentication failure still mapped to the existing safe protocol error boundary.
- runtime log review did not reveal API keys, Authorization headers, complete user/system prompts, complete model responses, raw response bodies, or reasoning content.

## 7. Development Baseline

Python:

- Python 3.11
- pytest
- Ruff
- mypy
- `pydantic-settings`
- `websockets`
- `openai`
- editable install through `pip install -e ".[dev]"`

Desktop:

- C# WPF
- target framework: .NET 8 Windows

Development workflow:

```text
Requirement / Goal
  -> Context Recovery
  -> Design
  -> Architecture Review
  -> ADR when required
  -> Task Breakdown
  -> Implementation
  -> Targeted pytest / Ruff / mypy
  -> Working diff review
  -> Stage specific files
  -> Staged diff review
  -> Commit
  -> Phase-final project-owner acceptance
  -> Final documentation / checkpoint
```

During implementation, use targeted checks. At a phase-final gate, the project owner runs the complete quality trio:

```powershell
python -m pytest -q
python -m ruff check .
python -m mypy src
```

## 8. Architectural Principles That Must Not Be Lost

### 8.1 Companion, not a turn-based chatbot

The runtime must eventually support proactive output without requiring a user request.

The C# independent receive loop and the Phase 2 Event System both preserve this direction.

### 8.2 Long-term proactive flow

```text
External World
  -> Perception
  -> Events
  -> Situation Engine
  -> Attention Engine
  -> Behavior Engine
  -> Voice / Avatar / Tools
```

Phase 2 implemented the Event infrastructure layer. Phase 3 added Character, temporal truth, and prompt/context composition. Situation, Attention, Behavior, Perception, Permission, Tools, Voice, and Avatar remain future work.

### 8.3 Intent != Permission

Character intent never grants system permission.

Runtime Events must not be interpreted as implicit authorization.

Sensitive actions must pass through a dedicated Permission / capability boundary in a later phase.

### 8.4 Real and fictional state must remain separate

Fictional state must not contaminate user facts, factual reasoning, memory retrieval, or tool decisions.

### 8.5 Local-first, cloud-enhanced

Target runtime modes remain Offline / Local / Hybrid / Cloud.

Cloud failure must not erase the companion's basic local runtime behavior.

The current implementation has safe Provider failure handling but still has no local fallback router.

### 8.6 Stable interfaces over hidden coupling

Continue using:

- Protocol / interface boundaries
- adapters
- Event boundaries
- dependency injection
- composition roots
- explicit lifecycle ownership

Do not move Provider, Memory, Tool, Perception, or Agent reasoning logic into the UI.

## 9. Important Persistent Decisions

Phase 0 ADRs:

```text
0001 WPF Desktop + Python Agent Core
0002 WebSocket Desktop/Core transport
0003 layered Desktop client
0004 local-first Provider abstraction
0005 Intent is not Permission
0006 real / fictional state separation
```

Phase 1 ADRs:

```text
0007 async non-streaming Provider contract
0008 DeepSeek V4 Flash via async OpenAI-compatible Chat Completions
0009 Provider error isolation and Desktop error mapping
```

Phase 2 ADRs:

```text
0010 Runtime Events are facts; commands remain explicit boundaries
0011 async bounded in-process EventBus
0012 fail-fast EventBus overload admission
```

ADR 0012 supersedes only the queue-full waiting/backpressure portion of ADR 0011. Other accepted ADR 0011 decisions remain in force.

Phase 3 ADRs:

```text
0013 Character definitions are structured domain data
0014 Character consumes User Context; Memory owns User Profile
0015 Runtime temporal truth comes from Clock context
```

## 10. Phase 3 Git Anchors

Confirmed development anchors:

```text
ecc1fce Record Phase 3 character architecture decisions
e648a19 Establish Phase 3 character domain models
21e658f Add Character definition loading
39f8537 Add runtime temporal context
cb1c5b0 Add Character context composition
5ecee74 Integrate Character context into runtime
a172a43 Update Agent integration test fixtures
```

The repository `git log --oneline` remains authoritative for complete history.

## 11. Known Technical Debt / Cleanup Candidates

1. Desktop WebSocket endpoint remains hard-coded in `App.xaml.cs`; shared endpoint configuration is not implemented.
2. C# automated tests still do not exist.
3. Python `Message` validation remains lightweight; documented protocol versioning is not implemented.
4. WPF reconnect / retry / connection-state recovery is not implemented.
5. Desktop shutdown/disposal is not hardened; abrupt WPF disconnect can produce a missing close-frame error in Python WebSocket logs.
6. `src/agent_core/requirements.txt` remains legacy duplicate dependency metadata.
7. Provider roles remain limited to `system`, `user`, and `assistant`.
8. streaming remains intentionally unsupported in the current Provider path.
9. automatic Provider retries remain intentionally unsupported.
10. local-model / cloud-fallback routing is not implemented.
11. WPF displays Provider error messages but does not yet implement code-specific UI behavior.
12. WPF still renders Markdown / LaTeX syntax as plain text.
13. dynamic Character switching UI is not implemented.
14. Character hot reload is not implemented.
15. Memory / User Profile persistence remains future work.
16. Runtime Events are internal only; there is no Desktop Event Bridge yet.
17. Event persistence, replay, priority, wildcard routing, dynamic unsubscribe, multiple dispatcher workers, and restart/supervision policy remain intentionally absent from the current Event baseline.
18. The repository does not yet enforce a canonical text EOL policy through a dedicated `.gitattributes`; keep `git diff --check` in the review workflow.
19. Generated caches, logs, local `.env`, build outputs, and `.git` must remain excluded from checkpoint/source archives.

## 12. Phase 4 Entry Point

Next roadmap phase:

> Phase 4 - Memory System

Goal:

Introduce explicit Memory ownership and separated memory domains without allowing temporary, fictional, or low-confidence state to contaminate factual user context.

Expected scope:

- User Profile
- Working Context
- Episodic Memory
- Long-term Memory
- Relationship Memory
- Today Memory
- Fictional Ephemeral State
- retrieval / persistence / retention / conflict rules

Phase 4 must preserve:

```text
Character != Memory
Character != Internal State
TemporalContext != Memory
Prompt composition != Permission
Intent != Permission
Real State != Fictional State
```

Character and `PromptContextComposer` should consume prepared memory/user context rather than owning Memory persistence, retrieval, learning, retention, or conflict behavior.

Before implementation:

- recover context from the Phase 3 checkpoint and current code
- preserve the verified Provider/WebSocket/EventBus/Character runtime
- design Memory ownership and retrieval contracts before implementation
- do not redesign Character merely to make Memory convenient
- do not allow fictional or ephemeral state to enter factual user-profile storage

## 13. Context Recovery Procedure

When continuing this project in a new conversation/session:

1. read `PROJECT_STATE.md`
2. read `docs/PHASE_3_CHECKPOINT.md`
3. read `ARCHITECTURE.md`
4. read `ROADMAP.md`
5. read `DESIGN.md`
6. read `TECH_STACK.md`
7. read `THIRD_PARTY.md`
8. read `docs/DEVELOPMENT.md`
9. read the current phase's design / architecture-review / ADR documents
10. inspect current source files, `git status`, and recent Git history
11. treat current repository code as authoritative when historical documentation conflicts with implementation

Historical checkpoint and design documents remain useful for development history but may intentionally describe earlier implementation states. Chat history is supplementary context, not the authoritative project state.
