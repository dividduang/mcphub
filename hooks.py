import logging
import os
from contextlib import asynccontextmanager
from dataclasses import dataclass

from fastapi import FastAPI

from backend.plugin.mcphub.transport.config import RuntimeConfig
from backend.plugin.mcphub.transport.dispatch import McpDispatcher
from backend.plugin.mcphub.transport.executor import BoundedExecutor
from backend.plugin.mcphub.transport.external_proxy import ExternalRouter

logger = logging.getLogger(__name__)

try:
    import fcntl
except ImportError:  # Windows 开发机没有 fcntl：单实例锁降级为跳过，其余功能照常
    fcntl = None  # type: ignore[assignment]
    logger.error(
        "MCP Hub: 当前平台没有 fcntl，单执行实例锁已跳过。"
        "同一台机器上跑多个进程会突破 MCPHUB_GLOBAL_LIMIT 全局配额；"
        "生产部署必须是 Linux。"
    )


@dataclass
class Hub:
    config: RuntimeConfig
    executor: BoundedExecutor
    external: ExternalRouter | None = None
    ready: bool = False


def setup(app: FastAPI) -> None:
    config = RuntimeConfig.from_env()
    hub = Hub(config=config, executor=BoundedExecutor(config))
    # 外部服务共用一个路由实例，slug 从请求路径里取；见 transport/external_proxy.py
    hub.external = ExternalRouter(config, hub.executor)
    app.state.mcphub = hub
    # Plugin setup runs after register_middleware; add_middleware inserts outermost.
    # No FBA-wide allowlist, JWT replacement or operation-log disable is needed.
    app.add_middleware(McpDispatcher, hub=hub)


@asynccontextmanager
async def lifespan(app: FastAPI):
    hub = app.state.mcphub
    lock_fd = -1
    acquired = False
    try:
        if fcntl is not None:
            lock_fd = os.open(hub.config.lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except BlockingIOError:
                raise RuntimeError(
                    "MCP Hub requires exactly one execution instance; a local executor already holds its lock"
                ) from None
        hub.ready = True
        # 端点目录首次同步：新库的表是空的，不同步「平台 API」目录页就没有可申请项。
        # 失败（典型：迁移 SQL 还没跑）只记日志——其余功能照常，可事后手动刷新。
        try:
            from backend.database.db import async_db_session
            from backend.plugin.mcphub.service import api_catalog

            async with async_db_session.begin() as db:
                stats = await api_catalog.sync_endpoints(db, app)
            logger.info("MCP Hub 平台 API 端点目录同步完成: %s", stats)
        except Exception:
            logger.exception("MCP Hub 端点目录同步失败（可手动 POST /api/v1/mcphub/api/refresh 重试）")
        try:
            yield
        finally:
            hub.ready = False
            # Drain while DB/Redis are still alive.
            await hub.executor.close()
    finally:
        hub.ready = False
        # Includes startup failure: never leave a live upstream pool or held lock.
        await hub.executor.close()
        if hub.external is not None:
            await hub.external.aclose()
        if acquired:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        if lock_fd >= 0:
            os.close(lock_fd)
