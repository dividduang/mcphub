"""平台 API Key 的 ASGI 中间件——调用强制的唯一入口。

带 `X-Api-Key` 头的请求在这里过五关：Key 存在且 active 且未过期、插件处于启用态、
用户有效、本次请求命中的端点模板在 Key 已授权范围内、路径不在兜底排除名单。
全部通过后用 Key 所属用户身份交换出一枚 swagger 语义的短期 JWT（写 Redis 会话登记、
不踢用户其他端），改写 scope 的 authorization 头后放行——后续
JwtAuthMiddleware / CurrentUser / DependsRBAC 原样生效，权限双层叠加。

设计文档：docs/20261122-平台API申请与APIKey-design.md §6。

缓存：Key 快照与有效端点集合进程内缓存 30s。吊销 / 改范围 / 端点停用时由
写操作路径调 invalidate_key() 即时清本实例；多实例部署下最长 30s 传播。
"""

import json
import logging
import re
import time

from backend.core.conf import settings

logger = logging.getLogger(__name__)

# ak_<32hex>.<43secret>，与 MCP Key 同构
_KEY_RE = re.compile(r"^ak_([0-9a-f]{32})\.([A-Za-z0-9_-]{43})$")
_CACHE_TTL = 30.0
_PRUNE_THRESHOLD = 512
# 兜底排除：即使端点行被误授权也不放行（与 api_catalog 的同步排除是两道闸）。
# 路由挂在 FASTAPI_API_V1_PATH 下；带不带前缀都拦，漏前缀曾让这道闸整体失灵。
_FORBIDDEN_PATH = re.compile(rf"^(?:{re.escape(settings.FASTAPI_API_V1_PATH)})?/(auth|mcp)(/|$)")


