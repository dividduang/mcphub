from datetime import datetime
from uuid import uuid4

from sqlalchemy import JSON, BigInteger, Boolean, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from backend.common.model import MappedBase, TimeZone
from backend.utils.timezone import timezone


def new_id() -> str:
    return uuid4().hex


class HubEntity(MappedBase):
    __abstract__ = True

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    created_time: Mapped[datetime] = mapped_column(TimeZone, default=timezone.now)


class McpServer(HubEntity):
    __tablename__ = "mcphub_server"

    slug: Mapped[str] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(String(128))
    description: Mapped[str] = mapped_column(String(2000), default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)


class McpTool(HubEntity):
    __tablename__ = "mcphub_tool"
    __table_args__ = (UniqueConstraint("server_id", "name", name="uq_mcphub_tool_server_name"),)

    server_id: Mapped[str] = mapped_column(ForeignKey("mcphub_server.id"), index=True)
    name: Mapped[str] = mapped_column(String(128))
    title: Mapped[str] = mapped_column(String(128))
    description: Mapped[str] = mapped_column(String(2000), default="")
    input_schema: Mapped[dict] = mapped_column(JSON, default=dict)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    auto_approve: Mapped[bool] = mapped_column(Boolean, default=False)


class McpRequest(HubEntity):
    __tablename__ = "mcphub_request"

    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    tool_ids: Mapped[list] = mapped_column(JSON)
    granted_tool_ids: Mapped[list] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    note: Mapped[str] = mapped_column(String(1000), default="")
    decided_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)


class McpGrant(HubEntity):
    __tablename__ = "mcphub_grant"
    __table_args__ = (UniqueConstraint("user_id", "tool_id", name="uq_mcphub_grant_user_tool"),)

    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    tool_id: Mapped[str] = mapped_column(ForeignKey("mcphub_tool.id"), index=True)
    generation: Mapped[str] = mapped_column(String(32), default=new_id)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    source: Mapped[str] = mapped_column(String(32))
    granted_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)


class McpKey(HubEntity):
    __tablename__ = "mcphub_key"

    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    name: Mapped[str] = mapped_column(String(64))
    secret_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16), default="active")
    expire_time: Mapped[datetime | None] = mapped_column(TimeZone, nullable=True)


class McpKeyBinding(HubEntity):
    __tablename__ = "mcphub_key_binding"
    __table_args__ = (UniqueConstraint("key_id", "tool_id", name="uq_mcphub_binding_key_tool"),)

    key_id: Mapped[str] = mapped_column(ForeignKey("mcphub_key.id"), index=True)
    tool_id: Mapped[str] = mapped_column(ForeignKey("mcphub_tool.id"), index=True)
    generation: Mapped[str] = mapped_column(String(32))


class McpExternalServer(HubEntity):
    """管理员登记的 MCP 服务——插件里唯一的服务来源。

    上游 URL 由管理员在「MCP服务器」页填写，代码不内置任何服务。
    本插件面向自建部署，上游以内网服务为主，所以不限制 scheme（http/https
    都放行）、不拒绝私网地址；只要求主机名能经系统 DNS 解析、协议是 http/https。

    请求头是一整张 map 而不是单个 auth_header：对齐常见 MCP 客户端配置的形状
    （Authorization + 若干自定义头），值可以写 ${ENV_VAR}，在出站时才从进程环境展开，
    敏感值因此不必进库。终端用户的 MCP Key 永远不会被转发到上游。
    """

    __tablename__ = "mcphub_external_server"

    slug: Mapped[str] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(String(128))
    description: Mapped[str] = mapped_column(String(2000), default="")
    url: Mapped[str] = mapped_column(String(500))
    transport: Mapped[str] = mapped_column(String(16), default="http")
    headers_ciphertext: Mapped[str] = mapped_column(String(4000), default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_by: Mapped[int] = mapped_column(BigInteger)


class ApiEndpoint(HubEntity):
    """FBA 自家 REST 端点目录行（OpenAPI 同步成果）。

    与 MCP 侧的工具行平行：工具来自外部上游的 tools/list，端点来自本进程的
    app.openapi()。二者都是「可申请、可授权、可绑定」的资源，但存储与调用
    路径本质不同（method+path 范围 vs 工具名；中间件强制 vs 代理强制），
    所以不共用表，只共用申请单状态机这类纯逻辑。
    """

    __tablename__ = "mcphub_api_endpoint"
    __table_args__ = (UniqueConstraint("method", "path", name="uq_mcphub_api_endpoint_method_path"),)

    method: Mapped[str] = mapped_column(String(8), index=True)
    path: Mapped[str] = mapped_column(String(300))
    tag: Mapped[str] = mapped_column(String(64), default="", index=True)
    summary: Mapped[str] = mapped_column(String(200), default="")
    description: Mapped[str] = mapped_column(String(2000), default="")
    input_schema: Mapped[dict] = mapped_column(JSON, default=dict)
    deprecated: Mapped[bool] = mapped_column(Boolean, default=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)


class ApiRequest(HubEntity):
    """平台 API 的访问申请单。状态机与 MCP 申请一致（见 service/api_request_service）。"""

    __tablename__ = "mcphub_api_request"

    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    endpoint_ids: Mapped[list] = mapped_column(JSON)
    granted_endpoint_ids: Mapped[list] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    note: Mapped[str] = mapped_column(String(1000), default="")
    decided_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)


class ApiGrant(HubEntity):
    """用户 ↔ 端点 的授权。撤销不删行（Key 绑定还指着它），重授即新 generation。"""

    __tablename__ = "mcphub_api_grant"
    __table_args__ = (UniqueConstraint("user_id", "endpoint_id", name="uq_mcphub_api_grant_user_endpoint"),)

    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    endpoint_id: Mapped[str] = mapped_column(ForeignKey("mcphub_api_endpoint.id"), index=True)
    generation: Mapped[str] = mapped_column(String(32), default=new_id)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    source: Mapped[str] = mapped_column(String(32))
    granted_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)


class ApiKey(HubEntity):
    """平台 API 的长期调用凭证。

    secret 形如 ak_<32hex>.<43secret>，只存 sha256；expire_time 为空即永久，
    与 MCP Key 的语义一致（创建时可选填，不强制过期）。
    """

    __tablename__ = "mcphub_api_key"

    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    name: Mapped[str] = mapped_column(String(64))
    secret_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16), default="active", index=True)
    expire_time: Mapped[datetime | None] = mapped_column(TimeZone, nullable=True)


class ApiKeyEndpoint(MappedBase):
    """Key 的端点范围绑定。generation 对齐授权代际，撤销后旧绑定自然失效。

    纯关联表：复合主键 (key_id, endpoint_id)，不带 HubEntity 的 id/created_time
    （那一对是业务行的时间戳，关联表不需要）——与 sql 20261122_01_api_key.sql 一致。
    """

    __tablename__ = "mcphub_api_key_endpoint"
    __table_args__ = (UniqueConstraint("key_id", "endpoint_id", name="uq_mcphub_api_key_endpoint"),)

    key_id: Mapped[str] = mapped_column(ForeignKey("mcphub_api_key.id"), primary_key=True, index=True)
    endpoint_id: Mapped[str] = mapped_column(ForeignKey("mcphub_api_endpoint.id"), primary_key=True, index=True)
    generation: Mapped[str] = mapped_column(String(32))
