from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


logger = logging.getLogger("eva-analysis-service")


@dataclass(frozen=True)
class WhisperTranscription:
    transcript: str
    language: Optional[str]
    confidence: Optional[float]


class WhisperModel:
    """Whisper transcription wrapper.

    Intended backend (future): faster-whisper.

    NOTE: We avoid downloading models to the internal disk by relying on EVA_MODEL_ROOT
    and setting HF_HOME/TRANSFORMERS_CACHE (see config.ensure_hf_cache_dirs).
    """

    def __init__(
        self,
        model_root: Path,
        device: str = "cpu",
        *,
        enabled: bool = False,
        model_name: str = "medium",
        download_root: Optional[Path] = None,
    ) -> None:
        self.model_name = model_name
        self.model_dir = model_root / "whisper" / model_name
        self.download_root = download_root
        self.device = device
        self.enabled = enabled
        self.loaded = False

        self._model = None

    def _ensure_model(self) -> None:
        if self._model is not None:
            return

        if not self.enabled:
            logger.info("WhisperModel: disabled (skipping load)")
            return

        try:
            from faster_whisper import WhisperModel as FasterWhisperModel  # type: ignore

            self._model = FasterWhisperModel(
                self.model_name,
                device=self.device,
                download_root=str(self.download_root) if self.download_root else None,
            )
            self.loaded = True
            logger.info(
                "WhisperModel: loaded model=%s device=%s download_root=%s",
                self.model_name,
                self.device,
                str(self.download_root) if self.download_root else None,
            )
        except Exception:
            logger.exception("WhisperModel: failed to load model=%s", self.model_name)
            self._model = None
            self.loaded = False

    def transcribe(self, audio_path: Path) -> WhisperTranscription:
        if not self.enabled:
            return WhisperTranscription(transcript="", language=None, confidence=0.0)

        self._ensure_model()
        if self._model is None:
            # No pudimos cargar modelo: fallback vacío
            return WhisperTranscription(transcript="", language=None, confidence=0.0)

        from time import perf_counter

        start = perf_counter()

        try:
            segments, info = self._model.transcribe(str(audio_path), vad_filter=True)
            text = "".join(seg.text for seg in segments).strip()
            lang = getattr(info, "language", None)
            conf = getattr(info, "language_probability", None)

            logger.info(
                "WhisperModel: transcribed %s (len=%d, lang=%s, conf=%s, %.3fs)",
                audio_path,
                len(text),
                lang,
                conf or 0.0,
                perf_counter() - start,
            )

            return WhisperTranscription(
                transcript=text,
                language=lang,
                confidence=conf or 0.0,
            )
        except Exception:
            logger.exception("WhisperModel: transcribe failed for %s", audio_path)
            return WhisperTranscription(transcript="", language=None, confidence=0.0)
