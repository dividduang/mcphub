"""外部 MCP 服务的透明 JSON-RPC 代理。

做成「原样转发」而不是给每个外部服务再造一个 FastMCP 实例：上游有哪些工具是运行期
才知道的，代理层不需要理解协议，只要把字节搬过去并把该拦的拦掉。

拦住的东西：
- **终端用户的 MCP Key 不上游**：入站 Authorization 直接丢弃，只注入管理员登记的
  请求头。用户即使拿到别人的 Key 也拿不到上游凭证。
- **查询串**：入站请求带查询串就拒（与固定服务一致）。
- **响应体大小**：超过 `max_bytes` 就中断。响应头已经发出，无法改状态码，只能断连。
- **上游响应头**：只回吐 content-type / cache-control 与会话字段
  （mcp-session-id / mcp-protocol-version），丢掉 Set-Cookie / CORS，避免把上游
  会话漏给客户端。
- **并发与站点速率**：走 `BoundedExecutor.network()`，与固定爬虫共用同一套配额。

透传的东西（有状态上游的续命线）：
- **入站 → 上游**：`mcp-session-id` / `mcp-protocol-version` / `last-event-id`。
  fastmcp 的 `http_app()` 默认 `stateless_http=False`（FBA 自家插件全是），初始化后
  的 tools/list 必须带 session，丢头就 400。Authorization / Cookie 仍然永不转发。
- **上游 → 入站**：`mcp-session-id` / `mcp-protocol-version`，否则客户端拿不到会话。

进程级共享一个 `httpx.AsyncClient`：连接池、keep-alive 与 TLS 会话跨请求复用；
由 `hooks.py` 的 lifespan 在关闭执行器时一并 `aclose()`。

不做 SSRF 防护：面向自建部署，上游以内网服务为主，http / https 与私网地址都放行。
上游地址的校验见 service/external_service.validate_url。
"""

import asyncio
import json
import re
from urllib.parse import urlsplit

import httpx

from backend.common.exception import errors
from backend.plugin.mcphub.service.auth_service import authenticate_key
from backend.plugin.mcphub.service.external_service import (
    decode_headers,
    resolve_headers,
)
from backend.plugin.mcphub.transport.config import RuntimeConfig
from backend.plugin.mcphub.transport.executor import BoundedExecutor, CapacityExceeded

_SLUG_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?$")
_PREFIX = "/mcp/ext-"


class UpstreamResponseTooLarge(Exception):
    """响应头已发出后的超限：只能断连，不能再回状态码。"""


def slug_from_path(path: str) -> str | None:
    if not path.startswith(_PREFIX):
        return None
    slug = path[len(_PREFIX) :]
    return slug if _SLUG_RE.match(slug) else None


def _bearer(scope) -> str | None:
    values = [value for key, value in scope.get("headers", ()) if key.lower() == b"authorization"]
    if len(values) != 1:
        return None
    scheme, _, token = values[0].decode("latin-1").partition(" ")
    if scheme.lower() != "bearer":
        return None
    return token


# MCP Streamable HTTP 的会话/恢复字段：入站这几个头要透传给上游，否则有状态上游
# （fastmcp http_app 默认 stateless_http=False，FBA 自家插件全是）会在 initialize
# 之后的 tools/list 报 400 Missing session ID。authorization / cookie 永远不转发。
_PASSTHROUGH_REQUEST_HEADERS = (b"mcp-session-id", b"mcp-protocol-version", b"last-event-id")
# 上游回给客户端的字段：没有会话这两个客户端拿不到 session，同样断链；
# retry-after 让上游的退避提示能到客户端（429 时）。
_PASSTHROUGH_RESPONSE_HEADERS = (b"mcp-session-id", b"mcp-protocol-version", b"retry-after")


def _passthrough_headers(scope) -> dict[str, str]:
    """从入站请求里挑出会话相关头（大小写不敏感）。"""
    picked: dict[str, str] = {}
    for key, value in scope.get("headers", ()):
        lowered = key.lower()
        if lowered in _PASSTHROUGH_REQUEST_HEADERS:
            picked[lowered.decode("latin-1")] = value.decode("latin-1")
    return picked


