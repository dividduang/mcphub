"""平台 API 的申请 / 审批 / 授权。

与 MCP 侧（hub_service）逐环节同构，但操作对象是 (method, path) 端点行。
端点没有 auto_approve 概念（第一版统一人工审批，避免把「谁能自动拿到
FBA 接口」这种决定藏进端点属性里），所以提交恒为 pending。
"""

import secrets

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.common.exception import errors
from backend.plugin.mcphub.crud.hub import lock_user
from backend.plugin.mcphub.model.entities import ApiEndpoint, ApiGrant, ApiKey, ApiKeyEndpoint, ApiRequest, new_id
from backend.plugin.mcphub.schema.hub import CreateApiKey, CreateApiRequest, DecideApiRequest
from backend.plugin.mcphub.service.auth_service import secret_digest
from backend.plugin.mcphub.transport.api_key_auth import invalidate_key
from backend.utils.timezone import timezone


def request_data(row: ApiRequest) -> dict:
    return {
        "id": row.id,
        "user_id": str(row.user_id),
        "endpoint_ids": list(row.endpoint_ids or []),
        "granted_endpoint_ids": list(row.granted_endpoint_ids or []),
        "status": row.status,
        "note": row.note,
        "decided_by": None if row.decided_by is None else str(row.decided_by),
        "created_time": row.created_time,
    }


def grant_data(row: ApiGrant) -> dict:
    return {
        "id": row.id,
        "user_id": str(row.user_id),
        "endpoint_id": row.endpoint_id,
        "generation": row.generation,
        "active": row.active,
        "source": row.source,
        "created_time": row.created_time,
    }


def key_data(row: ApiKey, endpoint_ids: list[str], effective: list[str]) -> dict:
    return {
        "id": row.id,
        "name": row.name,
        "status": row.status,
        "expire_time": row.expire_time,
        "created_time": row.created_time,
        "endpoint_ids": endpoint_ids,
        "effective_endpoint_ids": effective,
    }


async def available_endpoints(db: AsyncSession, endpoint_ids: list[str]) -> list[ApiEndpoint]:
    """可申请的端点：存在且 enabled。空集合合法（不允许空申请由 schema 拦）。"""
    if not endpoint_ids:
        return []
    rows = list((await db.scalars(select(ApiEndpoint).where(ApiEndpoint.id.in_(endpoint_ids)))).all())
    if len(rows) != len(set(endpoint_ids)):
        raise errors.RequestError(msg="Unknown endpoint")
    disabled = [row.id for row in rows if not row.enabled]
    if disabled:
        raise errors.RequestError(msg="Endpoint is disabled")
    return rows


async def grants_for(db: AsyncSession, user_id: int) -> list[ApiGrant]:
    return list(
        (
            await db.scalars(
                select(ApiGrant)
                .where(ApiGrant.user_id == user_id)
                .order_by(ApiGrant.created_time.desc(), ApiGrant.id)
            )
        ).all()
    )


async def _grant(db: AsyncSession, user_id: int, endpoint_ids: list[str], source: str, actor_id: int | None) -> None:
    # 调用方持有目标用户行锁（与 MCP 侧同一锁序约定）
    await available_endpoints(db, endpoint_ids)
    rows = {
        row.endpoint_id: row
        for row in (
            await db.scalars(
                select(ApiGrant)
                .where(ApiGrant.user_id == user_id, ApiGrant.endpoint_id.in_(endpoint_ids))
                .with_for_update()
            )
        ).all()
    }
    for endpoint_id in endpoint_ids:
        row = rows.get(endpoint_id)
        if row is None:
            db.add(ApiGrant(user_id=user_id, endpoint_id=endpoint_id, source=source, granted_by=actor_id))
        elif not row.active:
            row.active = True
            row.generation = new_id()
            row.source = source
            row.granted_by = actor_id
    await db.flush()


async def submit_request(db: AsyncSession, user_id: int, obj: CreateApiRequest) -> dict:
    await lock_user(db, user_id)
    await available_endpoints(db, obj.endpoint_ids)
    row = ApiRequest(
        user_id=user_id,
        endpoint_ids=list(obj.endpoint_ids),
        granted_endpoint_ids=[],
        note=obj.note,
        status="pending",
    )
    db.add(row)
    await db.flush()
    return request_data(row)


