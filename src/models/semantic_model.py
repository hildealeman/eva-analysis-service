from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Optional

from src.schemas.analysis import SemanticBlock, SemanticFlags, SignalFeaturesBlock

logger = logging.getLogger("eva-analysis-service")


_SYSTEM_PROMPT = """You are EVA, an emotional and semantic analyzer for short voice diary clips in Spanish.
You receive:
- transcript: what the person said (mostly Spanish, sometimes Spanglish)
- language: ISO language code if available
- basic acoustic features (rms, peak, centerFrequency, zcr)

Your job is to output a compact JSON object with:
- summary: 1–3 sentences summarizing what the person is expressing, in the same language as the transcript.
- topics: 2–5 short keywords (single words or very short phrases) capturing the main themes (e.g. \"ansiedad\", \"trabajo\", \"familia\", \"salud\", \"agradecimiento\").
- momentType: one of:
  - \"check-in\" (estado general / cómo se siente)
  - \"desahogo\" (queja, descarga emocional)
  - \"crisis\" (angustia intensa, pensamientos de daño, urgencia emocional)
  - \"recuerdo\" (memoria del pasado)
  - \"meta\" (planes, objetivos, compromisos)
  - \"agradecimiento\" (gratitud, cosas buenas)
  - \"otro\" (si no encaja en lo anterior)
- flags:
  - needsFollowup: true si este momento merece ser revisado en otra sesión aunque no sea una crisis.
  - possibleCrisis: true solo si hay señales claras de crisis emocional (desesperación extrema, ideas de daño, riesgo).

IMPORTANT:
- Output ONLY a JSON object. No explanations, no extra text.
- Be conservative with \"possibleCrisis\": only true if the language clearly suggests danger or serious risk.
"""


@dataclass(frozen=True)
class SemanticModelConfig:
    model: str = "gpt-4.1-mini"
    timeout_seconds: float = 20.0


class SemanticModel:
    def __init__(self, *, api_key: Optional[str], config: Optional[SemanticModelConfig] = None) -> None:
        self._api_key = api_key
        self._config = config or SemanticModelConfig()
        self._client = None
        self.loaded = bool(api_key)

    def _get_client(self):
        if self._client is not None:
            return self._client

        if not self._api_key:
            return None

        try:
            from openai import OpenAI  # type: ignore

            self._client = OpenAI(api_key=self._api_key)
            return self._client
        except Exception:
            return None

    def analyze(
        self,
        transcript: str,
        language: Optional[str],
        features: Optional[SignalFeaturesBlock],
    ) -> SemanticBlock:
        # Safe fallback if API key missing or empty transcript
        if not transcript.strip():
            return SemanticBlock(
                summary="",
                topics=[],
                momentType="otro",
                flags=SemanticFlags(needsFollowup=False, possibleCrisis=False),
            )

        client = self._get_client()
        if client is None:
            return SemanticBlock(
                summary="",
                topics=[],
                momentType="otro",
                flags=SemanticFlags(needsFollowup=False, possibleCrisis=False),
            )

        payload = {
            "transcript": transcript,
            "language": language,
            "signalFeatures": features.model_dump() if features else None,
        }

        try:
            # Prefer JSON-only response using response_format when supported.
            resp = client.chat.completions.create(
                model=self._config.model,
                messages=[
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {
                        "role": "user",
                        "content": json.dumps(payload, ensure_ascii=False),
                    },
                ],
                response_format={"type": "json_object"},
                temperature=0.2,
            )

            content = (resp.choices[0].message.content or "").strip()
            data = json.loads(content)

            flags = data.get("flags") or {}
            return SemanticBlock(
                summary=data.get("summary") or "",
                topics=data.get("topics") or [],
                momentType=data.get("momentType") or "otro",
                flags=SemanticFlags(
                    needsFollowup=bool(flags.get("needsFollowup")) if "needsFollowup" in flags else False,
                    possibleCrisis=bool(flags.get("possibleCrisis")) if "possibleCrisis" in flags else False,
                ),
            )
        except Exception:
            logger.exception("SemanticModel.analyze failed")
            return SemanticBlock(
                summary="",
                topics=[],
                momentType="otro",
                flags=SemanticFlags(needsFollowup=False, possibleCrisis=False),
            )
