# Desktop Companion Agent Communication Protocol

## 比赛分支：统一业务入口（2026-10-05）

外层仍为 `id/type/timestamp/source/payload`，用户消息继续使用 `type=chat`。
正常请求先经公共理解、校验和澄清，再进入支线，桌面不直接发送 SQL。

请求 payload 保留 `message`，增加桌面为本次会话创建的 UUID `session_id`。
澄清回答可附带上次响应的 `task_id`。`request_id` 取输入消息的 `id`；
`task_id` 在同一澄清任务中保持不变，补充请求仍有新的 `request_id`。
省略 session_id 时后端创建会话，客户端继续对话必须使用响应中的 session_id。
会话暂存内存，重启或容量淘汰后清空；过期任务需重新提问。

尚不能确定业务意图时，提取结果可为 `problem_type=undetermined`。
公共入口返回 `status=needs_clarification`、`issues.code=MISSING_INTENT`，
保存实际提供的时间等条件及待补充 task_id，不调用 SQL 或文档问答。
无待补充任务时，仅输入四位年份（可带“年”）由本地规则保留明确的整年区间，
不猜指标或筛选；待补充状态下的年份继续交给公共理解解析同一任务。
用户补充后可确定 sql 或 qa 支线；非业务请求仍为 unsupported，未支持的指标仍拒绝。

响应 payload 的共同字段：`message`（兼容原文字界面）、`status`、
`problem_type`、`session_id`、`task_id`、`request_id`、`issues`、`citations`、`trace`。
成功问数另有 `confirmed_query/source_id/metric_definition_id/columns/rows/sql/parameters/tables/truncated`。

| status | 含义 |
|---|---|
| ok | 成功，合法空结果仍为成功 |
| needs_clarification | 保存未明确任务并追问，不先执行业务 SQL |
| insufficient_evidence | 文档证据不足 |
| partial | 部分完成，有明确缺失 |
| error | 服务失败或不支持该查询 |

正常业务状态使用 `type=response`；error 使用 `type=error`，保留安全错误码 `code`。
不返回凭据或原始底层异常。trace 只记录实际调用，SQL 的 execution_attempted 单独保留。
当前可运行支线是 Chinook 发票销售额汇总，时间开始包含、结束不包含，
筛选为账单国家、城市、客户；口径固定 chinook_sales_amount_v1，不假定币种或退款语义。
所有条件仍交给 H 的 SQLGenerationService.query(query, context) 二次校验。
本阶段不支持分组、排名、任意跨表、明细及自由 SQL。
文档问答支线使用独立 legacy_sales_demo 集合，返回答案及 citations，引用包含
evidence_id、doc_id、title、version、section、line_start/line_end、quote 和原文摘要。
Markdown 的 page 为 null，不伪造页码。结果标明模拟来源，不修改 Chinook SQL 口径。
检索条件支持 doc_ids/doc_version/doc_as_of；缺少这些筛选时搜索全部已导入模拟文档。
qa_query 为补全指代后的检索文字，回答同时接收原问题；公共理解保留问题歧义并走澄清。
没有相关原文时不调用模型，返回 insufficient_evidence。模型结论先检查引用标识和
原文摘录，再通过共享 Provider 独立核对结论是否被所引用摘录支持。
此检查有模型判断局限，不代表已经完成忠实度效果评估；支持不足时不展示该结论。
资料不足时，message 显示待回答的问题（优先补全指代后的 qa_query）、本次检索范围和补充建议；
issues 使用 NO_RELEVANT_EVIDENCE（检索未命中）、MISSING_DOCUMENT_EVIDENCE（原文不足）
或 ANSWER_GROUNDING_REJECTED（结论未通过依据核验），不改变 response 外层和状态。
模型提供的具体缺失说明需另一次 missing_check 核对问题与实际片段后才展示；
说明被拒绝、格式无效或核验调用失败时，保留资料不足状态及确定的检索范围，不展示未核验文字。
缺失说明为空时不追加核验调用。只说明本次检索的不足，不断言整个知识库或现实中不存在相关事实。
资料不足结果的 citations 仍为空；issues 沿用桌面的条件与执行记录展示。
trace 分别记录实际的检索／缓存复用、答案生成、引用核验及依据核验；融合仍未接入。
共享 LLM Provider 由入口管理，模型不能覆盖可信字段、口径或权限边界。

## 1. Overview

本文档定义 Desktop Companion Agent 内部模块之间的通信协议。

主要用于：

    C# Desktop Layer

            ↕

    Python Agent Core

未来扩展：

-   Mobile Client
-   Plugin System
-   External Tools
-   Local Services

------------------------------------------------------------------------

# 2. Design Principles

## 2.1 Event Driven

系统采用事件驱动思想。

通信双方不直接调用对方内部方法。

而是发送：

    Message

接收：

    Event

优点：

-   模块解耦
-   易扩展
-   支持主动行为

------------------------------------------------------------------------

## 2.2 JSON Based

第一版本采用：

    JSON

作为数据格式。

原因：

-   人类可读
-   调试方便
-   跨语言支持

------------------------------------------------------------------------

# 3. Message Structure

所有消息统一格式：

``` json
{
    "id": "message_unique_id",
    "type": "message_type",
    "timestamp": "ISO8601",
    "source": "sender",
    "payload": {}
}
```

