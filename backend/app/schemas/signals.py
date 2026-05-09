from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class PendingItem(BaseModel):
    id: str
    source_id: str
    pillar: str
    url: str | None = None
    title: str | None = None
    author: str | None = None
    published_at: datetime | None = None
    raw_text: str | None = None
    raw_json: dict[str, Any] | None = None


class SignalIn(BaseModel):
    raw_item_id: str
    signal_type: str = "composite"
    analyst_version: str = "v1"
    pillar: str
    payload: dict[str, Any] = Field(default_factory=dict)


class SignalOut(BaseModel):
    id: int
    raw_item_id: str
    signal_type: str
    analyst_version: str
    pillar: str
    payload: dict[str, Any]
    created_at: datetime
