"""平台 API 的纯函数与单测。

中间件的强制逻辑按设计文档拆成可单测的纯函数：
- `_segments_match`：模板分段匹配（{x} 通配、段数必须相等）
- `_FORBIDDEN_PATH`：兜底排除
- `api_catalog`：OpenAPI 摊平时的排除名单与参数投影
"""

from backend.plugin.mcphub.service import api_catalog
from backend.plugin.mcphub.transport.api_key_auth import _FORBIDDEN_PATH, _segments_match


class TestSegmentsMatch:
    def test_exact_path(self):
        assert _segments_match("/api/v1/sys/users", "/api/v1/sys/users")

    def test_param_segment_matches_any_value(self):
        assert _segments_match("/api/v1/sys/users/{pk}", "/api/v1/sys/users/123")
        assert _segments_match("/api/v1/sys/users/{pk}", "/api/v1/sys/users/abc-def")

    def test_segment_count_must_match(self):
        # 申请 /sys/users/{pk} 不覆盖 /sys/users（列表）——这是范围精度的关键
        assert not _segments_match("/api/v1/sys/users/{pk}", "/api/v1/sys/users")
        assert not _segments_match("/api/v1/sys/users", "/api/v1/sys/users/1")
        assert not _segments_match("/api/v1/sys/users", "/api/v1/sys/users/1/extra")

    def test_static_segment_must_be_equal(self):
        assert not _segments_match("/api/v1/sys/users/{pk}", "/api/v1/sys/depts/1")
        # 前缀相同但段值不同：不匹配
        assert not _segments_match("/api/v1/sys/user", "/api/v1/sys/users")

    def test_trailing_slash_is_a_different_path(self):
        assert not _segments_match("/api/v1/sys/users", "/api/v1/sys/users/")


class TestForbiddenPath:
    def test_auth_and_mcp_prefixes_rejected(self):
        # 真实路由挂在 /api/v1 下（FASTAPI_API_V1_PATH）——带前缀是主路径，
        # 裸前缀也一并拦（纵深防御：将来若有路由挂在别处）
        for path in (
            "/api/v1/auth/login",
            "/api/v1/auth/captcha",
            "/api/v1/mcp/ext-deepwiki",
            "/api/v1/mcp/anything",
            "/auth/login",
            "/mcp/anything",
        ):
            assert _FORBIDDEN_PATH.match(path), path

    def test_similar_prefix_not_rejected(self):
        # /authorized 不是 /auth 开头（按整段判定，不能误伤）
        assert not _FORBIDDEN_PATH.match("/api/v1/authorized/list")
        assert not _FORBIDDEN_PATH.match("/authorized/list")

    def test_normal_api_allowed(self):
        assert not _FORBIDDEN_PATH.match("/api/v1/sys/users")


class TestApiCatalogExclusion:
    def test_excluded_paths(self):
        # 与 _FORBIDDEN_PATH 同一道事实：auth/mcp 的真实路径带 /api/v1 前缀。
        # 曾经这里写裸 /auth/*，正则会话对真实路径完全失效，测试还全绿。
        for path in (
            "/api/v1/auth/login",
            "/api/v1/auth/login/swagger",
            "/api/v1/auth/captcha",
            "/api/v1/auth/logout",
            "/api/v1/auth/refresh",
            "/api/v1/mcp/ext-x",
            "/docs",
            "/openapi.json",
            "/redoc",
            "/api/v1/mcphub/metadata",
            "/api/v1/mcphub/keys",
        ):
            assert api_catalog.is_excluded(path), path

    def test_normal_paths_kept(self):
        for path in ("/api/v1/sys/users", "/api/v1/sys/users/{pk}", "/api/v1/sys/menus"):
            assert not api_catalog.is_excluded(path), path

    def test_parameter_projection_picks_name_in_required(self):
        operation = {
            "parameters": [
                {"name": "pk", "in": "path", "required": True, "description": "主键"},
                {"name": "keyword", "in": "query"},
            ],
        }
        picked = api_catalog._parameters_of(operation)
        assert picked == [
            {"name": "pk", "in": "path", "required": True, "description": "主键"},
            {"name": "keyword", "in": "query", "required": False, "description": ""},
        ]

    def test_body_projection_returns_schema_when_present(self):
        operation = {"requestBody": {"required": True, "content": {"application/json": {"schema": {"type": "object"}}}}}
        assert api_catalog._body_of(operation) == {"required": True, "schema": {"type": "object"}}

    def test_body_projection_without_schema(self):
        assert api_catalog._body_of({}) is None
        assert api_catalog._body_of({"requestBody": {}}) == {"required": False, "schema": None}
