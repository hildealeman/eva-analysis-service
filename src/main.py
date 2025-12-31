from __future__ import annotations

from src.schemas.community import (
    CreateInvitationRequest,
    CreateInvitationResponse,
    InvitationOut,
    InvitationsSummaryOut,
    MeInvitationsResponse,
    MeProgressResponse,
    MeResponse,
    ProfileOut,
    ProgressSummaryOut,
)
import logging
import os
import shutil
import uuid
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional
from fastapi.middleware.cors import CORSMiddleware
from fastapi import BackgroundTasks, FastAPI, File, Form, Header, HTTPException, Query, UploadFile
from fastapi.responses import JSONResponse

from src.config import ensure_hf_cache_dirs, get_work_dir, load_config, model_root_available
from src.db import (
    compute_episode_insights,
    compute_progress_history,
    compute_progress_summary_for_date,
    compute_wav_features,
    create_invitation,
    create_shard_for_episode,
    curate_episode_detail,
    delete_published_shard_for_profile,
    episode_exists,
    get_episode_detail,
    get_episode_insights,
    get_feed_for_profile,
    get_or_create_profile,
    init_db,
    list_episodes_with_stats,
    list_invitations_for_profile,
    publish_shard,
    publish_shard_for_profile,
    run_full_analysis_for_shard,
    save_shard_with_analysis,
    soft_delete_shard,
    touch_profile_activity,
    update_episode,
    update_shard,
    get_shard,
)
from src.models.emotion_model import EmotionModel
from src.models.semantic_model import SemanticModel
from src.models.whisper_model import WhisperModel
from src.schemas.analysis import EmotionBlock, EmotionDistribution, SemanticBlock, SemanticFlags, ShardAnalysisResult, ShardFeatures, ShardMeta, SignalFeaturesBlock
from src.schemas.episode_insights import EpisodeInsightsResponse as EpisodeInsightsByEpisodeResponse
from src.schemas.episodes import EpisodeDetailResponse, EpisodeSummaryResponse, ShardWithAnalysisResponse
from src.schemas.feed import FeedResponse
from src.schemas.insights import EpisodeInsightsResponse
from src.schemas.updates import EpisodeCurateRequest, EpisodeUpdateRequest, ShardDeleteRequest, ShardPublishRequest, ShardUpdateRequest


def _map_valence_to_en(valence: Optional[str]) -> Optional[str]:
    if valence is None:
        return None
    v = str(valence).strip().lower()
    if v in {"positivo", "positive"}:
        return "positive"
    if v in {"neutral", "neutro"}:
        return "neutral"
    if v in {"negativo", "negative"}:
        return "negative"
    return None


def _map_activation_to_en(arousal: Optional[str]) -> Optional[str]:
    if arousal is None:
        return None
    a = str(arousal).strip().lower()
    if a in {"bajo", "low"}:
        return "low"
    if a in {"medio", "medium"}:
        return "medium"
    if a in {"alto", "high"}:
        return "high"
    return None


def _build_emotion_headline(primary: Optional[str], activation: Optional[str], peak: Optional[float]) -> Optional[str]:
    if not primary:
        return None

    act = (activation or "").lower()
    if act == "high":
        if primary.lower() in {"enojo", "ira"}:
            return "Alza de voz."
        if primary.lower() in {"miedo", "ansiedad"}:
            return "Tensión evidente."
        return "Emoción intensa."
    if act == "low":
        return "Tono contenido."
    if act == "medium":
        return "Emoción moderada."

    if peak is not None and peak >= 0.75:
        return "Alza de voz."
    return None


def _current_profile_id(x_profile_id: Optional[str]) -> str:
    candidate = (x_profile_id or "").strip()
    return candidate if candidate else "local_profile_1"


def _dt_to_iso(dt: Optional[datetime]) -> Optional[str]:
    if dt is None:
        return None
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _profile_to_out(p) -> ProfileOut:
    remaining = int(p.invitations_granted_total) - int(p.invitations_used)
    if remaining < 0:
        remaining = 0
    return ProfileOut(
        id=p.id,
        createdAt=_dt_to_iso(p.created_at) or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        updatedAt=_dt_to_iso(p.updated_at) or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        role=str(p.role),
        state=str(p.state),
        tevScore=float(p.tev_score),
        dailyStreak=int(p.daily_streak),
        lastActiveAt=_dt_to_iso(p.last_active_at) or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        invitationsGrantedTotal=int(p.invitations_granted_total),
        invitationsUsed=int(p.invitations_used),
        invitationsRemaining=int(remaining),
    )


