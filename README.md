# Desktop Companion Agent

比赛原型日常启动和演示先看 [10 月 7 日 Demo 操作说明](docs/10月7日Demo操作说明.md)，包括启动顺序、已验收问题、正常退出和当前范围。

小 Demo 已保存于 `10e0f12`；文档导入与引用问答已保存于 `b1065a0`。多格式文件准备及完整操作见 [文档导入与准备](docs/文档导入与准备.md)：本地 PDF／OCR、表格、来源版本与位置、复核更正及关键词检索。2026-10-06 已完成新集合的理解／检索／问答与 WPF 引用接线，并通过用户六项实际桌面案例验收。

J 接手先看 [给 J 的交接说明](docs/给J的交接说明.md)，包含新电脑搭建、现有接口、验收范围与融合接入边界。源码不包含本机密钥、数据库集群、资料索引或安装环境，拉取后需在本机准备。

> 当前分支 `competition/topic8-demo` 用于赛题八的 WPF 问数/问答迁移，基于 Phase 3 提交 `30c6bbd`。环境准备与启动步骤见 [比赛迁移记录](docs/比赛迁移记录.md)。下文保留原助手的历史项目说明，比赛开发状态以迁移记录为准。

比赛开发先看 [目录导航](docs/目录导航.md) 和 [SQL 接入方案](docs/SQL接入方案.md)。队员 SQL 模块复用现有 Provider 接口。用户已部署 PostgreSQL/Chinook 并验收真实 DeepSeek 生成 SQL、检验及只读查询（2024 年美国发票金额 127.98）。离线复查入口为 `scripts/check-sql.ps1`，结构化模型联调入口为 `scripts/check-model-sql.ps1`。当前新增统一自然语言理解、条件校验、内存会话及澄清，再调用原 SQL 服务；WPF 增加结果表格、实际 SQL 和执行记录。57 项针对性测试与 WPF 编译通过，真实自然语言与桌面澄清交互已通过用户验收。当前开放发票销售额汇总，已验收完整问题和澄清补充后的 127.98。5 份旧模拟 Markdown 已导入独立集合；关键词检索、带原文引用的文档问答及依据核验已接线，用户已验收张伟经验问答与原文引用，资料不足案例也已通过用户验收。J 的跨源融合尚未接入。业务协议见 [通信协议](docs/COMMUNICATION_PROTOCOL.md)。

Desktop Companion Agent 是一个长期 Windows 桌面 AI 伴侣项目。

目标不是制作普通的问答聊天框，而是构建一个可以长期运行、理解桌面情境、拥有角色与记忆、能够主动决定是否互动，并在权限边界内帮助用户完成工作的桌面 Companion Runtime。

## Current Status

Phase 0: **Complete**

Phase 1: **Complete**

Next:

```text
Phase 2 - Event System
```

Current verified real vertical slice:

```text
WPF UI
  -> ViewModel
  -> AgentClientService
  -> C# WebSocket client
  -> Python WebSocket server
  -> Agent Core
  -> LLMProvider
  -> DeepSeekProvider
  -> DeepSeek API
  -> response
  -> WPF UI
```

Real Phase 1 acceptance included:

```text
You: 请只回复：PHASE1_DEEPSEEK_OK
Agent: PHASE1_DEEPSEEK_OK
```

The original AI MVP is complete.

See:

- `PROJECT_STATE.md`
- `docs/PHASE_1_CHECKPOINT.md`
- `ROADMAP.md`

## Architecture Direction

Long-term flow:

```text
Perception
  -> Events
  -> Situation Engine
  -> Attention Engine
  -> Behavior Engine
  -> Voice / Avatar / Tools
```

Core principles:

- proactive companion, not turn-based chatbot
- `Intent != Permission`
- local-first, cloud-enhanced
- real memory and fictional flavor remain separated
- stable interfaces / adapters between replaceable providers
- event-driven sensing instead of continuous screenshot streaming

## Technology Baseline

Desktop:

```text
C# WPF
.NET 8 Windows target
```

Agent Core:

```text
Python 3.11
```

Communication:

```text
Local WebSocket
JSON protocol
```

Current Python tooling / runtime libraries include:

- pytest
- Ruff
- mypy
- pydantic-settings
- websockets
- OpenAI Python SDK for the concrete DeepSeek adapter

