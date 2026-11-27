"""平台 API 中间件的 ASGI 直呼测试（不需要 DB / 上游）。

设计文档 §9 的那一串：无 Key 放行 / 格式错 401 / 未知 401 / 停用 401 / 过期 401 /
用户失效 401 / 目录外放行 / 兜底路径 403 / 范围外 403 / 范围内换发身份 / HEAD 按 GET 判 /
缓存与即时失效。

DB 侧全部用假对象顶掉：`async_db_session`、`effective_endpoint_pairs`、
`create_access_token` 三个缝都是函数内 import，所以按模块属性打补丁即可。
"""

import asyncio
import contextlib
from datetime import datetime, timedelta
from datetime import timezone as tz
from types import SimpleNamespace

import pytest

from backend.app.admin.model.user import User
from backend.common.security import token as token_module
from backend.database import db as db_module
from backend.plugin.mcphub.model.entities import ApiKey, new_id
from backend.plugin.mcphub.service import api_request_service
from backend.plugin.mcphub.transport import api_key_auth
from backend.plugin.mcphub.transport.api_key_auth import ApiKeyAuthMiddleware

CATALOG = (
    ('GET', '/api/v1/sys/user'),
    ('GET', '/api/v1/sys/user/{pk}'),
    ('POST', '/api/v1/sys/user'),
)


class _InnerApp:
    """被中间件包裹的下游：记录收到的 scope，回一个 200。"""

    def __init__(self) -> None:
        self.scopes: list[dict] = []

    async def __call__(self, scope, receive, send) -> None:
        self.scopes.append(scope)
        await send({'type': 'http.response.start', 'status': 200, 'headers': [(b'content-type', b'text/plain')]})
        await send({'type': 'http.response.body', 'body': b'ok'})


class _Result:
    def __init__(self, rows) -> None:
        self._rows = rows

    def all(self):
        return list(self._rows)


class _FakeDb:
    """只实现中间件用到的两个方法：按主键取行、执行端点目录查询。"""

    def __init__(self, objects, catalog) -> None:
        self.objects = objects
        self.catalog = catalog
        self.gets = 0

    async def get(self, model, pk):
        self.gets += 1
        return self.objects.get((model, pk))

    async def execute(self, statement):
        return _Result(self.catalog)


def _secret(key_id: str) -> str:
    return f'ak_{key_id}.{"s" * 43}'


def _key_row(key_id: str, *, status: str = 'active', expire_time=None, user_id: int = 7):
    return SimpleNamespace(id=key_id, status=status, expire_time=expire_time, user_id=user_id)


def _user_row(user_id: int = 7, *, status: int = 1, deleted: int = 0):
    return SimpleNamespace(id=user_id, status=status, deleted=deleted)


@pytest.fixture(autouse=True)
def _clean_cache():
    """中间件的进程内缓存跨测试存活，逐用例清干净（30s TTL 不该串味）。"""
    api_key_auth._cache._keys.clear()
    api_key_auth._cache._scopes.clear()
    api_key_auth.invalidate_catalog()
    api_key_auth._enabled = None
    yield
    api_key_auth._cache._keys.clear()
    api_key_auth._cache._scopes.clear()
    api_key_auth.invalidate_catalog()
    api_key_auth._enabled = None


def _build(monkeypatch, *, key=None, user=None, scope_pairs=frozenset(), catalog=CATALOG, enabled=True):
    objects = {}
    if key is not None:
        objects[(ApiKey, key.id)] = key
    if user is not None:
        objects[(User, user.id)] = user
    fake_db = _FakeDb(objects, list(catalog))
    monkeypatch.setattr(db_module, 'async_db_session', lambda: _session(fake_db))

    async def fake_effective(db, key_id, user_id):
        return set(scope_pairs)

    monkeypatch.setattr(api_request_service, 'effective_endpoint_pairs', fake_effective)

    async def fake_enabled():
        return enabled

    monkeypatch.setattr(api_key_auth, '_plugin_enabled', fake_enabled)

    async def fake_token(user_id, *, multi_login, session_uuid=None, swagger=False, **kwargs):
        # swagger=True 的语义：写 Redis 会话登记、不踢用户其他端、不污染会话索引
        assert multi_login is False
        assert swagger is True
        return SimpleNamespace(access_token=f'exchanged-{user_id}')

    monkeypatch.setattr(token_module, 'create_access_token', fake_token)
    return ApiKeyAuthMiddleware(_InnerApp()), fake_db


@contextlib.asynccontextmanager
async def _session(fake_db):
    yield fake_db


