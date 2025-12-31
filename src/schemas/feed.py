from __future__ import annotations

from datetime import datetime
from typing import List, Literal, Optional

from pydantic import BaseModel


class FeedItemEmotion(BaseModel):
    primary: Optional[str] = None
    valence: Optional[Literal["positive", "neutral", "negative"]] = None
    activation: Optional[Literal["low", "medium", "high"]] = None
    headline: Optional[str] = None
    intensity: Optional[float] = None


class FeedItem(BaseModel):
    id: str
    shardId: str
    episodeId: str
    publishedAt: datetime
    startTimeSec: Optional[float] = None
    endTimeSec: Optional[float] = None
    status: Optional[str] = None
    userTags: List[str]
    emotion: FeedItemEmotion
    transcriptSnippet: Optional[str] = None


class FeedResponse(BaseModel):
    items: List[FeedItem]


class PublishShardResponse(BaseModel):
    ok: bool
    shardId: str
    profileId: str
    publishedAt: datetime


class DeletePublishedShardResponse(BaseModel):
    ok: bool
    shardId: str
    profileId: str
