"""FBA OpenAPI → 平台 API 端点目录。

把进程内的 `app.openapi()` 摊平成可申请、可授权、可绑定的端点行：每个
(method, path) 一行。与 MCP 侧的 tools_probe 对称——那边探上游 tools/list，
这边读本进程的路由表；两边都做「新行上行、消失行软下线、不删行」。

同步时机：插件 setup hook 首次全量 + 管理端 POST /api/mcphub/api/refresh。
"""

import re
from typing import Any

from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.conf import settings
from backend.plugin.mcphub.model.entities import ApiEndpoint, new_id

# 路由统一挂在 FASTAPI_API_V1_PATH（默认 /api/v1）下，排除式必须带真实前缀：
# 漏了前缀会对 /api/v1/auth/* 这类真实路径完全失效，同步与中间件两道闸双双失灵。
# 不开放申请的端点（正则，全路径匹配）：
#   {V1}/auth/*  登录、验证码、登出、刷新——对 API Key 持有者无意义，
#                 且 login/swagger 调试端点放开等于把爆破面交给外部
#   {V1}/mcp/*   MCP Hub 自身的代理与 Key 管理——避免自授权递归
#   /docs 等     文档与探测端点（不在 V1 前缀下）
_V1 = re.escape(settings.FASTAPI_API_V1_PATH)
_EXCLUDED = (
    re.compile(rf"^{_V1}/auth/"),
    re.compile(rf"^{_V1}/mcp/"),
    re.compile(r"^/docs"),
    re.compile(r"^/openapi\.json$"),
    re.compile(r"^/redoc"),
    re.compile(rf"^{_V1}/mcphub/"),
)


def is_excluded(path: str) -> bool:
    return any(pattern.match(path) for pattern in _EXCLUDED)


def _parameters_of(operation: dict[str, Any]) -> list[dict[str, Any]]:
    """path/query 参数的紧凑投影（供前端展示，不做请求体构造）。"""
    picked = []
    for param in operation.get("parameters", []) or []:
        if not isinstance(param, dict):
            continue
        picked.append(
            {
                "name": param.get("name"),
                "in": param.get("in"),
                "required": bool(param.get("required")),
                "description": param.get("description") or "",
            }
        )
    return picked


def _body_of(operation: dict[str, Any]) -> dict[str, Any] | None:
    """requestBody 的紧凑投影：有结构给结构，否则只标「有请求体」。"""
    body = operation.get("requestBody")
    if not isinstance(body, dict):
        return None
    content = body.get("content") or {}
    for media in content.values():
        if isinstance(media, dict) and isinstance(media.get("schema"), dict):
            return {"required": bool(body.get("required")), "schema": media["schema"]}
    return {"required": bool(body.get("required")), "schema": None}


def build_endpoints(app: FastAPI) -> list[dict[str, Any]]:
    """从 OpenAPI 摊平端点定义。顺序即 OpenAPI 顺序（前端按 tag 分组照此呈现）。"""
    spec = app.openapi()
    out: list[dict[str, Any]] = []
    for path, item in (spec.get("paths") or {}).items():
        if is_excluded(path):
            continue
        for method, operation in item.items():
            if method.lower() not in ("get", "post", "put", "patch", "delete"):
                continue
            if not isinstance(operation, dict):
                continue
            out.append(
                {
                    "method": method.upper(),
                    "path": path,
                    "tag": (operation.get("tags") or ["未分组"])[0],
                    "summary": operation.get("summary") or "",
                    "description": operation.get("description") or "",
                    "input_schema": {
                        "parameters": _parameters_of(operation),
                        "body": _body_of(operation),
                    },
                    "deprecated": bool(operation.get("deprecated")),
                }
            )
    return out


async def sync_endpoints(db: AsyncSession, app: FastAPI) -> dict[str, int]:
    """把 OpenAPI 同步进 mcphub_api_endpoint。返回 {created, updated, retired}。"""
    definitions = build_endpoints(app)
    existing = {(row.method, row.path): row for row in (await db.scalars(select(ApiEndpoint))).all()}
    created = updated = 0
    seen: set[tuple[str, str]] = set()
    for item in definitions:
        key = (item["method"], item["path"])
        seen.add(key)
        row = existing.get(key)
        if row is None:
            db.add(
                ApiEndpoint(
                    id=new_id(),
                    method=item["method"],
                    path=item["path"],
                    tag=item["tag"],
                    summary=item["summary"],
                    description=item["description"],
                    input_schema=item["input_schema"],
                    deprecated=item["deprecated"],
                    enabled=True,
                )
            )
            created += 1
            continue
        # 只刷新描述性字段与 deprecated；enabled 是管理员的决定，同步不覆盖
        if (
            row.tag != item["tag"]
            or row.summary != item["summary"]
            or row.description != item["description"]
            or row.input_schema != item["input_schema"]
            or row.deprecated != item["deprecated"]
        ):
            row.tag = item["tag"]
            row.summary = item["summary"]
            row.description = item["description"]
            row.input_schema = item["input_schema"]
            row.deprecated = item["deprecated"]
            updated += 1
    retired = 0
    for key, row in existing.items():
        if key not in seen and row.enabled:
            # 上游（这里是本进程路由）不再提供的端点软下线，不删行——
            # 授权与 Key 绑定还可能指着它
            row.enabled = False
            retired += 1
    await db.flush()
    return {"created": created, "updated": updated, "retired": retired}
