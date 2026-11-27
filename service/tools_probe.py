"""上游工具枚举：给「服务目录」展示每个外部 MCP 服务有哪些工具。

透明代理本身不解析协议（见 transport/external_proxy.py），工具清单只在浏览目录时
按需向上游拉一次：

* 严格按 MCP 客户端礼仪握手：`initialize` → `notifications/initialized` →
  `tools/list`，并透传上游返回的 `mcp-session-id`——FBA 自家插件（fastmcp，
  `stateless_http=False`）强制要求这个握手，跳过会直接报错；
* `tools/list` 跟随 `nextCursor` 翻页（有上游一页只返部分工具，不翻会静默截断）；
* 响应同时解 SSE（`event: message` + `data:` 行）与纯 JSON 两种形态；
* 进程内 60s TTL 缓存：翻页 / 切服务不会反复打上游；服务被改（URL / 请求头变了）
  或删掉时主动失效；
* 任何失败都向上抛 `UpstreamToolsError`。**消息只做分类，不带上游原文**——上游
  的 error.message 可能回显 URL / 主机名 / 内部细节，而这个端点任何登录用户都能调；
  细节只进服务端日志。

展示用途：工具清单只说明「上游提供什么」。调用鉴权不经过工具范围
（外部服务没有工具表，见 README「授权粒度缺口」），任何 active 的 Key 都能调用。
"""

import json
import logging
import time
from urllib.parse import urlsplit

import httpx

from backend.plugin.mcphub.service.external_service import (
    McpExternalServer,
    decode_headers,
    missing_env_vars,
    resolve_headers,
)

logger = logging.getLogger(__name__)

_PROBE_TIMEOUT = httpx.Timeout(connect=3.0, read=6.0, write=3.0, pool=3.0)
_TTL_SECONDS = 60.0
_MAX_PAGES = 20  # tools/list 翻页上限，防上游用 nextCursor 把我们赖住
_CLIENT_INFO = {"name": "fba-mcphub-catalog", "version": "1.0"}

# server_id -> (fetched_at, tools)
_CACHE: dict[str, tuple[float, list[dict]]] = {}
# (user_id, server_id) -> 上次强制刷新的 monotonic 时刻；端点按它做按用户去抖
_REFRESH_STAMPS: dict[tuple[int, str], float] = {}


def refresh_stamp(user_id: int, server_id: str, now: float) -> float:
    """记录一次强制刷新，返回上一次的时刻（没有则 0）。"""
    key = (user_id, server_id)
    previous = _REFRESH_STAMPS.get(key, 0.0)
    _REFRESH_STAMPS[key] = now
    return previous


class UpstreamToolsError(Exception):
    """上游工具清单拉取失败；消息安全到可以直接展示给用户（只含分类）。"""


def invalidate(server_id: str) -> None:
    """服务被改 / 删后调用：缓存里的旧工具清单不能再用了。"""
    _CACHE.pop(server_id, None)


def _rpc(method: str, request_id: int, cursor: str | None = None) -> dict:
    if method == "initialize":
        params = {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": _CLIENT_INFO,
        }
    elif method == "tools/list":
        params = {"cursor": cursor} if cursor else {}
    else:
        params = {}
    return {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}


def _notification(method: str) -> dict:
    """JSON-RPC 通知：没有 id，上游不得回复。"""
    return {"jsonrpc": "2.0", "method": method, "params": {}}


def _payload(response: httpx.Response) -> dict:
    """解出单个 JSON-RPC 对象：纯 JSON 或 SSE（取最后一条 message 的 data）。"""
    text = response.text.strip()
    if text.startswith("{"):
        return response.json()
    data = None
    for line in text.splitlines():
        if line.startswith("data:"):
            data = line[5:].strip()
    if not data:
        raise UpstreamToolsError("上游返回了无法识别的响应")
    return json.loads(data)