def _called_tool(body: bytes) -> str | None:
    """从 tools/call 请求体里取工具名；不是 tools/call 或解析不了就返回 None（放行）。"""
    try:
        payload = json.loads(body)
    except Exception:
        return None
    if not isinstance(payload, dict) or payload.get("method") != "tools/call":
        return None
    params = payload.get("params")
    name = params.get("name") if isinstance(params, dict) else None
    return str(name) if isinstance(name, str) and name else None


async def _rpc_error(send, body: bytes, message: str, status: int) -> None:
    """按 JSON-RPC 语义回错误（带上请求的 id），HTTP 状态码用 403。"""
    try:
        request_id = json.loads(body).get("id")
    except Exception:
        request_id = None
    payload = json.dumps({"jsonrpc": "2.0", "id": request_id, "error": {"code": -32000, "message": message}}).encode()
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [(b"content-type", b"application/json"), (b"cache-control", b"no-store")],
        }
    )
    await send({"type": "http.response.body", "body": payload})


def _response_headers(response: httpx.Response) -> list[tuple[bytes, bytes]]:
    """响应头白名单 + 上游会话字段；Set-Cookie / CORS 等一律不回吐。"""
    headers = [
        (b"content-type", (response.headers.get("content-type") or "application/json").encode("latin-1")),
        (b"cache-control", b"private, no-store"),
    ]
    for name in _PASSTHROUGH_RESPONSE_HEADERS:
        value = response.headers.get(name.decode("latin-1"))
        if value:
            headers.append((name, value.encode("latin-1")))
    return headers


async def _read_body(receive, limit: int) -> bytes:
    body = bytearray()
    while True:
        message = await receive()
        body.extend(message.get("body", b""))
        if len(body) > limit:
            raise errors.RequestError(msg="MCP request too large")
        if not message.get("more_body"):
            break
    return bytes(body)


async def _text(send, status: int, text: str, extra: list | None = None):
    payload = json.dumps({"error": text}).encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(payload)).encode("ascii")),
                (b"cache-control", b"private, no-store"),
                *(extra or []),
            ],
        }
    )
    await send({"type": "http.response.body", "body": payload})