def _invitation_to_out(i) -> InvitationOut:
    return InvitationOut(
        id=i.id,
        createdAt=_dt_to_iso(i.created_at) or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        updatedAt=_dt_to_iso(i.updated_at) or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        inviterId=i.inviter_id,
        inviteeId=i.invitee_id,
        email=i.email,
        code=i.code,
        state=str(i.state),
        expiresAt=_dt_to_iso(i.expires_at) or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        acceptedAt=_dt_to_iso(i.accepted_at),
        revokedAt=_dt_to_iso(i.revoked_at),
    )


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
        "service": "eva-analysis-service",
        "contractVersion": "0.5.0",
        "modelRootAvailable": available,
        "whisperLoaded": bool(app.state.whisper_loaded_runtime) if available else False,
        "emotionModelLoaded": bool(app.state.emotion_loaded_runtime) if available else False,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


@app.on_event("startup")
def on_startup() -> None:
    init_db()


@app.get("/me", response_model=MeResponse)
def get_me(x_profile_id: Optional[str] = Header(default=None, alias="X-Profile-Id")):
    profile_id = _current_profile_id(x_profile_id)
    prof = get_or_create_profile(profile_id)
    touch_profile_activity(profile_id)

    profile_out = _profile_to_out(prof)
    today_dict = compute_progress_summary_for_date(profile_id=profile_id, day=datetime.now(timezone.utc).date())
    today = ProgressSummaryOut.model_validate(today_dict)

    summary = InvitationsSummaryOut(
        grantedTotal=profile_out.invitationsGrantedTotal,
        used=profile_out.invitationsUsed,
        remaining=profile_out.invitationsRemaining,
    )

    return MeResponse(profile=profile_out, todayProgress=today, invitationsSummary=summary)


@app.get("/me/progress", response_model=MeProgressResponse)
def get_me_progress_v3(x_profile_id: Optional[str] = Header(default=None, alias="X-Profile-Id")):
    profile_id = _current_profile_id(x_profile_id)
    get_or_create_profile(profile_id)
    touch_profile_activity(profile_id)

    today_dict = compute_progress_summary_for_date(profile_id=profile_id, day=datetime.now(timezone.utc).date())
    history_dicts = compute_progress_history(profile_id=profile_id, days=30)
    return MeProgressResponse(
        today=ProgressSummaryOut.model_validate(today_dict),
        history=[ProgressSummaryOut.model_validate(d) for d in history_dicts],
    )


@app.get("/me/invitations", response_model=MeInvitationsResponse)
def get_me_invitations(x_profile_id: Optional[str] = Header(default=None, alias="X-Profile-Id")):
    profile_id = _current_profile_id(x_profile_id)
    get_or_create_profile(profile_id)
    touch_profile_activity(profile_id)

    invs = list_invitations_for_profile(profile_id)
    return MeInvitationsResponse(invitations=[_invitation_to_out(i) for i in invs])


@app.get("/me/feed", response_model=FeedResponse)
def get_me_feed(x_profile_id: Optional[str] = Header(default=None, alias="X-Profile-Id")):
    profile_id = _current_profile_id(x_profile_id)
    get_or_create_profile(profile_id)
    touch_profile_activity(profile_id)
    return get_feed_for_profile(profile_id)


@app.post("/invitations", response_model=CreateInvitationResponse)
def post_invitations(
    body: CreateInvitationRequest,
    x_profile_id: Optional[str] = Header(default=None, alias="X-Profile-Id"),
):
    profile_id = _current_profile_id(x_profile_id)
    get_or_create_profile(profile_id)
    touch_profile_activity(profile_id)

    email = (body.email or "").strip()
    if not email:
        raise HTTPException(status_code=400, detail="email is required")

    inv, status = create_invitation(inviter_profile_id=profile_id, email=email)
    if status == "no_invitations_remaining":
        raise HTTPException(status_code=400, detail="No invitations remaining")
    if inv is None:
        raise HTTPException(status_code=500, detail="Could not create invitation")

    return CreateInvitationResponse(invitation=_invitation_to_out(inv))


@app.get("/episodes", response_model=list[EpisodeSummaryResponse])
def list_episodes():
    return list_episodes_with_stats()


@app.get("/episodes/insights", response_model=EpisodeInsightsResponse)
def get_episodes_insights():
    return compute_episode_insights()


