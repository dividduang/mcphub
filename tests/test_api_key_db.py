"""平台 API 的真库测试：aiosqlite 建五张表 + sys_user，跑申请 / 审批 / 授权 / Key 状态机。

ASGI 中间件那层（401 / 403 / 放行 / JWT 交换）在 test_api_key_middleware.py 用假库覆盖；
这里只守住两条安全边界：
1. `effective_endpoint_pairs` 的交集——Key 绑定 ∩ 授权 active ∩ 端点 enabled，
   外加 generation 对齐（撤销后重授即新代，旧绑定必须失效）；
2. 申请单状态机——部分批准只授批准项、全拒不授权、不可重复审批、不许越权批准。

不连真 PG：模型全是通用类型（String/JSON/BigInteger/TimeZone），SQLite 足以表达，
而 mcphub 五张表的主键都是 uuid hex，不依赖自增——这是能白漂的原因。
"""

import asyncio
from datetime import timedelta

import pytest
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from backend.app.admin.model.user import User
from backend.common.exception import errors
from backend.plugin.mcphub.model.entities import (
    ApiEndpoint,
    ApiGrant,
    ApiKey,
    ApiKeyEndpoint,
    ApiRequest,
    new_id,
)
from backend.plugin.mcphub.schema.hub import (
    CreateApiKey,
    CreateApiRequest,
    DecideApiRequest,
    UpdateApiKey,
)
from backend.plugin.mcphub.service import api_request_service
from backend.plugin.mcphub.service.auth_service import secret_digest
from backend.plugin.mcphub.transport.api_key_auth import _KEY_RE
from backend.utils.timezone import timezone

API_TABLES = (ApiEndpoint, ApiRequest, ApiGrant, ApiKey, ApiKeyEndpoint)


@pytest.fixture
def sessions(tmp_path):
    """每个测试一个独立文件库：五张平台 API 表 + sys_user，跑完即销毁。"""
    engine = create_async_engine(f'sqlite+aiosqlite:///{(tmp_path / "mcphub-api.db").as_posix()}', poolclass=NullPool)
    maker = async_sessionmaker(engine, expire_on_commit=False)

    async def create() -> None:
        async with engine.begin() as conn:
            await conn.run_sync(User.__table__.create)
            for model in API_TABLES:
                await conn.run_sync(model.__table__.create)

    asyncio.run(create())
    yield maker
    asyncio.run(engine.dispose())


def _user(user_id: int = 7) -> User:
    # id 是 BigInteger 自增列且 init=False：SQLite 上必须建完对象再赋值
    user = User(username=f'tester{user_id}', nickname=f'Tester{user_id}', status=1, password='x', salt=b'y')
    user.id = user_id
    return user


def _endpoint(path: str = '/api/v1/sys/user', method: str = 'GET', *, enabled: bool = True) -> ApiEndpoint:
    return ApiEndpoint(id=new_id(), method=method, path=path, tag='系统用户', summary=path, enabled=enabled)


def _key(key_id: str, user_id: int = 7, *, status: str = 'active', expire_time=None) -> ApiKey:
    return ApiKey(id=key_id, user_id=user_id, name='ci', secret_hash='hash', status=status, expire_time=expire_time)


