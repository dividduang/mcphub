from sqlalchemy import select

from backend.database.db import async_db_session
from backend.plugin.mcphub.model.entities import McpServer, McpTool


async def sync_catalog(servers: list[dict]) -> None:
    """Sync code-owned descriptions without resetting administrator policy or grants."""
    async with async_db_session.begin() as db:
        existing_servers = {row.slug: row for row in (await db.scalars(select(McpServer))).all()}
        seen_servers: set[str] = set()
        for spec in servers:
            slug = spec['slug']
            seen_servers.add(slug)
            server = existing_servers.get(slug)
            if server is None:
                server = McpServer(slug=slug, name=spec['name'], description=spec.get('description', ''))
                db.add(server)
                await db.flush()
            else:
                server.name = spec['name']
                server.description = spec.get('description', '')
            tools = {row.name: row for row in (await db.scalars(
                select(McpTool).where(McpTool.server_id == server.id)
            )).all()}
            seen_tools: set[str] = set()
            for item in spec['tools']:
                name = item['name']
                seen_tools.add(name)
                tool = tools.get(name)
                if tool is None:
                    tool = McpTool(server_id=server.id, name=name, title=item.get('title', name))
                    db.add(tool)
                tool.title = item.get('title', name)
                tool.description = item.get('description', '')
                tool.input_schema = item['input_schema']
            for name, tool in tools.items():
                if name not in seen_tools:
                    tool.enabled = False
        for slug, server in existing_servers.items():
            if slug not in seen_servers:
                server.enabled = False
