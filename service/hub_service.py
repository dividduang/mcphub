import secrets

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.common.exception import errors
from backend.plugin.mcphub.crud.hub import available_tools, effective_tools, lock_user
from backend.plugin.mcphub.model.entities import McpGrant, McpKey, McpKeyBinding, McpRequest, McpServer, McpTool, new_id
from backend.plugin.mcphub.schema.hub import CreateKey, CreateRequest, UpdateKey
from backend.plugin.mcphub.service.auth_service import secret_digest
from backend.utils.timezone import timezone


def can_manage(user) -> bool:
    return bool(user.is_superuser or (user.is_staff and any(
        'sys:mcphub:manage' in (menu.perms or '').split(',')
        for role in user.roles if role.status
        for menu in role.menus if menu.status
    )))


def require_manager(user) -> None:
    if not can_manage(user):
        raise errors.ForbiddenError(msg='MCP Hub management permission required')


def request_data(row: McpRequest) -> dict:
    return {
        'id': row.id, 'user_id': str(row.user_id), 'tool_ids': row.tool_ids,
        'granted_tool_ids': row.granted_tool_ids, 'status': row.status, 'note': row.note,
        'created_time': row.created_time, 'decided_by': str(row.decided_by) if row.decided_by else None,
    }


def grant_data(row: McpGrant) -> dict:
    return {
        'id': row.id, 'user_id': str(row.user_id), 'tool_id': row.tool_id,
        'generation': row.generation, 'active': row.active, 'source': row.source,
        'created_time': row.created_time,
    }


def tool_data(row: McpTool, granted: bool = False) -> dict:
    return {
        'id': row.id, 'name': row.name, 'title': row.title, 'description': row.description,
        'enabled': row.enabled, 'auto_approve': row.auto_approve,
        'input_schema': row.input_schema, 'granted': granted,
    }


def server_data(row: McpServer, base_url: str) -> dict:
    return {
        'id': row.id, 'slug': row.slug, 'name': row.name, 'description': row.description,
        'enabled': row.enabled, 'endpoint': f'{base_url.rstrip("/")}/mcp/{row.slug}',
    }


async def catalog(db: AsyncSession, user_id: int, base_url: str) -> dict:
    servers = (await db.scalars(select(McpServer).order_by(McpServer.slug))).all()
    tools = (await db.scalars(select(McpTool).order_by(McpTool.created_time, McpTool.name))).all()
    grants = set((await db.scalars(select(McpGrant.tool_id).where(
        McpGrant.user_id == user_id, McpGrant.active.is_(True)
    ))).all())
    return {'servers': [dict(server_data(server, base_url), tools=[
        tool_data(tool, tool.id in grants and tool.enabled and server.enabled)
        for tool in tools if tool.server_id == server.id
    ]) for server in servers]}


async def page_rows(db: AsyncSession, query, page: int, size: int) -> tuple[list, int]:
    total = await db.scalar(select(func.count()).select_from(query.order_by(None).subquery()))
    rows = list((await db.scalars(query.offset((page - 1) * size).limit(size))).all())
    return rows, total or 0


async def grants_for(db: AsyncSession, user_id: int) -> list[dict]:
    rows = (await db.scalars(select(McpGrant).where(McpGrant.user_id == user_id).order_by(McpGrant.created_time))).all()
    return [grant_data(row) for row in rows]


async def _grant(db: AsyncSession, user_id: int, tool_ids: list[str], source: str, actor_id: int | None) -> None:
    # Caller holds the target user's row lock, also taken by every key mutation.
    await available_tools(db, tool_ids)
    rows = {row.tool_id: row for row in (await db.scalars(select(McpGrant).where(
        McpGrant.user_id == user_id, McpGrant.tool_id.in_(tool_ids)
    ).with_for_update())).all()}
    for tool_id in tool_ids:
        row = rows.get(tool_id)
        if row is None:
            db.add(McpGrant(user_id=user_id, tool_id=tool_id, source=source, granted_by=actor_id))
        elif not row.active:
            row.active = True
            row.generation = new_id()
            row.source = source
            row.granted_by = actor_id
    await db.flush()


async def submit_request(db: AsyncSession, user_id: int, obj: CreateRequest) -> dict:
    await lock_user(db, user_id)
    tools = await available_tools(db, obj.tool_ids)
    approved = [tool.id for tool in tools if tool.auto_approve]
    await _grant(db, user_id, approved, 'policy', None)
    row = McpRequest(
        user_id=user_id, tool_ids=obj.tool_ids, granted_tool_ids=approved, note=obj.note,
        status='approved' if len(approved) == len(obj.tool_ids) else ('partial' if approved else 'pending'),
    )
    db.add(row)
    await db.flush()
    return request_data(row)


