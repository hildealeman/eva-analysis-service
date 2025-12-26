from __future__ import annotations

import logging
import os
import shutil
import uuid
from datetime import datetime, timezone
from fastapi.middleware.cors import CORSMiddleware
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse

from src.config import ensure_hf_cache_dirs, get_work_dir, load_config, model_root_available
from src.db import (
    get_episode_detail,
    compute_episode_insights,
    init_db,
    list_episodes_with_stats,
    save_shard_with_analysis,
    update_episode,
    update_shard,
)
from src.models.emotion_model import EmotionModel
from src.models.semantic_model import SemanticModel
from src.models.whisper_model import WhisperModel
from src.schemas.analysis import EmotionBlock, SemanticBlock, SemanticFlags, ShardAnalysisResult, ShardFeatures, ShardMeta, SignalFeaturesBlock
from src.schemas.episodes import EpisodeDetailResponse, EpisodeSummaryResponse, ShardWithAnalysisResponse
from src.schemas.insights import EpisodeInsightsResponse
from src.schemas.updates import EpisodeUpdateRequest, ShardUpdateRequest

app = FastAPI(title="EVA Analysis Service", version="0.1.0")

logger = logging.getLogger("eva-analysis-service")

origins = [
    "http://localhost:3000",
    "http://127.0.0.1:3000",
    "http://localhost:3001",
    "http://127.0.0.1:3001",
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

cfg = load_config()
ensure_hf_cache_dirs(cfg)

app.state.whisper_model = None
app.state.emotion_model = None
app.state.semantic_model = None
app.state.whisper_loaded_runtime = False
app.state.emotion_loaded_runtime = False


def get_models() -> tuple[WhisperModel | None, EmotionModel | None]:
    if not model_root_available(cfg):
        return None, None

    assert cfg.model_root is not None

    whisper: WhisperModel | None = app.state.whisper_model
    if whisper is None:
        whisper = WhisperModel(
            cfg.model_root,
            device=cfg.device,
            enabled=cfg.use_real_whisper,
            model_name=cfg.whisper_model_name,
            download_root=cfg.whisper_model_root,
        )
        app.state.whisper_model = whisper

    emotion: EmotionModel | None = app.state.emotion_model
    if emotion is None:
        emotion = EmotionModel(cfg.model_root, enabled=cfg.use_real_emotion)
        app.state.emotion_model = emotion

    return whisper, emotion


def get_semantic_model() -> SemanticModel:
    semantic: SemanticModel | None = app.state.semantic_model
    if semantic is None:
        semantic = SemanticModel(api_key=os.getenv("OPENAI_API_KEY"))
        app.state.semantic_model = semantic
    return semantic


@app.get("/health")
def health():
    available = model_root_available(cfg)
    whisper, emotion = get_models()

    status = "ok" if available else "degraded"

    return {
        "status": status,
        "modelRootAvailable": available,
        "whisperLoaded": bool(app.state.whisper_loaded_runtime) if available else False,
        "emotionModelLoaded": bool(app.state.emotion_loaded_runtime) if available else False,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


@app.on_event("startup")
def on_startup() -> None:
    init_db()


@app.get("/episodes", response_model=list[EpisodeSummaryResponse])
def list_episodes():
    return list_episodes_with_stats()


@app.get("/episodes/insights", response_model=EpisodeInsightsResponse)
def get_episodes_insights():
    return compute_episode_insights()


@app.get("/episodes/{episode_id}", response_model=EpisodeDetailResponse)
def read_episode(episode_id: str):
    episode = get_episode_detail(episode_id)
    if episode is None:
        raise HTTPException(status_code=404, detail="Episode not found")
    return episode


@app.patch("/episodes/{episode_id}", response_model=EpisodeSummaryResponse)
def patch_episode(episode_id: str, body: EpisodeUpdateRequest):
    updated = update_episode(episode_id, title=body.title, note=body.note)
    if updated is None:
        raise HTTPException(status_code=404, detail="Episode not found")

    for ep in list_episodes_with_stats():
        if ep.id == episode_id:
            return ep

    return EpisodeSummaryResponse(
        id=updated.id,
        createdAt=updated.created_at,
        title=updated.title,
        note=updated.note,
        shardCount=0,
        durationSeconds=None,
        primaryEmotion=None,
        valence=None,
        arousal=None,
    )


@app.patch("/shards/{shard_id}", response_model=ShardWithAnalysisResponse)
def patch_shard(shard_id: str, body: ShardUpdateRequest):
    updates_dict = body.model_dump(exclude_none=True)
    updated = update_shard(shard_id, updates_dict)
    if updated is None:
        raise HTTPException(status_code=404, detail="Shard not found")

    return ShardWithAnalysisResponse(
        id=updated.id,
        episodeId=updated.episode_id,
        startTime=updated.start_time,
        endTime=updated.end_time,
        source=updated.source,
        meta=updated.meta_json or {},
        features=updated.features_json or {},
        analysis=updated.analysis_json or {},
    )


@app.post("/analyze-shard", response_model=ShardAnalysisResult)
async def analyze_shard(
    audio: UploadFile = File(...),
    sampleRate: str = Form(...),
    durationSeconds: str = Form(...),
    features: str = Form("{}"),
    meta: str = Form("{}"),
):
    if not model_root_available(cfg):
        return JSONResponse(
            status_code=503,
            content={
                "error": "model_root_not_available",
                "message": "EVA_MODEL_ROOT no está disponible (disco no montado o ruta inválida).",
            },
        )

    if audio.content_type and audio.content_type not in {"audio/wav", "audio/x-wav", "audio/wave", "audio/vnd.wave"}:
        return JSONResponse(
            status_code=400,
            content={
                "error": "invalid_audio_type",
                "message": f"Unsupported content type: {audio.content_type}",
            },
        )

    try:
        sample_rate = int(float(sampleRate))
        duration_seconds = float(durationSeconds)
    except Exception:
        return JSONResponse(
            status_code=400,
            content={
                "error": "invalid_parameters",
                "message": "sampleRate and durationSeconds must be numeric.",
            },
        )

    if sample_rate <= 0 or duration_seconds <= 0:
        return JSONResponse(
            status_code=400,
            content={
                "error": "invalid_parameters",
                "message": "sampleRate and durationSeconds must be > 0.",
            },
        )

    work_dir = get_work_dir(cfg)
    work_dir.mkdir(parents=True, exist_ok=True)

    # Persist audio to a temp file
    tmp_name = f"shard-{uuid.uuid4().hex}.wav"
    tmp_path = work_dir / tmp_name

    try:
        with tmp_path.open("wb") as f:
            shutil.copyfileobj(audio.file, f)

        try:
            with tmp_path.open("rb") as f:
                header = f.read(12)
            if len(header) < 12 or header[0:4] != b"RIFF" or header[8:12] != b"WAVE":
                return JSONResponse(
                    status_code=400,
                    content={"error": "invalid_wav", "message": "Uploaded file is not a valid WAV."},
                )
        except Exception:
            return JSONResponse(
                status_code=400,
                content={"error": "invalid_wav", "message": "Could not validate WAV header."},
            )

        try:
            shard_features = ShardFeatures.model_validate_json(features)
        except Exception:
            shard_features = ShardFeatures()

        try:
            shard_meta = ShardMeta.model_validate_json(meta)
        except Exception:
            shard_meta = ShardMeta()

        whisper, emotion = get_models()

        transcript = ""
        transcript_language = None
        transcript_confidence = 0.0

        if whisper:
            tr = whisper.transcribe(tmp_path)
            transcript = tr.transcript
            transcript_language = tr.language
            transcript_confidence = tr.confidence or 0.0
            app.state.whisper_loaded_runtime = True

        primary_emotion = None
        emotion_labels = None
        valence = None
        arousal = None
        prosody_flags = None

        if emotion:
            er = emotion.analyze(
                tmp_path,
                transcript,
                intensity=shard_features.intensity,
                duration_seconds=duration_seconds,
            )
            primary_emotion = er.primaryEmotion
            emotion_labels = er.emotionLabels
            valence = er.valence
            arousal = er.arousal
            prosody_flags = er.prosodyFlags
            app.state.emotion_loaded_runtime = True

        now = datetime.now(timezone.utc)

        # Enriched blocks (optional, additive)
        signal = SignalFeaturesBlock(
            rms=shard_features.rms,
            zcr=shard_features.zcr,
            centerFrequency=shard_features.spectralCentroid,
            peak=shard_features.intensity,
        )

        emotion_block = EmotionBlock(
            primary=primary_emotion,
            valence=valence,
            activation=arousal,
            scores=emotion_labels,
        )

        semantic_model = get_semantic_model()
        semantic = semantic_model.analyze(
            transcript=transcript,
            language=transcript_language,
            features=signal,
        )

        result = ShardAnalysisResult(
            transcript=transcript or None,
            transcriptLanguage=transcript_language,
            transcriptionConfidence=transcript_confidence,

            language=transcript_language,
            emotion=emotion_block,
            signalFeatures=signal,
            semantic=semantic,

            primaryEmotion=primary_emotion,
            emotionLabels=emotion_labels,
            valence=valence,
            arousal=arousal,
            prosodyFlags=prosody_flags,
            analysisSource="local",
            analysisMode="automatic",
            analysisVersion="0.1.0-local",
            analysisAt=now,
        )

        # --- Persistencia en DB local (Episode + Shard + Analysis) ---
        try:
            episode_id = getattr(shard_meta, "episodeId", None)
            save_shard_with_analysis(
                shard_id=shard_meta.shardId or tmp_name,
                episode_id=episode_id,
                start_time=getattr(shard_meta, "startTime", None),
                end_time=getattr(shard_meta, "endTime", None),
                source=getattr(shard_meta, "source", None),
                meta_obj=shard_meta.model_dump(),
                features_obj=shard_features.model_dump(),
                analysis_obj=result.model_dump(),
            )
        except Exception:
            logger.exception("Failed to persist shard analysis in DB")

        return result
    except Exception as e:
        logger.exception("/analyze-shard failed")
        return JSONResponse(
            status_code=500,
            content={"error": "internal_error", "message": str(e)},
        )
    finally:
        # Best-effort cleanup
        try:
            tmp_path.unlink(missing_ok=True)
        except Exception:
            pass
