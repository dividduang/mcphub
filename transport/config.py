from dataclasses import dataclass
from ipaddress import ip_address
from os import environ
from urllib.parse import urlsplit


def positive(name: str, default: int) -> int:
    value = int(environ.get(f'MCPHUB_{name}', default))
    if not 1 <= value <= 10000:
        raise ValueError(f'MCPHUB_{name} must be between 1 and 10000')
    return value


@dataclass(frozen=True)
class RuntimeConfig:
    global_limit: int = 32
    user_limit: int = 8
    key_limit: int = 4
    site_limit: int = 4
    site_rps: int = 4
    max_bytes: int = 2 * 1024 * 1024
    connect_timeout: float = 3.0
    read_timeout: float = 10.0
    deadline: float = 20.0
    allowed_hosts: tuple[str, ...] = ('127.0.0.1', 'localhost')
    allowed_origins: tuple[str, ...] = ()
    test_upstream: str | None = None
    lock_path: str = '/tmp/fba-mcphub-executor.lock'
    pypi_ip: str | None = None
    hackernews_ip: str | None = None

    @classmethod
    def from_env(cls) -> 'RuntimeConfig':
        upstream = environ.get('MCPHUB_TEST_UPSTREAM') or None
        if upstream:
            if environ.get('MCPHUB_TEST_MODE') != '1':
                raise ValueError('Test upstream requires explicit MCPHUB_TEST_MODE=1')
            parts = urlsplit(upstream)
            if (parts.scheme != 'http' or not parts.hostname or not ip_address(parts.hostname).is_loopback
                    or not parts.port or parts.username or parts.password or parts.path not in ('', '/')
                    or parts.query or parts.fragment):
                raise ValueError('Test upstream must be an explicit HTTP literal-loopback origin with a port')
            upstream = upstream.rstrip('/')
        hosts = tuple(x.strip() for x in environ.get('MCPHUB_ALLOWED_HOSTS', '127.0.0.1,localhost').split(',') if x.strip())
        origins = tuple(x.strip() for x in environ.get('MCPHUB_ALLOWED_ORIGINS', '').split(',') if x.strip())
        if not hosts or any('*' in x for x in (*hosts, *origins)):
            raise ValueError('Explicit non-wildcard MCP Host/Origin allowlists are required')
        pins = {site: environ.get(f'MCPHUB_{site.upper()}_IP') or None
                for site in ('pypi', 'hackernews')}
        for address in pins.values():
            if address and not ip_address(address).is_global:
                raise ValueError('Deployment DNS pins must be public IP literals')
        return cls(
            global_limit=positive('GLOBAL_LIMIT', 32), user_limit=positive('USER_LIMIT', 8),
            key_limit=positive('KEY_LIMIT', 4), site_limit=positive('SITE_LIMIT', 4),
            site_rps=positive('SITE_RPS', 4), allowed_hosts=hosts, allowed_origins=origins,
            test_upstream=upstream, lock_path=environ.get('MCPHUB_LOCK_PATH', '/tmp/fba-mcphub-executor.lock'),
            pypi_ip=pins['pypi'], hackernews_ip=pins['hackernews'],
        )
