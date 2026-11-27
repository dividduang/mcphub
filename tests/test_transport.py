"""传输层纯函数回归测试（不需要 DB / 上游）。

重点守住两条边界：
1. 出站头只含会话相关字段——authorization / cookie 永远不上游；
2. 回客户端头只白名单放行——set-cookie 等不上行。
这两条是「终端用户 Key 不泄露给上游」的具体落地，改动 external_proxy 时先跑这里。
"""

from backend.plugin.mcphub.transport.config import _setting
from backend.plugin.mcphub.transport.external_proxy import (
    _PASSTHROUGH_REQUEST_HEADERS,
    _PASSTHROUGH_RESPONSE_HEADERS,
    _passthrough_headers,
    _response_headers,
    slug_from_path,
)


class FakeResponse:
    def __init__(self, headers: dict[str, str]):
        self.headers = headers


def test_passthrough_picks_only_session_headers():
    scope = {
        "headers": [
            (b"authorization", b"Bearer mcp_secret"),
            (b"cookie", b"session=abc"),
            (b"MCP-Session-Id", b"sess-1"),
            (b"mcp-protocol-version", b"2025-03-26"),
            (b"Last-Event-ID", b"evt-9"),
            (b"content-type", b"application/json"),
        ]
    }
    picked = _passthrough_headers(scope)
    assert picked == {
        "mcp-session-id": "sess-1",
        "mcp-protocol-version": "2025-03-26",
        "last-event-id": "evt-9",
    }
    # 凭证类头一个都不许出现
    assert "authorization" not in picked
    assert "cookie" not in picked


def test_passthrough_empty_without_session_headers():
    assert _passthrough_headers({"headers": [(b"authorization", b"Bearer x")]}) == {}


def test_response_headers_whitelist():
    response = FakeResponse(
        {
            "content-type": "text/event-stream",
            "mcp-session-id": "sess-2",
            "mcp-protocol-version": "2025-06-18",
            "set-cookie": "upstream=secret; Path=/",
            "x-upstream-token": "leak",
        }
    )
    headers = dict(_response_headers(response))
    assert headers[b"content-type"] == b"text/event-stream"
    assert headers[b"cache-control"] == b"private, no-store"
    assert headers[b"mcp-session-id"] == b"sess-2"
    assert headers[b"mcp-protocol-version"] == b"2025-06-18"
    assert b"set-cookie" not in headers
    assert b"x-upstream-token" not in headers


def test_response_headers_defaults_content_type():
    headers = dict(_response_headers(FakeResponse({})))
    assert headers[b"content-type"] == b"application/json"
    assert b"mcp-session-id" not in headers


def test_response_whitelist_covers_request_passthrough_fields():
    # 上游回给客户端的字段必须覆盖客户端能带上游的那一套，否则链路断一半；
    # retry-after 是响应单向字段（429 退避），允许只在这一侧。
    session_fields = {b"mcp-session-id", b"mcp-protocol-version"}
    assert session_fields <= set(_PASSTHROUGH_RESPONSE_HEADERS)
    assert session_fields <= set(_PASSTHROUGH_REQUEST_HEADERS)
    assert b"retry-after" in _PASSTHROUGH_RESPONSE_HEADERS


def test_slug_from_path():
    assert slug_from_path("/mcp/ext-deepwiki") == "deepwiki"
    assert slug_from_path("/mcp/ext-a.b-c_d") == "a.b-c_d"
    assert slug_from_path("/mcp/ext-") is None
    assert slug_from_path("/mcp/ext--x") is None  # 首字符必须是字母数字
    assert slug_from_path("/mcp/ext-中文") is None
    assert slug_from_path("/mcp/pypi") is None
    assert slug_from_path("/api/v1/mcphub/catalog") is None


def test_setting_prefers_env(monkeypatch):
    monkeypatch.setenv("MCPHUB_TEST_PRECEDENCE", "from-env")
    assert _setting("TEST_PRECEDENCE") == "from-env"


def test_setting_falls_back_to_plugin_toml(monkeypatch):
    # MCPHUB_ALLOWED_HOSTS 在 plugin.toml [settings] 有值；env 未设时取它
    monkeypatch.delenv("MCPHUB_ALLOWED_HOSTS", raising=False)
    assert _setting("ALLOWED_HOSTS") == "127.0.0.1,localhost"


def test_setting_empty_toml_value_is_none(monkeypatch):
    # plugin.toml 里显式留空的项不能变成非空配置（PUBLIC_BASE_URL 就是靠这个回落 base_url）
    monkeypatch.delenv("MCPHUB_PUBLIC_BASE_URL", raising=False)
    assert _setting("PUBLIC_BASE_URL") is None


def test_setting_unknown_name_is_none(monkeypatch):
    monkeypatch.delenv("MCPHUB_NOT_A_SETTING", raising=False)
    assert _setting("NOT_A_SETTING") is None
