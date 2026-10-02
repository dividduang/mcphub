import asyncio
import threading
import unittest

from dataclasses import replace
from unittest.mock import patch

from pydantic import ValidationError

from backend.plugin.mcphub.crawlers.catalog import ProjectArgs, pypi_files, pypi_versions
from backend.plugin.mcphub.crawlers.network import Fetcher, PinnedHTTPSAdapter, UpstreamError
from backend.plugin.mcphub.service.auth_service import McpAuthorizationError
from backend.plugin.mcphub.transport.config import RuntimeConfig
from backend.plugin.mcphub.transport.executor import BoundedExecutor, CapacityExceeded
from backend.plugin.mcphub.transport.tests.upstream import ControlledUpstream


class RuntimeBehaviorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.upstream = ControlledUpstream(('127.0.0.1', 0))
        self.thread = threading.Thread(target=self.upstream.serve_forever, daemon=True)
        self.thread.start()
        self.config = RuntimeConfig(global_limit=2, user_limit=2, key_limit=1, site_limit=2, site_rps=100,
                                    test_upstream=f'http://127.0.0.1:{self.upstream.server_port}')
        self.executor = BoundedExecutor(self.config)

    async def asyncTearDown(self):
        if self.upstream.release:
            self.upstream.release.set()
        await self.executor.close()
        await asyncio.to_thread(self.upstream.shutdown)
        self.upstream.server_close()
        self.thread.join()

    def fetch(self, stopped, authorize=lambda: None):
        return Fetcher(self.config, self.executor, 'pypi', authorize, stopped)

    async def test_cancelled_waiter_does_not_release_real_request_slot(self):
        self.upstream.release = threading.Event()
        task = asyncio.create_task(self.executor.execute(7, 'key13', 'pypi',
                                  lambda stopped: pypi_versions(self.fetch(stopped), 'requests', 2)))
        self.assertTrue(await asyncio.to_thread(self.upstream.started.wait, 3))
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(self.upstream.counters()['active'], 1)
        self.assertEqual(self.executor.snapshot()['admitted'], 1)
        with self.assertRaises(CapacityExceeded):
            await self.executor.execute(7, 'key13', 'pypi',
                  lambda stopped: pypi_versions(self.fetch(stopped), 'requests', 2))
        self.upstream.release.set()
        await self.executor.close()
        self.assertEqual(self.executor.snapshot()['admitted'], 0)
        self.assertEqual(self.executor.snapshot()['active_requests'], 0)

    async def test_package_integrity_and_version_window_are_real_transformations(self):
        files = await self.executor.execute(7, 'key5', 'pypi',
                    lambda stopped: pypi_files(self.fetch(stopped), 'requests', '2.1.0'))
        self.assertEqual(files['files'][0]['size'], 1234)
        self.assertEqual(files['files'][0]['digests']['sha256'], '0' * 64)
        versions = await self.executor.execute(7, 'key13', 'pypi',
                       lambda stopped: pypi_versions(self.fetch(stopped), 'requests', 2))
        self.assertEqual(versions['versions'], ['2.0.0', '2.1.0'])
        self.assertEqual(versions['total'], 3)
        self.assertEqual(self.upstream.counters()['hits'], 2)

    async def test_revocation_before_outbound_makes_zero_upstream_requests(self):
        def revoked():
            raise McpAuthorizationError('grant revoked')
        with self.assertRaises(McpAuthorizationError):
            await self.executor.execute(7, 'key5', 'pypi',
                  lambda stopped: self.fetch(stopped, revoked).get('/pypi/requests/json'))
        self.assertEqual(self.upstream.counters()['hits'], 0)
        self.assertEqual(self.executor.snapshot()['admitted'], 0)

    async def test_redirect_is_not_followed_and_bytes_are_bounded(self):
        self.upstream.redirect = True
        with self.assertRaises(UpstreamError):
            await self.executor.execute(7, 'key5', 'pypi',
                  lambda stopped: self.fetch(stopped).get('/pypi/requests/json'))
        self.assertEqual(self.upstream.counters()['hits'], 1)
        self.upstream.redirect = False
        self.config = replace(self.config, max_bytes=50)
        with self.assertRaises(UpstreamError):
            await self.executor.execute(7, 'key5', 'pypi',
                  lambda stopped: self.fetch(stopped).get('/pypi/requests/json'))
        self.assertEqual(self.executor.snapshot()['active_requests'], 0)

    async def test_ssrf_inputs_and_private_dns_fail_closed(self):
        for project in ('https://evil.invalid', '../metadata', 'a/b', 'a?x=y'):
            with self.assertRaises(ValidationError):
                ProjectArgs(project=project)
        with patch('socket.getaddrinfo', return_value=[(2, 1, 6, '', ('127.0.0.1', 443))]):
            with self.assertRaises(UpstreamError):
                PinnedHTTPSAdapter('pypi.org')
        with patch.dict('os.environ', {'MCPHUB_TEST_UPSTREAM': 'http://127.0.0.1:19091', 'MCPHUB_TEST_MODE': '0'}):
            with self.assertRaises(ValueError):
                RuntimeConfig.from_env()
        for address in ('127.0.0.1', '198.18.0.145', '10.0.0.1', '::1'):
            with patch.dict('os.environ', {'MCPHUB_PYPI_IP': address}):
                with self.assertRaises(ValueError):
                    RuntimeConfig.from_env()
            with self.assertRaises(UpstreamError):
                PinnedHTTPSAdapter('pypi.org', address)


if __name__ == '__main__':
    unittest.main()
