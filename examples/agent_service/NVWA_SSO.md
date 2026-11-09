# 女娲 SSO 与智能体模板

需求确认、PR 评审、实现和自测过程见 [实施记录](NVWA_SSO_IMPLEMENTATION.md)。

用户名密码登录继续使用 `/login`；女娲 SSO 是额外入口。后端先向女娲验票，
再按返回的 `userName` 匹配本地用户名（不区分大小写）。已有普通用户保留
原 ID、密码和数据；不存在时创建普通用户。停用、删除的账号和内置 `admin`
不能通过 SSO 登录。仅在 URL 传 `username` 不构成认证。

## 配置来源

在来源智能体编辑页复制 URL，例如：

```text
/agents/5fa05c82019c465794aee1ffa53a3131?owner=wx
```

`agent_id` 是 `/agents/` 后的 ID，`owner_id` 是 `owner` 参数，不是智能体名称。
测试环境已确认这个来源是 `wx` 下的“本体测试1011”。生产环境可以指定
`admin` 下另一个智能体，不按名称搜索，也不会复制该账号的其他智能体。

在被 Git 忽略的 `service-config.local.json` 保留原 `redis`、管理员密码等配置，
合并以下字段，并把 `base_url` 换成后端可访问的女娲地址：

```json
{
  "auth": {
    "cookie_path": "/agentscope",
    "public_base_path": "/agentscope",
    "cookie_secure": true
  },
  "nvwa": {
    "base_url": "https://YOUR_NVWA_HOST",
    "auth_mode": "auto",
    "user_info_path": "/nvwa-user/v1/user/current",
    "validate_path": "/nvwa-certification/v1/ticket/resolve",
    "template_owner_id": "wx",
    "template_agent_id": "5fa05c82019c465794aee1ffa53a3131",
    "mcp_token_ttl_seconds": 3600,
    "mcp_authorization_prefix": ""
  }
}
```

HTTP 本地测试用 `cookie_secure: false`。根路径部署时，把两个路径都设为 `/`。
前端用相同的 `VITE_PUBLIC_BASE_PATH=/agentscope/` 构建；API 自动使用
`/agentscope/api/`。参考 `../web_ui/nginx.agentscope-under-nvwa.conf.example`，
网关去掉 API 前缀并保留外部 Host（含端口）。前端、登录入口与 API 应同源。
SSO 用 HttpOnly Cookie，不把新会话令牌写入网页脚本或浏览器存储。

可通过 `AGENTSCOPE_NVWA_BASE_URL`、`AGENTSCOPE_NVWA_TEMPLATE_OWNER_ID`、
`AGENTSCOPE_NVWA_TEMPLATE_AGENT_ID` 覆盖来源配置，通过
`AGENTSCOPE_PUBLIC_BASE_PATH`、`AGENTSCOPE_COOKIE_PATH` 覆盖部署路径。
未配置女娲地址时 SSO 不启用；未配置模板 ID 时只登录，不自动安装智能体。

多进程部署使用共享 `RedisMessageBus`（参见 `main.py` 的替换示例），使首次
安装、更新和会话运行锁跨进程生效；默认 `InMemoryMessageBus` 适用于单进程。

## 登录约定

- 短 ticket：女娲跳转到 `/agentscope/api/sso/nvwa?ticket=...&next=/chat`，
  后端 POST `validate_path`，请求体为 `{"ticket":"..."}`。
- 业务 token：使用 `?token=...` 或 `?nv_t=...`，后端 GET `user_info_path`，
  以 Authorization 传递。上游明确返回 401/403 时，允许重试 `Bearer ` 前缀。
- `auth_mode=auto` 按入口参数选择方式；也可固定 `bearer` 或 `ticket_post`。
  上游非成功状态、失败业务码、无效 JSON 或缺少用户名都拒绝登录。
  不接受仅解码、未验证签名的 JWT 声明作为身份。

成功响应接受 `data`、`datas` 或 `result` 对象及其 `contextUser` 中的
`userName` / `username` / `loginName`。应在真实女娲环境核对接口和响应契约。
使用用户名匹配意味着两个系统的同名普通账号视为同一用户；用户名应由女娲
可信管理。新建 SSO 用户的本地密码是随机值，如需密码登录，由管理员设置密码。

## 首次安装与手动更新

第一次成功 SSO 给用户安装独立智能体，复制系统提示词、运行/记忆配置、默认
模型选择、固定版本的完整 Skill 包和 HTTP MCP 配置。初始操作权限采用默认值。
智能体、Skill、MCP 和工作空间使用独立 ID；不复制来源的聊天记录、工作文件、
记忆内容或业务 token。重复登录不重复安装，也不自动覆盖用户修改。
首次安装失败会回滚新资源，重新登录可重试。

保留 MCP 和 Skill 名称，以兼容提示词中的名称引用。用户库已有同名的其他资源
时提示重命名，不覆盖原资源。Skill 安装包内容不可变，底层可复用相同内容哈希，
用户的资源记录、绑定和运行副本独立。来源所有者登录时继续使用原智能体。

用户在智能体编辑页点击“检查更新”，预览后“确认更新”。未改动的字段采用来源
新值；双方都修改的字段默认保留个人值，可逐项选择“用来源更新替换我的修改”。
先保存编辑页草稿才能检查更新。预览期间来源或个人配置发生变化时返回冲突，
需要重新检查；有运行中的会话时稍后再更新。

更新保留工作空间、文件、长期记忆内容和会话历史。继承智能体默认配置的已有
会话会使用新默认值；会话里单独设置的模型保留。来源配置失效时已有用户仍能
登录，但检查更新和依赖该来源的资源可能不可用。

## 凭据与 MCP

模型密钥不复制到新用户：后端只授予已通过 SSO 的用户使用来源智能体所引用
模型凭据、知识库的权限。接口隐藏共享密钥，用户不能编辑来源凭据；来源移除
引用后授权立即撤销。知识库共享内容，不复制索引；源账号其他模型、知识库、
工作空间不授予访问权限。模型调用仍使用来源配置的计费账户。

共享知识库内容及模型密钥轮换可立即影响使用，不受“检查更新”控制。
手动同步控制智能体配置与安装包版本，不冻结外部共享服务。

SSO 模板 MCP 只支持 Streamable HTTP。静态 headers、中心参数值和管理者 token
不复制；STDIO、旧 SSE 以及 URL 内嵌凭据/查询参数需要先调整来源或另行配置。
调用时仅向经过校验的来源地址注入当前用户的女娲业务 token；用户修改 MCP
地址或连接配置后不会继续注入 token。当前来源只有一个 HTTP MCP，未配置静态
headers。若服务需要额外固定请求头，应先评估其用途再扩展白名单。

业务 token 保存在独立的私有认证 Redis 键中，有效期默认一小时，登出、密码
变更或账号停用后不能继续使用。业务 token 不进入智能体、MCP 配置或接口响应。
短 ticket 不能直接当业务 token：验票响应必须明确返回 `accessToken` /
`access_token`，或者使用经过服务端验证的 bearer 登录。凭证缺失/到期时需从
女娲重新登录；本地会话续期不会续期业务 token。

`mcp_authorization_prefix` 默认空字符串；MCP 要求 Bearer 规范时设为 `"Bearer "`。
已有带前缀 token 不会重复添加。部署后应使用真实普通用户核对验票、模型调用
及 MCP 用户权限，不能仅以模拟验票测试代替联调。
