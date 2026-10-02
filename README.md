# MCP Hub

FBA应用级插件：发布代码中固定的PyPI与Hacker News爬虫工具，用户申请工具权限，再创建独立范围的MCP Key。**不是OAuth授权服务器**，不接受任意代码上传、任意抓取URL或客户端自带上游凭证。

## 当前交付与部署

本插件已在开发工作区完成 MySQL/Redis 完整 FBA、真实浏览器表单、现代/legacy 协议与本机受控 220 实际出站验收。这是原开发环境的结果，不代表任意 FBA 版本均已验证。本仓库仅包含插件，不含主工程依赖锁、数据库备份或私有部署配置。

支持当前MySQL8.0环境（CHECK保护要求8.0.16+），不声称PostgreSQL迁移可用。新建mcphub六张业务表及一张schema revision表，**不迁移或删除旧FBA API Key**。独立MCP Key仅存SHA256摘要，秘密只在创建时显示一次。

### 安装前提与仓库布局

- 后端仓库：https://github.com/dividduang/mcphub；配套前端：https://github.com/dividduang/mcphub_ui。
- 仓库根目录就是插件内容。安装到 FBA 的 `backend/plugin/mcphub/`，不要再套一层 `mcphub`；前端内容安装到 `apps/web-antdv-next/src/plugins/mcphub/`。
- 需要 Linux（使用 `fcntl.flock`）、MySQL 8.0.16+、Redis，以及支持应用级 `setup`/`lifespan` hooks 的 FBA。Python 和共同依赖版本必须兼容 `requirements.txt` 中的固定 MCP SDK 版本。
- 在主工程维护窗口将插件依赖合并到主工程：`uv add -r backend/plugin/mcphub/requirements.txt`，解决依赖冲突并生成该工程自己的 `uv.lock`，再执行下列命令。不要直接替换其他工程的 lock。
- 原开发主工程额外支持 `FBA_PLUGIN_DEPENDENCIES_LOCKED=1`：依赖不足时拒绝运行期安装。此核心改动不随插件上传，也不会由插件静默修改主工程；其他 FBA 版本未必识别该开关。部署方必须预装并校验依赖，不能仅靠设置开关假定启动已 fail-closed。

从项目根执行，由部署人员在维护窗口统一安装：

```sh
uv sync --locked --all-groups
uv pip check
.venv/bin/python -m backend.plugin.mcphub.scripts.migrate plan
.venv/bin/python -m backend.plugin.mcphub.scripts.migrate upgrade --backup-dir "$HOME/.local/state/mcphub-backups"
MCPHUB_PUBLIC_BASE_URL=http://127.0.0.1:8000 .venv/bin/python -m backend.plugin.mcphub.scripts.serve
```

**当前库 upgrade 前必须先验证私有备份可恢复，并在隔离数据库验证相同升级。** `scripts/restore_snapshot.py` 和 `scripts/migration_regression.py` 提供恢复与回归入口，后者仅接受名称以 `_mcphub_qa` 结尾的数据库；先阅读脚本并使用 `--help`，不得指向生产库。不执行 `fba init`、drop_all 或清库。快照含敏感数据，仅存私有目录，不提交。MySQL DDL 逐条提交，中断可重跑；已存在不兼容结构会拒绝，后续更改必须提供明确 ALTER 版本。destroy SQL 是不可逆卸载，不是升级或回退工具。

部署入口先验证已应用 schema 版本，再用现有 FBA/Granian 启动，强制单 worker、no-reload。只接受 `--host` 和 `--port`，默认 127.0.0.1:8000。入口设置 `FBA_PLUGIN_DEPENDENCIES_LOCKED=1`，该开关是否生效取决于主工程是否支持上述锁定检查。运行前必须完成依赖合并并按自己的共同 lock 安装。若更换服务器或代理，禁止记录含秘密的查询字符串和 Authorization。

## 页面与使用

登录FBA后打开 **MCP Hub**（`/plugins/mcphub`）：

1. 在服务目录选择所需工具并提交申请。默认人工授予；管理员可以显式开启某工具auto_approve，之后的申请才按该策略自动授予。
2. 管理员批准申请中的选定工具，或显式授予；普通用户不能操作他人grant或申请决定。
3. 用户在自己的Key页面选择**已获授予**的工具，创建Key并立即复制一次性秘密。后续列表不再返回秘密。每把Key独立绑定工具和授权generation；同用户两把Key可看到不同tools/list。
4. 把页面提供的绝对服务端点及Bearer Key配置到MCP客户端。端点为`/mcp/pypi`与`/mcp/hackernews`；不要附加`/mcp`后缀，也不要误用前端5173。
5. Key范围只能收缩，扩大范围需要新建Key。撤销Key不可恢复；撤销grant后重新授予不会让旧Key绑定复活。

私有Key客户端配置形状（用真实一次性秘密替换占位文字，**不要提交此配置**）：

```json
{
  "mcpServers": {
    "pypi": {
      "url": "http://127.0.0.1:8000/mcp/pypi",
      "headers": {"Authorization": "Bearer <一次性MCP Key>"}
    }
  }
}
```

具体客户端是否支持HTTP headers取决于该客户端；不能将本通路称为通用OAuth兼容。生产使用HTTPS和实际公开域名。

## 权限边界

