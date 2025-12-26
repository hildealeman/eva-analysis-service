from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel

from src.schemas.episodes import EpisodeSummaryResponse


class TagStat(BaseModel):
    tag: str
    count: int


class StatusStat(BaseModel):
    status: str
    count: int


class EmotionStat(BaseModel):
    emotion: str
    count: int


class EpisodeInsightsResponse(BaseModel):
    totalEpisodes: int
    totalShards: int
    totalDurationSeconds: Optional[float] = None
    tags: list[TagStat]
    statuses: list[StatusStat]
    emotions: list[EmotionStat]
    lastEpisode: Optional[EpisodeSummaryResponse] = None
