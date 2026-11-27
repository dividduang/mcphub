import re
from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, field_validator

from backend.utils.timezone import timezone

ToolIds = Annotated[list[str], Field(max_length=100)]


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ToolSelection(Input):
    tool_ids: ToolIds = Field(min_length=1)

    @field_validator("tool_ids")
    @classmethod
    def unique_ids(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value):
            raise ValueError("Duplicate tool IDs")
        return value


class CreateRequest(ToolSelection):
    note: str = Field(default="", max_length=1000)


class DecideRequest(Input):
    approve_tool_ids: ToolIds


class CreateGrant(ToolSelection):
    user_id: int = Field(gt=0)


class ResolveUser(Input):
    """按 AD 账号（sAMAccountName）解析目标用户；本地不存在时按 LDAP 建档。"""

    account: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9._-]+$")


class CreateKey(Input):
    """创建 Key。tool_ids 允许为空：外部 MCP 服务没有工具表、不校验工具范围，
    空范围 Key 照样能调用已启用的外部服务；绑了工具则只按范围放行。"""

    name: str = Field(min_length=1, max_length=64)
    tool_ids: ToolIds = Field(default_factory=list)
    expire_time: datetime | None = None

    @field_validator("tool_ids")
    @classmethod
    def unique_ids(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value):
            raise ValueError("Duplicate tool IDs")
        return value

    @field_validator("expire_time")
    @classmethod
    def future_expiry(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value <= timezone.now()):
            raise ValueError("Expiry must be a future timezone-aware timestamp")
        return value


class UpdateKey(Input):
    name: str | None = Field(default=None, min_length=1, max_length=64)
    tool_ids: ToolIds | None = None
    enabled: bool | None = None


class UpdateServer(Input):
    enabled: bool


class UpdateTool(Input):
    enabled: bool | None = None
    auto_approve: bool | None = None


class CreateExternalServer(Input):
    """登记一个外部 MCP 服务。slug 决定挂载路径 /mcp/ext-<slug>。"""

    slug: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?$")
    name: str = Field(min_length=1, max_length=128)
    description: str = Field(default="", max_length=2000)
    url: str = Field(min_length=1, max_length=500)
    transport: str = Field(default="http", pattern=r"^http$")
    # 请求头是键值对；值可以写 ${ENV_VAR}，出站时才从进程环境展开
    headers: dict[str, str] = Field(default_factory=dict)

    @field_validator("headers")
    @classmethod
    def valid_headers(cls, value: dict[str, str]) -> dict[str, str]:
        if len(value) > 20:
            raise ValueError("At most 20 headers")
        for key in value:
            # 只放行合法 HTTP 头名，避免把 CRLF 或非法字符带进出站请求
            if not re.fullmatch(r"[A-Za-z0-9-]+", key):
                raise ValueError("Invalid header name")
        return value


class UpdateExternalServer(Input):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    description: str | None = Field(default=None, max_length=2000)
    url: str | None = Field(default=None, min_length=1, max_length=500)
    transport: str | None = Field(default=None, pattern=r"^http$")
    # 传了字段就整张替换；不传则保持原样
    headers: dict[str, str] | None = None
    enabled: bool | None = None

    @field_validator("headers")
    @classmethod
    def valid_headers(cls, value: dict[str, str] | None) -> dict[str, str] | None:
        if value is None:
            return None
        if len(value) > 20:
            raise ValueError("At most 20 headers")
        for key in value:
            if not re.fullmatch(r"[A-Za-z0-9-]+", key):
                raise ValueError("Invalid header name")
        return value


# ---------------------------------------------------------------- 平台 API
# 与 MCP 侧同构但对象是端点行：端点没有 auto_approve，提交恒为 pending；
# Key 是长期凭证（expire_time 可空 = 永久），范围是 (method, path) 集合。
EndpointIds = Annotated[list[str], Field(max_length=500)]


class CreateApiRequest(Input):
    endpoint_ids: EndpointIds = Field(min_length=1)
    note: str = Field(default="", max_length=1000)

    @field_validator("endpoint_ids")
    @classmethod
    def unique_ids(cls, value: list[str]) -> list[str]:
        # 与 MCP 侧 ToolSelection 同口径：重复 ID 会让 granted 集合（set 去重）
        # 永远追不上 requested 计数，申请单永久卡在 partial、评不上 approved。
        if len(set(value)) != len(value):
            raise ValueError("Duplicate endpoint IDs")
        return value


class DecideApiRequest(Input):
    # 空列表 = 全部拒绝（与 MCP 侧审批语义一致）
    approve_endpoint_ids: EndpointIds = Field(default_factory=list)


class CreateApiGrant(Input):
    user_id: int = Field(gt=0)
    endpoint_ids: EndpointIds = Field(min_length=1)


class CreateApiKey(Input):
    name: str = Field(min_length=1, max_length=64)
    endpoint_ids: EndpointIds = Field(default_factory=list)
    expire_time: datetime | None = None


class UpdateApiKey(Input):
    name: str | None = Field(default=None, min_length=1, max_length=64)
    endpoint_ids: EndpointIds | None = None
    enabled: bool | None = None


class UpdateApiEndpoint(Input):
    enabled: bool
