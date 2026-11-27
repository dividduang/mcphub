"""管理员登记的外部 MCP 服务：请求头加密 + 上游地址校验 + CRUD。

面向自建 FBA 部署：本插件不限制上游必须是公网地址，http / https 都放行，
私网 / 回环 / 链路本地地址也放行——登记权在管理员手里，SSRF 风险评估由部署方自负。
这一层的职责是：

1. 上游地址能解析（走部署机系统解析器）、协议是 http/https；
2. 请求头整张 Fernet 加密后落库，只在插件向上游发请求时注入，
   终端用户的 MCP Key 永远不会被转发到上游；
3. 值里的 ${ENV_VAR} 在出站时才从进程环境展开，敏感值不必进库。

端点本身不授予任何权限：能不能用仍由 Key 是否有效和 `enabled` 决定。
"""

import base64
import hashlib
import json
import logging
import re
import socket
from os import environ
from urllib.parse import urlsplit

from cryptography.fernet import Fernet
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.common.exception import errors
from backend.core.conf import settings
from backend.plugin.mcphub.model.entities import (
    McpExternalServer,
    new_id,
)

logger = logging.getLogger(__name__)

_SLUG_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?$")


def _fernet() -> Fernet:
    # 专用 key 优先，缺省沿用 TOKEN_SECRET_KEY。
    # 没有单独配 key 时，轮换 TOKEN_SECRET_KEY 会让存量外部凭证解不开——
    # 届时需要重新登记，这是可接受的（fail-closed：解不开就当没配凭证）。
    secret = environ.get("MCPHUB_EXTERNAL_SECRET_KEY") or settings.TOKEN_SECRET_KEY
    digest = hashlib.sha256(secret.encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt_secret(secret: str) -> str:
    if not secret:
        return ""
    return _fernet().encrypt(secret.encode("utf-8")).decode("ascii")


def decrypt_secret(ciphertext: str) -> str:
    if not ciphertext:
        return ""
    try:
        return _fernet().decrypt(ciphertext.encode("ascii")).decode("utf-8")
    except Exception:
        # 解不开就不猜：当成未配置凭证，让上游按匿名处理并大概率 401，
        # 而不是把密文当明文发出去。
        return ""


_ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def resolve_headers(headers: dict[str, str]) -> dict[str, str]:
    """把 ${ENV_VAR} 展开成进程环境变量的值。

    解析不到就**丢掉这个头**并在日志里记一笔——宁可少一个头让上游 401，
    也不要把字面量 ${VAR} 发出去。敏感值因此可以只存在于部署环境里，不进库。
    """
    resolved: dict[str, str] = {}
    for key, value in headers.items():
        missing: list[str] = []

        def substitute(match: re.Match) -> str:
            name = match.group(1)
            found = environ.get(name)
            if found is None:
                missing.append(name)
                return ""
            return found

        expanded = _ENV_RE.sub(substitute, value)
        if missing:
            logger.warning("MCP Hub 外部服务请求头 %s 引用了未设置的环境变量: %s", key, ", ".join(missing))
            continue
        resolved[key] = expanded
    return resolved


def missing_env_vars(headers: dict[str, str]) -> list[str]:
    """登记头里引用了但进程环境里没有的 ${VAR} 名单（供探测端给出明确报错）。"""
    missing: set[str] = set()
    for value in headers.values():
        for name in _ENV_RE.findall(value or ""):
            if environ.get(name) is None:
                missing.add(name)
    return sorted(missing)


def encode_headers(headers: dict[str, str]) -> str:
    if not headers:
        return ""
    return _fernet().encrypt(json.dumps(headers, ensure_ascii=False).encode("utf-8")).decode("ascii")


def decode_headers(ciphertext: str) -> dict[str, str]:
    if not ciphertext:
        return {}
    try:
        payload = json.loads(_fernet().decrypt(ciphertext.encode("ascii")).decode("utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def validate_url(url: str) -> str:
    """上游地址校验。返回原 URL，不合法直接抛 RequestError。

    本项目面向自建部署：内网地址本身就是目标，所以**不做 SSRF 防护**——
    不限制 scheme 必须是 https，不拒绝私网 / 回环 / 链路本地地址。
    只保留两条真正影响可用性的检查：

    1. 协议得是 http 或 https（拒绝 ftp / file / gopher 之类）；
    2. 主机名必须能解析——走部署机的系统解析器，
       解析不了就是配错了，早失败比让用户看到一个 502 好。

    URL 里的 user:pass、查询串、片段都放行：自建服务用 basic auth 很常见，
    查询串对 MCP 端点也无害。
    """
    parts = urlsplit(url)
    if parts.scheme not in ("https", "http"):
        raise errors.RequestError(msg="只支持 http 和 https 上游地址")
    host = parts.hostname
    if not host:
        raise errors.RequestError(msg="上游地址没有主机名")

    try:
        infos = socket.getaddrinfo(host, parts.port or 443, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise errors.RequestError(msg=f"上游主机无法解析：{host}") from exc
    if not infos:
        raise errors.RequestError(msg=f"上游主机无法解析：{host}")
    return url


def server_data(row: McpExternalServer, base_url: str) -> dict:
    # 请求头只回名称和「是否已设置」，值永不回显；${ENV_VAR} 形态原样带回，
    # 让管理员知道这个头是引用环境变量而不是硬编码密钥。
    headers = decode_headers(row.headers_ciphertext)
    return {
        "id": row.id,
        "slug": row.slug,
        "name": row.name,
        "description": row.description,
        "enabled": row.enabled,
        "url": row.url,
        "transport": row.transport,
        "header_names": sorted(headers),
        "header_count": len(headers),
        "endpoint": f"{base_url.rstrip('/')}/mcp/ext-{row.slug}",
        "created_time": row.created_time,
        "created_by": row.created_by,
    }


def catalog_data(row: McpExternalServer, base_url: str) -> dict:
    """目录用的最小投影：人人可见的页面不回上游 URL / 请求头名 / 建档人。

    上游地址是内网拓扑，请求头名可能暴露集成了哪些系统，created_by 是管理员 ID——
    这些只属于管理端点（GET /external-servers）。
    """
    return {
        "id": row.id,
        "slug": row.slug,
        "name": row.name,
        "description": row.description,
        "enabled": row.enabled,
        "endpoint": f"{base_url.rstrip('/')}/mcp/ext-{row.slug}",
    }


async def list_servers(db: AsyncSession) -> list[McpExternalServer]:
    return list((await db.scalars(select(McpExternalServer).order_by(McpExternalServer.slug))).all())


async def get_by_slug(db: AsyncSession, slug: str) -> McpExternalServer | None:
    if not _SLUG_RE.match(slug or ""):
        return None
    return await db.scalar(select(McpExternalServer).where(McpExternalServer.slug == slug))


async def _unique_slug(db: AsyncSession, slug: str) -> str:
    exists = await db.scalar(select(McpExternalServer.id).where(McpExternalServer.slug == slug))
    if exists is None:
        return slug
    raise errors.ConflictError(msg="Slug already in use")


async def create_server(db: AsyncSession, obj, actor_id: int) -> McpExternalServer:
    url = validate_url(obj.url)
    slug = await _unique_slug(db, obj.slug)
    row = McpExternalServer(
        id=new_id(),
        slug=slug,
        name=obj.name,
        description=obj.description,
        url=url,
        transport=obj.transport,
        headers_ciphertext=encode_headers(obj.headers),
        created_by=actor_id,
    )
    db.add(row)
    await db.flush()
    return row


async def update_server(db: AsyncSession, row: McpExternalServer, obj) -> McpExternalServer:
    data = obj.model_dump(exclude_unset=True)
    if "url" in data and data["url"] is not None:
        data["url"] = validate_url(obj.url)
    # headers 是整张替换：传了（哪怕是空 dict）就按新值落库，不传才保持原样
    if "headers" in data and data["headers"] is not None:
        data["headers_ciphertext"] = encode_headers(data["headers"])
    data.pop("headers", None)
    for field, value in data.items():
        setattr(row, field, value)
    await db.flush()
    # URL / 请求头可能变了：目录里的工具清单缓存作废（延迟导入避免循环依赖）
    from backend.plugin.mcphub.service import tools_probe

    tools_probe.invalidate(row.id)
    return row


async def delete_server(db: AsyncSession, row: McpExternalServer) -> None:
    await db.delete(row)
    await db.flush()
    from backend.plugin.mcphub.service import tools_probe

    tools_probe.invalidate(row.id)
