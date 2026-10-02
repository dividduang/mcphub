import asyncio
import threading
import time

from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from typing import Any, Callable

from backend.plugin.mcphub.transport.config import RuntimeConfig


class CapacityExceeded(Exception):
    """No waiting queue: callers may retry later with backoff."""


class ExecutionStopped(Exception):
    pass


class BoundedExecutor:
    """Permits belong to workers, never to the coroutine awaiting them.

    This is deliberately a single-execution-instance limiter. The lifespan's OS
    lock rejects a second local executor; distributed deployment is unsupported.
    """

    def __init__(self, config: RuntimeConfig):
        self.config = config
        self.pool = ThreadPoolExecutor(max_workers=config.global_limit, thread_name_prefix='mcphub')
        self.lock = threading.Lock()
        self.users = Counter()
        self.keys = Counter()
        self.sites = Counter()
        self.running = self.active = self.peak_active = self.completed = self.rejected = 0
        self.site_active = Counter()
        self.rate: dict[str, tuple[float, float]] = {}
        self.closing = False

    def snapshot(self) -> dict:
        with self.lock:
            return {
                'admitted': self.running, 'active_requests': self.active,
                'peak_active_requests': self.peak_active, 'completed': self.completed,
                'rejected': self.rejected, 'site_active_requests': dict(self.site_active),
                'global_limit': self.config.global_limit, 'site_limit': self.config.site_limit,
                'queue_capacity': 0, 'deployment': 'single-execution-instance',
            }

    def _reserve(self, user_id: int, key_id: str, site: str) -> None:
        with self.lock:
            if (self.closing or self.running >= self.config.global_limit
                    or self.users[user_id] >= self.config.user_limit
                    or self.keys[key_id] >= self.config.key_limit
                    or self.sites[site] >= self.config.site_limit):
                self.rejected += 1
                raise CapacityExceeded('MCP execution capacity exhausted; retry later with backoff')
            self.running += 1
            self.users[user_id] += 1
            self.keys[key_id] += 1
            self.sites[site] += 1

    def _release(self, user_id: int, key_id: str, site: str) -> None:
        with self.lock:
            self.running -= 1
            self.completed += 1
            for counter, key in ((self.users, user_id), (self.keys, key_id), (self.sites, site)):
                counter[key] -= 1
                if not counter[key]:
                    del counter[key]

    @contextmanager
    def network(self, site: str):
        """Count real requests and apply a per-site token bucket without waiting."""
        with self.lock:
            now = time.monotonic()
            tokens, updated = self.rate.get(site, (float(self.config.site_rps), now))
            tokens = min(self.config.site_rps, tokens + (now - updated) * self.config.site_rps)
            if tokens < 1:
                self.rejected += 1
                raise CapacityExceeded('Upstream request rate exhausted; retry later with backoff')
            self.rate[site] = (tokens - 1, now)
            self.active += 1
            self.site_active[site] += 1
            self.peak_active = max(self.peak_active, self.active)
        try:
            yield
        finally:
            with self.lock:
                self.active -= 1
                self.site_active[site] -= 1

    async def execute(self, user_id: int, key_id: str, site: str, work: Callable[[threading.Event], Any]) -> Any:
        self._reserve(user_id, key_id, site)
        stopped = threading.Event()

        def run():
            try:
                if stopped.is_set():
                    raise ExecutionStopped('Request cancelled before execution')
                return work(stopped)
            finally:
                self._release(user_id, key_id, site)

        try:
            future = self.pool.submit(run)
        except BaseException:
            self._release(user_id, key_id, site)
            raise
        wrapped = asyncio.wrap_future(future)
        # Consume abandoned errors without retaining inputs or logging secrets.
        wrapped.add_done_callback(lambda done: None if done.cancelled() else done.exception())
        try:
            # wait() does not cancel the worker future when its waiter leaves.
            # Unlike shield(), it also does not report an expected late worker
            # cancellation as an unhandled exception on Python 3.14.
            await asyncio.wait({wrapped})
            return wrapped.result()
        except asyncio.CancelledError:
            stopped.set()
            raise

    async def close(self) -> None:
        with self.lock:
            self.closing = True
        # Keep the event loop alive: workers may still need DB authorization.
        await asyncio.to_thread(self.pool.shutdown, wait=True, cancel_futures=False)