async def _call(middleware, path, *, api_key=None, method='GET', authorization=None):
    headers = []
    if api_key is not None:
        headers.append((b'x-api-key', api_key.encode()))
    if authorization is not None:
        headers.append((b'authorization', authorization.encode()))
    scope = {'type': 'http', 'method': method, 'path': path, 'headers': headers, 'query_string': b''}
    sent = []

    async def receive():
        return {'type': 'http.request', 'body': b'', 'more_body': False}

    async def send(message):
        sent.append(message)

    await middleware(scope, receive, send)
    return scope, sent


def _status(sent) -> int | None:
    for message in sent:
        if message['type'] == 'http.response.start':
            return message['status']
    return None


def _authorization(scope) -> str | None:
    for key, value in scope.get('headers', ()):
        if key.lower() == b'authorization':
            return value.decode()
    return None


def test_request_without_key_passes_through(monkeypatch):
    middleware, _ = _build(monkeypatch)
    scope, sent = asyncio.run(_call(middleware, '/api/v1/sys/user'))
    assert _status(sent) == 200
    assert middleware.app.scopes == [scope]


def test_non_http_scope_passes_through(monkeypatch):
    middleware, _ = _build(monkeypatch)
    scope = {'type': 'websocket', 'path': '/ws', 'headers': [(b'x-api-key', _secret(new_id()).encode())]}

    async def run():
        seen = []

        async def receive():
            return {'type': 'websocket.connect'}

        async def send(message):
            seen.append(message)

        await middleware(scope, receive, send)
        return seen

    sent = asyncio.run(run())
    # 非 HTTP  scope 原样交给下游，中间件不碰
    assert middleware.app.scopes == [scope]
    assert _status(sent) == 200


@pytest.mark.parametrize(
    'api_key',
    ['nope', f'ak_{new_id()}', f'ak_{"z" * 32}.{"s" * 43}', f'ak_{new_id()}.short', f'ak_{new_id()}.{"s" * 42}'],
)
def test_malformed_key_is_401(monkeypatch, api_key):
    middleware, _ = _build(monkeypatch)
    _, sent = asyncio.run(_call(middleware, '/api/v1/sys/user', api_key=api_key))
    assert _status(sent) == 401
    assert middleware.app.scopes == []


def test_empty_key_header_is_treated_as_no_key(monkeypatch):
    # 空值等于没带：走原有 JWT 链路，不该被中间件判 401
    middleware, _ = _build(monkeypatch)
    _, sent = asyncio.run(_call(middleware, '/api/v1/sys/user', api_key=''))
    assert _status(sent) == 200
    assert len(middleware.app.scopes) == 1


def test_unknown_key_is_401(monkeypatch):
    # 库里没有这把 Key（格式合法但不存在）
    key_id = new_id()
    middleware, _ = _build(monkeypatch)
    _, sent = asyncio.run(_call(middleware, '/api/v1/sys/user', api_key=_secret(key_id)))
    assert _status(sent) == 401
    assert middleware.app.scopes == []


@pytest.mark.parametrize('status', ['disabled', 'revoked'])
def test_inactive_key_is_401(monkeypatch, status):
    key_id = new_id()
    middleware, _ = _build(monkeypatch, key=_key_row(key_id, status=status), user=_user_row())
    _, sent = asyncio.run(_call(middleware, '/api/v1/sys/user', api_key=_secret(key_id)))
    assert _status(sent) == 401


def test_expired_key_is_401(monkeypatch):
    key_id = new_id()
    expired = datetime.now(tz.utc) - timedelta(hours=1)
    middleware, _ = _build(monkeypatch, key=_key_row(key_id, expire_time=expired), user=_user_row())
    _, sent = asyncio.run(_call(middleware, '/api/v1/sys/user', api_key=_secret(key_id)))
    assert _status(sent) == 401


@pytest.mark.parametrize('user_kwargs', [{'status': 0}, {'deleted': 1}])
def test_inactive_user_is_401(monkeypatch, user_kwargs):
    key_id = new_id()
    middleware, _ = _build(monkeypatch, key=_key_row(key_id), user=_user_row(**user_kwargs))
    _, sent = asyncio.run(_call(middleware, '/api/v1/sys/user', api_key=_secret(key_id)))
    assert _status(sent) == 401


def test_path_outside_catalog_passes_through_without_exchange(monkeypatch):
    key_id = new_id()
    middleware, _ = _build(monkeypatch, key=_key_row(key_id), user=_user_row())
    scope, sent = asyncio.run(_call(middleware, '/api/v1/not-a-real-route', api_key=_secret(key_id)))
    # 目录里没有的路径（404 路线）：不判范围、不换身份，交给下游
    assert _status(sent) == 200
    assert _authorization(scope) is None