@app.get("/episodes/{episode_id}/insights", response_model=EpisodeInsightsByEpisodeResponse)
def get_episode_insights_endpoint(episode_id: str):
    insights = get_episode_insights(episode_id)
    if insights is None:
        raise HTTPException(status_code=404, detail="Episode not found")
    return insights


@app.get("/episodes/{episode_id}", response_model=EpisodeDetailResponse)
def read_episode(episode_id: str):
    episode = get_episode_detail(episode_id)
    if episode is None:
        raise HTTPException(status_code=404, detail="Episode not found")
    return episode


@app.post("/episodes/{episode_id}/curate", response_model=EpisodeDetailResponse)
def curate_episode_endpoint(episode_id: str, body: EpisodeCurateRequest):
    curated = curate_episode_detail(episode_id=episode_id, max_shards=body.max_shards)
    if curated is None:
        raise HTTPException(status_code=404, detail="Episode not found")
    return curated


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

    analysis = updated.analysis_json if isinstance(updated.analysis_json, dict) else {}
    publish_state = analysis.get("publishState") if isinstance(analysis.get("publishState"), str) else None
    deleted = bool(analysis.get("deleted")) if isinstance(analysis.get("deleted"), (bool, int)) else False
    deleted_reason = analysis.get("deletedReason") if isinstance(analysis.get("deletedReason"), str) else None
    deleted_at = None
    deleted_at_raw = analysis.get("deletedAt")
    if isinstance(deleted_at_raw, str) and deleted_at_raw.strip():
        try:
            deleted_at = datetime.fromisoformat(deleted_at_raw.replace("Z", "+00:00"))
        except Exception:
            deleted_at = None

    return ShardWithAnalysisResponse(
        id=updated.id,
        episodeId=updated.episode_id,
        startTime=updated.start_time,
        endTime=updated.end_time,
        source=updated.source,
        publishState=publish_state,
        deleted=deleted,
        deletedReason=deleted_reason,
        deletedAt=deleted_at,
        meta=updated.meta_json or {},
        features=updated.features_json or {},
        analysis=updated.analysis_json or {},
    )


@app.get("/api/shards/{shard_id}", response_model=ShardWithAnalysisResponse, include_in_schema=False)
@app.get("/shards/{shard_id}", response_model=ShardWithAnalysisResponse)
def read_shard(shard_id: str):
    shard = get_shard(shard_id)
    if shard is None:
        raise HTTPException(status_code=404, detail="Shard not found")

    analysis = shard.analysis_json if isinstance(shard.analysis_json, dict) else {}
    publish_state = analysis.get("publishState") if isinstance(analysis.get("publishState"), str) else None
    deleted = bool(analysis.get("deleted")) if isinstance(analysis.get("deleted"), (bool, int)) else False
    deleted_reason = analysis.get("deletedReason") if isinstance(analysis.get("deletedReason"), str) else None
    deleted_at = None
    deleted_at_raw = analysis.get("deletedAt")
    if isinstance(deleted_at_raw, str) and deleted_at_raw.strip():
        try:
            deleted_at = datetime.fromisoformat(deleted_at_raw.replace("Z", "+00:00"))
        except Exception:
            deleted_at = None

    return ShardWithAnalysisResponse(
        id=shard.id,
        episodeId=shard.episode_id,
        startTime=shard.start_time,
        endTime=shard.end_time,
        source=shard.source,
        publishState=publish_state,
        deleted=deleted,
        deletedReason=deleted_reason,
        deletedAt=deleted_at,
        meta=shard.meta_json or {},
        features=shard.features_json or {},
        analysis=analysis or {},
    )


