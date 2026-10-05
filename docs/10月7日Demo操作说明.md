# 10 月 7 日 Demo 操作说明

适用环境：本机已准备好的项目 `F:\01_Projects\Creative\AI_projects\电子杯`，分支 `competition/topic8-demo`。2026 年 10 月 5 日已保存的代码检查点为 `f2b3ad1`；本说明初版已保存于 `b1df363`。本次不完整意图澄清改动已通过人工验收，待保存新检查点。

目标是在 10 月 7 日当天演示一个小范围可运行原型：WPF 输入经统一 Agent 理解和澄清，调用问数或文档问答服务，再展示表格、SQL、原文引用和执行记录。以下流程沿用团队架构。

## 1. 已准备的内容

| 内容 | 当前位置与状态 |
| --- | --- |
| WPF 与 Python 后端 | 已准备 .NET 8、Python 虚拟环境，启动入口在 `scripts/` |
| 模型配置 | 项目根目录 `.env`，使用现有 DeepSeek Provider；问答和自然语言理解需要联网 |
| PostgreSQL 程序 | `.tools/pgsql/`，18.6 便携版 |
| 数据库数据 | `.local/postgres/data/`，已导入 Chinook；服务地址 `127.0.0.1:5432` |
| 只读连接配置 | `database/.local/connection_chinook.json`，本机 Windows 加密配置 |
| 文档知识库 | `.local/knowledge/legacy_sales_demo/`，已导入 5 份 Markdown、10 个片段 |
| 前后端连接 | 本机 WebSocket `127.0.0.1:8765` |

正常重启不需要再次初始化数据库、导入 Chinook 或导入演示文档。数据库、索引和原文快照保存在磁盘；聊天历史和待澄清任务只在内存中，重启后需重新提问。

本说明用于这台已准备环境的电脑。Git 保存代码和源资料，不包含 `.env`、工具安装、本机数据库、加密连接配置或导入后的索引；复制仓库到其他电脑还需重新准备这些环境。

## 2. 启动数据库：PowerShell 窗口 A

进入项目，并查看目录映射：

```powershell
Set-Location -LiteralPath 'F:\01_Projects\Creative\AI_projects\电子杯'
subst
```

已配置的 `P:` 应指向这个项目目录。它是目录别名，数据库数据仍在 F 盘；用于绕过已遇到的 PostgreSQL 中文路径初始化问题。

如果重启 Windows 后映射不存在，且 `P:` 未被其他磁盘或目录占用，再执行：

```powershell
subst P: 'F:\01_Projects\Creative\AI_projects\电子杯'
```

如果 `P:` 指向其他位置，先确认映射，不继续使用下面的 P 盘命令。

检查连接状态：

```powershell
& 'P:\.tools\pgsql\bin\pg_isready.exe' -h 127.0.0.1 -p 5432 -t 3
```

看到 `accepting connections` 或“接受连接”表示服务已运行，直接进入后端启动步骤。这个命令只检查服务状态，不显示数据库中的表格数据。

如果提示 `no response`，启动现有数据库，再检查一次：

```powershell
Start-Process -FilePath 'P:\.tools\pgsql\bin\pg_ctl.exe' -ArgumentList '-D P:\.local\postgres\data -l P:\.local\postgres\server.log -o "-h 127.0.0.1 -p 5432" -w -t 30 start' -WindowStyle Hidden -Wait

& 'P:\.tools\pgsql\bin\pg_isready.exe' -h 127.0.0.1 -p 5432 -t 3
```

数据库在独立的隐藏窗口中启动，因此没有启动输出也是正常的，以连接检查结果为准。看到“接受连接”后再继续。

## 3. 启动后端：PowerShell 窗口 B

另开一个 PowerShell 窗口：

```powershell
Set-Location -LiteralPath 'F:\01_Projects\Creative\AI_projects\电子杯'
powershell -ExecutionPolicy Bypass -File .\scripts\start-core.ps1
```

成功标志：`server listening on 127.0.0.1:8765`。保持这个窗口运行；它显示消息连接和模型调用日志。

## 4. 启动前端：PowerShell 窗口 C

再开一个 PowerShell 窗口：

```powershell
Set-Location -LiteralPath 'F:\01_Projects\Creative\AI_projects\电子杯'
powershell -ExecutionPolicy Bypass -File .\scripts\start-desktop.ps1
```

WPF 打开后确认顶部显示 `Connected`。若尚未连接，点击“连接”。点击“新建会话”后开始演示。

只更新前端时，关闭 WPF 后重新执行这一节即可，数据库和后端继续运行。更新后端时需在窗口 B 停止旧后端并重新启动，再让 WPF 连接。

## 5. 固定演示流程

等待每次回复完成再进行下一步。下表的核心结果已实际验收；模型可能生成不同措辞或引用数量，不能把两次回答逐字相同作为成功条件。