async def decide_request(db: AsyncSession, request_id: str, approved: list[str], actor_id: int) -> dict:
    row = await db.get(McpRequest, request_id)
    if row is None:
        raise errors.NotFoundError(msg='Request not found')
    await lock_user(db, row.user_id)
    await db.refresh(row, with_for_update=True)
    if row.decided_by is not None or row.status not in {'pending', 'partial'}:
        raise errors.ConflictError(msg='Request has already been decided')
    if not set(approved).issubset(row.tool_ids) or len(set(approved)) != len(approved):
        raise errors.RequestError(msg='Approval must be a subset of requested tools')
    await _grant(db, row.user_id, approved, 'administrator', actor_id)
    # Prior policy grants are audit history, not silently revoked by this decision.
    row.granted_tool_ids = sorted(set(row.granted_tool_ids) | set(approved))
    row.status = 'approved' if len(row.granted_tool_ids) == len(row.tool_ids) else (
        'partial' if row.granted_tool_ids else 'rejected'
    )
    row.decided_by = actor_id
    await db.flush()
    return request_data(row)


async def grant_tools(db: AsyncSession, user_id: int, tool_ids: list[str], actor_id: int) -> list[dict]:
    await lock_user(db, user_id)
    await _grant(db, user_id, tool_ids, 'administrator', actor_id)
    return await grants_for(db, user_id)


async def revoke_grant(db: AsyncSession, grant_id: str) -> dict:
    row = await db.get(McpGrant, grant_id)
    if row is None:
        raise errors.NotFoundError(msg='Grant not found')
    await lock_user(db, row.user_id, require_active=False)
    await db.refresh(row, with_for_update=True)
    row.active = False
    await db.flush()
    return {'id': row.id, 'active': False}


async def key_data(db: AsyncSession, row: McpKey) -> dict:
    bindings = list((await db.scalars(select(McpKeyBinding.tool_id).where(McpKeyBinding.key_id == row.id))).all())
    effective = []
    if row.status == 'active' and (row.expire_time is None or row.expire_time > timezone.now()):
        effective = [tool.id for tool in await effective_tools(db, row.id, row.user_id)]
    return {
        'id': row.id, 'name': row.name, 'status': row.status, 'expire_time': row.expire_time,
        'created_time': row.created_time, 'tool_ids': bindings, 'effective_tool_ids': effective,
    }


async def create_key(db: AsyncSession, user_id: int, obj: CreateKey) -> dict:
    await lock_user(db, user_id)
    await available_tools(db, obj.tool_ids)
    grants = {row.tool_id: row for row in (await db.scalars(select(McpGrant).where(
        McpGrant.user_id == user_id, McpGrant.active.is_(True), McpGrant.tool_id.in_(obj.tool_ids)
    ).with_for_update())).all()}
    if set(grants) != set(obj.tool_ids):
        raise errors.ForbiddenError(msg='Key scope exceeds current grants')
    key_id = new_id()
    secret = f'mcp_{key_id}.{secrets.token_urlsafe(32)}'
    row = McpKey(id=key_id, user_id=user_id, name=obj.name, secret_hash=secret_digest(secret), expire_time=obj.expire_time)
    db.add(row)
    await db.flush()
    for grant in grants.values():
        db.add(McpKeyBinding(key_id=row.id, tool_id=grant.tool_id, generation=grant.generation))
    await db.flush()
    return {'key': await key_data(db, row), 'secret': secret}


async def owned_key(db: AsyncSession, user_id: int, key_id: str) -> McpKey:
    await lock_user(db, user_id)
    row = await db.scalar(select(McpKey).where(McpKey.id == key_id, McpKey.user_id == user_id).with_for_update())
    if row is None:
        raise errors.NotFoundError(msg='Key not found')
    return row


async def update_key(db: AsyncSession, user_id: int, key_id: str, obj: UpdateKey) -> dict:
    row = await owned_key(db, user_id, key_id)
    if row.status == 'revoked':
        raise errors.ConflictError(msg='Revoked keys cannot be changed')
    if obj.tool_ids is not None:
        current = {tool.id for tool in await effective_tools(db, row.id, user_id)}
        if not set(obj.tool_ids).issubset(current):
            raise errors.ForbiddenError(msg='Key scope can only shrink within current grants')
        await db.execute(delete(McpKeyBinding).where(
            McpKeyBinding.key_id == row.id, McpKeyBinding.tool_id.not_in(obj.tool_ids)
        ))
    if obj.name is not None:
        row.name = obj.name
    if obj.enabled is not None:
        row.status = 'active' if obj.enabled else 'disabled'
    await db.flush()
    return await key_data(db, row)


async def revoke_key(db: AsyncSession, user_id: int, key_id: str) -> dict:
    row = await owned_key(db, user_id, key_id)
    row.status = 'revoked'
    await db.flush()
    return await key_data(db, row)