class _Denied(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


class _KeySnapshot:
    __slots__ = ("expire_time", "key_id", "status", "user_id")

    def __init__(self, key_id: str, user_id: int, status: str, expire_time) -> None:
        self.key_id = key_id
        self.user_id = user_id
        self.status = status
        self.expire_time = expire_time


class _Cache:
    """key_id -> (monotonic 截止, 快照/范围集合)。30s TTL + 写操作即时清。"""

    def __init__(self) -> None:
        self._keys: dict[str, tuple[float, _KeySnapshot]] = {}
        self._scopes: dict[str, tuple[float, frozenset[tuple[str, str]]]] = {}

    def _prune(self) -> None:
        """过期条目清扫：Key 历史单调增长，不清就是慢泄漏。"""
        now = time.monotonic()
        self._keys = {k: v for k, v in self._keys.items() if v[0] > now}
        self._scopes = {k: v for k, v in self._scopes.items() if v[0] > now}

    def key(self, key_id: str) -> _KeySnapshot | None:
        hit = self._keys.get(key_id)
        if hit is None or hit[0] < time.monotonic():
            return None
        return hit[1]

    def put_key(self, snapshot: _KeySnapshot) -> None:
        if len(self._keys) >= _PRUNE_THRESHOLD:
            self._prune()
        self._keys[snapshot.key_id] = (time.monotonic() + _CACHE_TTL, snapshot)

    def scope(self, key_id: str) -> frozenset[tuple[str, str]] | None:
        hit = self._scopes.get(key_id)
        if hit is None or hit[0] < time.monotonic():
            return None
        return hit[1]

    def put_scope(self, key_id: str, scope: frozenset[tuple[str, str]]) -> None:
        self._scopes[key_id] = (time.monotonic() + _CACHE_TTL, scope)

    def drop(self, key_id: str) -> None:
        self._keys.pop(key_id, None)
        self._scopes.pop(key_id, None)


_cache = _Cache()
# 全量端点目录（method, path 模板）——匹配用；与 Key 范围集合同源、同 TTL
_catalog: tuple[float, frozenset[tuple[str, str]]] | None = None
# 插件启用态缓存：与 Key 快照同周期，停用插件后最长 30s 全实例生效
_enabled: tuple[float, bool] | None = None


async def _plugin_enabled() -> bool:
    """平台 API 的 kill switch：停用 mcphub 插件后，既有 Key（含永久 Key）
    也必须立刻失效——否则「停用」只停得掉 MCP 入口，停不掉平台 API 入口。
    取不到状态（Redis 异常）按未启用处理，fail-closed，与 dispatch 同口径。"""
    global _enabled
    now = time.monotonic()
    if _enabled is not None and _enabled[0] > now:
        return _enabled[1]
    from backend.plugin.mcphub.transport.dispatch import plugin_enabled

    enabled = await plugin_enabled()
    _enabled = (now + _CACHE_TTL, enabled)
    return enabled


def invalidate_key(key_id: str) -> None:
    """Key 的写操作（吊销 / 停用 / 改范围）后调用，让本实例立即生效。"""
    _cache.drop(key_id)


def invalidate_catalog() -> None:
    """端点目录刷新 / 端点启停后调用。"""
    global _catalog
    _catalog = None


def _segments_match(template: str, path: str) -> bool:
    """模板与请求路径逐段比：{x} 段通配任一段，其余段必须全等。

    分段数不同即不匹配——申请 GET /sys/user/{pk} 不会覆盖 GET /sys/user。
    """
    template_parts = template.split("/")
    path_parts = path.split("/")
    if len(template_parts) != len(path_parts):
        return False
    for expected, actual in zip(template_parts, path_parts, strict=True):
        if expected.startswith("{") and expected.endswith("}"):
            continue
        if expected != actual:
            return False
    return True


async def _catalog_pairs() -> frozenset[tuple[str, str]]:
    """全部端点（含已停用）的 (method, path) 对，30s 缓存。

    停用端点也在匹配集里：被停用端点的调用会命中模板但不在任何 Key 的有效
    集合内，得到的是 403「未授权」而不是 404——语义更准。
    """
    global _catalog
    now = time.monotonic()
    if _catalog is not None and _catalog[0] > now:
        return _catalog[1]
    from sqlalchemy import select

    from backend.database.db import async_db_session
    from backend.plugin.mcphub.model.entities import ApiEndpoint

    async with async_db_session() as db:
        pairs = frozenset((await db.execute(select(ApiEndpoint.method, ApiEndpoint.path))).all())
    _catalog = (now + _CACHE_TTL, pairs)
    return pairs


async def _match_template(method: str, path: str) -> str | None:
    """请求 (method, path) 命中的端点模板；目录里没有则 None（404 路线放行）。"""
    for endpoint_method, template in await _catalog_pairs():
        if endpoint_method == method and _segments_match(template, path):
            return template
    return None


def _header(scope, name: bytes) -> str | None:
    values = [value for key, value in scope.get("headers", ()) if key.lower() == name]
    if len(values) != 1:
        return None
    return values[0].decode("latin-1")


class ApiKeyAuthMiddleware:
    """纯 ASGI 中间件：无 Key 的请求零成本直穿（只多一次 headers 查找）。"""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        api_key = _header(scope, b"x-api-key")
        if not api_key:
            await self.app(scope, receive, send)
            return
        try:
            await self._authorize(scope, api_key)
        except _Denied as denial:
            await self._deny(send, denial)
            return
        await self.app(scope, receive, send)

    async def _authorize(self, scope, api_key: str) -> None:
        match = _KEY_RE.match(api_key.strip())
        if match is None:
            raise _Denied(401, "Invalid API key")
        key_id = match.group(1)
        if not await _plugin_enabled():
            raise _Denied(503, "MCP Hub is disabled")

        from backend.app.admin.model.user import User
        from backend.database.db import async_db_session
        from backend.plugin.mcphub.model.entities import ApiKey
        from backend.plugin.mcphub.service.api_request_service import effective_endpoint_pairs

        snapshot = _cache.key(key_id)
        if snapshot is None:
            async with async_db_session() as db:
                row = await db.get(ApiKey, key_id)
                if row is None or row.status != "active":
                    raise _Denied(401, "Invalid API key")
                from backend.utils.timezone import timezone

                if row.expire_time is not None and row.expire_time <= timezone.now():
                    raise _Denied(401, "API key expired")
                user = await db.get(User, row.user_id)
                if user is None or user.deleted or not user.status:
                    raise _Denied(401, "Inactive API key user")
                snapshot = _KeySnapshot(key_id, row.user_id, row.status, row.expire_time)
            _cache.put_key(snapshot)

        method = (scope.get("method") or "GET").upper()
        if method == "HEAD":
            method = "GET"  # HEAD 与 GET 同一路由模板，范围按 GET 判
        template = await _match_template(method, scope.get("path") or "/")
        if template is None:
            # 端点目录里没有的路径（404 路线）：不需要 Key 范围判断，交给下游
            return
        if _FORBIDDEN_PATH.match(template):
            raise _Denied(403, "Endpoint is not available via API key")

        scope_pairs = _cache.scope(key_id)
        if scope_pairs is None:
            async with async_db_session() as db:
                scope_pairs = frozenset(await effective_endpoint_pairs(db, key_id, snapshot.user_id))
            _cache.put_scope(key_id, scope_pairs)
        if (method, template) not in scope_pairs:
            raise _Denied(403, "Endpoint is not authorized for this key")

        await self._exchange(scope, snapshot.user_id)

    async def _exchange(self, scope, user_id: int) -> None:
        """用 Key 所属用户换一枚短期 JWT，改写 authorization 头后放行。"""
        from backend.common.security.token import create_access_token

        result = await create_access_token(user_id, multi_login=False, swagger=True)
        headers = [
            (key, value) for key, value in scope.get("headers", ()) if key.lower() != b"authorization"
        ]
        headers.append((b"authorization", f"Bearer {result.access_token}".encode()))
        scope["headers"] = headers
        logger.debug("API key 已交换为用户 %s 的短期凭证", user_id)

    async def _deny(self, send, denial: _Denied) -> None:
        payload = json.dumps({"code": denial.status, "msg": denial.message, "data": None}).encode()
        headers = [(b"content-type", b"application/json"), (b"cache-control", b"no-store")]
        if denial.status == 401:
            headers.append((b"www-authenticate", b"Bearer"))
        await send({"type": "http.response.start", "status": denial.status, "headers": headers})
        await send({"type": "http.response.body", "body": payload})
