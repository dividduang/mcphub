import json

from fastmcp.server.http import HostOriginGuardMiddleware
from starlette.responses import PlainTextResponse

from backend.plugin.mcphub.transport.external_proxy import slug_from_path


async def plugin_enabled() -> bool:
    # Do not use PluginStatusChecker: its missing-cache repair enables the plugin.
    from backend.core.conf import settings
    from backend.database.redis import redis_client

    try:
        raw = await redis_client.get(f"{settings.PLUGIN_REDIS_PREFIX}:mcphub")
        return bool(raw and str(json.loads(raw)["plugin"]["enable"]) == "1")
    except Exception:
        return False


class McpDispatcher:
    """Exact MCP routing outside FBA's JWT, body-reading and credential log layers.

    All non-MCP requests continue through the untouched FBA middleware stack.
    只接管 `/mcp/ext-<slug>`（管理员手动登记的外部 MCP 服务）；其它插件自己的
    `/mcp/*` 端点原样放行，互不干扰。
    """

    def __init__(self, app, hub):
        self.app, self.hub = app, hub
        self.inflight = 0
        self.guarded_mcp = HostOriginGuardMiddleware(
            self.admit_mcp,
            allowed_hosts=hub.config.allowed_hosts,
            allowed_origins=hub.config.allowed_origins,
            mode="strict",
        )

    async def __call__(self, scope, receive, send):
        # lifespan / websocket 等非 HTTP 作用域没有 path，必须先放行
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        if slug_from_path(scope["path"]) is None:
            await self.app(scope, receive, send)
            return
        await self.guarded_mcp(scope, receive, send)

    async def admit_mcp(self, scope, receive, send):
        # Event-loop-owned counter: no task queue before DB/body processing.
        if self.inflight >= self.hub.config.global_limit + 32:
            await PlainTextResponse("MCP request capacity exhausted", status_code=429, headers={"Retry-After": "1"})(
                scope, receive, send
            )
            return
        self.inflight += 1
        try:
            await self.handle_mcp(scope, receive, send)
        finally:
            self.inflight -= 1

    async def handle_mcp(self, scope, receive, send):
        slug = slug_from_path(scope["path"])
        if slug is None:
            await PlainTextResponse("Not found", status_code=404)(scope, receive, send)
            return
        if scope.get("query_string"):
            await PlainTextResponse("MCP query parameters are not supported", status_code=400)(scope, receive, send)
            return
        if not self.hub.ready or not await plugin_enabled():
            await PlainTextResponse("MCP Hub unavailable", status_code=503)(scope, receive, send)
            return
        # 外部服务：透明代理，请求体的读取与出站都在 ExternalRouter 里完成
        await self.hub.external.handle(scope, receive, send, slug)
