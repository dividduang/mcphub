import logging
import os
import time
from types import SimpleNamespace
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.common.exception import errors
from backend.common.response.response_schema import ResponseModel, response_base
from backend.common.security.jwt import get_current_user, jwt_authentication
from backend.core.conf import settings
from backend.database.db import CurrentSession, async_db_session, get_db_transaction
from backend.plugin.mcphub.model.entities import (
    ApiEndpoint,
    ApiGrant,
    ApiKey,
    ApiKeyEndpoint,
    ApiRequest,
    McpExternalServer,
    McpKey,
    McpRequest,
    McpTool,
)
from backend.plugin.mcphub.schema.hub import (
    CreateApiGrant,
    CreateApiKey,
    CreateApiRequest,
    CreateExternalServer,
    CreateGrant,
    CreateKey,
    CreateRequest,
    DecideApiRequest,
    DecideRequest,
    ResolveUser,
    UpdateApiEndpoint,
    UpdateApiKey,
    UpdateExternalServer,
    UpdateKey,
    UpdateServer,
    UpdateTool,
)
from backend.plugin.mcphub.service import (
    api_catalog,
    api_request_service,
    external_service,
    hub_service,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/mcphub", tags=["MCP Hub"])


async def management_user(credentials: Annotated[HTTPAuthorizationCredentials, Depends(HTTPBearer())]):
    # Do not trust request.user: legacy API keys also populate it as authenticated.
    if credentials.credentials.startswith(("mcp_", "fba-")):
        raise errors.TokenError(msg="FBA login JWT required")
    identity = await jwt_authentication(credentials.credentials)
    async with async_db_session() as db:
        return await get_current_user(db, identity.id)


CurrentUser = Annotated[Any, Depends(management_user)]
# A returned key/grant must already be visible to the next MCP request.
# FBA's default request-scope dependency commits after the HTTP response.
CurrentSessionTransaction = Annotated[AsyncSession, Depends(get_db_transaction, scope="function")]
Page = Annotated[int, Query(ge=1)]
Size = Annotated[int, Query(ge=1, le=100)]


def public_base(request: Request) -> str:
    """目录里发给用户的端点 origin。进程环境变量优先，其次 plugin.toml [settings]
    （PluginSettingsSource 只并进 pydantic settings，不写回环境变量），最后才退回
    request.base_url——那会是内网地址，用户复制出去不能用。"""
    override = os.environ.get("MCPHUB_PUBLIC_BASE_URL") or getattr(settings, "MCPHUB_PUBLIC_BASE_URL", None)
    return str(override) if override else str(request.base_url)


@router.get("/metadata")
async def metadata(user: CurrentUser) -> ResponseModel:
    return response_base.success(
        data={
            "user_id": str(user.id),
            "can_manage": hub_service.can_manage(user),
            "auth_mode": "private_bearer_key",
            "management_auth": "fba_jwt",
            "dynamic_code": False,
        }
    )


@router.get("/catalog")
async def catalog(db: CurrentSession, request: Request, user: CurrentUser) -> ResponseModel:
    # 目录是外部登记表的投影：不按用户过滤，人人看到同一份服务清单。
    return response_base.success(data=await hub_service.catalog(db, user.id, public_base(request)))


@router.patch("/servers/{server_id}")
async def update_server(
    server_id: str, obj: UpdateServer, db: CurrentSessionTransaction, request: Request, user: CurrentUser
) -> ResponseModel:
    """目录页里的服务启停：操作对象就是外部登记行（目录与登记同源，id 通用）。"""
    hub_service.require_manager(user)
    row = await db.get(McpExternalServer, server_id, with_for_update=True)
    if row is None:
        raise errors.NotFoundError(msg="Server not found")
    row.enabled = obj.enabled
    await db.flush()
    # URL / 请求头可能变：缓存里的旧工具清单不能再用了（延迟导入避免循环依赖）
    from backend.plugin.mcphub.service import tools_probe

    tools_probe.invalidate(row.id)
    return response_base.success(data=external_service.server_data(row, public_base(request)))


@router.get("/servers/{server_id}/tools")
async def server_tools(
    server_id: str,
    db: CurrentSession,
    request: Request,
    user: CurrentUser,
    refresh: bool = False,
) -> ResponseModel:
    """目录里某个服务的工具清单：按需向上游 tools/list，60s TTL 缓存。

    展示用途（见 service/tools_probe.py 头注释）：调用鉴权不走工具范围，
    任何 active 的 Key 都能调用已启用的外部服务。上游失败不阻断页面——
    返回 200 + error 字段，由前端渲染成行内错误态 + 重试。
    """
    row = await db.get(McpExternalServer, server_id)
    if row is None:
        raise errors.NotFoundError(msg="Server not found")
    if not row.enabled:
        # 停用的服务不拿管理员登记的凭证去打上游；目录照常列出，工具一栏空
        return response_base.success(data={"items": [], "error": None})
    # 只把探测需要的字段带出会话：探测最坏约 20s（握手 + 翻页），
    # 不能让 SQLAlchemy 连接在整个探测期间不归还池子。
    target = SimpleNamespace(id=row.id, slug=row.slug, url=row.url, headers_ciphertext=row.headers_ciphertext)

    from backend.plugin.mcphub.service import tools_probe
    from backend.plugin.mcphub.transport.executor import CapacityExceeded

    # 按用户去抖：本端点无权限门槛，没有这层就是「任意登录用户无限 refresh 打上游」
    if refresh:
        previous = tools_probe.refresh_stamp(user.id, target.id, time.monotonic())
        if time.monotonic() - previous < 5.0:
            refresh = False  # 太频繁：退回缓存

    hub = getattr(request.app.state, "mcphub", None)
    try:
        if hub is None:
            tools = await tools_probe.fetch_tools(target, refresh=refresh)
        else:
            with hub.executor.network(tools_probe.probe_url(target)):
                tools = await tools_probe.fetch_tools(target, refresh=refresh)
    except CapacityExceeded:
        return response_base.success(data={"items": [], "error": "上游请求过于频繁，请稍后重试"})
    except tools_probe.UpstreamToolsError as exc:
        return response_base.success(data={"items": [], "error": str(exc)})

    # 落地：申请 / 授权 / Key 范围都指向本地工具行，探测成功即 upsert。
    # 单独开一个短事务——探测期间不持有连接（见上面的 DTO）。
    from backend.plugin.mcphub.service import tool_sync

    try:
        async with async_db_session.begin() as sdb:
            ext = await sdb.get(McpExternalServer, server_id)
            if ext is not None:
                await tool_sync.sync_tools(sdb, ext, tools)
    except Exception:
        # 落库失败不影响本次展示（清单已经拿到），下次刷新再补
        logger.warning("MCP Hub 工具清单落库失败 server=%s", server_id, exc_info=True)

    return response_base.success(data={"items": tools, "error": None})


@router.patch("/tools/{tool_id}")
async def update_tool(tool_id: str, obj: UpdateTool, db: CurrentSessionTransaction, user: CurrentUser) -> ResponseModel:
    hub_service.require_manager(user)
    row = await db.get(McpTool, tool_id, with_for_update=True)
    if row is None:
        raise errors.NotFoundError(msg="Tool not found")
    for field, value in obj.model_dump(exclude_none=True).items():
        setattr(row, field, value)
    await db.flush()
    return response_base.success(data=hub_service.tool_data(row))


@router.get("/requests")
async def requests(db: CurrentSession, user: CurrentUser, page: Page = 1, size: Size = 20) -> ResponseModel:
    """本人的申请历史。管理视角是独立路径 /admin/requests，不靠查询参数切权限。"""
    query = (
        select(McpRequest)
        .where(McpRequest.user_id == user.id)
        .order_by(McpRequest.created_time.desc(), McpRequest.id)
    )
    rows, total = await hub_service.page_rows(db, query, page, size)
    profiles = await hub_service.user_profiles(db, [row.user_id for row in rows])
    items = [{**hub_service.request_data(row), "applicant": profiles.get(row.user_id)} for row in rows]
    return response_base.success(data={"items": items, "total": total, "page": page, "size": size})


@router.get("/admin/requests")
async def admin_requests(db: CurrentSession, user: CurrentUser, page: Page = 1, size: Size = 20) -> ResponseModel:
    """全部用户的申请（管理视角）。"""
    hub_service.require_manager(user)
    query = select(McpRequest).order_by(McpRequest.created_time.desc(), McpRequest.id)
    rows, total = await hub_service.page_rows(db, query, page, size)
    profiles = await hub_service.user_profiles(db, [row.user_id for row in rows])
    items = [{**hub_service.request_data(row), "applicant": profiles.get(row.user_id)} for row in rows]
    return response_base.success(data={"items": items, "total": total, "page": page, "size": size})


@router.post("/requests")
async def submit_request(obj: CreateRequest, db: CurrentSessionTransaction, user: CurrentUser) -> ResponseModel:
    return response_base.success(data=await hub_service.submit_request(db, user.id, obj))


@router.post("/requests/{request_id}/decision")
async def decide_request(
    request_id: str, obj: DecideRequest, db: CurrentSessionTransaction, user: CurrentUser
) -> ResponseModel:
    hub_service.require_manager(user)
    return response_base.success(data=await hub_service.decide_request(db, request_id, obj.approve_tool_ids, user.id))


@router.get("/grants")
async def grants(db: CurrentSession, user: CurrentUser) -> ResponseModel:
    """本人的授权清单。管理视角是独立路径 /admin/grants。"""
    profile = (await hub_service.user_profiles(db, [user.id])).get(user.id)
    return response_base.success(data={"items": await hub_service.grants_for(db, user.id), "user": profile})


@router.get("/admin/grants")
async def admin_grants(db: CurrentSession, user: CurrentUser, user_id: int) -> ResponseModel:
    """查看指定用户的授权（管理视角）。"""
    hub_service.require_manager(user)
    profile = (await hub_service.user_profiles(db, [user_id])).get(user_id)
    return response_base.success(data={"items": await hub_service.grants_for(db, user_id), "user": profile})


@router.get("/users")
async def search_directory(
    db: CurrentSession,
    user: CurrentUser,
    keyword: str = "",
    limit: int = Query(20, ge=1, le=50),
) -> ResponseModel:
    """按姓名 / AD 账号 / 邮箱搜索员工目录。"""
    hub_service.require_manager(user)
    return response_base.success(data=await hub_service.search_directory(db, keyword, limit))


@router.post("/grants")
async def grant_tools(obj: CreateGrant, db: CurrentSessionTransaction, user: CurrentUser) -> ResponseModel:
    hub_service.require_manager(user)
    return response_base.success(data=await hub_service.grant_tools(db, obj.user_id, obj.tool_ids, user.id))


@router.post("/users/resolve")
async def resolve_user(obj: ResolveUser, db: CurrentSessionTransaction, user: CurrentUser) -> ResponseModel:
    """按 AD 账号解析目标用户；不存在时按 LDAP 自动建档，避免必须先登录过 FBA。"""
    hub_service.require_manager(user)
    return response_base.success(data=await hub_service.resolve_user(db, obj.account))


@router.delete("/grants/{grant_id}")
async def revoke_grant(grant_id: str, db: CurrentSessionTransaction, user: CurrentUser) -> ResponseModel:
    hub_service.require_manager(user)
    return response_base.success(data=await hub_service.revoke_grant(db, grant_id))


@router.get("/keys")
async def keys(db: CurrentSession, user: CurrentUser, page: Page = 1, size: Size = 20) -> ResponseModel:
    query = select(McpKey).where(McpKey.user_id == user.id).order_by(McpKey.created_time.desc(), McpKey.id)
    rows, total = await hub_service.page_rows(db, query, page, size)
    profiles = await hub_service.user_profiles(db, [user.id])
    items = []
    for row in rows:
        items.append({**await hub_service.key_data(db, row), "owner": profiles.get(row.user_id)})
    return response_base.success(data={"items": items, "total": total, "page": page, "size": size})


@router.post("/keys")
async def create_key(
    obj: CreateKey, db: CurrentSessionTransaction, user: CurrentUser, response: Response
) -> ResponseModel:
    response.headers["Cache-Control"] = "no-store"
    return response_base.success(data=await hub_service.create_key(db, user.id, obj))


@router.patch("/keys/{key_id}")
async def update_key(key_id: str, obj: UpdateKey, db: CurrentSessionTransaction, user: CurrentUser) -> ResponseModel:
    return response_base.success(data=await hub_service.update_key(db, user.id, key_id, obj))


@router.delete("/keys/{key_id}")
async def revoke_key(key_id: str, db: CurrentSessionTransaction, user: CurrentUser) -> ResponseModel:
    return response_base.success(data=await hub_service.revoke_key(db, user.id, key_id))


@router.post("/keys/{key_id}/purge")
async def purge_key(key_id: str, db: CurrentSessionTransaction, user: CurrentUser) -> ResponseModel:
    """永久删除已撤销的 Key。"""
    return response_base.success(data=await hub_service.purge_key(db, user.id, key_id))


# ---------------------------------------------------------------- 外部 MCP 服务（仅管理）
# 面向自建部署，http / https 与内网地址都放行（见 external_service.validate_url），
# 所以每个端点都显式 require_manager，且请求头只回名称、值永不回显。


@router.get("/external-servers")
async def list_external_servers(db: CurrentSession, request: Request, user: CurrentUser) -> ResponseModel:
    hub_service.require_manager(user)
    rows = await external_service.list_servers(db)
    return response_base.success(
        data={
            "items": [external_service.server_data(row, public_base(request)) for row in rows],
            "total": len(rows),
        }
    )


@router.post("/external-servers")
async def create_external_server(
    obj: CreateExternalServer, db: CurrentSessionTransaction, request: Request, user: CurrentUser
) -> ResponseModel:
    hub_service.require_manager(user)
    row = await external_service.create_server(db, obj, user.id)
    return response_base.success(data=external_service.server_data(row, public_base(request)))


@router.patch("/external-servers/{server_id}")
async def update_external_server(
    server_id: str,
    obj: UpdateExternalServer,
    db: CurrentSessionTransaction,
    request: Request,
    user: CurrentUser,
) -> ResponseModel:
    hub_service.require_manager(user)
    row = await db.get(McpExternalServer, server_id, with_for_update=True)
    if row is None:
        raise errors.NotFoundError(msg="External server not found")
    row = await external_service.update_server(db, row, obj)
    return response_base.success(data=external_service.server_data(row, public_base(request)))


@router.delete("/external-servers/{server_id}")
async def delete_external_server(server_id: str, db: CurrentSessionTransaction, user: CurrentUser) -> ResponseModel:
    hub_service.require_manager(user)
    row = await db.get(McpExternalServer, server_id, with_for_update=True)
    if row is None:
        raise errors.NotFoundError(msg="External server not found")
    await external_service.delete_server(db, row)
    return response_base.success(data={"id": server_id, "deleted": True})


# ====================================================================
# 平台 API（FBA 自家 REST 端点的申请 / 审批 / 授权 / API Key）
# 设计文档：docs/20261122-平台API申请与APIKey-design.md
# ====================================================================


def _api_endpoint_data(row: ApiEndpoint, granted: bool) -> dict:
    return {
        "id": row.id,
        "method": row.method,
        "path": row.path,
        "tag": row.tag,
        "summary": row.summary,
        "description": row.description,
        "input_schema": row.input_schema,
        "deprecated": row.deprecated,
        "enabled": row.enabled,
        "granted": granted,
    }


async def _api_requests_with_applicant(db: AsyncSession, rows: list[ApiRequest]) -> list[dict]:
    profiles = await hub_service.user_profiles(db, [row.user_id for row in rows])
    return [{**api_request_service.request_data(row), "applicant": profiles.get(row.user_id)} for row in rows]


@router.get("/api/catalog")
async def api_catalog_view(db: CurrentSession, request: Request, user: CurrentUser) -> ResponseModel:
    """平台 API 目录：按模块（OpenAPI tag）分组的端点行 + granted 标记。"""
    rows = (await db.scalars(select(ApiEndpoint).order_by(ApiEndpoint.created_time, ApiEndpoint.id))).all()
    granted_ids = set(
        (
            await db.scalars(
                select(ApiGrant.endpoint_id).where(ApiGrant.user_id == user.id, ApiGrant.active.is_(True))
            )
        ).all()
    )
    modules: dict[str, list[dict]] = {}
    for row in rows:
        # 授权了但端点被停用/下架：标记仍为 False（调用会被中间件拒）
        granted = row.id in granted_ids and row.enabled
        modules.setdefault(row.tag, []).append(_api_endpoint_data(row, granted))
    return response_base.success(
        data={"modules": [{"tag": tag, "endpoints": items} for tag, items in modules.items()]}
    )


@router.post("/api/refresh")
async def api_catalog_refresh(request: Request, user: CurrentUser) -> ResponseModel:
    """重新同步 OpenAPI → 端点目录（管理）。"""
    hub_service.require_manager(user)
    app = request.app
    async with async_db_session.begin() as db:
        stats = await api_catalog.sync_endpoints(db, app)
        if stats.get("retired"):
            # 消失端点被软下线：绑定了它们的 Key 的有效范围立刻不含这些端点，
            # 缓存不清就有最长 30s 空窗——与端点启停 api_endpoint_update 同一处置。
            from backend.plugin.mcphub.transport.api_key_auth import invalidate_catalog

            key_ids = list(
                (
                    await db.execute(
                        select(ApiKey.id)
                        .join(ApiKeyEndpoint, ApiKeyEndpoint.key_id == ApiKey.id)
                        .join(ApiEndpoint, ApiEndpoint.id == ApiKeyEndpoint.endpoint_id)
                        .where(ApiEndpoint.enabled.is_(False))
                        .distinct()
                    )
                )
                .scalars()
                .all()
            )
            for key_id in key_ids:
                api_request_service.invalidate_key(key_id)
            invalidate_catalog()
    return response_base.success(data=stats)


@router.patch("/api/endpoints/{endpoint_id}")
async def api_endpoint_update(
    endpoint_id: str, obj: UpdateApiEndpoint, db: CurrentSessionTransaction, user: CurrentUser
) -> ResponseModel:
    """端点启停（管理）。停用后不可申请，已授权用户的调用也会被中间件拒。"""
    hub_service.require_manager(user)
    row = await db.get(ApiEndpoint, endpoint_id, with_for_update=True)
    if row is None:
        raise errors.NotFoundError(msg="Endpoint not found")
    row.enabled = obj.enabled
    await db.flush()
    # 启停影响两侧缓存，都必须即时清：①绑定了该端点的全部 Key 的有效范围
    # （停用要立刻拒、启用要立刻放，不能等 30s TTL）②目录的 enabled 标记
    from backend.plugin.mcphub.transport.api_key_auth import invalidate_catalog

    key_ids = list(
        (
            await db.scalars(
                select(ApiKey.id)
                .join(ApiKeyEndpoint, ApiKeyEndpoint.key_id == ApiKey.id)
                .where(ApiKeyEndpoint.endpoint_id == endpoint_id)
            )
        ).all()
    )
    for key_id in key_ids:
        api_request_service.invalidate_key(key_id)
    invalidate_catalog()
    return response_base.success(data=_api_endpoint_data(row, False))


@router.get("/api/requests")
async def api_requests(db: CurrentSession, user: CurrentUser, page: Page = 1, size: Size = 20) -> ResponseModel:
    """本人的 API 申请历史。"""
    query = (
        select(ApiRequest)
        .where(ApiRequest.user_id == user.id)
        .order_by(ApiRequest.created_time.desc(), ApiRequest.id)
    )
    rows, total = await hub_service.page_rows(db, query, page, size)
    items = await _api_requests_with_applicant(db, rows)
    return response_base.success(data={"items": items, "total": total, "page": page, "size": size})


@router.get("/api/admin/requests")
async def api_admin_requests(db: CurrentSession, user: CurrentUser, page: Page = 1, size: Size = 20) -> ResponseModel:
    hub_service.require_manager(user)
    query = select(ApiRequest).order_by(ApiRequest.created_time.desc(), ApiRequest.id)
    rows, total = await hub_service.page_rows(db, query, page, size)
    items = await _api_requests_with_applicant(db, rows)
    return response_base.success(data={"items": items, "total": total, "page": page, "size": size})


@router.post("/api/requests")
async def api_submit_request(obj: CreateApiRequest, db: CurrentSessionTransaction, user: CurrentUser) -> ResponseModel:
    return response_base.success(data=await api_request_service.submit_request(db, user.id, obj))


@router.post("/api/requests/{request_id}/decision")
async def api_decide_request(
    request_id: str, obj: DecideApiRequest, db: CurrentSessionTransaction, user: CurrentUser
) -> ResponseModel:
    hub_service.require_manager(user)
    return response_base.success(data=await api_request_service.decide_request(db, request_id, obj, user.id))


@router.get("/api/grants")
async def api_grants(db: CurrentSession, user: CurrentUser) -> ResponseModel:
    profile = (await hub_service.user_profiles(db, [user.id])).get(user.id)
    rows = await api_request_service.grants_for(db, user.id)
    endpoints = {row.id: row for row in (await db.scalars(select(ApiEndpoint))).all()}
    items = [
        {
            **api_request_service.grant_data(row),
            "endpoint": (
                _api_endpoint_data(endpoints[row.endpoint_id], row.active) if row.endpoint_id in endpoints else None
            ),
        }
        for row in rows
    ]
    return response_base.success(data={"items": items, "user": profile})


@router.get("/api/admin/grants")
async def api_admin_grants(db: CurrentSession, user: CurrentUser, user_id: int) -> ResponseModel:
    hub_service.require_manager(user)
    profile = (await hub_service.user_profiles(db, [user_id])).get(user_id)
    rows = await api_request_service.grants_for(db, user_id)
    endpoints = {row.id: row for row in (await db.scalars(select(ApiEndpoint))).all()}
    items = [
        {
            **api_request_service.grant_data(row),
            "endpoint": (
                _api_endpoint_data(endpoints[row.endpoint_id], row.active) if row.endpoint_id in endpoints else None
            ),
        }
        for row in rows
    ]
    return response_base.success(data={"items": items, "user": profile})


@router.post("/api/grants")
async def api_grant_tools(obj: CreateApiGrant, db: CurrentSessionTransaction, user: CurrentUser) -> ResponseModel:
    hub_service.require_manager(user)
    data = await api_request_service.grant_endpoints(db, obj.user_id, obj.endpoint_ids, user.id)
    return response_base.success(data=data)


@router.delete("/api/grants/{grant_id}")
async def api_revoke_grant(grant_id: str, db: CurrentSessionTransaction, user: CurrentUser) -> ResponseModel:
    hub_service.require_manager(user)
    return response_base.success(data=await api_request_service.revoke_grant(db, grant_id))


@router.get("/api/users")
async def api_search_directory(
    db: CurrentSession, user: CurrentUser, keyword: str = "", limit: int = Query(20, ge=1, le=50)
) -> ResponseModel:
    hub_service.require_manager(user)
    return response_base.success(data=await hub_service.search_directory(db, keyword, limit))


@router.post("/api/users/resolve")
async def api_resolve_user(obj: ResolveUser, db: CurrentSessionTransaction, user: CurrentUser) -> ResponseModel:
    hub_service.require_manager(user)
    return response_base.success(data=await hub_service.resolve_user(db, obj.account))


@router.get("/api/keys")
async def api_keys(db: CurrentSession, user: CurrentUser, page: Page = 1, size: Size = 20) -> ResponseModel:
    query = select(ApiKey).where(ApiKey.user_id == user.id).order_by(ApiKey.created_time.desc(), ApiKey.id)
    rows, total = await hub_service.page_rows(db, query, page, size)
    items = [await api_request_service.key_row_data(db, row) for row in rows]
    return response_base.success(data={"items": items, "total": total, "page": page, "size": size})


@router.post("/api/keys")
async def api_create_key(
    obj: CreateApiKey, db: CurrentSessionTransaction, user: CurrentUser, response: Response
) -> ResponseModel:
    # 响应体一次性含 secret：与 MCP 侧 create_key 同口径禁缓存
    response.headers["Cache-Control"] = "no-store"
    return response_base.success(data=await api_request_service.create_key(db, user.id, obj))


@router.patch("/api/keys/{key_id}")
async def api_update_key(
    key_id: str, obj: UpdateApiKey, db: CurrentSessionTransaction, user: CurrentUser
) -> ResponseModel:
    return response_base.success(data=await api_request_service.update_key(db, user.id, key_id, obj))


@router.delete("/api/keys/{key_id}")
async def api_revoke_key(key_id: str, db: CurrentSessionTransaction, user: CurrentUser) -> ResponseModel:
    return response_base.success(data=await api_request_service.revoke_key(db, user.id, key_id))


@router.post("/api/keys/{key_id}/purge")
async def api_purge_key(key_id: str, db: CurrentSessionTransaction, user: CurrentUser) -> ResponseModel:
    return response_base.success(data=await api_request_service.purge_key(db, user.id, key_id))
