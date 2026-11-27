import threading
import time
from contextlib import contextmanager

from backend.plugin.mcphub.transport.config import RuntimeConfig


class CapacityExceeded(Exception):
    """No waiting queue: callers may retry later with backoff."""


class BoundedExecutor:
    """Per-site token bucket for outbound upstream requests.

    Single-execution-instance by design: the lifespan's OS lock rejects a second
    local executor; distributed deployment is unsupported. 全局在途上限由
    transport/dispatch.McpDispatcher 的事件循环计数器负责，这里只管单站点速率。
    """

    def __init__(self, config: RuntimeConfig):
        self.config = config
        self.lock = threading.Lock()
        self.rate: dict[str, tuple[float, float]] = {}
        self.closing = False

    @contextmanager
    def network(self, site: str):
        """Apply a per-site token bucket without waiting."""
        with self.lock:
            if self.closing:
                raise CapacityExceeded("MCP Hub is shutting down")
            now = time.monotonic()
            tokens, updated = self.rate.get(site, (float(self.config.site_rps), now))
            tokens = min(self.config.site_rps, tokens + (now - updated) * self.config.site_rps)
            if tokens < 1:
                raise CapacityExceeded("Upstream request rate exhausted; retry later with backoff")
            self.rate[site] = (tokens - 1, now)
        yield

    async def close(self) -> None:
        with self.lock:
            self.closing = True
        # 之后 network() 一律抛 CapacityExceeded：在途请求排空后不再放行新的上游调用。
        # 代理据此返回 503（关停）而不是 429（限流）。
