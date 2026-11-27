"""上游工具清单落地：让「申请 / 授权 / Key 范围」有稳定的一行可以指向。

外部服务的工具活在上游，只有 tools/list 才知道；而申请、授权、Key 绑定都需要
本地有稳定的行。所以在枚举成功时把上游清单 upsert 进 mcphub_tool：

- **mirror 行**（`mcphub_server`，slug = `ext-<slug>`）承载外键：
  `mcphub_tool.server_id` 指着它，`authenticate_key` 的「服务 enabled」也读它；
- 新出现的工具插入（`enabled=True`、`auto_approve=False`——默认要人工批）；
- 已存在的只刷新标题 / 描述 / 入参，不动 enabled / auto_approve（那是管理员的决定）；
- 上游不再提供的置 `enabled=False`，**不删行**：授权和 Key 绑定还可能指着它，
  删行会让历史授权变成悬空引用。

同步时机：`GET /servers/{id}/tools` 探测成功之后（懒同步——登记时上游不一定可达）。
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.plugin.mcphub.model.entities import McpExternalServer, McpServer, McpTool, new_id


async def ensure_mirror(db: AsyncSession, ext: McpExternalServer) -> McpServer:
    """外部服务在 mcphub_server 里的镜像行（slug 前缀 ext-，与 REST 建服务时一致）。"""
    slug = f"ext-{ext.slug}"
    row = await db.scalar(select(McpServer).where(McpServer.slug == slug))
    if row is None:
        row = McpServer(id=new_id(), slug=slug, name=ext.name, description=ext.description, enabled=ext.enabled)
        db.add(row)
        await db.flush()
    else:
        row.name = ext.name
        row.description = ext.description
        row.enabled = ext.enabled
    return row


async def sync_tools(db: AsyncSession, ext: McpExternalServer, tools: list[dict]) -> None:
    """把一次上游枚举的结果 upsert 进 mcphub_tool。tools 为 tools_probe 的形状。"""
    mirror = await ensure_mirror(db, ext)
    existing = {
        row.name: row
        for row in (await db.scalars(select(McpTool).where(McpTool.server_id == mirror.id))).all()
    }
    seen: set[str] = set()
    for item in tools:
        name = item["name"]
        seen.add(name)
        row = existing.get(name)
        if row is None:
            db.add(
                McpTool(
                    id=new_id(),
                    server_id=mirror.id,
                    name=name,
                    title=item.get("title") or name,
                    description=item.get("description") or "",
                    input_schema=item.get("input_schema") or {},
                )
            )
        else:
            row.title = item.get("title") or name
            row.description = item.get("description") or ""
            row.input_schema = item.get("input_schema") or {}
    # 上游不再提供的工具软下线，不删行（授权 / 绑定可能还指着）
    for name, row in existing.items():
        if name not in seen:
            row.enabled = False
    await db.flush()