字段说明：

  字段        说明
  ----------- --------------
  id          消息唯一编号
  type        消息类型
  timestamp   时间
  source      发送方
  payload     具体内容

------------------------------------------------------------------------

# 4. Message Types

## 4.1 Chat Message

用户主动聊天。

Example:

``` json
{
    "id":"001",
    "type":"chat",
    "source":"desktop",
    "payload":{
        "message":"你好"
    }
}
```

流程：

    User

    ↓

    Desktop

    ↓

    Agent Core

------------------------------------------------------------------------

## 4.2 Agent Response

Agent回复。

Example:

``` json
{
    "id":"002",
    "type":"response",
    "source":"agent",
    "payload":{
        "message":"你好，我在这里。"
    }
}
```

------------------------------------------------------------------------

## 4.3 Event Message

系统事件。

用途：

未来：

-   软件启动
-   文件变化
-   时间提醒
-   天气变化

Example:

``` json
{
    "id":"003",
    "type":"event",
    "source":"system",
    "payload":{
        "event":"application_started"
    }
}
```

------------------------------------------------------------------------

## 4.4 Permission Request

需要用户授权。

Example:

``` json
{
    "id":"004",
    "type":"permission_request",
    "source":"agent",
    "payload":{
        "action":"read_file",
        "target":"example.txt"
    }
}
```

原则：

Agent不能绕过权限。

------------------------------------------------------------------------

## 4.5 Tool Request

工具调用。

Example:

``` json
{
    "id":"005",
    "type":"tool_request",
    "source":"agent",
    "payload":{
        "tool":"web_search",
        "parameters":{
            "query":"weather"
        }
    }
}
```

------------------------------------------------------------------------

# 5. Communication Flow

## User Chat

    User

     ↓

    Desktop

     ↓

    chat message

     ↓

    Agent Core

     ↓

    response

     ↓

    Desktop

     ↓

    User

------------------------------------------------------------------------

## Agent主动行为

未来：

    System Event

     ↓

    Agent Core

     ↓

    Decision

     ↓

    Agent Message

     ↓

    Desktop

------------------------------------------------------------------------

# 6. Error Message

统一错误格式：

``` json
{
    "type":"error",
    "payload":{
        "code":"NETWORK_ERROR",
        "message":"Connection failed"
    }
}
```

## 6.1 Phase 1 Provider Errors

Phase 1 中，LLM Provider 失败通过统一 `error` Message 返回 Desktop。

Provider 内部诊断信息、SDK 异常、API Key、Prompt、Reasoning Content 和原始响应体不能通过协议暴露。

Provider error codes：

| Code | Message | Meaning |
| --- | --- | --- |
| `PROVIDER_NOT_CONFIGURED` | `AI provider is not configured.` | Provider 配置缺失或无效 |
| `PROVIDER_AUTHENTICATION_FAILED` | `AI provider authentication failed.` | Provider 身份验证失败 |
| `PROVIDER_QUOTA_EXHAUSTED` | `AI provider quota or balance is insufficient.` | Provider 配额或余额不足 |
| `PROVIDER_RATE_LIMITED` | `AI provider is rate-limited. Please try again later.` | Provider 限流 |
| `PROVIDER_TIMEOUT` | `AI provider request timed out.` | Provider 请求超时 |
| `PROVIDER_UNAVAILABLE` | `AI provider is temporarily unavailable.` | Provider 网络连接失败或服务暂时不可用 |
| `PROVIDER_REQUEST_FAILED` | `AI provider rejected the request.` | Provider 拒绝请求 |
| `PROVIDER_INVALID_RESPONSE` | `AI provider returned an invalid response.` | Provider 返回无法满足统一契约的响应 |
| `PROVIDER_ERROR` | `AI provider request failed.` | 未进一步分类的 Provider 错误 |

Example:

``` json
{
    "type":"error",
    "source":"Desktop Companion",
    "payload":{
        "code":"PROVIDER_TIMEOUT",
        "message":"AI provider request timed out."
    }
}
```

当前 WPF Desktop 仍可只读取 `payload.message`。`payload.code` 为稳定的机器可读错误类别，供后续 UI 行为使用。

Phase 0 已存在的非 Provider 错误将在各自协议维护任务中逐步统一，本节不改变其当前运行行为。

------------------------------------------------------------------------

# 7. Version Control

协议版本：

    v1

未来：

    v2

需要保持兼容。

格式：

``` json
{
    "version":"1.0"
}
```

------------------------------------------------------------------------

# 8. Security Rules

## Permission First

任何涉及：

-   文件
-   网络
-   系统操作

必须经过：

    Permission Layer

Agent自主性：

不能超过：

用户授权。

------------------------------------------------------------------------

# 9. Future Extension

## Memory Event

``` json
{
"type":"memory_update"
}
```

## Emotion Event

``` json
{
"type":"emotion_change"
}
```

## Avatar Event

``` json
{
"type":"avatar_action"
}
```

## Tool Result

``` json
{
"type":"tool_response"
}
```

------------------------------------------------------------------------

# 10. Summary

通信协议负责：

    连接 Desktop

    连接 Agent

    连接未来所有模块

它是整个 Desktop Companion Agent 的基础通信契约。