Provider baseline:

```text
LLMProvider: provider-neutral async Protocol
DeepSeek client: openai.AsyncOpenAI
Default model: deepseek-flash
Streaming: disabled in Phase 1
Thinking: disabled by default
Default timeout: 60 seconds
Automatic retries: disabled
```

## Repository Structure

```text
src/
├── agent_core/
│   ├── communication/
│   ├── config/
│   ├── core/
│   ├── observability/
│   ├── providers/
│   ├── tests/
│   └── main.py
│
└── Desktop/
    └── DesktopCompanion.Desktop/

tools/
└── DesktopCompanion.ConnectionProbe/

docs/
├── adr/
├── COMMUNICATION_PROTOCOL.md
├── DEVELOPMENT.md
├── PHASE_0_CHECKPOINT.md
├── PHASE_1_CHECKPOINT.md
├── PHASE_1_PROVIDER_LAYER_DESIGN.md
└── PHASE_1_DEEPSEEK_ADAPTER_DESIGN.md
```

## Run the Phase 1 Runtime

### 1. Activate Python environment

From repository root on Windows PowerShell:

```powershell
.\.venv\Scripts\activate
```

For a fresh environment:

```powershell
pip install -e ".[dev]"
```

`pyproject.toml` is the canonical Python dependency source.

### 2. Configure the local Provider

Create a local `.env` from the template:

```powershell
Copy-Item .env.example .env
```

Set a real local DeepSeek key:

```text
DCA_DEEPSEEK_API_KEY=<your local secret>
```

Keep `.env` private and uncommitted.

Default Phase 1 Provider settings:

```text
DCA_MODEL_PROVIDER=deepseek
DCA_DEEPSEEK_MODEL=deepseek-v4-flash
DCA_DEEPSEEK_TIMEOUT_SECONDS=60
DCA_DEEPSEEK_THINKING_ENABLED=false
```

Current runtime model alias after the Phase 2 checkpoint:
`deepseek-flash`.

### 3. Start Agent Core

```powershell
python -m agent_core.main
```

Expected endpoint:

```text
ws://127.0.0.1:8765
```

The server remains running until stopped with `Ctrl + C`.

### 4. Start WPF Desktop

Run the `DesktopCompanion.Desktop` project from Visual Studio.

In the current MVP UI:

1. click `Connect`
2. confirm status becomes `Connected`
3. type a message
4. click `Send`
5. receive a real DeepSeek response

### 5. Optional transport diagnostic

With Agent Core already running:

```powershell
dotnet run --project tools/DesktopCompanion.ConnectionProbe/DesktopCompanion.ConnectionProbe.csproj
```

The probe isolates C# / protocol / WebSocket / Provider behavior from WPF UI behavior.

## Provider Failure Behavior

Phase 1 maps Provider failures to safe Desktop protocol errors.

Example:

```json
{
  "type": "error",
  "payload": {
    "code": "PROVIDER_AUTHENTICATION_FAILED",
    "message": "AI provider authentication failed."
  }
}
```

Vendor exception strings, API keys, prompts, raw response bodies, and reasoning content must not be exposed through the Desktop protocol.

## Development Rules

Normal change flow:

```text
Requirement
-> Design
-> Task Breakdown
-> Implementation
-> Targeted Tests
-> Quality Checks
-> Documentation Update
-> Git Review
-> Commit
```

Development AIs should run targeted tests for their changes, not the complete pytest suite by default.

The project owner performs final full-suite acceptance.

## Phase 1 Final Validation

Project-owner final validation:

```text
python -m pytest -q
-> 66 passed

python -m ruff check .
-> All checks passed

python -m mypy src
-> no issues found in 28 source files
```

Real acceptance also covered:

- ConnectionProbe real Provider response
- two consecutive WPF real Provider responses
- real 401 authentication failure mapped to a safe Desktop message
- log review for sensitive-data leakage

## Current Non-Goals

The completed AI MVP still does not implement:

- Event System
- character/persona composition
- memory
- perception
- Situation Engine
- Attention Engine
- Behavior Engine
- tools
- permission UI
- avatar
- voice
- local-model fallback
- streaming
- automatic Provider retries

See `ROADMAP.md` for the next phases.
