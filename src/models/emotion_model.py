from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from src.schemas.analysis import ArousalLevel, CoreEmotion, EmotionLabelScore, ProsodyFlags, Valence


@dataclass(frozen=True)
class EmotionResult:
    primaryEmotion: Optional[CoreEmotion]
    emotionLabels: list[EmotionLabelScore]
    valence: Optional[Valence]
    arousal: Optional[ArousalLevel]
    prosodyFlags: Optional[ProsodyFlags]


class EmotionModel:
    """Stub SER model.

    Future: load a real SER model from `${EVA_MODEL_ROOT}/emotion-ser`.
    """

    def __init__(self, model_root: Path, *, enabled: bool = False) -> None:
        self.model_dir = model_root / "emotion-ser"
        self.enabled = enabled
        self.loaded = self.enabled and self.model_dir.exists() and self.model_dir.is_dir()

    def analyze(
        self,
        audio_path: Path,
        transcript: Optional[str],
        *,
        intensity: Optional[float] = None,
        duration_seconds: Optional[float] = None,
    ) -> EmotionResult:
        # Deterministic stub (when disabled): simple heuristics.
        # A real SER model will be integrated later when enabled+loaded.
        text = (transcript or "").lower()

        shouting = "present" if (intensity is not None and intensity >= 0.75) else "none"
        tired = bool(duration_seconds is not None and duration_seconds >= 6.0 and (intensity or 0.0) < 0.2)

        if any(k in text for k in ["gracias", "bien", "feliz", "genial"]):
            primary: CoreEmotion = "alegria"
            valence: Valence = "positivo"
        elif any(k in text for k in ["no", "mal", "odio", "enojo", "enojado"]):
            primary = "enojo"
            valence = "negativo"
        elif tired:
            primary = "cansancio"
            valence = "neutral"
        else:
            primary = "neutro"
            valence = "neutral"

        if intensity is None:
            arousal: ArousalLevel = "medio"
        elif intensity < 0.25:
            arousal = "bajo"
        elif intensity < 0.7:
            arousal = "medio"
        else:
            arousal = "alto"

        labels = [
            EmotionLabelScore(label=primary, score=0.6),
            EmotionLabelScore(label="neutro", score=0.4 if primary != "neutro" else 0.6),
        ]

        return EmotionResult(
            primaryEmotion=primary,
            emotionLabels=labels,
            valence=valence,
            arousal=arousal,
            prosodyFlags=ProsodyFlags(
                laughter="none",
                crying="none",
                shouting=shouting,
                sighing="none",
                tension="light" if shouting == "present" else "none",
            ),
        )
