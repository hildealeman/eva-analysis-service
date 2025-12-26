from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel


class EpisodeSummaryResponse(BaseModel):
    id: str
    createdAt: datetime
    title: Optional[str] = None
    note: Optional[str] = None
    shardCount: int
    durationSeconds: Optional[float] = None
    primaryEmotion: Optional[str] = None
    valence: Optional[str] = None
    arousal: Optional[str] = None


class ShardWithAnalysisResponse(BaseModel):
    id: str
    episodeId: Optional[str] = None
    startTime: Optional[float] = None
    endTime: Optional[float] = None
    source: Optional[str] = None
    meta: dict[str, Any]
    features: dict[str, Any]
    analysis: dict[str, Any]


class EpisodeDetailResponse(BaseModel):
    summary: EpisodeSummaryResponse
    shards: list[ShardWithAnalysisResponse]