def _tools_of(payload: dict) -> tuple[list[dict], str | None]:
    """解一页 tools/list；返回 (tools, nextCursor)。上游 error 只分类不透原文。"""
    result = payload.get("result") if isinstance(payload, dict) else None
    if not isinstance(result, dict) or not isinstance(result.get("tools"), list):
        if isinstance(payload, dict) and isinstance(payload.get("error"), dict):
            # 上游的错误文案可能回显 URL / 主机名 / 内部细节：只记日志，不给用户
            logger.info("MCP Hub tools/list 上游错误: %s", json.dumps(payload["error"])[:500])
            raise UpstreamToolsError("上游拒绝了工具列表请求")
        raise UpstreamToolsError("上游未返回工具清单")
    tools = []
    for item in result["tools"]:
        if not isinstance(item, dict) or not item.get("name"):
            continue
        annotations = item.get("annotations") if isinstance(item.get("annotations"), dict) else {}
        tools.append(
            {
                "name": str(item["name"]),
                "title": str(item.get("title") or annotations.get("title") or item["name"]),
                "description": str(item.get("description") or ""),
                "input_schema": item.get("inputSchema") or item.get("input_schema") or {},
            }
        )
    cursor = result.get("nextCursor")
    return tools, (str(cursor) if cursor else None)


async def fetch_tools(row: McpExternalServer, *, refresh: bool = False) -> list[dict]:
    """向上游拉工具清单；命中 60s 缓存且未要求刷新时直接返回。"""
    cached = _CACHE.get(row.id)
    if cached and not refresh and time.monotonic() - cached[0] < _TTL_SECONDS:
        return cached[1]

    registered = decode_headers(row.headers_ciphertext)
    absent = missing_env_vars(registered)
    if absent:
        # 变量名只进日志：这个端点任何登录用户都能调，names 可能是 secret 命名
        logger.warning("MCP Hub 工具枚举 slug=%s 请求头引用未设置的环境变量: %s", row.slug, ", ".join(absent))
        raise UpstreamToolsError("服务配置不完整（请求头引用了未设置的环境变量）")

    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        **resolve_headers(registered),
    }
    origin_host = urlsplit(row.url).hostname

    async def guard(request: httpx.Request) -> None:
        # 跟随重定向可以（上游 307 到带尾斜杠的地址很常见，FBA 自家插件就这样），
        # 但换了主机就把管理员登记的请求头摘掉：上游凭证不离开登记时的主机。
        if urlsplit(str(request.url)).hostname != origin_host:
            for key in [k for k in request.headers if k.lower() not in ("content-type", "accept")]:
                del request.headers[key]

    try:
        async with httpx.AsyncClient(
            timeout=_PROBE_TIMEOUT,
            follow_redirects=True,
            verify=True,
            event_hooks={"request": [guard]},
        ) as client:
            handshake = await client.post(row.url, headers=headers, json=_rpc("initialize", 1))
            if handshake.status_code >= 400:
                raise UpstreamToolsError(f"上游 initialize 返回 {handshake.status_code}")
            session = handshake.headers.get("mcp-session-id")
            if session:
                headers["mcp-session-id"] = session
            # MCP 规范要求客户端 initialize 之后发这个通知；严格实现会因缺失拒绝后续请求
            await client.post(row.url, headers=headers, json=_notification("notifications/initialized"))
            tools: list[dict] = []
            cursor = None
            for page_index in range(_MAX_PAGES):
                listed = await client.post(row.url, headers=headers, json=_rpc("tools/list", 2, cursor))
                if listed.status_code >= 400:
                    raise UpstreamToolsError(f"上游 tools/list 返回 {listed.status_code}")
                page_tools, cursor = _tools_of(_payload(listed))
                tools.extend(page_tools)
                if not cursor:
                    break
            else:
                raise UpstreamToolsError("上游工具列表页数过多，已停止拉取")
    except UpstreamToolsError:
        raise
    except httpx.HTTPError as exc:
        # 不把 httpx 的异常细节（可能含 URL / 主机名）透给页面
        logger.warning("MCP Hub 工具枚举网络失败 slug=%s: %s", row.slug, type(exc).__name__)
        raise UpstreamToolsError("上游连接失败") from exc
    except Exception as exc:
        logger.warning("MCP Hub 工具枚举解析失败 slug=%s: %s", row.slug, type(exc).__name__)
        raise UpstreamToolsError("上游响应无法解析") from exc

    _CACHE[row.id] = (time.monotonic(), tools)
    # 稳定行键：跨服务同名工具常见（都叫 search），表格复用需要 <serverId>::<name>
    for tool in tools:
        tool["id"] = f"{row.id}::{tool['name']}"
    return tools


def probe_url(row: McpExternalServer) -> str:
    """站点键：与代理同口径按主机名限速。"""
    return urlsplit(row.url).hostname or row.slug
