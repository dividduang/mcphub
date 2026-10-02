import fcntl
import os

from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field

from fastapi import FastAPI

from backend.plugin.mcphub.crawlers.catalog import CATALOG
from backend.plugin.mcphub.service.catalog_service import sync_catalog
from backend.plugin.mcphub.transport.config import RuntimeConfig
from backend.plugin.mcphub.transport.dispatch import McpDispatcher
from backend.plugin.mcphub.transport.executor import BoundedExecutor
from backend.plugin.mcphub.transport.server import make_apps


@dataclass
class Hub:
    config: RuntimeConfig
    executor: BoundedExecutor
    apps: dict = field(default_factory=dict)
    ready: bool = False


def setup(app: FastAPI) -> None:
    config = RuntimeConfig.from_env()
    hub = Hub(config=config, executor=BoundedExecutor(config))
    hub.apps = make_apps(config, hub.executor)
    app.state.mcphub = hub
    # Plugin setup runs after register_middleware; add_middleware inserts outermost.
    # No FBA-wide allowlist, JWT replacement or operation-log disable is needed.
    app.add_middleware(McpDispatcher, hub=hub)


@asynccontextmanager
async def lifespan(app: FastAPI):
    hub = app.state.mcphub
    lock_fd = os.open(hub.config.lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    acquired = False
    try:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except BlockingIOError:
            raise RuntimeError('MCP Hub requires exactly one execution instance; a local executor already holds its lock') from None
        # FBA core stage has created tables and initialized Redis before this hook.
        await sync_catalog(CATALOG)
        async with AsyncExitStack() as stack:
            for child in hub.apps.values():
                await stack.enter_async_context(child.lifespan(child))
            hub.ready = True
            try:
                yield
            finally:
                hub.ready = False
                # Drain while child managers and DB/Redis are still alive.
                await hub.executor.close()
    finally:
        hub.ready = False
        # Includes startup failure: never leave a live worker pool or held lock.
        await hub.executor.close()
        if acquired:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)
