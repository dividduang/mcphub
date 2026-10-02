import asyncio
import inspect
import json

from concurrent.futures import TimeoutError as FutureTimeout

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.auth import AccessToken, TokenVerifier
from fastmcp.server.dependencies import get_access_token

from backend.plugin.mcphub.crawlers.catalog import TOOLS, ToolSpec
from backend.plugin.mcphub.crawlers.network import Fetcher, UpstreamError
from backend.plugin.mcphub.service.auth_service import (
    McpPrincipal, authenticate_key, authorize_tool, current_tools,
)
from backend.plugin.mcphub.transport.config import RuntimeConfig
from backend.plugin.mcphub.transport.executor import BoundedExecutor, CapacityExceeded, ExecutionStopped


async def plugin_enabled() -> bool:
    # Do not use PluginStatusChecker: its missing-cache repair enables the plugin.
    from backend.core.conf import settings
    from backend.database.redis import redis_client

    try:
        raw = await redis_client.get(f'{settings.PLUGIN_REDIS_PREFIX}:mcphub')
        return bool(raw and str(json.loads(raw)['plugin']['enable']) == '1')
    except Exception:
        return False


def principal_from_token(token, slug: str) -> McpPrincipal:
    if token is None or token.claims.get('server_slug') != slug:
        raise ToolError('MCP credential is unavailable for this server')
    return McpPrincipal(key_id=token.claims['key_id'], user_id=token.claims['user_id'],
                        server_slug=slug, tool_names=frozenset())


async def authorize(principal: McpPrincipal, tool_name: str) -> None:
    if not await plugin_enabled():
        raise ToolError('MCP Hub is disabled or unavailable')
    await authorize_tool(principal, tool_name)


class DatabaseTokenVerifier(TokenVerifier):
    def __init__(self, slug: str):
        super().__init__()
        self.slug = slug

    async def verify_token(self, token: str) -> AccessToken | None:
        if not await plugin_enabled():
            return None
        try:
            principal = await authenticate_key(token, self.slug)
        except Exception:
            # Invalid credentials and unavailable authority both fail closed.
            return None
        return AccessToken(token=token, client_id=principal.key_id, scopes=sorted(principal.tool_names),
                           claims={'key_id': principal.key_id, 'user_id': principal.user_id,
                                   'server_slug': principal.server_slug})


def make_auth_check(slug: str, tool_name: str):
    async def check(context) -> bool:
        try:
            principal = principal_from_token(context.token, slug)
            return await plugin_enabled() and tool_name in await current_tools(principal)
        except Exception:
            return False
    return check


def make_tool(spec: ToolSpec, slug: str, config: RuntimeConfig, executor: BoundedExecutor):
    async def call(**arguments) -> dict:
        try:
            values = spec.arguments.model_validate(arguments).model_dump()
            principal = principal_from_token(get_access_token(), slug)
            await authorize(principal, spec.name)
            loop = asyncio.get_running_loop()

            def work(stopped):
                def check_authority():
                    future = asyncio.run_coroutine_threadsafe(authorize(principal, spec.name), loop)
                    try:
                        future.result(timeout=config.read_timeout)
                    except FutureTimeout:
                        future.cancel()
                        raise UpstreamError('Authorization authority timed out') from None

                fetch = Fetcher(config, executor, slug, check_authority, stopped)
                return spec.function(fetch, **values)

            result = await executor.execute(principal.user_id, principal.key_id, slug, work)
            await authorize(principal, spec.name)
            return result
        except (CapacityExceeded, UpstreamError, ExecutionStopped) as exc:
            raise ToolError(str(exc)) from None
        except asyncio.CancelledError:
            raise
        except Exception:
            # Do not expose ORM/requests exception details, input bodies or credentials.
            raise ToolError('Tool unavailable, unauthorized, or invalid upstream response') from None

    original = inspect.signature(spec.function)
    call.__signature__ = original.replace(parameters=list(original.parameters.values())[1:])
    call.__annotations__ = {name: hint for name, hint in spec.function.__annotations__.items() if name != 'fetch'}
    call.__name__ = spec.name
    call.__doc__ = spec.function.__doc__
    return call


def make_apps(config: RuntimeConfig, executor: BoundedExecutor) -> dict:
    apps = {}
    for slug, tools in TOOLS.items():
        server = FastMCP(name=f'MCP Hub: {slug}', auth=DatabaseTokenVerifier(slug))
        for spec in tools:
            server.tool(name=spec.name, auth=make_auth_check(slug, spec.name))(
                make_tool(spec, slug, config, executor)
            )
        apps[f'/mcp/{slug}'] = server.http_app(
            path=f'/mcp/{slug}', transport='streamable-http', json_response=True,
            stateless_http=False, host_origin_protection=True,
            allowed_hosts=list(config.allowed_hosts), allowed_origins=list(config.allowed_origins),
            session_idle_timeout=300,
        )
    return apps
