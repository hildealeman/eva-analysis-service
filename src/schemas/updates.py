from __future__ import annotations

from typing import Optional

from pydantic import BaseModel


class EpisodeUpdateRequest(BaseModel):
    title: Optional[str] = None
    note: Optional[str] = None


class ShardUpdateRequest(BaseModel):
    status: Optional[str] = None
    userTags: Optional[list[str]] = None
    userNotes: Optional[str] = None
    transcriptOverride: Optional[str] = None


class ShardPublishRequest(BaseModel):
    force: bool = False


class ShardDeleteRequest(BaseModel):
    reason: str