class TestEffectiveEndpointPairs:
    def test_needs_binding_active_grant_and_enabled_endpoint(self, sessions):
        granted = _endpoint()
        disabled = _endpoint('/api/v1/sys/dept', enabled=False)
        key_id = new_id()

        async def scenario():
            async with sessions.begin() as db:
                db.add_all([_user(), granted, disabled])
                db.add(_key(key_id))
                db.add_all(
                    [
                        ApiGrant(user_id=7, endpoint_id=granted.id, generation='g1', source='applicant'),
                        ApiGrant(user_id=7, endpoint_id=disabled.id, generation='g1', source='applicant'),
                        ApiKeyEndpoint(key_id=key_id, endpoint_id=granted.id, generation='g1'),
                        ApiKeyEndpoint(key_id=key_id, endpoint_id=disabled.id, generation='g1'),
                    ]
                )
            async with sessions() as db:
                return await api_request_service.effective_endpoint_pairs(db, key_id, 7)

        # 停用端点即使绑了也授权了也不算有效：调用拿 403「未授权」而不是静默放行
        assert asyncio.run(scenario()) == {('GET', granted.path)}

    def test_unbound_endpoint_is_never_effective(self, sessions):
        endpoint = _endpoint()
        key_id = new_id()

        async def scenario():
            async with sessions.begin() as db:
                db.add_all([_user(), endpoint])
                db.add(_key(key_id))
                db.add(ApiGrant(user_id=7, endpoint_id=endpoint.id, generation='g1', source='applicant'))
            async with sessions() as db:
                return await api_request_service.effective_endpoint_pairs(db, key_id, 7)

        assert asyncio.run(scenario()) == set()

    def test_revoke_then_regrant_invalidates_old_binding(self, sessions):
        endpoint = _endpoint()
        key_id = new_id()

        async def scenario():
            async with sessions.begin() as db:
                db.add_all([_user(), endpoint])
                db.add(_key(key_id))
                db.add(ApiGrant(user_id=7, endpoint_id=endpoint.id, generation='g1', source='applicant'))
                db.add(ApiKeyEndpoint(key_id=key_id, endpoint_id=endpoint.id, generation='g1'))
            async with sessions() as db:
                before = await api_request_service.effective_endpoint_pairs(db, key_id, 7)
            async with sessions.begin() as db:
                grant = (await api_request_service.grants_for(db, 7))[0]
                await api_request_service.revoke_grant(db, grant.id)
            async with sessions() as db:
                after_revoke = await api_request_service.effective_endpoint_pairs(db, key_id, 7)
            async with sessions.begin() as db:
                await api_request_service.grant_endpoints(db, 7, [endpoint.id], 99)
                generation = (await api_request_service.grants_for(db, 7))[0].generation
            async with sessions() as db:
                after_regrant = await api_request_service.effective_endpoint_pairs(db, key_id, 7)
            return before, after_revoke, after_regrant, generation

        before, after_revoke, after_regrant, generation = asyncio.run(scenario())
        assert before == {('GET', endpoint.path)}
        assert after_revoke == set()
        # 重授是新 generation：旧绑定对不上，Key 不会因为撤销又放行
        assert after_regrant == set()
        assert generation != 'g1'


