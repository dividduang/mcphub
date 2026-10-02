from datetime import datetime
from uuid import uuid4

from sqlalchemy import BigInteger, Boolean, ForeignKey, JSON, String, UniqueConstraint
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
    __tablename__ = 'mcphub_server'

    slug: Mapped[str] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(String(128))
    description: Mapped[str] = mapped_column(String(2000), default='')
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)


class McpTool(HubEntity):
    __tablename__ = 'mcphub_tool'
    __table_args__ = (UniqueConstraint('server_id', 'name', name='uq_mcphub_tool_server_name'),)

    server_id: Mapped[str] = mapped_column(ForeignKey('mcphub_server.id'), index=True)
    name: Mapped[str] = mapped_column(String(128))
    title: Mapped[str] = mapped_column(String(128))
    description: Mapped[str] = mapped_column(String(2000), default='')
    input_schema: Mapped[dict] = mapped_column(JSON, default=dict)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    auto_approve: Mapped[bool] = mapped_column(Boolean, default=False)


class McpRequest(HubEntity):
    __tablename__ = 'mcphub_request'

    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    tool_ids: Mapped[list] = mapped_column(JSON)
    granted_tool_ids: Mapped[list] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(16), default='pending', index=True)
    note: Mapped[str] = mapped_column(String(1000), default='')
    decided_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)


class McpGrant(HubEntity):
    __tablename__ = 'mcphub_grant'
    __table_args__ = (UniqueConstraint('user_id', 'tool_id', name='uq_mcphub_grant_user_tool'),)

    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    tool_id: Mapped[str] = mapped_column(ForeignKey('mcphub_tool.id'), index=True)
    generation: Mapped[str] = mapped_column(String(32), default=new_id)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    source: Mapped[str] = mapped_column(String(32))
    granted_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)


class McpKey(HubEntity):
    __tablename__ = 'mcphub_key'

    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    name: Mapped[str] = mapped_column(String(64))
    secret_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16), default='active')
    expire_time: Mapped[datetime | None] = mapped_column(TimeZone, nullable=True)


class McpKeyBinding(HubEntity):
    __tablename__ = 'mcphub_key_binding'
    __table_args__ = (UniqueConstraint('key_id', 'tool_id', name='uq_mcphub_binding_key_tool'),)

    key_id: Mapped[str] = mapped_column(ForeignKey('mcphub_key.id'), index=True)
    tool_id: Mapped[str] = mapped_column(ForeignKey('mcphub_tool.id'), index=True)
    generation: Mapped[str] = mapped_column(String(32))
