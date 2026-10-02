from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, field_validator

from backend.utils.timezone import timezone

ToolIds = Annotated[list[str], Field(max_length=100)]


class Input(BaseModel):
    model_config = ConfigDict(extra='forbid')


class ToolSelection(Input):
    tool_ids: ToolIds = Field(min_length=1)

    @field_validator('tool_ids')
    @classmethod
    def unique_ids(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value):
            raise ValueError('Duplicate tool IDs')
        return value


class CreateRequest(ToolSelection):
    note: str = Field(default='', max_length=1000)


class DecideRequest(Input):
    approve_tool_ids: ToolIds


class CreateGrant(ToolSelection):
    user_id: int = Field(gt=0)


class CreateKey(ToolSelection):
    name: str = Field(min_length=1, max_length=64)
    expire_time: datetime | None = None

    @field_validator('expire_time')
    @classmethod
    def future_expiry(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value <= timezone.now()):
            raise ValueError('Expiry must be a future timezone-aware timestamp')
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