class TestRequestStateMachine:
    def test_duplicate_endpoint_ids_rejected_at_schema(self):
        # 重复 ID 会让 granted 集合（set 去重）永远追不上 requested 计数，
        # 申请单永远评不上 approved、永久卡 partial——schema 层直接拦掉。
        with pytest.raises(ValidationError):
            CreateApiRequest(endpoint_ids=['dup', 'dup'])

    def test_partial_approval_grants_only_approved(self, sessions):
        first = _endpoint('/api/v1/sys/user')
        second = _endpoint('/api/v1/sys/dept')

        async def scenario():
            async with sessions.begin() as db:
                db.add_all([_user(), first, second])
            async with sessions.begin() as db:
                submitted = await api_request_service.submit_request(
                    db, 7, CreateApiRequest(endpoint_ids=[first.id, second.id], note='数据同步')
                )
            async with sessions.begin() as db:
                decided = await api_request_service.decide_request(
                    db, submitted['id'], DecideApiRequest(approve_endpoint_ids=[first.id]), 99
                )
            async with sessions() as db:
                grants = await api_request_service.grants_for(db, 7)
            return submitted, decided, grants

        submitted, decided, grants = asyncio.run(scenario())
        assert submitted['status'] == 'pending'
        assert decided['status'] == 'partial'
        assert decided['granted_endpoint_ids'] == [first.id]
        assert [grant.endpoint_id for grant in grants] == [first.id]
        assert grants[0].source == 'administrator'
        assert grants[0].granted_by == 99

    def test_empty_approval_rejects_without_grants(self, sessions):
        endpoint = _endpoint()

        async def scenario():
            async with sessions.begin() as db:
                db.add_all([_user(), endpoint])
            async with sessions.begin() as db:
                submitted = await api_request_service.submit_request(
                    db, 7, CreateApiRequest(endpoint_ids=[endpoint.id])
                )
            async with sessions.begin() as db:
                decided = await api_request_service.decide_request(db, submitted['id'], DecideApiRequest(), 99)
            async with sessions() as db:
                grants = await api_request_service.grants_for(db, 7)
            return decided, grants

        decided, grants = asyncio.run(scenario())
        assert decided['status'] == 'rejected'
        assert decided['granted_endpoint_ids'] == []
        assert grants == []

    def test_full_approval_is_approved(self, sessions):
        first = _endpoint('/api/v1/sys/user')
        second = _endpoint('/api/v1/sys/dept')

        async def scenario():
            async with sessions.begin() as db:
                db.add_all([_user(), first, second])
            async with sessions.begin() as db:
                submitted = await api_request_service.submit_request(
                    db, 7, CreateApiRequest(endpoint_ids=[first.id, second.id])
                )
            async with sessions.begin() as db:
                return await api_request_service.decide_request(
                    db, submitted['id'], DecideApiRequest(approve_endpoint_ids=[second.id, first.id]), 99
                )

        decided = asyncio.run(scenario())
        assert decided['status'] == 'approved'
        assert decided['granted_endpoint_ids'] == sorted([first.id, second.id])

    def test_request_cannot_be_decided_twice(self, sessions):
        endpoint = _endpoint()

        async def scenario():
            async with sessions.begin() as db:
                db.add_all([_user(), endpoint])
            async with sessions.begin() as db:
                submitted = await api_request_service.submit_request(
                    db, 7, CreateApiRequest(endpoint_ids=[endpoint.id])
                )
            async with sessions.begin() as db:
                await api_request_service.decide_request(
                    db, submitted['id'], DecideApiRequest(approve_endpoint_ids=[endpoint.id]), 99
                )
                with pytest.raises(errors.ConflictError):
                    await api_request_service.decide_request(
                        db, submitted['id'], DecideApiRequest(approve_endpoint_ids=[endpoint.id]), 99
                    )

        asyncio.run(scenario())

    def test_approval_outside_request_is_rejected(self, sessions):
        requested = _endpoint('/api/v1/sys/user')
        stranger = _endpoint('/api/v1/sys/dept')

        async def scenario():
            async with sessions.begin() as db:
                db.add_all([_user(), requested, stranger])
            async with sessions.begin() as db:
                submitted = await api_request_service.submit_request(
                    db, 7, CreateApiRequest(endpoint_ids=[requested.id])
                )
                with pytest.raises(errors.RequestError):
                    await api_request_service.decide_request(
                        db, submitted['id'], DecideApiRequest(approve_endpoint_ids=[stranger.id]), 99
                    )

        asyncio.run(scenario())

    def test_submit_rejects_disabled_and_unknown_endpoint(self, sessions):
        disabled = _endpoint(enabled=False)

        async def scenario():
            async with sessions.begin() as db:
                db.add_all([_user(), disabled])
                with pytest.raises(errors.RequestError):
                    await api_request_service.submit_request(db, 7, CreateApiRequest(endpoint_ids=[disabled.id]))
                with pytest.raises(errors.RequestError):
                    await api_request_service.submit_request(db, 7, CreateApiRequest(endpoint_ids=[new_id()]))

        asyncio.run(scenario())

    def test_submit_requires_live_user(self, sessions):
        endpoint = _endpoint()

        async def scenario():
            async with sessions.begin() as db:
                db.add_all([_user(), endpoint])
            async with sessions.begin() as db:
                with pytest.raises(errors.NotFoundError):
                    await api_request_service.submit_request(db, 404, CreateApiRequest(endpoint_ids=[endpoint.id]))

        asyncio.run(scenario())


