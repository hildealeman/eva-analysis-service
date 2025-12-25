from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel


CoreEmotion = Literal[
    'alegria',
    'calma',
    'tristeza',
    'enojo',
    'miedo',
    'sorpresa',
    'cansancio',
    'neutro',
]

Valence = Literal['negativo', 'neutral', 'positivo']

ArousalLevel = Literal['bajo', 'medio', 'alto']


class EmotionLabelScore(BaseModel):
    label: str
    score: float


class ProsodyFlags(BaseModel):
    laughter: Optional[Literal['none', 'light', 'strong']] = None
    crying: Optional[Literal['none', 'present']] = None
    shouting: Optional[Literal['none', 'present']] = None
    sighing: Optional[Literal['none', 'present']] = None
    tension: Optional[Literal['none', 'light', 'high']] = None


class ShardFeatures(BaseModel):
    rms: Optional[float] = None
    zcr: Optional[float] = None
    spectralCentroid: Optional[float] = None
    intensity: Optional[float] = None


class ShardMeta(BaseModel):
    shardId: Optional[str] = None
    source: Optional[str] = None
    startTime: Optional[float] = None
    endTime: Optional[float] = None


class EmotionBlock(BaseModel):
    primary: Optional[CoreEmotion] = None
    valence: Optional[Valence] = None
    activation: Optional[ArousalLevel] = None
    scores: Optional[list[EmotionLabelScore]] = None


class SignalFeaturesBlock(BaseModel):
    rms: Optional[float] = None
    peak: Optional[float] = None
    centerFrequency: Optional[float] = None
    zcr: Optional[float] = None


class SemanticFlags(BaseModel):
    needsFollowup: bool = False
    possibleCrisis: bool = False


class SemanticBlock(BaseModel):
    summary: Optional[str] = None
    topics: Optional[list[str]] = None
    momentType: Optional[str] = None
    flags: Optional[SemanticFlags] = None


class ShardAnalysisResult(BaseModel):
    transcript: Optional[str] = None
    transcriptLanguage: Optional[str] = None
    transcriptionConfidence: Optional[float] = None

    # Additive enriched fields (do not break existing frontend consumers)
    language: Optional[str] = None
    emotion: Optional[EmotionBlock] = None
    signalFeatures: Optional[SignalFeaturesBlock] = None
    semantic: Optional[SemanticBlock] = None

    primaryEmotion: Optional[CoreEmotion] = None
    emotionLabels: Optional[list[EmotionLabelScore]] = None
    valence: Optional[Valence] = None
    arousal: Optional[ArousalLevel] = None
    prosodyFlags: Optional[ProsodyFlags] = None

    analysisSource: Literal['local', 'cloud'] = 'local'
    analysisMode: Literal['automatic', 'manual'] = 'automatic'
    analysisVersion: Optional[str] = None
    analysisAt: datetime
