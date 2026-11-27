import hashlib
import hmac
import re

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.common.exception import errors
from backend.common.security.jwt import get_current_user
from backend.database.db import async_db_session
from backend.plugin.mcphub.crud.hub import effective_tools
from backend.plugin.mcphub.model.entities import McpExternalServer, McpKey, McpServer
from backend.utils.timezone import timezone


class McpAuthenticationError(Exception):
    """Invalid credential, user or endpoint; safe to expose without credential detail."""


class McpAuthorizationError(Exception):
    """Key 的范围不含请求的工具；消息安全到可以直接给客户端。"""


def secret_digest(secret: str) -> str:
    return hashlib.sha256(secret.encode("ascii")).hexdigest()


async def _current(db: AsyncSession, key_id: str, slug: str) -> tuple[McpKey, McpExternalServer]:
    key = await db.get(McpKey, key_id)
    if (
        key is None
        or key.status != "active"
        or (key.expire_time is not None and key.expire_time <= timezone.now())
    ):
        raise McpAuthenticationError("Invalid MCP credential")
    try:
        # Deliberately bypass the JWT Redis user cache for every MCP authorization.
        await get_current_user(db, key.user_id)
    except (errors.TokenError, errors.AuthorizationError) as exc:
        raise McpAuthenticationError("Inactive MCP user") from exc
    # 端点存不存在的唯一真相就是外部登记表：目录、鉴权、代理三方同源，
    # 停用（enabled=false）与删除都立刻在鉴权上生效。
    server = await db.scalar(
        select(McpExternalServer).where(McpExternalServer.slug == slug, McpExternalServer.enabled.is_(True))
    )
    if server is None:
        raise McpAuthenticationError("Unavailable MCP server")
    return key, server


async def authenticate_key(token: str, slug: str) -> None:
    """校验 MCP Key 能否调用某个外部服务（slug 即登记时的标识，无 ext- 前缀）。

    只管 Key 自身：status / 过期时间 / 用户状态，加服务登记行 enabled。
    工具级范围在 authorize_tool_call 里单独校验（tools/call 出站前）。
    """
    match = re.fullmatch(r"mcp_([0-9a-f]{32})\.([A-Za-z0-9_-]{43})", token)
    if match is None:
        raise McpAuthenticationError("Invalid MCP credential")
    async with async_db_session() as db:
        key, _ = await _current(db, match.group(1), slug)
        if not hmac.compare_digest(key.secret_hash, secret_digest(token)):
            raise McpAuthenticationError("Invalid MCP credential")


async def authorize_tool_call(token: str, slug: str, tool_name: str) -> None:
    """tools/call 出站前的工具级校验：tool_name 必须在该 Key 的有效范围内。

    有效范围 = Key 绑定 ∩ 用户有效授权 ∩ 工具 enabled ∩ 服务 enabled
    （crud.hub.effective_tools）。没有绑定的 Key 什么都没有——这正是「批准才能用」的
    落点：3.0 之前「任意 Key 调全部工具」的缺口在这里关闭。
    """
    match = re.fullmatch(r"mcp_([0-9a-f]{32})\.([A-Za-z0-9_-]{43})", token)
    if match is None:
        raise McpAuthenticationError("Invalid MCP credential")
    async with async_db_session() as db:
        key, _ = await _current(db, match.group(1), slug)
        if not hmac.compare_digest(key.secret_hash, secret_digest(token)):
            raise McpAuthenticationError("Invalid MCP credential")
        mirror = await db.scalar(select(McpServer).where(McpServer.slug == f"ext-{slug}"))
        if mirror is None:
            raise McpAuthorizationError("Service has no synchronized tools")
        allowed = {tool.name for tool in await effective_tools(db, key.id, key.user_id, mirror.id)}
        if tool_name not in allowed:
            raise McpAuthorizationError("Tool is not authorized for this key")
