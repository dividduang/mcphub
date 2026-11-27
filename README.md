# MCP Hub

FBA 应用级插件，把两类外部能力收进同一个入口：

1. **MCP 服务器目录 + 透明代理**：管理员在「MCP服务器」页手动登记 MCP 服务
   （URL + 请求头），插件为每个服务挂一个 `/mcp/ext-<slug>` 端点；用户申请
   工具权限，创建独立范围的 MCP Key。**不是 OAuth 授权服务器**，不接受任意
   代码上传，也不接受客户端自带上游凭证——上游凭证由管理员登记、服务端持有。
2. **平台 API**：把 FBA 自身的 REST 端点（读自进程内 OpenAPI）变成可申请、
   可授权、可用 `X-Api-Key` 调用的资源，与 MCP Key 是两套独立凭证体系。

代码不内置任何服务：目录里有什么完全由管理员决定。初始化默认只种一个 DeepWiki
（免费、无需 API Key），不需要就停用或删掉。

配套前端：https://github.com/dividduang/mcphub_ui

## 安装

- 后端：本仓库根目录内容复制到 FBA 的 `backend/plugin/mcphub/`，不要再套一层目录。
- 前端：https://github.com/dividduang/mcphub_ui 根目录内容复制到
  `apps/web-antdv-next/src/plugins/mcphub/`。
- 依赖：PostgreSQL、Redis，以及支持应用级 `setup`/`lifespan` hooks 的 FBA；
  声明依赖 `ldap_auth` 插件（用户授权页按 AD 账号建档用）。
- Python 依赖：`fastmcp>=3.3.1,<4` 与主工程已有的 `httpx`；fastmcp 只用到
  `transport/dispatch.py` 里的 Host/Origin 白名单中间件，对外请求走 httpx 直连。
  在主工程维护窗口执行 `uv add -r backend/plugin/mcphub/requirements.txt`
  并解决冲突后生成该工程自己的 lock，不要直接替换其他工程的 lock。
- 需要 Linux：单实例执行器锁用 `fcntl.flock`；没有 fcntl 的平台（如 Windows
  开发机）锁降级为跳过并打 ERROR 日志，其余功能照常，但生产部署必须是 Linux。

### 数据库

全新库（ORM `create_all` 之后执行，均可重复执行）：

```sh
psql "$DATABASE_URL" -f backend/plugin/mcphub/sql/postgresql/init.sql
psql "$DATABASE_URL" -f backend/plugin/mcphub/sql/postgresql/menu.sql
```

从 1.x（MySQL 版）升级：先备份并验证可恢复，再按文件名顺序各跑一次
`20261119_01_normalize.sql` ～ `20261122_01_api_key.sql`，最后重跑 `menu.sql`。
各迁移的职责写在文件头注释里：表结构归整、外部登记表、默认种子、退役旧内置
服务与镜像行、恢复镜像行（外键载体）、平台 API 表。

`menu.sql` 可重复执行：插入用户面 / 管理面菜单，并把用户面绑给全部有效角色。
**管理面默认不绑任何角色**——需要授权时在「用户管理 → 角色分配」里把
「MCP Hub 管理后台」勾给对应角色。

## 运行时配置

读取顺序：**进程环境变量 → `plugin.toml` 的 `[settings]` → 代码默认值**。
前两层都改不动既有 `conf.py` / `.env`；`plugin.toml [settings]` 由
`PluginSettingsSource` 并进 pydantic settings，不会写回环境变量，所以配置读取
必须显式回落（`transport/config.py::_setting`），否则域名部署下
`MCPHUB_ALLOWED_HOSTS` 不生效、`/mcp/ext-*` 直接 421。

