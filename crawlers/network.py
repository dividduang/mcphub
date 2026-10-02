import json
import socket
import threading
import time

from ipaddress import ip_address
from typing import Callable

import requests

from requests.adapters import HTTPAdapter

from backend.plugin.mcphub.transport.config import RuntimeConfig
from backend.plugin.mcphub.transport.executor import BoundedExecutor, ExecutionStopped

HOSTS = {'pypi': 'pypi.org', 'hackernews': 'hacker-news.firebaseio.com'}


class UpstreamError(Exception):
    """Sanitized failure; URLs, bodies and credentials are not included."""


class PinnedHTTPSAdapter(HTTPAdapter):
    """Resolve once, reject non-public DNS answers, pin socket IP and verify host TLS."""

    def __init__(self, host: str, address: str | None = None):
        self.host = host
        addresses = {address} if address else {row[4][0] for row in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)}
        if not addresses or any(not ip_address(address).is_global for address in addresses):
            raise UpstreamError('Upstream DNS returned a prohibited address')
        self.address = sorted(addresses)[0]
        super().__init__(max_retries=0, pool_connections=1, pool_maxsize=1, pool_block=True)

    def get_connection_with_tls_context(self, request, verify, proxies=None, cert=None):
        if verify is not True or proxies:
            raise UpstreamError('Upstream TLS verification and direct egress are required')
        host_params, pool_kwargs = self.build_connection_pool_key_attributes(request, True, None)
        if host_params['host'] != self.host or host_params['scheme'] != 'https':
            raise UpstreamError('Unexpected upstream destination')
        host_params['host'] = self.address
        pool_kwargs.update(assert_hostname=self.host, server_hostname=self.host)
        return self.poolmanager.connection_from_host(**host_params, pool_kwargs=pool_kwargs)


class Fetcher:
    def __init__(self, config: RuntimeConfig, executor: BoundedExecutor, site: str,
                 authorize: Callable[[], None], stopped: threading.Event):
        self.config, self.executor, self.site = config, executor, site
        self.authorize, self.stopped = authorize, stopped
        self.deadline = time.monotonic() + config.deadline

    def check(self):
        if self.stopped.is_set():
            raise ExecutionStopped('Request cancelled')
        if time.monotonic() >= self.deadline:
            raise UpstreamError('Upstream operation deadline exceeded')

    def get(self, path: str, *, index: bool = False):
        self.check()
        # Only implementation-owned paths reach here; never follow returned URLs.
        if not path.startswith('/') or any(x in path for x in ('..', '?', '#', '\\', '//')):
            raise UpstreamError('Invalid fixed-source path')
        host = HOSTS[self.site]
        origin = self.config.test_upstream or f'https://{host}'
        try:
            with requests.Session() as session:
                session.trust_env = False  # No ambient proxy, netrc, cookies or user credentials.
                if not self.config.test_upstream:
                    pin = self.config.pypi_ip if self.site == 'pypi' else self.config.hackernews_ip
                    session.mount(f'https://{host}/', PinnedHTTPSAdapter(host, pin))
                headers = {
                    'Accept': 'application/vnd.pypi.simple.v1+json' if index else 'application/json',
                    'Accept-Encoding': 'gzip, deflate', 'User-Agent': 'FBA-MCPHub/1.0 (public metadata)',
                    'Host': host if not self.config.test_upstream else origin.split('//', 1)[1],
                }
                # Authorize after DNS/setup, directly before each new network operation.
                self.authorize()
                self.check()
                with self.executor.network(self.site):
                    with session.get(origin + path, headers=headers, timeout=(self.config.connect_timeout, self.config.read_timeout),
                                     allow_redirects=False, stream=True, verify=True) as response:
                        if response.status_code != 200:
                            raise UpstreamError(f'Upstream returned HTTP {response.status_code}')
                        if 'json' not in response.headers.get('Content-Type', '').lower():
                            raise UpstreamError('Upstream did not return JSON')
                        data = bytearray()
                        for chunk in response.iter_content(chunk_size=1024):
                            self.check()
                            if len(data) + len(chunk) > self.config.max_bytes:
                                raise UpstreamError('Decompressed upstream response exceeds byte limit')
                            data.extend(chunk)
                self.check()
                return json.loads(data)
        except (requests.RequestException, OSError, ValueError):
            raise UpstreamError('Upstream network or JSON response failed') from None
