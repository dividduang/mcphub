from dataclasses import dataclass
from os import environ

from backend.core.conf import settings


def _setting(name: str) -> str | None:
    """进程环境变量优先，其次 plugin.toml [settings]（经 PluginSettingsSource 并进
    pydantic settings）。plugin.toml 那一层自己不写回环境变量，只读 environ 等于
    把 toml 里的配置变成死配置——域名部署下 Host 白名单不生效会直接 421。
    """
    value = environ.get(f"MCPHUB_{name}")
    if value:
        return value
    merged = getattr(settings, f"MCPHUB_{name}", None)
    return str(merged) if merged not in (None, "") else None


def positive(name: str, default: int) -> int:
    value = int(_setting(name) or default)
    if not 1 <= value <= 10000:
        raise ValueError(f"MCPHUB_{name} must be between 1 and 10000")
    return value


def provision_id(name: str, default: int) -> int:
    """LDAP 自动建档的部门 ID：非整数配置直接报错，调用方转 503——
    不让 int() 的 ValueError 以 500 的形态漏给用户。"""
    raw = _setting(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"MCPHUB_{name} must be an integer, got {raw!r}") from exc


def provision_ids(name: str, default: str) -> list[int]:
    """LDAP 自动建档的角色 ID 列表（逗号分隔）：同上，逐项校验。"""
    raw = _setting(name) or default
    try:
        return [int(x) for x in raw.split(",") if x.strip()]
    except (TypeError, ValueError) as exc:
        raise ValueError(f"MCPHUB_{name} must be a comma-separated integer list, got {raw!r}") from exc


@dataclass(frozen=True)
class RuntimeConfig:
    global_limit: int = 32
    site_rps: int = 4
    max_bytes: int = 2 * 1024 * 1024
    max_request_bytes: int = 64 * 1024
    connect_timeout: float = 3.0
    read_timeout: float = 10.0
    # 代理转发用的读超时：MCP 的 tools/call 可能是长任务，两次 chunk 之间间隔
    # 几分钟很正常。用 read_timeout（10s）会把「首字节后长时间无输出」的长任务
    # 在响应头已发出后直接断连。这里单独放宽，默认 300s。
    stream_read_timeout: float = 300.0
    allowed_hosts: tuple[str, ...] = ("127.0.0.1", "localhost")
    allowed_origins: tuple[str, ...] = ()
    lock_path: str = "/tmp/fba-mcphub-executor.lock"

    @classmethod
    def from_env(cls) -> "RuntimeConfig":
        hosts = tuple(
            x.strip() for x in (_setting("ALLOWED_HOSTS") or "127.0.0.1,localhost").split(",") if x.strip()
        )
        origins = tuple(x.strip() for x in (_setting("ALLOWED_ORIGINS") or "").split(",") if x.strip())
        if not hosts or any("*" in x for x in (*hosts, *origins)):
            raise ValueError("Explicit non-wildcard MCP Host/Origin allowlists are required")
        max_request_bytes = int(_setting("MAX_REQUEST_BYTES") or cls.max_request_bytes)
        if not 1024 <= max_request_bytes <= 16 * 1024 * 1024:
            raise ValueError("MCPHUB_MAX_REQUEST_BYTES must be between 1024 and 16777216")
        stream_read_timeout = float(_setting("STREAM_READ_TIMEOUT") or cls.stream_read_timeout)
        if not 1 <= stream_read_timeout <= 3600:
            raise ValueError("MCPHUB_STREAM_READ_TIMEOUT must be between 1 and 3600")
        return cls(
            global_limit=positive("GLOBAL_LIMIT", 32),
            site_rps=positive("SITE_RPS", 4),
            max_request_bytes=max_request_bytes,
            stream_read_timeout=stream_read_timeout,
            allowed_hosts=hosts,
            allowed_origins=origins,
            lock_path=_setting("LOCK_PATH") or "/tmp/fba-mcphub-executor.lock",
        )
