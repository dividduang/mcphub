import secrets

from sqlalchemy import delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.admin.model import User
from backend.common.exception import errors
from backend.plugin.mcphub.crud.hub import available_tools, effective_tools, lock_user
from backend.plugin.mcphub.model.entities import (
    McpExternalServer,
    McpGrant,
    McpKey,
    McpKeyBinding,
    McpRequest,
    McpServer,
    McpTool,
    new_id,
)
from backend.plugin.mcphub.schema.hub import CreateKey, CreateRequest, UpdateKey
from backend.plugin.mcphub.service import external_service
from backend.plugin.mcphub.service.auth_service import secret_digest
from backend.utils.timezone import timezone


def can_manage(user) -> bool:
    return bool(
        user.is_superuser
        or (
            user.is_staff
            and any(
                "sys:mcphub:manage" in (menu.perms or "").split(",")
                for role in user.roles
                if role.status
                for menu in role.menus
                if menu.status
            )
        )
    )


def require_manager(user) -> None:
    if not can_manage(user):
        raise errors.ForbiddenError(msg="MCP Hub management permission required")


def request_data(row: McpRequest) -> dict:
    return {
        "id": row.id,
        "user_id": str(row.user_id),
        "tool_ids": row.tool_ids,
        "granted_tool_ids": row.granted_tool_ids,
        "status": row.status,
        "note": row.note,
        "created_time": row.created_time,
        "decided_by": str(row.decided_by) if row.decided_by else None,
    }


def grant_data(row: McpGrant) -> dict:
    return {
        "id": row.id,
        "user_id": str(row.user_id),
        "tool_id": row.tool_id,
        "generation": row.generation,
        "active": row.active,
        "source": row.source,
        "created_time": row.created_time,
    }


def tool_data(row: McpTool, granted: bool = False) -> dict:
    return {
        "id": row.id,
        "name": row.name,
        "title": row.title,
        "description": row.description,
        "enabled": row.enabled,
        "auto_approve": row.auto_approve,
        "input_schema": row.input_schema,
        "granted": granted,
    }


async def catalog(db: AsyncSession, user_id: int, base_url: str) -> dict:
    """服务目录 = 外部登记表的投影 + 本地工具行。

    目录里出现什么服务，完全由「MCP服务器」页的登记决定：登记 → 出现，删除 → 消失，
    不存在第二份能往目录里掺行的数据源。停用只是 enabled=false，仍在列表里。

    每个服务的 `tools` 来自本地 `mcphub_tool` 行——上游工具清单经
    `GET /servers/{id}/tools` 探测后 upsert 落地（见 service/tool_sync.py）。
    申请 / 授权 / Key 范围都指向这些行，所以目录必须带它们。
    只回最小投影字段：上游 URL / 请求头名 / 建档人属于管理端点，人人可见的目录不回。
    """
    rows = (await db.scalars(select(McpExternalServer).order_by(McpExternalServer.slug))).all()
    mirrors = {row.slug: row for row in (await db.scalars(select(McpServer))).all()}
    tools = (await db.scalars(select(McpTool).order_by(McpTool.created_time, McpTool.name))).all()
    grants = set(
        (await db.scalars(select(McpGrant.tool_id).where(McpGrant.user_id == user_id, McpGrant.active.is_(True)))).all()
    )
    servers = []
    for ext in rows:
        mirror = mirrors.get(f"ext-{ext.slug}")
        server_tools = (
            [
                tool_data(tool, tool.id in grants and tool.enabled and ext.enabled)
                for tool in tools
                if tool.server_id == mirror.id
            ]
            if mirror is not None
            else []
        )
        servers.append(dict(external_service.catalog_data(ext, base_url), tools=server_tools))
    return {"servers": servers}


async def page_rows(db: AsyncSession, query, page: int, size: int) -> tuple[list, int]:
    total = await db.scalar(select(func.count()).select_from(query.order_by(None).subquery()))
    rows = list((await db.scalars(query.offset((page - 1) * size).limit(size))).all())
    return rows, total or 0


async def grants_for(db: AsyncSession, user_id: int) -> list[dict]:
    rows = (await db.scalars(select(McpGrant).where(McpGrant.user_id == user_id).order_by(McpGrant.created_time))).all()
    return [grant_data(row) for row in rows]


