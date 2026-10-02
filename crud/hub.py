from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.admin.model.user import User
from backend.common.exception import errors
from backend.plugin.mcphub.model.entities import McpGrant, McpKeyBinding, McpServer, McpTool


async def lock_user(db: AsyncSession, user_id: int, *, require_active: bool = True) -> User:
    user = await db.scalar(select(User).where(User.id == user_id, User.deleted == 0).with_for_update())
    if user is None or (require_active and not user.status):
        raise errors.NotFoundError(msg='User not found or inactive')
    return user


async def available_tools(db: AsyncSession, tool_ids: list[str]) -> list[McpTool]:
    tools = list((await db.scalars(
        select(McpTool).join(McpServer, McpTool.server_id == McpServer.id).where(
            McpTool.id.in_(tool_ids), McpTool.enabled.is_(True), McpServer.enabled.is_(True)
        )
    )).all())
    if len(tools) != len(set(tool_ids)):
        raise errors.RequestError(msg='Unknown or disabled tool')
    return tools


async def effective_tools(db: AsyncSession, key_id: str, user_id: int, server_id: str | None = None) -> list[McpTool]:
    query = (
        select(McpTool)
        .join(McpServer, McpTool.server_id == McpServer.id)
        .join(McpKeyBinding, McpKeyBinding.tool_id == McpTool.id)
        .join(McpGrant, and_(
            McpGrant.tool_id == McpTool.id,
            McpGrant.user_id == user_id,
            McpGrant.generation == McpKeyBinding.generation,
        ))
        .where(
            McpKeyBinding.key_id == key_id, McpGrant.active.is_(True),
            McpTool.enabled.is_(True), McpServer.enabled.is_(True),
        )
    )
    if server_id is not None:
        query = query.where(McpServer.id == server_id)
    return list((await db.scalars(query)).all())