async def decide_request(db: AsyncSession, request_id: str, obj: DecideApiRequest, actor_id: int) -> dict:
    row = await db.get(ApiRequest, request_id)
    if row is None:
        raise errors.NotFoundError(msg="Request not found")
    await lock_user(db, row.user_id)
    await db.refresh(row, with_for_update=True)
    # 准入门槛与 MCP 侧同口径（status in {pending, partial}）；decided_by 守卫
    # 保证「已决过的单子不能重开」，partial 的剩余端点走管理员直授。
    if row.decided_by is not None or row.status not in {"pending", "partial"}:
        raise errors.ConflictError(msg="Request has already been decided")
    approved = list(obj.approve_endpoint_ids)
    if not set(approved).issubset(set(row.endpoint_ids)) or len(set(approved)) != len(approved):
        raise errors.RequestError(msg="Approval must be a subset of requested endpoints")
    if approved:
        await _grant(db, row.user_id, approved, "administrator", actor_id)
    row.granted_endpoint_ids = sorted(set(row.granted_endpoint_ids) | set(approved))
    row.status = (
        "approved"
        if len(row.granted_endpoint_ids) == len(row.endpoint_ids)
        else ("partial" if approved else "rejected")
    )
    row.decided_by = actor_id
    await db.flush()
    return request_data(row)


async def grant_endpoints(db: AsyncSession, user_id: int, endpoint_ids: list[str], actor_id: int) -> list[dict]:
    await lock_user(db, user_id)
    await _grant(db, user_id, endpoint_ids, "administrator", actor_id)
    return [grant_data(row) for row in await grants_for(db, user_id)]


async def revoke_grant(db: AsyncSession, grant_id: str) -> dict:
    row = await db.get(ApiGrant, grant_id)
    if row is None:
        raise errors.NotFoundError(msg="Grant not found")
    await lock_user(db, row.user_id, require_active=False)
    await db.refresh(row, with_for_update=True)
    row.active = False
    await db.flush()
    await _invalidate_user_keys(db, row.user_id)
    return {"id": row.id, "active": False}


async def _invalidate_user_keys(db: AsyncSession, user_id: int) -> None:
    """该用户的授权变了，名下每把 Key 的中间件缓存都要作废。"""
    key_ids = list((await db.scalars(select(ApiKey.id).where(ApiKey.user_id == user_id))).all())
    for key_id in key_ids:
        invalidate_key(key_id)


async def effective_endpoint_ids(db: AsyncSession, key_id: str, user_id: int) -> list[str]:
    """Key 实际能调的端点 = 绑定 ∩ 有效授权 ∩ 端点启用。"""
    query = (
        select(ApiEndpoint.id)
        .join(ApiKeyEndpoint, ApiKeyEndpoint.endpoint_id == ApiEndpoint.id)
        .join(
            ApiGrant,
            (ApiGrant.endpoint_id == ApiEndpoint.id)
            & (ApiGrant.user_id == user_id)
            & (ApiGrant.generation == ApiKeyEndpoint.generation),
        )
        .where(
            ApiKeyEndpoint.key_id == key_id,
            ApiGrant.active.is_(True),
            ApiEndpoint.enabled.is_(True),
        )
    )
    return list((await db.scalars(query)).all())


async def effective_endpoint_pairs(db: AsyncSession, key_id: str, user_id: int) -> set[tuple[str, str]]:
    """同 effective_endpoint_ids，但给 (method, path) 对——中间件按请求匹配。"""
    query = (
        select(ApiEndpoint.method, ApiEndpoint.path)
        .join(ApiKeyEndpoint, ApiKeyEndpoint.endpoint_id == ApiEndpoint.id)
        .join(
            ApiGrant,
            (ApiGrant.endpoint_id == ApiEndpoint.id)
            & (ApiGrant.user_id == user_id)
            & (ApiGrant.generation == ApiKeyEndpoint.generation),
        )
        .where(
            ApiKeyEndpoint.key_id == key_id,
            ApiGrant.active.is_(True),
            ApiEndpoint.enabled.is_(True),
        )
    )
    return {(row[0], row[1]) for row in (await db.execute(query)).all()}


