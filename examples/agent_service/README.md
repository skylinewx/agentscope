# Agent Service

Agent service is a FastAPI-based, multi-tenant and multi-session service built with AgentScope 2.0.

This example demonstrates

- how to set up the agent service with Redis storage, and
- how to launch the service and its companion Web UI

Details about the agent service please refer to the [tutorial](https://docs.agentscope.io/latest/en/deploy/agent-service).

## Prerequisites

- Python ≥ 3.11
- Node.js ≥ 20 with `npx`

## Quickstart

Install AgentScope from PyPI or source:

```bash
uv pip install agentscope[full]
# or
# uv pip install -e [full]
```

Install Redis and start it as backend storage:

```bash
# macOS (Homebrew)
brew install redis
brew services start redis

# Linux (systemd)
sudo apt install redis-server
sudo systemctl start redis-server

# Docker (cross-platform)
docker run --rm -p 6379:6379 redis:7
```

Configure Redis using `AGENTSCOPE_REDIS_HOST`, `AGENTSCOPE_REDIS_PORT`,
`AGENTSCOPE_REDIS_DB` and `AGENTSCOPE_REDIS_PASSWORD`. Environment variables
override the optional `service-config.local.json` next to `main.py`; use
`service-config.example.json` as the file format reference. The local file is
gitignored. Defaults are `localhost:6379`, database `0`, without a password.
An empty password environment variable explicitly disables password authentication.
Do not put connection credentials in source files or the example config.

Before starting, also set `auth.admin_password` in the local configuration
or provide `AGENTSCOPE_ADMIN_PASSWORD`. This is required; see the login and
user management section below for the configuration format.

Start the agent service:

```bash
cd examples/agent_service

python main.py
```

Launch the Web UI in a separate terminal to experience a chat-style interface:

```bash
cd examples/web_ui/

pnpm install
# or npm install

# Run in dev mode
pnpm dev
```

The Web UI uses a fixed same-origin `/api` endpoint. Its development proxy
forwards to `http://127.0.0.1:8000`; deployment operators can override the target
with `AGENTSCOPE_BACKEND_URL` in the frontend `.env.local` or environment.
Users sign in with their username and password, without entering an address.

## 调试模型、工具和 MCP 调用

在 PyCharm 的后端 Run/Debug Configuration 中添加环境变量
`AGENTSCOPE_LOG_IO=1`，重新启动服务。在 PowerShell 启动时也可以先执行
`$env:AGENTSCOPE_LOG_IO = '1'`。详细日志默认关闭，移除变量并重启即可关闭。

在后端控制台搜索 `CALL_IO`。启动日志会给出 UTF-8 文件的绝对路径，
位于仓库的 `.cache/agentscope/calls-<进程号>.jsonl`。每行一个 JSON 事件，
按进程写入，达到约 32 MiB 时轮转，保留两份备份。消息、提示词和业务回执
会保留在日志中，适合在本地排查。常见凭据字段会遮盖，二进制附件和模型
思考块只保留标记。

| `kind` | `direction=input` | `direction=output` |
| --- | --- | --- |
| `LLM` | 内置模型适配器传给供应商 SDK 的最终参数，包含提示词、对话和工具选择 | 模型返回的正文、工具调用及可用的用量信息。流式调用合并后记录一次，失败可查看已接收的部分 |
| `TOOL` | 工具名与实际参数 | 工具的完整结果和执行状态。被拒绝和外部执行的调用会标明 `execution` |
| `MCP` | MCP 服务名、原始工具名与请求参数 | 本地 MCP 连接返回的原始回执，包括 `isError`、`content` 和可用的 `structuredContent`。经网关调用时记录网关回包 |

查某个会话时，先筛选 `sessionId`，用 `callId` 配对输入和输出。
模型重试还要对照 `attempt`，结构化输出回退再看 `strategy`。例如排查净利润时，
找到 `name=execute_semantic_plan` 的 MCP 输入，检查实际的科目、期间和组织；
配对输出就是进入分析扩展之前的回执。`toolCallId` 可关联发起它的工具调用。

## 用户登录与管理

启动前，在 `service-config.local.json` 中保留现有 `redis` 段并增加：

```json
{
  "auth": {
    "admin_password": "CHANGE_ME",
    "session_ttl_seconds": 43200,
    "cookie_secure": false,
    "allowed_origins": []
  }
}
```

`admin_password` 必填，长度为 8–128 个字符。示例文件中的 `CHANGE_ME`
不能用于启动。可通过 `AGENTSCOPE_ADMIN_PASSWORD` 覆盖密码，
`AGENTSCOPE_SESSION_TTL_SECONDS` 覆盖会话有效期，
`AGENTSCOPE_COOKIE_SECURE` 覆盖 Cookie 的 Secure 属性。密码不返回浏览器，
配置文件已被 Git 忽略。所有服务进程应使用相同的配置和 Redis。

会话采用滚动续期：默认连续 12 小时没有有效请求才过期。通过身份、来源及
CSRF 校验的请求会同步延长 Redis 会话和浏览器 Cookie，包括队列轮询和
登录状态检查，因此页面保持打开且正常请求时会持续保持登录。公开接口、
服务端内部检查和流式心跳不会续期；已退出、过期或撤销的会话不能续活。

使用 `admin` 登录后，从侧栏进入“用户管理”，可搜索、分页、新增、编辑、
启用/禁用、删除用户及重置密码。普通用户从“个人账户”修改自己的密码或
退出登录。admin 账号不能在页面中删除、禁用、重命名或重置密码；修改
配置并重启后更新 admin 密码并撤销其旧会话，同密码重启保留会话。
管理用户不授予读取其他用户业务资源的权限。

账号数据保存在独立的 `agentscope:auth:users` Redis Hash，账户不设 TTL，
密码使用 Argon2id 哈希；随机登录凭证通过 HttpOnly Cookie 传输，其摘要
作为 Redis 会话键，会话到期自动删除。部署时开启 Redis 持久化并将认证
命名空间纳入备份，不要对账户记录设置逐出或过期策略。

修改密码、禁用、删除都会撤销已有登录会话。禁用/删除还阻止新的定时任务
和渠道消息触发；每个进程约每秒检查运行任务并请求中断，长 HTTP 流约每
两秒检查会话。软删除保留业务数据，重新创建同名账号使用新 ID。

迁移旧账号时，在新增用户对话框展开“绑定历史数据”，填写原先在连接页面
使用的用户名作为原用户 ID。由管理员确认归属后绑定，原会话、凭证和资源
继续关联；留空则创建全新账号。已经绑定或已删除的 ID 不能再次绑定。

例如旧用户名为 `wx`，管理员新增用户时将“用户名”和“原用户 ID”都填为
`wx`，设置密码后改用 `wx` 登录，即可继续查看原智能体和相关配置。
仅创建同名账号而不绑定原用户 ID，会分配新的空数据空间。`admin` 的用户
管理权限不包含读取这些业务数据的权限，因此用 `admin` 登录看到空列表
不代表旧数据已经删除。

部署到 HTTPS 时设置 `cookie_secure: true`，并使浏览器和 `/api` 同域。
反向代理需去除 `/api` 前缀、保留外部 `Host` 并正确传递协议，流式响应
关闭代理缓冲；参考 `../web_ui/nginx.conf.example`。默认校验请求 Origin
与请求主机一致，代理确实需要不同主机时才在 `allowed_origins` 配置完整
可信来源（协议、域名和端口），不要填写通配符。

启用认证后所有业务 HTTP 接口必须带有效会话，写请求还必须携带登录或
`GET /auth/me` 返回的 `csrf_token`，置于 `X-CSRF-Token` 请求头中并发送
可信 `Origin`。`X-User-ID` 被统一认证层覆盖，不能用于冒充用户。文件、
报告和知识库下载均要求当前登录；URL 中旧下载 token 不再单独授权。
生成的 HTML 报告与工作区文件使用 sandbox CSP，避免在应用来源执行脚本。

SDK 的 `create_app()` 通过可选 `auth_service` 接入认证；本示例始终启用。
嵌入者若未配置该参数，仍需自行提供可信身份边界。

## MCP / Skill 中心直接管理

示例服务不再加载内置 MCP / Skill 清单，也不再自动创建 Playwright、AMap
或 workspace-summary 资源。进入 Web UI 的 MCP 中心或 Skill 中心，在“我的”
页面即可直接新增、编辑和删除。开关在智能体和会话中设置；来源安装继续保留，
作为另一种添加方式。

### MCP

- 点击“新增 MCP”，粘贴单个标准 `mcpServers` 配置，或 AgentScope
  `MCPClient` 配置，填写可选说明和版本后保存。
- 点击资源行的编辑按钮，修改名称、启动命令、参数、HTTP 连接配置等。
  配置中的 `name`、`is_stateful` 和 `mcp_config` 分别表示名称、连接模式
  和连接配置。STDIO 必须使用有状态模式。
- 已保存的环境变量值、请求头和敏感参数用 `[KEEP_EXISTING_SECRET]` 占位。
  保留占位符即保留原值，替换为新值会更新它；向新位置复制占位符会被拒绝。
  资源列表仍不返回密钥和完整配置，管理接口不会建立 MCP 连接。
- 直接修改来源安装或历史清单记录的完整配置，是一次显式自定义操作：
  保留记录 ID，保存为用户管理的资源，不再随来源升级；已有绑定开关不变。
- 保存到资源库后，在工作区 MCP 面板从资源库选择装配；新运行使用最新配置。

示例输入：

```json
{
  "mcpServers": {
    "my-http-mcp": {
      "url": "https://example.com/mcp",
      "headers": {"Authorization": "Bearer ${ENV:MY_MCP_TOKEN}"}
    }
  }
}
```

`${ENV:变量名}` 在建立运行时连接时读取服务进程环境。也可以在编辑器中
使用完整 `MCPClient` JSON，配置工具过滤和执行超时。

### Skill

- 点击“新增 Skill”可直接编写 `SKILL.md`，增加脚本和参考文件；也可以
  上传完整目录或 ZIP / TAR / TAR.GZ 包。目录根部必须包含 `SKILL.md`，
  YAML 头部必须有非空 `name` 和 `description`。
- 点击编辑按钮可修改、增加或删除 UTF-8 文本文件。二进制文件和超过
  1 MiB 的文件保留在完整包中，通过重新上传目录或压缩包来修改它们。
- 在线编辑只提交改动的文件；未修改文件，包括二进制资源，全部保留。
  上传目录或压缩包是显式替换整个包。保存前验证路径、文件数量、大小和
  Skill 元数据；保存失败不会改变当前资源版本。
- 整个包按目录路径和全部文件内容计算哈希，仅修改 `scripts/` 也会产生
  新版本。版本留空时自动生成；指定版本时，内容改变必须使用不同版本。
- Skill 列表、详情、编辑和装配选择器同时显示版本号和更新日期。日期为
  当前资源库版本的保存时间，以 UTC 持久化，按浏览器本地时区展示。
  内容或版本号变化才会更新日期，启停、重复保存相同版本不会改变日期。
  工作区显示已装配版本的日期，只有显式应用新版本才随之变化。
  历史记录没有日期时显示“未记录”，不推算或迁移旧数据。
- 保存资源库版本不会覆盖现有工作区副本。在工作区 Skill 面板点击
  “应用新版本”才会更新对应作用域；副本有本地修改时返回 409，保留文件。
  新版本应用成功后旧文件仍保留。
- 对来源安装或历史清单记录显式编辑、重新上传，会保存为自定义资源，
  保留 ID，已有绑定开关不变。之后更新内容由中心管理。

### 智能体绑定与会话覆盖

资源库只管理 MCP 定义和 Skill 完整包，不再保存全局 `enabled`。
智能体的 `data.resource_config` 保存默认绑定，会话的
`config.resource_config` 保存额外绑定及开关覆盖。绑定以资源 ID 为键，
重命名不会影响选择。MCP 和 Skill 使用相同的作用域规则：

- 未指定会话覆盖时，所有已有和新会话动态继承智能体默认值。
- 会话显式指定 `true` 或 `false` 后，仅改变当前会话；即使该值恰好
  等于智能体默认值，也保留为显式覆盖。`null` 表示恢复继承。
- 会话可以额外绑定资源。同一个 ID 同时来自智能体和会话时只装配一次，
  会话绑定优先；移除会话绑定后恢复智能体默认。
- 智能体解绑后，旧会话覆盖不再生效；重新绑定会生成新的 `binding_id`，
  不会意外恢复已失效的覆盖。不同 ID 即使内容相同也各自保留。
- Skill 绑定固定完整包版本。更新资源库不会替换已固定的版本，应用新版本
  才改变绑定；会话副本按用户、智能体、会话和绑定隔离，本地修改互不影响。
  会话应用新版本时拒绝覆盖已修改副本，旧版本文件保留。
- 停用或移除绑定保留 Skill 文件。删除会话会清理该会话的安装副本。
  删除资源库记录后绑定显示不可用，新运行不会加载该资源。
- 共享智能体不会自动授予私有 MCP/Skill 的使用权限；需要通过
  `ResourceAccessPolicyBase` 对 `ResourceKind.MCP` / `SKILL` 单独授权。

新的 Chat 运行与 Realtime 连接使用当时解析出的资源快照。已开始的运行、
工具调用和后台任务保留原快照；被淘汰的 MCP 连接在最后一个使用者退出后
关闭。Realtime 资源变更需要重新连接。Skill 开关控制技能清单、提示词和
SkillViewer；文件访问仍沿用工作区的权限系统。

示例服务中的分析和报告扩展也使用同一个会话解析结果：`data-analysis`、
`report-generation` 及用于采集的 MCP 都遵守当前会话开关，Skill 完整性
校验使用绑定固定的包版本。不会再读取资源库的全局开关或旧绑定索引。

Redis 使用 WATCH/MULTI，SQL 使用比较交换，原子保存最新记录上的变更。
会话资源开关可以在运行期间保存；状态写入、自动命名不会覆盖资源配置。
整份配置保存遇到同时发生的资源修改会返回 409，客户端应重新加载。

本次不迁移历史全局开关或旧工作区绑定，不做双写。应重新配置智能体和会话
资源。新安装索引使用 `.resource-installs-*.json`，只记录副本位置，不作为
绑定来源。已有数据没有被自动删除。

部署与备份覆盖 Redis / SQL Storage、Skill 原包和工作区三个位置。
原包默认在 `resource-artifacts/`，可通过 `AGENTSCOPE_SKILL_ARTIFACT_DIR`
设置；绑定状态保存在数据库中。

### 主要管理接口

| 操作 | 接口 |
| --- | --- |
| 直接新增 MCP | `POST /mcp`，请求体包含 `client`，可选 `description`、`version` |
| MCP 资源列表及编辑配置 | `GET /mcp`、`GET /mcp/{id}` |
| 修改 MCP 定义 | `PATCH /mcp/{id}`，支持 `client`、`name`、`description`、`version` |
| 删除 MCP | `DELETE /mcp/{id}` |
| 编写新 Skill | `POST /skill`，请求体包含 `files`，可选 `version` |
| 上传完整 Skill 目录 | `POST /skill/upload`，multipart 字段 `manifest`、`files` |
| 上传完整 Skill 压缩包 | `POST /skill/archive?archive_format=zip`，multipart 字段 `archive` |
| Skill 列表和说明 | `GET /skill`、`GET /skill/{id}` |
| Skill 包文件列表和文本 | `GET /skill/{id}/files`、`GET /skill/{id}/file?path=...` |
| 修改 Skill 文本文件 | `PATCH /skill/{id}/files`，`files` 为相对路径到文本的映射，值为 null 表示删除 |
| 替换完整目录或压缩包 | `POST /skill/{id}/upload`、`POST /skill/{id}/package?archive_format=zip` |
| 删除 Skill | `DELETE /skill/{id}` |
| 智能体默认绑定 | `POST /agent/` / `PATCH /agent/{id}` 的 `resource_config.mcps` / `skills` |
| 会话额外绑定与覆盖 | `POST /sessions/` / `PATCH /sessions/{id}` 的 `resource_config` |
| 会话开关与恢复继承 | `PATCH /workspace/{mcp或skill}/{resource_id}?agent_id=...&session_id=...`，请求体 `{"enabled": true/false/null}` |
| 移除会话额外绑定 | `DELETE /workspace/{mcp或skill}/{resource_id}?agent_id=...&session_id=...` |
| 从资源库装配 | `POST /workspace/mcp/from-library`、`POST /workspace/skill/from-library` |
| 应用工作区 Skill 新版本 | `POST /workspace/skill/{resource_id}/upgrade?agent_id=...&session_id=...` |

接口从登录会话获取用户身份，记录、文件编辑和开关仅作用于当前用户。
同名资源新增返回 409，不会覆盖已有资源。在线编辑保留资源 ID，避免工作区
绑定失效。管理接口不会自动读取旧工作区文件来建立记录或绑定。

### 本地自评自测与验收

```powershell
python tests/run_resource_regression.py --temp-dir .test-tmp
```

前端在 `examples/web_ui` 执行 `pnpm --filter frontend build`。
浏览器自测入口为 `/tests/resource-scopes.html` 与 `/tests/workspace-mcp.html`，
均使用真实 React 组件和模拟 API，不修改用户数据。覆盖绑定、默认停用、
会话启用与恢复继承、失败保持原状态、缺失资源，以及 MCP/Skill 跨会话迟到响应。

后端回归覆盖解析器真值表、动态继承、重绑后旧覆盖失效、额外绑定去重、
用户与共享资源权限、Redis/SQL 并发保存、重启持久化、会话删除清理、
Chat/Realtime 共用装配、MCP 调用期间停用、后台调用连接释放，以及
Skill 版本固定、完整包与二进制保留、本地修改保护和独立副本。
扩展回归还覆盖分析/报告装配、HTTP 权限和会话开关，及 Windows 目录占用
时的有界重试、并发安装与持久错误处理。

2026-10-13 本地验收结果：后端共运行 402 项，399 项通过，3 项要求 POSIX
沙箱的测试在 Windows 跳过；浏览器两组交互测试共 17/17 通过；TypeScript
检查和前端生产构建通过，核心改动的 Flake8 与 `git diff --check` 通过。
浏览器测试使用模拟 API，后端包含真实 SQLite、fakeredis 和本地 STDIO MCP
测试进程；没有连接生产 MCP、模型或真实语音服务。

| 目标 | 自评结果 | 验证范围 |
| --- | --- | --- |
| 全局开关改为智能体默认 | 完成 | 资源库接口不再返回或接受原全局开关，智能体保存默认绑定 |
| 所有会话继承智能体设置 | 完成 | 已有会话动态继承，解绑重绑不会复活旧覆盖 |
| 会话可以额外添加和覆盖 | 完成 | 显式 true/false、null 恢复继承、同 ID 去重及移除后回退 |
| 运行、文件和权限隔离 | 完成 | MCP 调用租约、Skill 独立副本、版本固定、共享资源显式授权 |
| 持久化与既有功能联动 | 完成 | Redis/SQL 并发保存、重启、删除清理、分析/报告扩展 |
| 不做历史兼容 | 完成 | 未新增迁移或双写逻辑；未清除已有用户数据 |

部署验收时可为智能体绑定一个 MCP 和一个带脚本的 Skill，再创建两个会话：
切换默认值应影响两者；对其中一个显式覆盖只影响该会话；恢复继承后再跟随
默认值。然后验证额外绑定、停用保留文件、重启、版本更新及真实 MCP/模型连接。

<img src="https://gw.alicdn.com/imgextra/i2/O1CN01Phmg1G1brIVC8WXyU_!!6000000003518-2-tps-2938-1736.png" alt="Web UI Screenshot" width="100%">

## 独立智能体管理

前端 `/agents` 使用 `/agent-management` 接口统一管理智能体配置。权限从登录态获取：
`admin` 可管理所有用户（含团队创建）的智能体，普通用户只能访问自己的记录。
管理员通过 `owner_id` 指定所属用户；保存配置不会改变智能体所有者。旧用户、智能体、
资源和会话无需迁移，仍按原用户 ID 加载。

- `GET/POST /agent-management`：查询和新建；`GET/PATCH/DELETE /agent-management/{id}`：详情、保存和删除。
- `GET /agent-management/resources`：所属用户可用的资源；现有 MCP/Skill 编辑接口使用 `resource_owner` 显式指定资源所有者，并在服务端校验管理员权限。
- `GET /agent-management/{id}/copy-preview`、`POST /agent-management/{id}/copy`：预览和执行复制。保存、复制均检查 `revision`；复制使用 `request_id` 防止相同请求重复创建。
- `GET /agent-management/{id}/files`：工作空间文件列表；`GET/PUT/DELETE /agent-management/{id}/memory`、`POST /agent-management/{id}/memory/clear`：长期记忆的查看、编辑、删除和清空。

配置保存在 `AgentData.defaults` 和 `memory_config` 中。仅新会话自动继承模型、知识库和
权限，历史会话保留显式值。`PATCH /sessions/{id}` 的 `inherit_defaults` 可逐项恢复继承。
继承解析覆盖 Redis/SQL 存储入口，运行中的会话使用读取时的配置快照；显式 `null` 表示清空。
记忆配置在聊天运行时装配，关闭记忆会移除对应中间件；未设置该字段的旧智能体保留原工厂行为。

复制始终生成新的智能体、资源绑定和工作空间标识。Skill 可保留绑定的完整历史包版本；
独立资源副本使用新名称，修改副本不会改变原资源。跨用户的 Skill/MCP/AI 配置复制到目标用户，
AI 密钥仅在服务端转存。外部服务账号和 API 额度仍是同一账号，知识库索引不会自动克隆。
记忆默认不复制，选择后复制 `Memory/*.md`；业务文件排除隐藏目录、已安装 Skill 和缓存。
单文件限 20 MiB、合计限 50 MiB、最多 1000 个文件。服务拒绝越界路径、链接和正在运行的
工作空间写入；复制失败清理本次新增资源，全部完成后才发布副本。删除智能体沿用原有会话和
定时任务清理流程，资源库和工作空间文件保留；需要删除记忆时请先清空记忆。

隔离测试：

```powershell
$env:TEMP = (Resolve-Path .test-tmp).Path
$env:TMP = $env:TEMP
.venv/Scripts/python.exe -m unittest tests.agent_management_test tests.agent_config_grouping_test tests.session_config_patch_test tests.user_auth_test -q
pnpm --dir examples/web_ui/frontend build
```

测试使用 fakeredis、内存 SQLite 和临时工作空间，不读取生产密钥、不调用付费模型。

## 分析首次图文交付与宿主页面检查

分析、公式构成和分布构成查询使用 `AnalysisTask(mode="ANALYSIS", delivery="DOCUMENT")`，
第一次回答就交付适合问题的图形、解读、明细或关系页面。普通单值、筛选和排序查询保留
`DIRECT/TEXT`。`ANALYSIS/TEXT` 会返回 `INVALID_DELIVERY_MODE`；显式 `DIRECT/DOCUMENT`
同样需要完成页面发布。只改变展示时复用已保存的事实和计算，沿用已确认的报告模板。

宿主可直接检查冻结页面，无需把用户 Cookie 复制到 Playwright，也无需跳转后端预览地址。
在服务所用 Python 环境中安装：

```text
python -m pip install -r requirements-analysis-render.txt
python -m playwright install chromium
```

Linux 部署可使用 `python -m playwright install --with-deps chromium` 安装浏览器及系统依赖；
以实际运行服务的账号安装和验证。当前 Windows 已验证，Linux 仍需部署环境验证。
在 `analysis-config.local.json` 中配置：

```json
"rendering": {
  "mode": "local",
  "widths": [420, 900],
  "webUiUrl": "http://localhost:5173"
}
```

`webUiUrl` 改为部署的 Web UI 源地址。无头检查使用隔离上下文、`sandbox="allow-scripts"`
和冻结的内联资源，阻断网络请求。同一内容摘要复用宽窄两档测量，页面改变后重新检查；
脚本错误仍阻断发布。未安装依赖或检查失败明确保留未检查状态，不伪造通过结果。
需要独立渲染服务时使用 `mode="external"`，地址和凭据继续从 `ANALYSIS_RENDER_URL`、
`ANALYSIS_RENDER_TOKEN` 读取；`widths` 优先于 `ANALYSIS_RENDER_WIDTHS`。

性能信息分为取数、构建、渲染和最终审查；`REPLY_END.metadata.analysisPerformance` 汇总工具
次数、错误、复用和耗时。同一用户轮内只复用同连接、同参数、成功完成的预算只读执行；
部分结果、失败结果、后台续取、新用户问题及明确后续核验均继续执行来源协议。

可选真实浏览器回归：设置 `RUN_ANALYSIS_BROWSER_TESTS=1` 后运行 `analysis_local_render_test`。
默认测试环境未安装浏览器时跳过这两项，不能将跳过记为视觉验收。

## What Next

- Manage your MCPs and Skills directly in their centers. You can customize middlewares or workspace manager implementations in `main.py`.

- Experience the agent service, including
    - human-in-the-loop interactions & permission system
<img src="https://gw.alicdn.com/imgextra/i1/O1CN01vGGiBw20agWwpzmjy_!!6000000006866-2-tps-2934-1732.png" alt="Permission System" width="100%">

    - schedule tasks
<img src="https://gw.alicdn.com/imgextra/i1/O1CN01Xi3Qw71E2haKKu4z0_!!6000000000294-2-tps-2932-1738.png" alt="Schedule Tasks" width="100%">

    - and more! (stay tuned for future updates)