| 变量 | 默认 | 意义 |
| --- | --- | --- |
| `MCPHUB_PUBLIC_BASE_URL` | 空（回落 `request.base_url`） | **建议**显式设为公开绝对 origin；留空时用浏览器实际访问的主机，域名部署下即为公网 origin |
| `MCPHUB_ALLOWED_HOSTS` | `127.0.0.1,localhost` | MCP 入口 Host 白名单，禁止通配符；经 nginx 域名访问时要含该域名 |
| `MCPHUB_ALLOWED_ORIGINS` | 空 | 浏览器调用需显式可信 Origin |
| `MCPHUB_GLOBAL_LIMIT` | 32 | 在途请求总上限（dispatcher 计数器 + httpx 连接池） |
| `MCPHUB_SITE_RPS` | 4 | 单上游站点速率上限（令牌桶，不等待，超了直接 429） |
| `MCPHUB_STREAM_READ_TIMEOUT` | 300（秒） | 代理转发的流式读超时。MCP 的 `tools/call` 可能是长任务，两次 chunk 间隔几分钟很正常；响应头已发出后超时只能断连，所以这个值就是长任务上限。探测端仍用短超时 |
| `MCPHUB_PROVISION_DEPT_ID` | 1 | LDAP 自动建档的部门 ID（用户授权页按 AD 账号建档时用）。**换库必须改**：部门不存在会让建档用户报「所属部门不存在」 |
| `MCPHUB_PROVISION_ROLE_IDS` | `1` | LDAP 自动建档的角色 ID 列表（逗号分隔）。**换库必须改**：角色停用会把建档用户锁在全角色锁定态 |
| `MCPHUB_LOCK_PATH` | `/tmp/fba-mcphub-executor.lock` | 同机所有进程共用的执行器锁 |

只支持**单执行实例**：`fcntl.flock` 阻止同机第二实例，跨机器多副本不能靠该锁
实现全局配额。

## 权限边界

- 用户面（服务目录 / 我的申请 / 我的 Keys / MCP服务器登记）只操作本人数据，
  菜单无 `perms`；管理面需要数据库当前超管，或 staff 且有效角色带有效
  `sys:mcphub:manage` 菜单权限，管理 REST 逐个 `require_manager`。
- 管理 REST 只接受 FBA 登录 JWT；MCP Key / ApiKey 在 `management_user` 里直接拒绝。
- 用户端点与管理端点在 URL 层面分家：`/requests`、`/grants` 永远只返回本人数据，
  管理视角是 `/admin/requests`、`/admin/grants`，不存在「同一个端点用查询参数
  切权限」的形态。
- MCP Key 与 ApiKey 是两套独立凭证：`mcp_` 前缀的 Key 只走 `/mcp/ext-<slug>`，
  `ak_` 前缀的 Key 只走带 `X-Api-Key` 头的平台 API 请求，互不通用。
- **已知的授权粒度缺口**：外部服务没有工具表，`/mcp/ext-<slug>` 不校验 Key 的
  工具范围——任何 active 的 Key 都能调用已启用的外部服务（上游侧由管理登记的
  请求头把关）。Key 自身仍受 status / 过期 / 用户状态约束，`authenticate_key`
  每次调用都查。同理会话头也不绑定用户：A 拿到的上游 `mcp-session-id` 被 B 用
  自己的 Key 重放，上游会视为同一会话——自建工具 + 共享凭证的场景下可接受，
  但要知道存在这条路径。
- 终端用户的 MCP Key 不上游：入站 `Authorization` 直接丢弃，只注入管理员登记的
  请求头。

## 页面与使用

菜单是一棵子树（`menu.sql` 种三条 `sys_menu` 行）：

```text
MCP Hub          /plugins/mcphub           父级目录，自动 redirect 到第一个子菜单
├── 服务目录      /plugins/mcphub/catalog   用户面（服务目录 / 我的申请 / 我的 Keys）
├── MCP服务器     /plugins/mcphub/external  登记自定义 MCP 服务（管理）
└── 管理后台      /plugins/mcphub/admin     管理面（申请审批 / 用户授权 / 平台 API）
```

1. **服务目录**：列出管理员登记的全部 MCP 服务器——目录就是
   `mcphub_external_server` 的投影，登记即出现、删除即消失、停用仍在列表里
   （`enabled=false`），不存在第二份能往目录里掺行的数据源。选中某个服务时，
   后端按需向上游 `tools/list`（`initialize` 握手 + 透传 `mcp-session-id`，
   跟随同主机重定向，60s TTL 缓存），只读展示工具与参数；上游失败显示行内
   错误 + 重试，不阻断页面。探测成功即把清单 upsert 进 `mcphub_tool`
   （`service/tool_sync.py`）——申请、授权、Key 范围都指向这些本地行。