async def key_endpoint_ids(db: AsyncSession, key_id: str) -> list[str]:
    return list((await db.scalars(select(ApiKeyEndpoint.endpoint_id).where(ApiKeyEndpoint.key_id == key_id))).all())


async def key_row_data(db: AsyncSession, row: ApiKey) -> dict:
    endpoint_ids = await key_endpoint_ids(db, row.id)
    effective: list[str] = []
    if row.status == "active" and (row.expire_time is None or row.expire_time > timezone.now()):
        effective = await effective_endpoint_ids(db, row.id, row.user_id)
    return key_data(row, endpoint_ids, effective)


async def owned_key(db: AsyncSession, user_id: int, key_id: str) -> ApiKey:
    await lock_user(db, user_id)
    row = await db.scalar(select(ApiKey).where(ApiKey.id == key_id, ApiKey.user_id == user_id).with_for_update())
    if row is None:
        raise errors.NotFoundError(msg="Key not found")
    return row


async def create_key(db: AsyncSession, user_id: int, obj: CreateApiKey) -> dict:
    await lock_user(db, user_id)
    await available_endpoints(db, obj.endpoint_ids)
    grants: dict[str, ApiGrant] = {}
    if obj.endpoint_ids:
        grants = {
            row.endpoint_id: row
            for row in (
                await db.scalars(
                    select(ApiGrant)
                    .where(
                        ApiGrant.user_id == user_id,
                        ApiGrant.active.is_(True),
                        ApiGrant.endpoint_id.in_(obj.endpoint_ids),
                    )
                    .with_for_update()
                )
            ).all()
        }
    if set(grants) != set(obj.endpoint_ids):
        raise errors.ForbiddenError(msg="Key scope exceeds current grants")
    key_id = new_id()
    secret = f"ak_{key_id}.{secrets.token_urlsafe(32)}"
    row = ApiKey(
        id=key_id,
        user_id=user_id,
        name=obj.name,
        secret_hash=secret_digest(secret),
        expire_time=obj.expire_time,
    )
    db.add(row)
    await db.flush()
    for grant in grants.values():
        db.add(ApiKeyEndpoint(key_id=row.id, endpoint_id=grant.endpoint_id, generation=grant.generation))
    await db.flush()
    return {"key": await key_row_data(db, row), "secret": secret}


async def update_key(db: AsyncSession, user_id: int, key_id: str, obj) -> dict:
    row = await owned_key(db, user_id, key_id)
    if row.status == "revoked":
        raise errors.ConflictError(msg="Revoked keys cannot be changed")
    if obj.endpoint_ids is not None:
        current = set(await effective_endpoint_ids(db, row.id, user_id))
        if not set(obj.endpoint_ids).issubset(current):
            raise errors.ForbiddenError(msg="Key scope can only shrink within current grants")
        if obj.endpoint_ids:
            await db.execute(
                delete(ApiKeyEndpoint).where(
                    ApiKeyEndpoint.key_id == row.id, ApiKeyEndpoint.endpoint_id.not_in(obj.endpoint_ids)
                )
            )
        else:
            await db.execute(delete(ApiKeyEndpoint).where(ApiKeyEndpoint.key_id == row.id))
    if obj.name is not None:
        row.name = obj.name
    if obj.enabled is not None:
        row.status = "active" if obj.enabled else "disabled"
    await db.flush()
    invalidate_key(row.id)
    return await key_row_data(db, row)


async def revoke_key(db: AsyncSession, user_id: int, key_id: str) -> dict:
    row = await owned_key(db, user_id, key_id)
    row.status = "revoked"
    await db.flush()
    invalidate_key(row.id)
    return await key_row_data(db, row)


async def purge_key(db: AsyncSession, user_id: int, key_id: str) -> dict:
    """永久删除已撤销的 Key（仅限本人的 revoked Key）。"""
    row = await owned_key(db, user_id, key_id)
    if row.status != "revoked":
        raise errors.ConflictError(msg="Only revoked keys can be deleted")
    await db.execute(delete(ApiKeyEndpoint).where(ApiKeyEndpoint.key_id == key_id))
    await db.delete(row)
    invalidate_key(key_id)
    return {"id": key_id, "deleted": True}
