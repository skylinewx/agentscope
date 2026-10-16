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

After that, you can set the API endpoint `http://localhost:8000` in the Web UI and start experiencing the agent service.

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

所有接口沿用 `X-User-ID`，记录、文件编辑和开关仅作用于当前用户。
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

## What Next

- Manage your MCPs and Skills directly in their centers. You can customize middlewares or workspace manager implementations in `main.py`.

- Experience the agent service, including
    - human-in-the-loop interactions & permission system
<img src="https://gw.alicdn.com/imgextra/i1/O1CN01vGGiBw20agWwpzmjy_!!6000000006866-2-tps-2934-1732.png" alt="Permission System" width="100%">

    - schedule tasks
<img src="https://gw.alicdn.com/imgextra/i1/O1CN01Xi3Qw71E2haKKu4z0_!!6000000000294-2-tps-2932-1738.png" alt="Schedule Tasks" width="100%">

    - and more! (stay tuned for future updates)