2. **我的申请 → 我的 Keys**：申请工具权限；创建 Key 时秘密只显示一次，随后
   列表不再返回。每把 Key 行内给出「可用 / 绑定」范围读数；「展开接入信息」
   把服务端点与客户端配置放到同一屏——端点即 `/mcp/ext-<slug>`。
   Key 出站前按 `tools/call` 的工具名校验（`service/auth_service.authorize_tool_call`），
   范围 = Key 绑定 ∩ 有效授权 ∩ 工具 enabled ∩ 服务 enabled；不在范围内的调用
   拿到 403 + JSON-RPC error。
3. **MCP服务器**：登记自定义 MCP 服务，挂载为 `/mcp/ext-<slug>`。表单对齐常见
   MCP 客户端配置：名称 / 备注 / URL / 标识 / 请求头（可增删键值对），并提供
   表单 ⇄ JSON 切换（JSON 用 `mcpServers` 标准形状）。请求头的值可以写
   `${ENV_VAR}`，**出站时才从进程环境展开**，敏感值不必进库；值加密保存，
   `GET /external-servers` 只回名称不回值。**默认种子**：`init.sql`（或升级时的
   `20261120_02_seed_external.sql`）种下一个 DeepWiki
   （`https://mcp.deepwiki.com/mcp`），只 INSERT 不 UPDATE，不覆盖人工决定。
4. **管理后台 → 申请审批 / 用户授权**：勾选本次实际批准项，全不选即拒绝；
   按用户名 / 昵称 / 邮箱搜本地用户授权，或用 AD 账号按 LDAP 建档
   （建档落点见 `MCPHUB_PROVISION_*`）。
5. **管理后台 → 平台 API**：端点是 FBA 自身 REST 路由的投影，由
   `service/api_catalog.py` 从进程内 OpenAPI 摊平（每个 (method, path) 一行），
   启动时首次全量同步、可手动 `POST /api/v1/mcphub/api/refresh`；
   `/auth/*`、`/mcp/*`、`/mcphub/*`、文档端点不开放申请（`is_excluded`）。
   管理员可停用单个端点；用户申请 → 审批 → 创建 ApiKey 时勾选已授权端点。
   ApiKey 形状 `ak_<32hex>.<43secret>`，秘密同样只显示一次。

平台 API 调用方式：请求带 `X-Api-Key: ak_....` 头，由
`transport/api_key_auth.py` 的 ASGI 中间件过五关——Key 存在且 active 且未过期、
插件处于启用态、用户有效、本次请求命中的端点模板在 Key 已授权范围内、路径不在
兜底排除名单。全部通过后用 Key 所属用户身份交换出一枚短期 JWT（写 Redis 会话
登记、不踢用户其他端），改写 authorization 头后放行——后续 JwtAuthMiddleware /
CurrentUser / DependsRBAC 原样生效，权限双层叠加。缓存 30s，吊销 / 改范围 /
端点停用时由写操作路径即时清本实例，多实例部署下最长 30s 传播。

私有 Key 客户端配置形状（用真实一次性秘密替换占位文字，**不要提交此配置**）：

```json
{
  "mcpServers": {
    "deepwiki": {
      "url": "https://<你的域名>/mcp/ext-deepwiki",
      "headers": {"Authorization": "Bearer <一次性MCP Key>"}
    }
  }
}
```

具体客户端是否支持 HTTP headers 取决于该客户端；不能将此通路称为通用 OAuth 兼容。

## 卸载

停服务 → 禁用插件 → 备份并验证可恢复，然后执行
`sql/postgresql/destroy.sql`（需先在会话设置两项确认变量），最后删插件目录。
只删后端不会撤销前端菜单，需一并删。

卸载会删掉：全部 `mcphub_*` 表**含 `mcphub_external_server`**（Fernet 加密的
上游凭证与 URL 在里面），以及菜单行和对应角色绑定。**外部登记表的凭证随表一起
消失，重装不会复活旧登记**——真要保留先
`pg_dump --table=mcphub_external_server`。

## 反馈

问题反馈：https://github.com/dividduang/mcphub/issues。报告问题时提供
FBA / Python / 数据库版本与脱敏错误，不上传 Key、JWT、`.env` 或备份。
此仓库上传不等于插件市场收录。
