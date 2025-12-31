from __future__ import annotations

from typing import Dict, List, Literal, Optional

from pydantic import BaseModel


class EpisodeInsightStats(BaseModel):
    totalShards: int
    durationSeconds: Optional[float] = None
    shardsWithEmotion: int
    firstShardAt: Optional[float] = None
    lastShardAt: Optional[float] = None


class EpisodeEmotionSummary(BaseModel):
    primaryCounts: Dict[str, int]
    valenceCounts: Dict[str, int]
    activationCounts: Dict[str, int]


class EpisodeKeyMomentEmotion(BaseModel):
    primary: Optional[str] = None
    valence: Optional[Literal["positive", "neutral", "negative"]] = None
    activation: Optional[Literal["low", "medium", "high"]] = None
    headline: Optional[str] = None


class EpisodeKeyMoment(BaseModel):
    shardId: str
    episodeId: str
    startTime: Optional[float] = None
    endTime: Optional[float] = None
    reason: Literal["highestIntensity", "strongNegative", "strongPositive"]
    emotion: EpisodeKeyMomentEmotion
    transcriptSnippet: Optional[str] = None


class EpisodeInsightsResponse(BaseModel):
    episodeId: str
    stats: EpisodeInsightStats
    emotionSummary: EpisodeEmotionSummary
    keyMoments: List[EpisodeKeyMoment]