async def resolve_user(db: AsyncSession, account: str) -> dict:
    """按 AD 账号解析授权目标；本地无档案时按 LDAP 信息自动建档。"""
    from backend.plugin.ldap_auth.service.auth_service import AuthService
    from backend.plugin.mcphub.transport import config

    account = account.strip()
    # 建档落点（部门 / 角色）按环境配置：写死 ID 换库就坏——
    # 部门不存在时 get_current_user 判「所属部门不存在」，角色停用时新用户被全角色锁定。
    # 配置非法时给明确错误，不让 int() 的 ValueError 变成 500。
    try:
        dept_id = config.provision_id("PROVISION_DEPT_ID", 10)
        role_ids = config.provision_ids("PROVISION_ROLE_IDS", "3")
    except ValueError as exc:
        raise errors.ServiceUnavailableError(msg=str(exc)) from exc
    from backend.app.admin.crud.crud_user import user_dao
    from backend.plugin.ldap_auth.crud import ldap_user_dao

    user = await user_dao.get_by_username(db, account)
    provisioned = False
    if user is None:
        info = await AuthService.ldap_lookup(account)
        if not info or not (info.get("applicant") or info.get("mail")):
            raise errors.NotFoundError(msg=f"LDAP 中不存在账号 {account}")
        try:
            await ldap_user_dao.add_external(
                db,
                username=account,
                nickname=info.get("applicant") or account,
                email=info.get("mail") or None,
                company=info.get("company"),
                dept_id=dept_id,
                role_ids=role_ids,
                status=1,
            )
        except Exception:
            # 并发建档或用户名冲突：add_external 的 flush 失败会把请求级会话置为
            # pending-rollback，复用该会话回查只会再抛 PendingRollbackError——
            # 必须换新会话回查；仍不存在才上抛。
            from backend.database.db import async_db_session

            async with async_db_session() as fresh_db:
                user = await user_dao.get_by_username(fresh_db, account)
            if user is None:
                raise
        else:
            user = await user_dao.get_by_username(db, account)
        provisioned = True
    if user is None or not user.status:
        raise errors.NotFoundError(msg="目标用户不可用")
    return {
        "id": user.id,
        "username": user.username,
        "nickname": user.nickname,
        "email": user.email or "",
        "provisioned": provisioned,
    }


async def user_profiles(db: AsyncSession, user_ids: list[int]) -> dict[int, dict]:
    """批量解析用户展示信息（本地账号）。"""
    ids = {int(uid) for uid in user_ids if uid is not None}
    if not ids:
        return {}
    users = list((await db.scalars(select(User).where(User.id.in_(ids), User.deleted == 0))).all())
    by_id = {user.id: user for user in users}
    profiles: dict[int, dict] = {}
    for uid in ids:
        user = by_id.get(uid)
        if user is None:
            profiles[uid] = {
                "id": uid,
                "username": "",
                "nickname": f"未知用户 {uid}",
                "ad_account": "",
                "email": "",
                "dept": "",
            }
            continue
        profiles[uid] = {
            "id": uid,
            "username": user.username,
            "ad_account": user.username,
            "nickname": user.nickname or user.username,
            "email": user.email or "",
            "dept": "",
        }
    return profiles


async def search_directory(db: AsyncSession, keyword: str, limit: int = 20) -> list[dict]:
    """按用户名 / 昵称 / 邮箱搜索本地用户，供管理员选择授权目标。"""
    keyword = (keyword or "").strip()
    if not keyword:
        return []
    like = f"%{keyword}%"
    rows = (
        await db.scalars(
            select(User)
            .where(
                or_(
                    User.username.like(like),
                    User.nickname.like(like),
                    User.email.like(like),
                ),
                User.deleted == 0,
            )
            .limit(min(max(limit, 1), 50))
        )
    ).all()
    return [
        {
            "username": row.username,
            "nickname": row.nickname or row.username,
            "email": row.email or "",
            "dept": "",
            "has_local": True,
        }
        for row in rows
        if row.username
    ]


async def purge_key(db: AsyncSession, user_id: int, key_id: str) -> dict:
    """永久删除已撤销的 Key（仅限本人的 revoked Key）。"""
    row = await owned_key(db, user_id, key_id)
    if row.status != "revoked":
        raise errors.ConflictError(msg="Only revoked keys can be deleted")
    await db.execute(delete(McpKeyBinding).where(McpKeyBinding.key_id == key_id))
    await db.delete(row)
    return {"id": key_id, "deleted": True}


async def _grant(db: AsyncSession, user_id: int, tool_ids: list[str], source: str, actor_id: int | None) -> None:
    # Caller holds the target user's row lock, also taken by every key mutation.
    await available_tools(db, tool_ids)
    rows = {
        row.tool_id: row
        for row in (
            await db.scalars(
                select(McpGrant).where(McpGrant.user_id == user_id, McpGrant.tool_id.in_(tool_ids)).with_for_update()
            )
        ).all()
    }
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
    await _grant(db, user_id, approved, "policy", None)
    row = McpRequest(
        user_id=user_id,
        tool_ids=obj.tool_ids,
        granted_tool_ids=approved,
        note=obj.note,
        status="approved" if len(approved) == len(obj.tool_ids) else ("partial" if approved else "pending"),
    )
    db.add(row)
    await db.flush()
    return request_data(row)