class TestKeyLifecycle:
    def test_create_key_binds_current_grant_generation(self, sessions):
        endpoint = _endpoint()

        async def scenario():
            async with sessions.begin() as db:
                db.add_all([_user(), endpoint])
            async with sessions.begin() as db:
                await api_request_service.grant_endpoints(db, 7, [endpoint.id], 99)
            async with sessions.begin() as db:
                created = await api_request_service.create_key(
                    db, 7, CreateApiKey(name='ci', endpoint_ids=[endpoint.id])
                )
            async with sessions() as db:
                row = await db.get(ApiKey, created['key']['id'])
                binding = (await api_request_service.key_endpoint_ids(db, row.id))
                effective = await api_request_service.effective_endpoint_pairs(db, row.id, 7)
            return created, row, binding, effective

        created, row, binding, effective = asyncio.run(scenario())
        assert _KEY_RE.match(created['secret'])
        assert created['secret'].split('.')[0] == f'ak_{created["key"]["id"]}'
        assert row.secret_hash == secret_digest(created['secret'])
        assert binding == [endpoint.id]
        assert effective == {('GET', endpoint.path)}

    def test_create_key_refuses_scope_beyond_grants(self, sessions):
        endpoint = _endpoint()

        async def scenario():
            async with sessions.begin() as db:
                db.add_all([_user(), endpoint])
            async with sessions.begin() as db:
                with pytest.raises(errors.ForbiddenError):
                    await api_request_service.create_key(db, 7, CreateApiKey(name='ci', endpoint_ids=[endpoint.id]))

        asyncio.run(scenario())

    def test_key_scope_can_only_shrink(self, sessions):
        first = _endpoint('/api/v1/sys/user')
        second = _endpoint('/api/v1/sys/dept')

        async def scenario():
            async with sessions.begin() as db:
                db.add_all([_user(), first, second])
            async with sessions.begin() as db:
                await api_request_service.grant_endpoints(db, 7, [first.id, second.id], 99)
            async with sessions.begin() as db:
                created = await api_request_service.create_key(
                    db, 7, CreateApiKey(name='ci', endpoint_ids=[first.id, second.id])
                )
            key_id = created['key']['id']
            async with sessions.begin() as db:
                shrunk = await api_request_service.update_key(
                    db, 7, key_id, UpdateApiKey(endpoint_ids=[first.id], name='renamed')
                )
                with pytest.raises(errors.ForbiddenError):
                    # 没有新授权就不能把范围扩回去
                    await api_request_service.update_key(
                        db, 7, key_id, UpdateApiKey(endpoint_ids=[first.id, second.id])
                    )
            return shrunk

        shrunk = asyncio.run(scenario())
        assert shrunk['name'] == 'renamed'
        assert shrunk['endpoint_ids'] == [first.id]
        assert shrunk['effective_endpoint_ids'] == [first.id]

    def test_disabled_and_expired_keys_have_no_effective_scope(self, sessions):
        endpoint = _endpoint()
        live, expired = new_id(), new_id()

        async def scenario():
            async with sessions.begin() as db:
                db.add_all([_user(), endpoint])
                db.add_all(
                    [
                        _key(live),
                        _key(expired, expire_time=timezone.now() - timedelta(hours=1)),
                        ApiGrant(user_id=7, endpoint_id=endpoint.id, generation='g1', source='applicant'),
                        ApiKeyEndpoint(key_id=live, endpoint_id=endpoint.id, generation='g1'),
                        ApiKeyEndpoint(key_id=expired, endpoint_id=endpoint.id, generation='g1'),
                    ]
                )
            async with sessions() as db:
                live_row = await api_request_service.key_row_data(db, await db.get(ApiKey, live))
                expired_row = await api_request_service.key_row_data(db, await db.get(ApiKey, expired))
            return live_row, expired_row

        live_row, expired_row = asyncio.run(scenario())
        assert live_row['effective_endpoint_ids'] == [endpoint.id]
        assert expired_row['effective_endpoint_ids'] == []
        assert expired_row['endpoint_ids'] == [endpoint.id]

    def test_revoked_key_cannot_be_updated_and_can_be_purged(self, sessions):
        endpoint = _endpoint()

        async def scenario():
            async with sessions.begin() as db:
                db.add_all([_user(), endpoint])
            async with sessions.begin() as db:
                await api_request_service.grant_endpoints(db, 7, [endpoint.id], 99)
            async with sessions.begin() as db:
                created = await api_request_service.create_key(
                    db, 7, CreateApiKey(name='ci', endpoint_ids=[endpoint.id])
                )
            key_id = created['key']['id']
            async with sessions.begin() as db:
                revoked = await api_request_service.revoke_key(db, 7, key_id)
                with pytest.raises(errors.ConflictError):
                    await api_request_service.update_key(db, 7, key_id, UpdateApiKey(name='again'))
                purged = await api_request_service.purge_key(db, 7, key_id)
                with pytest.raises(errors.NotFoundError):
                    await api_request_service.purge_key(db, 7, key_id)
            async with sessions() as db:
                return revoked, purged, key_id, await db.get(ApiKey, key_id)

        revoked, purged, key_id, row = asyncio.run(scenario())
        assert revoked['status'] == 'revoked'
        assert revoked['effective_endpoint_ids'] == []
        assert purged == {'id': key_id, 'deleted': True}
        assert row is None

    def test_active_key_cannot_be_purged(self, sessions):
        endpoint = _endpoint()

        async def scenario():
            async with sessions.begin() as db:
                db.add_all([_user(), endpoint])
            async with sessions.begin() as db:
                await api_request_service.grant_endpoints(db, 7, [endpoint.id], 99)
            async with sessions.begin() as db:
                created = await api_request_service.create_key(
                    db, 7, CreateApiKey(name='ci', endpoint_ids=[endpoint.id])
                )
                with pytest.raises(errors.ConflictError):
                    await api_request_service.purge_key(db, 7, created['key']['id'])

        asyncio.run(scenario())

    def test_revoke_grant_requires_existing_row(self, sessions):
        async def scenario():
            async with sessions.begin() as db:
                db.add(_user())
                with pytest.raises(errors.NotFoundError):
                    await api_request_service.revoke_grant(db, new_id())

        asyncio.run(scenario())