- 菜单种子只向当前有效角色添加使用页面，不改用户身份、密码、is_staff、is_superuser或工具grant。
- 管理动作要求数据库当前超管，或staff且有效角色具有有效`sys:mcphub:manage`菜单权限；管理按钮不自动分给任何角色。
- 管理REST仅接受FBA登录JWT。新MCP Key不能调用FBA管理接口，旧FBA API Key不获得MCP工具。MCP Key即使属于超管，也不能绕过自身工具范围。
- `tools/list`与`tools/call`基于Key范围、当前用户grant及服务/工具状态交集；实际出站前和结果交付前重新检查。上游请求已发出后无法撤回，撤销不等于远端工作瞬间停止。
- 固定站点、TLS、响应字节、超时、重定向及DNS规则由Runtime执行；不把MCP Authorization转发到上游，不共享用户私有Cookie。

## Runtime配置

配置使用进程环境，不修改既有FBA conf.py或.env。除明确测试外保留保守默认：

| 变量 | 默认/意义 |
| --- | --- |
| MCPHUB_PUBLIC_BASE_URL | 部署时显式设置公开绝对origin；本机http://127.0.0.1:8000 |
| MCPHUB_ALLOWED_HOSTS | 127.0.0.1,localhost；生产显式逗号分隔域名，禁止通配符 |
| MCPHUB_ALLOWED_ORIGINS | 空列表；浏览器调用需显式可信Origin，CORS不替代Origin保护 |
| MCPHUB_GLOBAL_LIMIT | 32，实际同时执行总上限 |
| MCPHUB_USER_LIMIT | 8，单用户上限 |
| MCPHUB_KEY_LIMIT | 4，单Key上限 |
| MCPHUB_SITE_LIMIT | 4，单站点并发上限 |
| MCPHUB_SITE_RPS | 4，单站点速率上限 |
| MCPHUB_LOCK_PATH | /tmp/fba-mcphub-executor.lock，同机所有进程必须共用 |
| MCPHUB_TEST_MODE | 非1时禁止测试上游 |
| MCPHUB_TEST_UPSTREAM | 仅双开关测试时允许带显式端口的literal-loopback HTTP origin |
| MCPHUB_PYPI_IP / MCPHUB_HACKERNEWS_IP | 默认无；仅部署者可为既有固定域名配置公网IP literal，TLS/SNI仍验证原域名 |

当前只支持**单执行实例**，flock阻止同机第二实例；跨机器多副本不能靠该锁实现全局配额，不支持直接横向复制此配置。线程取消不是requests退出：只有真实工作结束才释放额度。达到上限拒绝/有界行为以Runtime实现为准，不把连接数或排队数当真实执行数。

200+验收只能针对自有本机受控上游，显式调高测试额度/RPS。本次220调用全部成功，runtime与独立上游peak均220，最终active0；这不代表公共站点允许压测或默认生产额度220。生产仍32/8/4/4，站点RPS4。

### Fake-IP DNS环境（QA更正）

本机系统DNS将两个公共域名解析到198.18/15代理fake-IP，默认安全门会拒绝。不要将fake-IP/私网加入允许范围，也不要启用环境代理继承。部署者可通过可信DNS查询核实当前公网A记录，再设置上述两个固定目标pin；实际socket连接pin，Host/SNI/证书仍是pypi.org或hacker-news.firebaseio.com，verify=True，禁止重定向。配置绝不来自工具参数/REST。

当前已验证pin为PyPI151.101.0.223、HN34.120.160.131（2026-10-02查询Google Public DNS HTTPS JSON，见17）。这是进程生命周期内的静态部署值，不自动追随TTL；CDN地址调整、连接失败或证书不匹配时应重新查询并由部署者更新后重启。不得自动回退到未校验地址、关闭证书校验或宣称pin永久有效。

## 变更记录

- 2026-10-02：新增固定爬虫目录、申请/授予与generation绑定、独立摘要Key及MCP transport；新增FBA页面菜单/管理操作边界、共同依赖lock、带备份与形状检查的版本化迁移、隔离恢复与锁定启动入口。保留现有用户和旧API Key数据；真实部署验收由后续QA记录。
- 2026-10-02 QA更正：备份在元数据读事务结束后建立一致快照；MCP写API采用function-scope事务，成功响应前提交，避免新Key立即使用偶发401；Python3.14取消等待采用不取消worker的wait；隔离测试不跨事件循环复用DB池；添加严格公网部署pin及实测容量尾延迟输出。

## 卸载

先停服务并禁用插件，备份数据库且验证恢复；保留数据时只移除前后端插件文件与对应菜单，不执行 destroy SQL。永久删除插件数据时，审阅 `sql/mysql/destroy.sql`（snowflake 配置使用对应版本），按文件开头说明在同一 MySQL 会话设置两项确认变量后再执行。该操作删除 MCP Hub 表、授权与 Key，无法恢复；不删除旧 FBA API Key。最后移除本插件文件，重新检查主工程共同依赖；不要直接卸载其他插件仍依赖的共享包。

## 反馈与发布状态

问题反馈：https://github.com/dividduang/mcphub/issues。前端问题：https://github.com/dividduang/mcphub_ui/issues。报告问题时提供 FBA/Python/数据库版本与脱敏错误，不上传 Key、JWT、`.env` 或备份。

此仓库上传不等于插件市场收录；市场发布还需按 FBA 官方流程提交子模块 PR 并审核。当前单实例/MySQL/私有 Bearer 接入限制不因公开上传而改变。