@app.post("/episodes/{episode_id}/shards", response_model=ShardWithAnalysisResponse)
async def create_episode_shard(
    episode_id: str,
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    start_time: float = Form(0.0),
    end_time: float = Form(0.0),
):
    if not episode_exists(episode_id):
        raise HTTPException(status_code=404, detail="episode_not_found")

    if file.content_type and file.content_type not in {"audio/wav", "audio/x-wav", "audio/wave", "audio/vnd.wave"}:
        raise HTTPException(status_code=400, detail="invalid_audio_type")

    shard_id = uuid.uuid4().hex

    base_dir = Path("data") / "audio" / episode_id
    base_dir.mkdir(parents=True, exist_ok=True)
    wav_path = base_dir / f"{shard_id}.wav"

    # Persist audio to stable disk path
    try:
        with wav_path.open("wb") as out:
            shutil.copyfileobj(file.file, out)
    finally:
        try:
            file.file.close()
        except Exception:
            pass

    # Validate WAV header quickly (same heuristic as /analyze-shard)
    try:
        with wav_path.open("rb") as f:
            header = f.read(12)
        if len(header) < 12 or header[0:4] != b"RIFF" or header[8:12] != b"WAVE":
            raise HTTPException(status_code=400, detail="invalid_wav")
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=400, detail="invalid_wav")

    # Features from WAV
    features_obj = compute_wav_features(wav_path=wav_path)

    duration = features_obj.get("duration") if isinstance(features_obj, dict) else None
    if (not isinstance(end_time, (int, float)) or float(end_time) <= 0.0) and isinstance(duration, (int, float)):
        end_time = float(start_time) + float(duration)

    now = datetime.now(timezone.utc)
    now_iso = now.isoformat().replace("+00:00", "Z")

    meta_obj = {
        "createdAt": now_iso,
        "inputSource": "mic",
        "intensity": 1,
        "status": "raw",
        "publishState": None,
        "audioPath": str(wav_path),
        "analysisSource": "local",
        "analysisMode": "automatic",
        "analysisVersion": "0.1.0-local",
        "analysisAt": None,
        "transcript": None,
        "transcriptLanguage": None,
        "transcriptionConfidence": 0,
    }

    analysis_obj = {
        "emotion": {
            "primary": "neutro",
            "valence": "neutral",
            "activation": "medium",
            "distribution": {},
            "headline": None,
            "explanation": None,
        },
        "semantic": {
            "summary": "",
            "topics": [],
            "momentType": "otro",
            "flags": {"needsFollowup": False, "possibleCrisis": False},
        },
    }

    shard = create_shard_for_episode(
        shard_id=shard_id,
        episode_id=episode_id,
        start_time=float(start_time) if isinstance(start_time, (int, float)) else None,
        end_time=float(end_time) if isinstance(end_time, (int, float)) else None,
        source="local",
        meta_obj=meta_obj,
        features_obj=features_obj,
        analysis_obj=analysis_obj,
    )

    background_tasks.add_task(run_full_analysis_for_shard, shard.id)

    analysis = shard.analysis_json if isinstance(shard.analysis_json, dict) else {}
    publish_state = analysis.get("publishState") if isinstance(analysis.get("publishState"), str) else None

    return ShardWithAnalysisResponse(
        id=shard.id,
        episodeId=shard.episode_id,
        startTime=shard.start_time,
        endTime=shard.end_time,
        source=shard.source,
        publishState=publish_state,
        deleted=False,
        deletedReason=None,
        deletedAt=None,
        meta=shard.meta_json or {},
        features=shard.features_json or {},
        analysis=analysis or {},
    )


@app.post("/api/shards/{shard_id}/publish", response_model=ShardWithAnalysisResponse, include_in_schema=False)
@app.post("/shards/{shard_id}/publish", response_model=ShardWithAnalysisResponse)
def publish_shard_endpoint(
    shard_id: str,
    body: Optional[ShardPublishRequest] = None,
    force: Optional[bool] = Query(default=None),
    x_profile_id: Optional[str] = Header(default=None, alias="X-Profile-Id"),
):
    profile_id = _current_profile_id(x_profile_id)
    resolved_force = bool(body.force) if body is not None else False
    if force is not None:
        resolved_force = bool(force)
    shard = get_shard(shard_id)
    if shard is None:
        raise HTTPException(status_code=404, detail="Shard not found")

    analysis = shard.analysis_json if isinstance(shard.analysis_json, dict) else {}
    deleted = bool(analysis.get("deleted")) if isinstance(analysis.get("deleted"), (bool, int)) else False
    if deleted:
        raise HTTPException(status_code=400, detail="Cannot publish a deleted shard")

    try:
        publish_shard_for_profile(profile_id=profile_id, shard_id=shard_id, force=resolved_force)
    except ValueError as e:
        if str(e) == "not_ready_to_publish":
            raise HTTPException(status_code=400, detail="not_ready_to_publish")
        if str(e) == "shard_not_found":
            raise HTTPException(status_code=404, detail="Shard not found")
        if str(e) == "shard_deleted":
            raise HTTPException(status_code=400, detail="Cannot publish a deleted shard")
        raise

    updated = publish_shard(shard_id=shard_id, force=resolved_force)
    if updated is None:
        raise HTTPException(status_code=404, detail="Shard not found")

    updated_analysis = updated.analysis_json if isinstance(updated.analysis_json, dict) else {}
    publish_state = updated_analysis.get("publishState") if isinstance(updated_analysis.get("publishState"), str) else None
    deleted_reason = updated_analysis.get("deletedReason") if isinstance(updated_analysis.get("deletedReason"), str) else None
    deleted_at = None
    deleted_at_raw = updated_analysis.get("deletedAt")
    if isinstance(deleted_at_raw, str) and deleted_at_raw.strip():
        try:
            deleted_at = datetime.fromisoformat(deleted_at_raw.replace("Z", "+00:00"))
        except Exception:
            deleted_at = None

    return ShardWithAnalysisResponse(
        id=updated.id,
        episodeId=updated.episode_id,
        startTime=updated.start_time,
        endTime=updated.end_time,
        source=updated.source,
        publishState=publish_state,
        deleted=False,
        deletedReason=deleted_reason,
        deletedAt=deleted_at,
        meta=updated.meta_json or {},
        features=updated.features_json or {},
        analysis=updated_analysis or {},
    )