def test_forbidden_path_is_403_even_if_granted(monkeypatch):
    key_id = new_id()
    forbidden = '/api/v1/auth/login/swagger'
    catalog = CATALOG + (('POST', forbidden),)
    middleware, _ = _build(
        monkeypatch,
        key=_key_row(key_id),
        user=_user_row(),
        catalog=catalog,
        scope_pairs={('POST', forbidden)},
    )
    _, sent = asyncio.run(_call(middleware, forbidden, api_key=_secret(key_id), method='POST'))
    assert _status(sent) == 403
    assert middleware.app.scopes == []


def test_plugin_disabled_is_503_even_with_valid_key(monkeypatch):
    # 停用插件 = 平台 API 的 kill switch：既有 Key（含永久 Key）同时失效，
    # 不能只停 MCP 入口（dispatch）而漏掉这里。
    key_id = new_id()
    middleware, _ = _build(
        monkeypatch,
        key=_key_row(key_id),
        user=_user_row(),
        scope_pairs={('GET', '/api/v1/sys/user')},
        enabled=False,
    )
    _, sent = asyncio.run(_call(middleware, '/api/v1/sys/user', api_key=_secret(key_id)))
    assert _status(sent) == 503
    assert middleware.app.scopes == []


def test_endpoint_outside_key_scope_is_403(monkeypatch):
    key_id = new_id()
    middleware, _ = _build(
        monkeypatch, key=_key_row(key_id), user=_user_row(), scope_pairs={('GET', '/api/v1/sys/user/{pk}')}
    )
    _, sent = asyncio.run(_call(middleware, '/api/v1/sys/user', api_key=_secret(key_id)))
    assert _status(sent) == 403
    assert middleware.app.scopes == []


def test_endpoint_inside_key_scope_exchanges_identity(monkeypatch):
    key_id = new_id()
    middleware, _ = _build(
        monkeypatch, key=_key_row(key_id), user=_user_row(), scope_pairs={('GET', '/api/v1/sys/user')}
    )
    scope, sent = asyncio.run(
        _call(middleware, '/api/v1/sys/user', api_key=_secret(key_id), authorization='Bearer stale-jwt')
    )
    assert _status(sent) == 200
    # 换发的新凭证覆盖旧 authorization，且只保留这一个头（不叠加）
    assert _authorization(scope) == 'Bearer exchanged-7'


def test_parametrized_template_requires_exact_segment_count(monkeypatch):
    key_id = new_id()
    middleware, _ = _build(
        monkeypatch, key=_key_row(key_id), user=_user_row(), scope_pairs={('GET', '/api/v1/sys/user/{pk}')}
    )
    _, inside = asyncio.run(_call(middleware, '/api/v1/sys/user/42', api_key=_secret(key_id)))
    assert _status(inside) == 200
    # 申请 {pk} 不隐含列表端点
    _, outside = asyncio.run(_call(middleware, '/api/v1/sys/user', api_key=_secret(key_id)))
    assert _status(outside) == 403


def test_head_request_is_judged_as_get(monkeypatch):
    key_id = new_id()
    middleware, _ = _build(
        monkeypatch, key=_key_row(key_id), user=_user_row(), scope_pairs={('GET', '/api/v1/sys/user')}
    )
    _, sent = asyncio.run(_call(middleware, '/api/v1/sys/user', api_key=_secret(key_id), method='HEAD'))
    assert _status(sent) == 200


def test_snapshot_is_cached_until_invalidated(monkeypatch):
    key_id = new_id()
    middleware, fake_db = _build(
        monkeypatch, key=_key_row(key_id), user=_user_row(), scope_pairs={('GET', '/api/v1/sys/user')}
    )
    asyncio.run(_call(middleware, '/api/v1/sys/user', api_key=_secret(key_id)))
    assert fake_db.gets == 2  # Key 快照 + 用户行
    asyncio.run(_call(middleware, '/api/v1/sys/user', api_key=_secret(key_id)))
    assert fake_db.gets == 2  # 第二次全走缓存

    # 缓存未清时改库不生效——这是 30s TTL 的代价
    fake_db.objects[(ApiKey, key_id)] = _key_row(key_id, status='revoked')
    _, sent = asyncio.run(_call(middleware, '/api/v1/sys/user', api_key=_secret(key_id)))
    assert _status(sent) == 200

    # 吊销后 invalidate_key：下一个请求立刻 401
    api_key_auth.invalidate_key(key_id)
    _, sent = asyncio.run(_call(middleware, '/api/v1/sys/user', api_key=_secret(key_id)))
    assert _status(sent) == 401
