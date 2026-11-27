"""tools_probe 纯函数回归测试（不需要 DB / 上游）。

守住三条：
1. `tools/list` 跟随 nextCursor 翻页——不翻会静默截断上游工具清单；
2. 上游 error 只做分类，原文（可能回显 URL / 主机名）不进用户可见消息；
3. 按用户刷新去抖的戳记语义。
"""

import time

from backend.plugin.mcphub.service.tools_probe import (
    _CACHE,
    UpstreamToolsError,
    _notification,
    _rpc,
    _tools_of,
    refresh_stamp,
)


def _page(tools, cursor=None):
    result = {"tools": tools}
    if cursor:
        result["nextCursor"] = cursor
    return {"jsonrpc": "2.0", "id": 2, "result": result}


def test_tools_of_normal_page():
    tools, cursor = _tools_of(_page([{"name": "a", "title": "A", "description": "d", "inputSchema": {}}]))
    assert [t["name"] for t in tools] == ["a"]
    assert tools[0]["title"] == "A"
    assert cursor is None


def test_tools_of_returns_next_cursor():
    _, cursor = _tools_of(_page([{"name": "a"}], cursor="page-2"))
    assert cursor == "page-2"


def test_tools_of_skips_entries_without_name():
    tools, _ = _tools_of(_page([{"name": "a"}, {"title": "no-name"}, "not-a-dict"]))
    assert [t["name"] for t in tools] == ["a"]


def test_tools_of_annotations_title_fallback():
    tools, _ = _tools_of(_page([{"name": "a", "annotations": {"title": "From Annotations"}}]))
    assert tools[0]["title"] == "From Annotations"


def test_tools_of_upstream_error_is_classified_not_surfaced():
    # 上游 error.message 可能回显 URL / 主机名 / 内部细节：消息里不许出现
    payload = {"jsonrpc": "2.0", "id": 2, "error": {"code": -32600, "message": "boom http://203.0.113.7:8000/x"}}
    try:
        _tools_of(payload)
    except UpstreamToolsError as exc:
        assert "boom" not in str(exc)
        assert "203.0.113.7" not in str(exc)
    else:
        raise AssertionError("expected UpstreamToolsError")


def test_tools_of_malformed_payload_raises():
    for payload in ({"result": {}}, {"result": {"tools": "nope"}}, {"jsonrpc": "2.0", "id": 2}):
        try:
            _tools_of(payload)
        except UpstreamToolsError:
            continue
        raise AssertionError(f"expected UpstreamToolsError for {payload}")


def test_rpc_shapes():
    init = _rpc("initialize", 1)
    assert init["method"] == "initialize"
    assert init["id"] == 1
    assert init["params"]["protocolVersion"]
    assert init["params"]["clientInfo"]["name"]

    listed = _rpc("tools/list", 2, cursor="abc")
    assert listed["params"] == {"cursor": "abc"}
    assert _rpc("tools/list", 2)["params"] == {}

    note = _notification("notifications/initialized")
    assert "id" not in note  # 通知没有 id，上游不得回复
    assert note["method"] == "notifications/initialized"


def test_refresh_stamp_tracks_previous():
    now = time.monotonic()
    first = refresh_stamp(1, "srv", now)
    assert first == 0.0  # 没有上一次
    later = refresh_stamp(1, "srv", now + 10)
    assert later == now
    # 不同用户 / 不同服务各算各的
    assert refresh_stamp(2, "srv", now + 20) == 0.0
    assert refresh_stamp(1, "other", now + 20) == 0.0


def test_fetch_tools_cache_hit_skips_upstream():
    # 缓存命中直接返回，不打上游（这里没有上游可打，打了必失败）
    _CACHE.clear()
    _CACHE["cached-srv"] = (
        time.monotonic(),
        [{"name": "a", "title": "A", "description": "", "input_schema": {}, "id": "cached-srv::a"}],
    )

    import asyncio
    from types import SimpleNamespace

    from backend.plugin.mcphub.service.tools_probe import fetch_tools

    row = SimpleNamespace(id="cached-srv", headers_ciphertext="")
    tools = asyncio.run(fetch_tools(row))
    assert [t["name"] for t in tools] == ["a"]
    _CACHE.clear()
