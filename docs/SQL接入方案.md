# 赛题八 SQL 接入：第一步

## 本步骤目标

保留已验收的 WPF、WebSocket 和 DeepSeek 对话链路，接入队员的 SQL 服务。
本步骤只验证模块导入和离线 SQL 检验。数据库部署、真实只读查询、前端接线由用户理解本步骤后再逐步推进。
本步骤不把结构化查询冒充自然语言问数，也不以固定示例 Provider 的结果评估模型质量。

## 接入边界

- 来源：`F:\01_Projects\School\竞赛\电子杯\SQL查询以及评估`。
- 原样保留 `src/agent_core/sql`、`src/agent_core/evaluation`、`database` 公共代码与数据、`evaluation` 固定数据集和队员测试；来源摘要见 `docs/sql-import-manifest.json`。
- 只保留当前项目的一份 `agent_core.providers` 契约，不复制队员的重复 Provider。
- 不复制队员的 `.local`、缓存、临时目录或旧迁移备份。历史报告置于原路径供追溯，不作为当前项目已通过的证据。
- `database` 暂保留在仓库根目录，与上游相对路径一致；通过项目根目录的脚本启动。当前部署是源码开发环境，不声称已经支持独立 wheel 发布。
- 新增依赖 `psycopg[binary]==3.3.6`、`sqlglot==30.18.0`；依赖主声明仍为 `pyproject.toml`。

## 后续本地数据库方案（本步骤不执行）

使用 PostgreSQL 官方 Windows 页面指向的 EDB 二进制 ZIP，在 `.tools` 内解压；独立集群和凭据放在 Git 忽略的 `.local` 下。
仅监听 `127.0.0.1:5432`，初始化前检查端口；如已有其他服务占用则停止准备，不修改已有数据库。
应用连接使用队员代码要求的 `chinook_reader` 只读角色，密码通过当前 Windows 用户 DPAPI 保存。
复用队员的 `prepare_script()` 和 `setup(prepare_only=True)`：先核对固定 SQL 源文件 SHA-256，再剥离库级删除指令，导入后核对表、行数、外键、日期及金额。

下载来源：

- https://www.postgresql.org/download/windows/
- https://www.enterprisedb.com/download-postgresql-binaries
- https://www.postgresql.org/docs/18/app-initdb.html
- https://www.postgresql.org/docs/18/app-pg-ctl.html

## 验收与下一步

1. 配置、Provider、通信原有针对性测试继续通过。
2. SQL 生成、检验、数据源绑定和固定数据集的相关上游测试通过。
3. 离线明细示例返回 `validated`，且 `execution_attempted=false`。
4. 检验两个代表性案例：2024 年美国发票金额的 SQL、曲目名称和单价的明细 SQL。两者都只检验语句，不返回真实数据。

复查入口：`powershell -ExecutionPolicy Bypass -File .\scripts\check-sql.ps1`。
本次离线报告写入 `.cache/sql-integration-check.json`，不提交 Git；队员原样文件摘要见 `docs/sql-import-manifest.json`。

下一步先部署数据库并实查，然后再接请求路由、WPF 结果表格和自然语言意图解析与澄清。