| 顺序 | 操作或输入 | 检查结果 |
| --- | --- | --- |
| 1 | 点击“新建会话”，发送“美国销售额是多少？” | 追问查询时间，不提前查询金额 |
| 2 | 仅回答“2024年” | 保留待澄清任务中的美国条件，返回 **127.98**，自动显示结果表格 |
| 3 | 手动打开“实际 SQL”和“条件与执行记录” | 展示实际执行 SQL、参数和调用记录；时间为 2024-01-01 至 2025-01-01，结束不包含 |
| 4 | 发送“根据演示文档，张伟是如何减少沟通偏差的？” | 回答有原文依据，自动显示原文引用；来源为《张伟销售经验分享》v1 |
| 5 | 继续问“那陈敏是怎么提升合作效率的？” | 继续走文档问答，引用改为《陈敏销售经验分享》v1；涉及渠道合作、交付责任和季度复盘 |
| 6 | 发送“根据演示文档，张伟的手机号码是多少？” | 显示资料不足，不编造号码；自动显示条件与执行记录，原文引用为空 |
| 7 | 发送“美国销售额是多少？”，等追问后点击“新建会话” | 清空聊天、输入和结果，连接保持；清除旧的待澄清任务 |
| 8 | 在新会话仅发送“2024年” | 追问要查询什么指标或查阅哪些文档，显示“需要补充条件”；保存年份但不自动继承旧指标／美国筛选 |

第 8 步后回答“美国销售额”，应沿用此次用户提供的 2024 年，返回 127.98。该链路已通过离线编排检查及用户实际界面验收；补充成功后自动显示结果表格。

如果只需直接验证问数，可在新会话输入“2024 年美国销售额是多少？”，预期同样返回 127.98。

问数采用 Chinook 发票 `total` 汇总，数据源没有指定币种，因此不能把 127.98 介绍为美元、人民币、利润或扣退款后的净额。文档为 H 提供的旧模拟业务资料，不能作为 Chinook 的业务口径或同实体融合依据。

## 6. 正常退出

1. 关闭 WPF，等待前端启动命令返回。
2. 在后端窗口 B 按 `Ctrl+C` 停止后端。终端可能显示 `KeyboardInterrupt`，这是手动中断记录。
3. 在窗口 A 正常停止数据库：

```powershell
& 'P:\.tools\pgsql\bin\pg_ctl.exe' -D 'P:\.local\postgres\data' -m fast -w -t 30 stop
```

看到数据库停止提示即可结束。数据库数据和文档索引不会因此删除；数据库运行期间保留 P 盘映射。

## 7. 常见问题处理

| 现象 | 本次操作 |
| --- | --- |
| 找不到 `P:\` | 按第 2 节检查映射是否存在、是否指向本项目 |
| `pg_isready` 无响应或 WPF 提示数据库连接超时 | 先检查数据库是否在运行；按第 2 节启动现有数据，不重新建库 |
| 隐藏启动后仍无响应 | 查看下面的数据库日志末尾，把错误内容发来诊断；不删除数据目录或再次执行初始化 |
| 后端提示未配置模型密钥 | 本机检查 `.env` 中的模型配置；不把密钥粘贴到聊天或文档 |
| 后端无法监听 8765 | 检查是否已有后端窗口；保留已有服务或在原窗口正常停止后重启 |
| WPF 未连接 | 确认后端出现监听成功提示，再点击“连接” |
| 后端重启后提示待补充任务过期 | 点击“新建会话”，重新发送完整问题 |
| 文档问答提示知识库未就绪 | 检查索引文件是否存在；仅在索引缺失时按下面步骤导入 H 的 5 份文档 |
| 文档回答措辞或引用数量变化 | 检查结论是否被显示的原文支持；不只比较措辞或引用条数 |

查看数据库日志末尾：

```powershell
Get-Content -LiteralPath 'P:\.local\postgres\server.log' -Tail 30
```

仅当演示文档索引缺失时，进入项目、重新导入，并确认 `status=ok`、5 文档、10 片段：

```powershell
Set-Location -LiteralPath 'F:\01_Projects\Creative\AI_projects\电子杯'
Test-Path -LiteralPath '.local\knowledge\legacy_sales_demo\index.json'
```

若上一步返回 `False`，执行：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\import-demo-documents.ps1
```

导入后由后续问答读取索引，不需要重新建数据库。现有索引若损坏，应先诊断，不能把重新导入当作通用修复。

## 8. 本轮范围与下一阶段

已验收的是有限案例下的问数、澄清、独立 Markdown 问答、引用、资料不足、文档追问、新建会话、重新启动及三种结果页签切换。

当前 WPF 问数入口仅开放发票销售额汇总与国家／城市／客户编号筛选，不能演示任意 SQL、分组排名或任意跨表查询。文档导入目前没有 WPF 文件选择入口，只能通过现有脚本导入这批 Markdown；PDF、扫描 OCR、向量检索和完整模型效果评估尚未完成。意图澄清时自动显示条件与执行记录已验收，错误及部分结果的自动页签尚未单独人工验收。

跨源融合尚未接入，仍归 J 的交付范围；SQL 与全项目效果评估沿用 H 的分工。下一阶段先对照团队架构确定缺项和交付接口，再继续用户负责的前端、公共理解、会话与问答运行。

计划保持：10 月 7 日当天可运行 Demo；10 月 20 日目标交付；10 月 25 日初赛截止。本机演示链路跑通不等于全部赛题要求完成。
