import os

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.common.exception import errors
from backend.common.response.response_schema import ResponseModel, response_base
from backend.common.security.jwt import get_current_user, jwt_authentication
from backend.database.db import CurrentSession, async_db_session, get_db_transaction
from backend.plugin.mcphub.model.entities import McpKey, McpRequest, McpServer, McpTool
from backend.plugin.mcphub.schema.hub import CreateGrant, CreateKey, CreateRequest, DecideRequest, UpdateKey, UpdateServer, UpdateTool
from backend.plugin.mcphub.service import hub_service

router = APIRouter(prefix='/api/v1/mcphub', tags=['MCP Hub'])


async def management_user(credentials: Annotated[HTTPAuthorizationCredentials, Depends(HTTPBearer())]):
    # Do not trust request.user: legacy API keys also populate it as authenticated.
    if credentials.credentials.startswith(('mcp_', 'fba-')):
        raise errors.TokenError(msg='FBA login JWT required')
    identity = await jwt_authentication(credentials.credentials)
    async with async_db_session() as db:
        return await get_current_user(db, identity.id)


CurrentUser = Annotated[Any, Depends(management_user)]
# A returned key/grant must already be visible to the next MCP request.
# FBA's default request-scope dependency commits after the HTTP response.
CurrentSessionTransaction = Annotated[AsyncSession, Depends(get_db_transaction, scope='function')]
Page = Annotated[int, Query(ge=1)]
Size = Annotated[int, Query(ge=1, le=100)]


def public_base(request: Request) -> str:
    return os.environ.get('MCPHUB_PUBLIC_BASE_URL') or str(request.base_url)


@router.get('/metadata')
async def metadata(user: CurrentUser) -> ResponseModel:
    return response_base.success(data={
        'user_id': str(user.id), 'can_manage': hub_service.can_manage(user),
        'auth_mode': 'private_bearer_key', 'management_auth': 'fba_jwt', 'dynamic_code': False,
    })


@router.get('/catalog')
async def catalog(db: CurrentSession, request: Request, user: CurrentUser) -> ResponseModel:
    return response_base.success(data=await hub_service.catalog(db, user.id, public_base(request)))


@router.patch('/servers/{server_id}')
async def update_server(server_id: str, obj: UpdateServer, db: CurrentSessionTransaction, request: Request, user: CurrentUser) -> ResponseModel:
    hub_service.require_manager(user)
    row = await db.get(McpServer, server_id, with_for_update=True)
    if row is None:
        raise errors.NotFoundError(msg='Server not found')
    row.enabled = obj.enabled
    await db.flush()
    return response_base.success(data=hub_service.server_data(row, public_base(request)))


@router.patch('/tools/{tool_id}')
async def update_tool(tool_id: str, obj: UpdateTool, db: CurrentSessionTransaction, user: CurrentUser) -> ResponseModel:
    hub_service.require_manager(user)
    row = await db.get(McpTool, tool_id, with_for_update=True)
    if row is None:
        raise errors.NotFoundError(msg='Tool not found')
    for field, value in obj.model_dump(exclude_none=True).items():
        setattr(row, field, value)
    await db.flush()
    return response_base.success(data=hub_service.tool_data(row))


@router.get('/requests')
async def requests(db: CurrentSession, user: CurrentUser, page: Page = 1, size: Size = 20, all: bool = False) -> ResponseModel:
    query = select(McpRequest).order_by(McpRequest.created_time.desc(), McpRequest.id)
    if all:
        hub_service.require_manager(user)
    else:
        query = query.where(McpRequest.user_id == user.id)
    rows, total = await hub_service.page_rows(db, query, page, size)
    return response_base.success(data={'items': [hub_service.request_data(row) for row in rows], 'total': total, 'page': page, 'size': size})


@router.post('/requests')
async def submit_request(obj: CreateRequest, db: CurrentSessionTransaction, user: CurrentUser) -> ResponseModel:
    return response_base.success(data=await hub_service.submit_request(db, user.id, obj))


@router.post('/requests/{request_id}/decision')
async def decide_request(request_id: str, obj: DecideRequest, db: CurrentSessionTransaction, user: CurrentUser) -> ResponseModel:
    hub_service.require_manager(user)
    return response_base.success(data=await hub_service.decide_request(db, request_id, obj.approve_tool_ids, user.id))


@router.get('/grants')
async def grants(db: CurrentSession, user: CurrentUser, user_id: int | None = None) -> ResponseModel:
    if user_id is not None and user_id != user.id:
        hub_service.require_manager(user)
    return response_base.success(data=await hub_service.grants_for(db, user_id if user_id is not None else user.id))


@router.post('/grants')
async def grant_tools(obj: CreateGrant, db: CurrentSessionTransaction, user: CurrentUser) -> ResponseModel:
    hub_service.require_manager(user)
    return response_base.success(data=await hub_service.grant_tools(db, obj.user_id, obj.tool_ids, user.id))


@router.delete('/grants/{grant_id}')
async def revoke_grant(grant_id: str, db: CurrentSessionTransaction, user: CurrentUser) -> ResponseModel:
    hub_service.require_manager(user)
    return response_base.success(data=await hub_service.revoke_grant(db, grant_id))


@router.get('/keys')
async def keys(db: CurrentSession, user: CurrentUser, page: Page = 1, size: Size = 20) -> ResponseModel:
    query = select(McpKey).where(McpKey.user_id == user.id).order_by(McpKey.created_time.desc(), McpKey.id)
    rows, total = await hub_service.page_rows(db, query, page, size)
    return response_base.success(data={'items': [await hub_service.key_data(db, row) for row in rows], 'total': total, 'page': page, 'size': size})


@router.post('/keys')
async def create_key(obj: CreateKey, db: CurrentSessionTransaction, user: CurrentUser, response: Response) -> ResponseModel:
    response.headers['Cache-Control'] = 'no-store'
    return response_base.success(data=await hub_service.create_key(db, user.id, obj))


@router.patch('/keys/{key_id}')
async def update_key(key_id: str, obj: UpdateKey, db: CurrentSessionTransaction, user: CurrentUser) -> ResponseModel:
    return response_base.success(data=await hub_service.update_key(db, user.id, key_id, obj))


@router.delete('/keys/{key_id}')
async def revoke_key(key_id: str, db: CurrentSessionTransaction, user: CurrentUser) -> ResponseModel:
    return response_base.success(data=await hub_service.revoke_key(db, user.id, key_id))
