import hashlib
import hmac
import re

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.common.exception import errors
from backend.common.security.jwt import get_current_user
from backend.database.db import async_db_session
from backend.plugin.mcphub.crud.hub import effective_tools
from backend.plugin.mcphub.model.entities import McpKey, McpServer
from backend.utils.timezone import timezone


class McpAuthenticationError(Exception):
    """Invalid credential, user or endpoint; safe to expose without credential detail."""


class McpAuthorizationError(Exception):
    """The current key delegation does not permit the requested tool."""


@dataclass(frozen=True)
class McpPrincipal:
    key_id: str
    user_id: int
    server_slug: str
    tool_names: frozenset[str]


def secret_digest(secret: str) -> str:
    return hashlib.sha256(secret.encode('ascii')).hexdigest()


async def _current(db: AsyncSession, key_id: str, server_slug: str, user_id: int | None = None) -> tuple[McpKey, McpServer]:
    key = await db.get(McpKey, key_id)
    if (
        key is None or key.status != 'active'
        or (key.expire_time is not None and key.expire_time <= timezone.now())
        or (user_id is not None and key.user_id != user_id)
    ):
        raise McpAuthenticationError('Invalid MCP credential')
    try:
        # Deliberately bypass the JWT Redis user cache for every MCP authorization.
        await get_current_user(db, key.user_id)
    except (errors.TokenError, errors.AuthorizationError) as exc:
        raise McpAuthenticationError('Inactive MCP user') from exc
    server = await db.scalar(select(McpServer).where(McpServer.slug == server_slug, McpServer.enabled.is_(True)))
    if server is None:
        raise McpAuthenticationError('Unavailable MCP server')
    return key, server


async def authenticate_key(token: str, server_slug: str) -> McpPrincipal:
    match = re.fullmatch(r'mcp_([0-9a-f]{32})\.([A-Za-z0-9_-]{43})', token)
    if match is None:
        raise McpAuthenticationError('Invalid MCP credential')
    async with async_db_session() as db:
        key, server = await _current(db, match.group(1), server_slug)
        if not hmac.compare_digest(key.secret_hash, secret_digest(token)):
            raise McpAuthenticationError('Invalid MCP credential')
        tools = await effective_tools(db, key.id, key.user_id, server.id)
        return McpPrincipal(key.id, key.user_id, server.slug, frozenset(tool.name for tool in tools))


async def current_tools(principal: McpPrincipal) -> frozenset[str]:
    async with async_db_session() as db:
        key, server = await _current(db, principal.key_id, principal.server_slug, principal.user_id)
        tools = await effective_tools(db, key.id, key.user_id, server.id)
        return frozenset(tool.name for tool in tools)


async def authorize_tool(principal: McpPrincipal, tool_name: str) -> None:
    if tool_name not in await current_tools(principal):
        raise McpAuthorizationError('Tool is not authorized for this key')