async def decide_request(db: AsyncSession, request_id: str, approved: list[str], actor_id: int) -> dict:
    row = await db.get(McpRequest, request_id)
    if row is None:
        raise errors.NotFoundError(msg="Request not found")
    await lock_user(db, row.user_id)
    await db.refresh(row, with_for_update=True)
    if row.decided_by is not None or row.status not in {"pending", "partial"}:
        raise errors.ConflictError(msg="Request has already been decided")
    if not set(approved).issubset(row.tool_ids) or len(set(approved)) != len(approved):
        raise errors.RequestError(msg="Approval must be a subset of requested tools")
    await _grant(db, row.user_id, approved, "administrator", actor_id)
    # Prior policy grants are audit history, not silently revoked by this decision.
    row.granted_tool_ids = sorted(set(row.granted_tool_ids) | set(approved))
    row.status = (
        "approved"
        if len(row.granted_tool_ids) == len(row.tool_ids)
        else ("partial" if row.granted_tool_ids else "rejected")
    )
    row.decided_by = actor_id
    await db.flush()
    return request_data(row)


async def grant_tools(db: AsyncSession, user_id: int, tool_ids: list[str], actor_id: int) -> list[dict]:
    await lock_user(db, user_id)
    await _grant(db, user_id, tool_ids, "administrator", actor_id)
    return await grants_for(db, user_id)


async def revoke_grant(db: AsyncSession, grant_id: str) -> dict:
    row = await db.get(McpGrant, grant_id)
    if row is None:
        raise errors.NotFoundError(msg="Grant not found")
    await lock_user(db, row.user_id, require_active=False)
    await db.refresh(row, with_for_update=True)
    row.active = False
    await db.flush()
    return {"id": row.id, "active": False}


async def key_data(db: AsyncSession, row: McpKey) -> dict:
    bindings = list((await db.scalars(select(McpKeyBinding.tool_id).where(McpKeyBinding.key_id == row.id))).all())
    effective = []
    if row.status == "active" and (row.expire_time is None or row.expire_time > timezone.now()):
        effective = [tool.id for tool in await effective_tools(db, row.id, row.user_id)]
    return {
        "id": row.id,
        "name": row.name,
        "status": row.status,
        "expire_time": row.expire_time,
        "created_time": row.created_time,
        "tool_ids": bindings,
        "effective_tool_ids": effective,
    }


async def create_key(db: AsyncSession, user_id: int, obj: CreateKey) -> dict:
    await lock_user(db, user_id)
    await available_tools(db, obj.tool_ids)
    # 空 scope 合法（外部服务不校验工具范围）：没有可绑定授权，直接空集合，
    # 不走 in_([])——空 IN 列表的语义随 SQLAlchemy 版本变化。
    grants: dict[str, McpGrant] = {}
    if obj.tool_ids:
        grants = {
            row.tool_id: row
            for row in (
                await db.scalars(
                    select(McpGrant)
                    .where(McpGrant.user_id == user_id, McpGrant.active.is_(True), McpGrant.tool_id.in_(obj.tool_ids))
                    .with_for_update()
                )
            ).all()
        }
    if set(grants) != set(obj.tool_ids):
        raise errors.ForbiddenError(msg="Key scope exceeds current grants")
    key_id = new_id()
    secret = f"mcp_{key_id}.{secrets.token_urlsafe(32)}"
    row = McpKey(
        id=key_id, user_id=user_id, name=obj.name, secret_hash=secret_digest(secret), expire_time=obj.expire_time
    )
    db.add(row)
    await db.flush()
    for grant in grants.values():
        db.add(McpKeyBinding(key_id=row.id, tool_id=grant.tool_id, generation=grant.generation))
    await db.flush()
    return {"key": await key_data(db, row), "secret": secret}


async def owned_key(db: AsyncSession, user_id: int, key_id: str) -> McpKey:
    await lock_user(db, user_id)
    row = await db.scalar(select(McpKey).where(McpKey.id == key_id, McpKey.user_id == user_id).with_for_update())
    if row is None:
        raise errors.NotFoundError(msg="Key not found")
    return row


async def update_key(db: AsyncSession, user_id: int, key_id: str, obj: UpdateKey) -> dict:
    row = await owned_key(db, user_id, key_id)
    if row.status == "revoked":
        raise errors.ConflictError(msg="Revoked keys cannot be changed")
    if obj.tool_ids is not None:
        current = {tool.id for tool in await effective_tools(db, row.id, user_id)}
        if not set(obj.tool_ids).issubset(current):
            raise errors.ForbiddenError(msg="Key scope can only shrink within current grants")
        # 空 scope 是合法的（外部服务不校验工具范围）：显式删全部绑定，
        # 不要走 not_in([])——空 IN 列表在不同 SQLAlchemy 版本上的语义会变。
        if obj.tool_ids:
            await db.execute(
                delete(McpKeyBinding).where(McpKeyBinding.key_id == row.id, McpKeyBinding.tool_id.not_in(obj.tool_ids))
            )
        else:
            await db.execute(delete(McpKeyBinding).where(McpKeyBinding.key_id == row.id))
    if obj.name is not None:
        row.name = obj.name
    if obj.enabled is not None:
        row.status = "active" if obj.enabled else "disabled"
    await db.flush()
    return await key_data(db, row)


async def revoke_key(db: AsyncSession, user_id: int, key_id: str) -> dict:
    row = await owned_key(db, user_id, key_id)
    row.status = "revoked"
    await db.flush()
    return await key_data(db, row)