class ExternalRouter:
    """一个实例服务所有 `/mcp/ext-<slug>`；slug 从请求路径里取。"""

    def __init__(self, config: RuntimeConfig, executor: BoundedExecutor):
        self.config = config
        self.executor = executor
        self._client: httpx.AsyncClient | None = None
        self._client_lock = asyncio.Lock()
        self._closed = False

    async def client(self) -> httpx.AsyncClient:
        """延迟创建：setup() 是同步的，不能在构造期起连接池。"""
        if self._client is not None:
            return self._client
        async with self._client_lock:
            if self._client is None:
                if self._closed:
                    raise RuntimeError("External MCP proxy is shutting down")
                # 连接池下限必须覆盖 dispatch.admit_mcp 的在途上限（global_limit + 32 的固定余量），
                # 否则超出的请求会先在池里排队到 pool timeout。热连接留 global_limit 条。
                self._client = httpx.AsyncClient(
                    limits=httpx.Limits(
                        max_connections=self.config.global_limit + 32,
                        max_keepalive_connections=self.config.global_limit,
                    ),
                    # read 用流式超时（默认 300s）：MCP 的 tools/call 可能是长任务，
                    # 两个 chunk 之间间隔几分钟很正常；connect/write/pool 仍用短超时。
                    # 响应头已发出后超时只能断连，所以这个值就是长任务的上限。
                    timeout=httpx.Timeout(
                        connect=self.config.connect_timeout,
                        read=self.config.stream_read_timeout,
                        write=self.config.read_timeout,
                        pool=self.config.connect_timeout,
                    ),
                    follow_redirects=False,
                    verify=True,
                )
            return self._client

    async def aclose(self) -> None:
        """幂等：lifespan 正常退出与启动失败都会调用。"""
        self._closed = True
        client, self._client = self._client, None
        if client is not None:
            await client.aclose()

    async def handle(self, scope, receive, send, slug: str) -> None:
        if scope.get("query_string"):
            await _text(send, 400, "MCP query parameters are not supported")
            return
        method = scope["method"]
        if method not in ("POST", "GET", "DELETE"):
            await _text(send, 405, "Method not allowed")
            return

        token = _bearer(scope)
        if not token or not token.startswith("mcp_"):
            await _text(send, 401, "Bearer authentication required", [(b"WWW-Authenticate", b"Bearer")])
            return
        try:
            await authenticate_key(token, slug)
        except Exception:
            await _text(send, 401, "Invalid MCP credential", [(b"WWW-Authenticate", b"Bearer")])
            return

        try:
            body = await _read_body(receive, self.config.max_request_bytes)
        except errors.RequestError:
            await _text(send, 413, "MCP request too large")
            return

        # 工具级授权的落点：只解析 tools/call 的 JSON-RPC 信封拿工具名，
        # 其余方法（initialize / tools/list / ping / notifications）仍是纯字节透传。
        tool_name = _called_tool(body)
        if tool_name is not None:
            from backend.plugin.mcphub.service.auth_service import (
                McpAuthorizationError,
                authorize_tool_call,
            )

            try:
                await authorize_tool_call(token, slug, tool_name)
            except McpAuthorizationError as exc:
                await _rpc_error(send, body, str(exc), 403)
                return
            except Exception:
                await _text(send, 401, "Invalid MCP credential", [(b"WWW-Authenticate", b"Bearer")])
                return

        from sqlalchemy import select

        from backend.database.db import async_db_session
        from backend.plugin.mcphub.model.entities import McpExternalServer

        async with async_db_session() as db:
            row = await db.scalar(select(McpExternalServer).where(McpExternalServer.slug == slug))
            if row is None or not row.enabled:
                await _text(send, 404, "External MCP server unavailable")
                return
            url = row.url
            headers = self._outbound_headers(row)

        # 客户端带来的会话/恢复头原样透传（管理员登记的请求头之上，客户端状态优先）
        headers.update(_passthrough_headers(scope))

        site = urlsplit(url).hostname or slug
        started = False
        try:
            client = await self.client()
            with self.executor.network(site):
                async with client.stream(method, url, headers=headers, content=body) as response:
                    await send(
                        {
                            "type": "http.response.start",
                            "status": response.status_code,
                            "headers": _response_headers(response),
                        }
                    )
                    started = True
                    # 边读边写：SSE / 长任务的首个 chunk 立刻到达客户端。
                    # 不给 content-length，让 ASGI 服务器用 chunked。
                    # 不能传 chunk_size：aiter_bytes 会攒满该大小才吐，流式就没了。
                    total = 0
                    async for chunk in response.aiter_bytes():
                        total += len(chunk)
                        if total > self.config.max_bytes:
                            raise UpstreamResponseTooLarge("Upstream response exceeds byte limit")
                        await send({"type": "http.response.body", "body": chunk, "more_body": True})
                    await send({"type": "http.response.body", "body": b"", "more_body": False})
        except CapacityExceeded:
            # 关停窗口不是限流：给 503 + Retry-After，让客户端知道稍后重试而不是退避
            if self.executor.closing:
                await _text(send, 503, "MCP Hub is shutting down", [(b"Retry-After", b"1")])
                return
            await _text(send, 429, "Upstream capacity exhausted", [(b"Retry-After", b"1")])
            return
        except UpstreamResponseTooLarge:
            # 响应头已发出：把异常抛给服务器，让它直接断连；客户端看到的是不完整响应。
            raise
        except Exception:
            # 不把上游/httpx 的异常细节透给客户端
            if started:
                raise
            await _text(send, 502, "Upstream request failed")

    def _outbound_headers(self, row) -> dict[str, str]:
        # 全部小写：HTTP 头大小写不敏感，但大小写混用会在 dict 里变成两个 key
        # （管理员登记一个小写 content-type 就会和固定头发重复）
        headers = {
            "content-type": "application/json",
            "accept": "application/json, text/event-stream",
        }
        # 管理员登记的请求头；${ENV_VAR} 在这里才展开，密文/占位符都不出这个进程
        registered = resolve_headers(decode_headers(row.headers_ciphertext))
        headers.update({key.lower(): value for key, value in registered.items()})
        return headers