@app.post("/api/shards/{shard_id}/delete", response_model=ShardWithAnalysisResponse, include_in_schema=False)
@app.post("/shards/{shard_id}/delete", response_model=ShardWithAnalysisResponse)
def delete_shard_endpoint(
    shard_id: str,
    body: Optional[ShardDeleteRequest] = None,
    x_profile_id: Optional[str] = Header(default=None, alias="X-Profile-Id"),
):
    profile_id = _current_profile_id(x_profile_id)
    try:
        delete_published_shard_for_profile(profile_id=profile_id, shard_id=shard_id)
    except ValueError:
        pass

    reason = (body.reason if body is not None else "user_deleted").strip()
    if not reason:
        reason = "user_deleted"

    updated = soft_delete_shard(shard_id=shard_id, reason=reason)
    if updated is None:
        raise HTTPException(status_code=404, detail="Shard not found")

    updated_analysis = updated.analysis_json if isinstance(updated.analysis_json, dict) else {}
    publish_state = updated_analysis.get("publishState") if isinstance(updated_analysis.get("publishState"), str) else None
    deleted_reason = updated_analysis.get("deletedReason") if isinstance(updated_analysis.get("deletedReason"), str) else None
    deleted_at = None
    deleted_at_raw = updated_analysis.get("deletedAt")
    if isinstance(deleted_at_raw, str) and deleted_at_raw.strip():
        try:
            deleted_at = datetime.fromisoformat(deleted_at_raw.replace("Z", "+00:00"))
        except Exception:
            deleted_at = None

    return ShardWithAnalysisResponse(
        id=updated.id,
        episodeId=updated.episode_id,
        startTime=updated.start_time,
        endTime=updated.end_time,
        source=updated.source,
        publishState=publish_state,
        deleted=True,
        deletedReason=deleted_reason,
        deletedAt=deleted_at,
        meta=updated.meta_json or {},
        features=updated.features_json or {},
        analysis=updated_analysis or {},
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

        emotion_legacy = EmotionBlock(
            primary=primary_emotion,
            valence=valence,
            activation=arousal,
            scores=emotion_labels,
        )

        distribution: dict[str, float] = {}
        if isinstance(emotion_labels, list):
            total = 0.0
            for item in emotion_labels:
                try:
                    label = getattr(item, "label", None)
                    score = getattr(item, "score", None)
                    if not isinstance(label, str):
                        continue
                    if not isinstance(score, (int, float)):
                        continue
                    score_f = float(score)
                    if score_f < 0:
                        continue
                    distribution[label] = score_f
                    total += score_f
                except Exception:
                    continue
            if total > 0:
                distribution = {k: v / total for k, v in distribution.items()}

        emotion_v2 = EmotionDistribution(
            primary=str(primary_emotion) if primary_emotion is not None else None,
            valence=_map_valence_to_en(str(valence) if valence is not None else None),
            activation=_map_activation_to_en(str(arousal) if arousal is not None else None),
            distribution=distribution,
            headline=_build_emotion_headline(
                str(primary_emotion) if primary_emotion is not None else None,
                _map_activation_to_en(str(arousal) if arousal is not None else None),
                shard_features.intensity,
            ),
            explanation=None,
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
            emotion=emotion_v2,
            emotionLegacy=emotion_legacy,
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
