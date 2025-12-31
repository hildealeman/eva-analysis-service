from __future__ import annotations

import os
import secrets
import string
import uuid
import logging
import wave
from pathlib import Path
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import Column
from sqlalchemy.types import JSON
from sqlmodel import Field, SQLModel, Session, create_engine, select

from src.schemas.episode_insights import (
    EpisodeEmotionSummary,
    EpisodeInsightStats,
    EpisodeInsightsResponse as EpisodeInsightsByEpisodeResponse,
    EpisodeKeyMoment,
    EpisodeKeyMomentEmotion,
)
from src.schemas.episodes import EpisodeDetailResponse, EpisodeSummaryResponse, ShardWithAnalysisResponse
from src.schemas.feed import FeedItem, FeedItemEmotion, FeedResponse
from src.schemas.insights import EmotionStat, EpisodeInsightsResponse, StatusStat, TagStat

EVA_DB_URL = os.getenv("EVA_DB_URL", "sqlite:///./eva.db")

engine = create_engine(
    EVA_DB_URL,
    echo=False,
    connect_args={"check_same_thread": False} if EVA_DB_URL.startswith("sqlite") else {},
)


logger = logging.getLogger("eva-analysis-service")


def _json_safe(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_json_safe(v) for v in value]
    if isinstance(value, tuple):
        return tuple(_json_safe(v) for v in value)
    return value


def episode_exists(episode_id: str) -> bool:
    with Session(engine) as session:
        return session.get(Episode, episode_id) is not None


def compute_wav_features(*, wav_path: Path) -> dict[str, Any]:
    """Best-effort WAV feature extraction without extra deps.

    Returns a dict compatible with existing `features_json` usage.
    """

    try:
        with wave.open(str(wav_path), "rb") as wf:
            n_channels = wf.getnchannels()
            sampwidth = wf.getsampwidth()
            framerate = wf.getframerate() or 0
            n_frames = wf.getnframes() or 0

            raw = wf.readframes(n_frames)
            duration = float(n_frames) / float(framerate) if framerate > 0 else None

            rms = None
            peak = None
            zcr = None

            # Best support: 16-bit PCM. For anything else, keep None.
            if raw and sampwidth == 2:
                try:
                    import struct

                    frame_count = len(raw) // (2 * max(1, n_channels))
                    if frame_count > 0:
                        # Unpack interleaved int16 samples
                        total_samples = frame_count * max(1, n_channels)
                        samples = struct.unpack("<" + "h" * total_samples, raw[: total_samples * 2])

                        # Downmix to mono by taking channel 0
                        mono = samples[0:: max(1, n_channels)]
                        if len(mono) > 0:
                            abs_vals = [abs(int(s)) for s in mono]
                            peak = float(max(abs_vals)) if abs_vals else None
                            # RMS
                            sq_sum = 0
                            for s in mono:
                                v = int(s)
                                sq_sum += v * v
                            rms = float((sq_sum / len(mono)) ** 0.5) if sq_sum >= 0 else None

                            # Zero crossings
                            if len(mono) > 1:
                                crossings = 0
                                prev = mono[0]
                                for s in mono[1:]:
                                    if (prev >= 0 and s < 0) or (prev < 0 and s >= 0):
                                        crossings += 1
                                    prev = s
                                zcr = float(crossings)
                except Exception:
                    rms = None
                    peak = None
                    zcr = None

            return {
                "rms": rms,
                "peak": peak,
                "zcr": zcr,
                "spectralCentroid": None,
                "tempo": None,
                "duration": duration,
                "pitch": None,
            }
    except Exception:
        logger.exception("compute_wav_features failed")
        return {
            "rms": None,
            "peak": None,
            "zcr": None,
            "spectralCentroid": None,
            "tempo": None,
            "duration": None,
            "pitch": None,
        }


def create_shard_for_episode(
    *,
    shard_id: str,
    episode_id: str,
    start_time: Optional[float],
    end_time: Optional[float],
    source: str,
    meta_obj: dict[str, Any],
    features_obj: dict[str, Any],
    analysis_obj: dict[str, Any],
) -> Shard:
    with Session(engine) as session:
        meta_json = _json_safe(dict(meta_obj))
        features_json = _json_safe(dict(features_obj))
        analysis_json = _json_safe(dict(analysis_obj))
        shard = Shard(
            id=shard_id,
            episode_id=episode_id,
            start_time=start_time,
            end_time=end_time,
            source=source,
            meta_json=meta_json,
            features_json=features_json,
            analysis_json=analysis_json,
        )
        session.add(shard)
        session.commit()
        session.refresh(shard)
        return shard


def run_full_analysis_for_shard(shard_id: str) -> None:
    """Background task: fill transcript + semantic analysis using OpenAI when available."""

    with Session(engine) as session:
        shard = session.get(Shard, shard_id)
        if shard is None:
            return

        analysis = dict(shard.analysis_json) if isinstance(shard.analysis_json, dict) else {}
        meta = dict(shard.meta_json) if isinstance(shard.meta_json, dict) else {}

        logger.info("run_full_analysis_for_shard: start shard_id=%s", shard_id)

        audio_path_raw = meta.get("audioPath")
        audio_path: Optional[Path] = None
        if isinstance(audio_path_raw, str) and audio_path_raw.strip():
            audio_path = Path(audio_path_raw)

        if audio_path is None or not audio_path.exists() or not audio_path.is_file():
            logger.warning("run_full_analysis_for_shard: missing audioPath shard_id=%s audioPath=%r", shard_id, audio_path_raw)
            return

        transcript_text = ""
        transcript_language = None
        transcript_confidence = 0.0

        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            logger.info("run_full_analysis_for_shard: OPENAI_API_KEY not set; skipping transcript+semantic shard_id=%s", shard_id)
            return

        if api_key:
            try:
                from openai import OpenAI  # type: ignore

                client = OpenAI(api_key=api_key)
                with audio_path.open("rb") as f:
                    tr = client.audio.transcriptions.create(
                        model=os.getenv("EVA_OPENAI_TRANSCRIBE_MODEL", "gpt-4o-mini-transcribe"),
                        file=f,
                    )

                # SDK returns different shapes depending on version; be defensive.
                transcript_text = getattr(tr, "text", None) or getattr(tr, "transcript", None) or ""
                transcript_language = getattr(tr, "language", None)
                transcript_confidence = float(getattr(tr, "confidence", 0.0) or 0.0)
            except Exception:
                logger.exception("OpenAI transcription failed")

        # Semantic analysis (uses existing SemanticModel with safe fallback)
        try:
            from src.models.semantic_model import SemanticModel
            from src.schemas.analysis import SignalFeaturesBlock

            features = shard.features_json if isinstance(shard.features_json, dict) else {}
            signal = SignalFeaturesBlock(
                rms=features.get("rms") if isinstance(features.get("rms"), (int, float)) else None,
                peak=features.get("peak") if isinstance(features.get("peak"), (int, float)) else None,
                zcr=features.get("zcr") if isinstance(features.get("zcr"), (int, float)) else None,
                centerFrequency=features.get("spectralCentroid") if isinstance(features.get("spectralCentroid"), (int, float)) else None,
            )

            semantic_model = SemanticModel(api_key=api_key)
            semantic = semantic_model.analyze(
                transcript=transcript_text or "",
                language=transcript_language,
                features=signal,
            )
            semantic_dict = semantic.model_dump() if hasattr(semantic, "model_dump") else {}
        except Exception:
            logger.exception("Semantic analysis failed")
            semantic_dict = {
                "summary": "",
                "topics": [],
                "momentType": "otro",
                "flags": {"needsFollowup": False, "possibleCrisis": False},
            }

        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

        # Update meta + analysis (no in-place mutation without reassign)
        meta["transcript"] = transcript_text
        if transcript_language:
            meta["transcriptLanguage"] = transcript_language
        meta["transcriptionConfidence"] = transcript_confidence
        meta["analysisSource"] = "openai" if api_key else (meta.get("analysisSource") or "local")
        meta["analysisMode"] = "automatic"
        meta["analysisAt"] = now

        analysis["semantic"] = semantic_dict

        shard.meta_json = _json_safe(meta)
        shard.analysis_json = _json_safe(analysis)
        session.add(shard)
        session.commit()

        logger.info(
            "run_full_analysis_for_shard: done shard_id=%s transcript_len=%s", 
            shard_id, 
            len(transcript_text) if isinstance(transcript_text, str) else None,
        )


class Episode(SQLModel, table=True):
    id: str = Field(primary_key=True, index=True)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    title: Optional[str] = None
    note: Optional[str] = None


class PublishedShard(SQLModel, table=True):
    id: str = Field(primary_key=True, index=True, default_factory=lambda: uuid.uuid4().hex)
    profile_id: str = Field(index=True)
    shard_id: str = Field(index=True)
    episode_id: str = Field(index=True)
    published_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    deleted_at: Optional[datetime] = None


class Profile(SQLModel, table=True):
    id: str = Field(primary_key=True, index=True)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)

    role: str = "ghost"  # "ghost" | "active"
    state: str = "ok"  # "ok" | "suspended" | "banned"

    tev_score: float = 12.5
    daily_streak: int = 0
    last_active_at: Optional[datetime] = None

    invitations_granted_total: int = 3
    invitations_used: int = 0


class Invitation(SQLModel, table=True):
    id: str = Field(primary_key=True, index=True)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)

    inviter_id: str = Field(index=True, foreign_key="profile.id")
    invitee_id: Optional[str] = Field(default=None, index=True)

    email: str = Field(index=True)
    code: str = Field(index=True)

    state: str = "pending"  # "pending" | "accepted" | "revoked" | "expired"
    expires_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc) + timedelta(days=30))

    accepted_at: Optional[datetime] = None
    revoked_at: Optional[datetime] = None


class VoteEvent(SQLModel, table=True):
    id: str = Field(primary_key=True, index=True)
    created_at: datetime = Field(default_factory=datetime.utcnow)

    profile_id: str = Field(index=True, foreign_key="profile.id")
    shard_id: Optional[str] = Field(default=None, index=True)
    direction: str  # "up" | "down"


class Shard(SQLModel, table=True):
    id: str = Field(primary_key=True, index=True)
    episode_id: Optional[str] = Field(default=None, index=True, foreign_key="episode.id")
    start_time: Optional[float] = None
    end_time: Optional[float] = None
    source: Optional[str] = None

    meta_json: dict = Field(default_factory=dict, sa_column=Column(JSON))
    features_json: dict = Field(default_factory=dict, sa_column=Column(JSON))
    analysis_json: dict = Field(default_factory=dict, sa_column=Column(JSON))

    created_at: datetime = Field(default_factory=datetime.utcnow)


def init_db() -> None:
    SQLModel.metadata.create_all(engine)


def save_shard_with_analysis(
    *,
    shard_id: str,
    episode_id: Optional[str],
    start_time: Optional[float],
    end_time: Optional[float],
    source: Optional[str],
    meta_obj: dict,
    features_obj: dict,
    analysis_obj: Any,
) -> None:
    with Session(engine) as session:
        if episode_id:
            ep = session.get(Episode, episode_id)
            if ep is None:
                ep = Episode(id=episode_id)
                session.add(ep)
                session.commit()

        analysis_dict: dict
        if hasattr(analysis_obj, "model_dump"):
            analysis_dict = analysis_obj.model_dump()
        elif isinstance(analysis_obj, dict):
            analysis_dict = analysis_obj
        else:
            analysis_dict = {"value": analysis_obj}

        meta_json = _json_safe(dict(meta_obj) if isinstance(meta_obj, dict) else {})
        features_json = _json_safe(dict(features_obj) if isinstance(features_obj, dict) else {})
        analysis_json = _json_safe(dict(analysis_dict) if isinstance(analysis_dict, dict) else {})

        existing = session.exec(select(Shard).where(Shard.id == shard_id)).first()
        if existing is None:
            shard = Shard(
                id=shard_id,
                episode_id=episode_id,
                start_time=start_time,
                end_time=end_time,
                source=source,
                meta_json=meta_json,
                features_json=features_json,
                analysis_json=analysis_json,
            )
            session.add(shard)
        else:
            prev_analysis = existing.analysis_json if isinstance(existing.analysis_json, dict) else {}
            if isinstance(prev_analysis.get("user"), dict) and "user" not in analysis_json:
                analysis_json["user"] = prev_analysis.get("user")
            for k in ("publishState", "deleted", "deletedReason", "deletedAt"):
                if k in prev_analysis and k not in analysis_json:
                    analysis_json[k] = prev_analysis.get(k)

            existing.episode_id = episode_id
            existing.start_time = start_time
            existing.end_time = end_time
            existing.source = source
            existing.meta_json = _json_safe(meta_json)
            existing.features_json = _json_safe(features_json)
            existing.analysis_json = _json_safe(analysis_json)
            session.add(existing)

        session.commit()


def get_or_create_profile(profile_id: str) -> Profile:
    with Session(engine) as session:
        existing = session.get(Profile, profile_id)
        if existing is not None:
            return existing

        now = datetime.now(timezone.utc)
        prof = Profile(
            id=profile_id,
            created_at=now,
            updated_at=now,
            role="ghost",
            state="ok",
            tev_score=12.5,
            daily_streak=0,
            last_active_at=now,
            invitations_granted_total=3,
            invitations_used=0,
        )
        session.add(prof)
        session.commit()
        session.refresh(prof)
        return prof


def touch_profile_activity(profile_id: str) -> None:
    with Session(engine) as session:
        prof = session.get(Profile, profile_id)
        if prof is None:
            return
        prof.updated_at = datetime.now(timezone.utc)
        prof.last_active_at = prof.updated_at
        session.add(prof)
        session.commit()


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _code(prefix: str = "HGI") -> str:
    alphabet = string.ascii_uppercase + string.digits
    a = "".join(secrets.choice(alphabet) for _ in range(4))
    b = "".join(secrets.choice(alphabet) for _ in range(4))
    return f"{prefix}-{a}-{b}"


def list_invitations_for_profile(profile_id: str) -> list[Invitation]:
    with Session(engine) as session:
        return session.exec(
            select(Invitation)
            .where(Invitation.inviter_id == profile_id)
            .order_by(Invitation.created_at.desc())
        ).all()


def create_invitation(*, inviter_profile_id: str, email: str) -> tuple[Optional[Invitation], str]:
    with Session(engine) as session:
        prof = session.get(Profile, inviter_profile_id)
        if prof is None:
            return None, "profile_not_found"

        remaining = int(prof.invitations_granted_total) - int(prof.invitations_used)
        if remaining <= 0:
            return None, "no_invitations_remaining"

        now = datetime.now(timezone.utc)
        inv = Invitation(
            id=f"inv_{secrets.token_hex(8)}",
            created_at=now,
            updated_at=now,
            inviter_id=inviter_profile_id,
            invitee_id=None,
            email=email,
            code=_code(),
            state="pending",
            expires_at=now + timedelta(days=30),
            accepted_at=None,
            revoked_at=None,
        )
        session.add(inv)

        prof.invitations_used = int(prof.invitations_used) + 1
        prof.updated_at = now
        prof.last_active_at = now
        session.add(prof)

        session.commit()
        session.refresh(inv)
        return inv, "ok"


def _date_str(d: date) -> str:
    return d.isoformat()


def compute_progress_summary_for_date(*, profile_id: str, day: date) -> dict:
    start_dt = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
    end_dt = start_dt + timedelta(days=1)

    with Session(engine) as session:
        shards = session.exec(select(Shard).where(Shard.created_at >= start_dt, Shard.created_at < end_dt)).all()
        votes = session.exec(
            select(VoteEvent).where(
                VoteEvent.profile_id == profile_id,
                VoteEvent.created_at >= start_dt,
                VoteEvent.created_at < end_dt,
            )
        ).all()
        prof = session.get(Profile, profile_id)

    up = 0
    down = 0
    for v in votes:
        if getattr(v, "direction", None) == "up":
            up += 1
        elif getattr(v, "direction", None) == "down":
            down += 1

    reviewed = 0
    published = 0
    for s in shards:
        analysis = s.analysis_json if isinstance(s.analysis_json, dict) else {}
        user_block = analysis.get("user") if isinstance(analysis.get("user"), dict) else {}
        status = user_block.get("status")
        if isinstance(status, str) and status.strip().lower() == "reviewed":
            reviewed += 1
        publish_state = analysis.get("publishState")
        if isinstance(publish_state, str) and publish_state == "published":
            published += 1

    activity_minutes = min(180, max(0, reviewed * 3 + published * 2 + (up + down)))

    tev_end = float(prof.tev_score) if prof is not None else 12.5
    tev_start = tev_end
    tev_delta = 0.0

    return {
        "date": _date_str(day),
        "tevScoreStart": tev_start,
        "tevScoreEnd": tev_end,
        "tevDelta": tev_delta,
        "votesGiven": {"up": up, "down": down},
        "activityMinutes": int(activity_minutes),
        "shardsReviewed": int(reviewed),
        "shardsPublished": int(published),
        "levelLabel": "ghost" if (prof is None or prof.role == "ghost") else "active",
        "progressPercentToNextLevel": 42,
    }


def compute_progress_history(*, profile_id: str, days: int = 30) -> list[dict]:
    today = datetime.now(timezone.utc).date()
    out: list[dict] = []
    for i in range(days):
        d = today - timedelta(days=i)
        out.append(compute_progress_summary_for_date(profile_id=profile_id, day=d))
    return out


def _extract_emotion_fields_from_analysis(analysis_json: dict) -> tuple[Optional[str], Optional[str], Optional[str]]:
    primary_emotion = analysis_json.get("primaryEmotion")
    valence = analysis_json.get("valence")
    arousal = analysis_json.get("arousal")

    if primary_emotion is None:
        emotion_block = analysis_json.get("emotion")
        if isinstance(emotion_block, dict):
            primary_emotion = emotion_block.get("primary")
            valence = valence or emotion_block.get("valence")
            arousal = arousal or emotion_block.get("activation")

    return (
        str(primary_emotion) if primary_emotion is not None else None,
        str(valence) if valence is not None else None,
        str(arousal) if arousal is not None else None,
    )


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


def _extract_emotion_compact(analysis_json: dict) -> tuple[Optional[str], Optional[str], Optional[str], Optional[str]]:
    emotion_block = analysis_json.get("emotion")
    if isinstance(emotion_block, dict):
        primary = emotion_block.get("primary")
        valence = emotion_block.get("valence")
        activation = emotion_block.get("activation")
        headline = emotion_block.get("headline")
        return (
            str(primary) if isinstance(primary, str) and primary.strip() else None,
            str(valence) if isinstance(valence, str) and valence.strip() else None,
            str(activation) if isinstance(activation, str) and activation.strip() else None,
            str(headline) if isinstance(headline, str) and headline.strip() else None,
        )

    primary_legacy, valence_legacy, arousal_legacy = _extract_emotion_fields_from_analysis(analysis_json)
    return (
        primary_legacy,
        _map_valence_to_en(valence_legacy),
        _map_activation_to_en(arousal_legacy),
        None,
    )


def list_episodes_with_stats() -> list[EpisodeSummaryResponse]:
    with Session(engine) as session:
        episodes = session.exec(select(Episode).order_by(Episode.created_at.desc())).all()

        out: list[EpisodeSummaryResponse] = []
        for ep in episodes:
            shards = session.exec(select(Shard).where(Shard.episode_id == ep.id)).all()

            shard_count = len(shards)

            start_times = [s.start_time for s in shards if s.start_time is not None]
            end_times = [s.end_time for s in shards if s.end_time is not None]

            duration_seconds: Optional[float] = None
            if start_times and end_times:
                duration_seconds = max(end_times) - min(start_times)
                if duration_seconds < 0:
                    duration_seconds = None

            primary_emotion: Optional[str] = None
            valence: Optional[str] = None
            arousal: Optional[str] = None

            if shards:
                latest = max(shards, key=lambda s: s.created_at)
                if isinstance(latest.analysis_json, dict):
                    primary_emotion, valence, arousal = _extract_emotion_fields_from_analysis(latest.analysis_json)

            out.append(
                EpisodeSummaryResponse(
                    id=ep.id,
                    createdAt=ep.created_at,
                    title=ep.title,
                    note=ep.note,
                    shardCount=shard_count,
                    durationSeconds=duration_seconds,
                    primaryEmotion=primary_emotion,
                    valence=valence,
                    arousal=arousal,
                )
            )

        return out


def get_episode_detail(episode_id: str) -> Optional[EpisodeDetailResponse]:
    with Session(engine) as session:
        ep = session.get(Episode, episode_id)
        if ep is None:
            return None

        shards = session.exec(
            select(Shard)
            .where(Shard.episode_id == episode_id)
            .order_by(Shard.start_time.asc(), Shard.created_at.asc())
        ).all()

        start_times = [s.start_time for s in shards if s.start_time is not None]
        end_times = [s.end_time for s in shards if s.end_time is not None]
        duration_seconds: Optional[float] = None
        if start_times and end_times:
            duration_seconds = max(end_times) - min(start_times)
            if duration_seconds < 0:
                duration_seconds = None

        primary_emotion: Optional[str] = None
        valence: Optional[str] = None
        arousal: Optional[str] = None
        if shards:
            latest = max(shards, key=lambda s: s.created_at)
            if isinstance(latest.analysis_json, dict):
                primary_emotion, valence, arousal = _extract_emotion_fields_from_analysis(latest.analysis_json)

        summary = EpisodeSummaryResponse(
            id=ep.id,
            createdAt=ep.created_at,
            title=ep.title,
            note=ep.note,
            shardCount=len(shards),
            durationSeconds=duration_seconds,
            primaryEmotion=primary_emotion,
            valence=valence,
            arousal=arousal,
        )

        shard_items: list[ShardWithAnalysisResponse] = []
        for s in shards:
            analysis = s.analysis_json if isinstance(s.analysis_json, dict) else {}
            publish_state = analysis.get("publishState") if isinstance(analysis.get("publishState"), str) else None
            deleted = bool(analysis.get("deleted")) if isinstance(analysis.get("deleted"), (bool, int)) else False
            deleted_reason = analysis.get("deletedReason") if isinstance(analysis.get("deletedReason"), str) else None
            deleted_at: Optional[datetime] = None
            deleted_at_raw = analysis.get("deletedAt")
            if isinstance(deleted_at_raw, str) and deleted_at_raw.strip():
                try:
                    deleted_at = datetime.fromisoformat(deleted_at_raw.replace("Z", "+00:00"))
                except Exception:
                    deleted_at = None

            shard_items.append(
                ShardWithAnalysisResponse(
                    id=s.id,
                    episodeId=s.episode_id,
                    startTime=s.start_time,
                    endTime=s.end_time,
                    source=s.source,
                    publishState=publish_state,
                    deleted=deleted,
                    deletedReason=deleted_reason,
                    deletedAt=deleted_at,
                    meta=s.meta_json or {},
                    features=s.features_json or {},
                    analysis=analysis or {},
                )
            )

        return EpisodeDetailResponse(summary=summary, shards=shard_items)


def curate_episode_detail(*, episode_id: str, max_shards: int = 5) -> Optional[EpisodeDetailResponse]:
    with Session(engine) as session:
        ep = session.get(Episode, episode_id)
        if ep is None:
            return None

        shards = session.exec(
            select(Shard)
            .where(Shard.episode_id == episode_id)
            .order_by(Shard.start_time.asc(), Shard.created_at.asc())
        ).all()

        logger.info("curate_episode_detail: start episode_id=%s shard_count=%s max_shards=%s", episode_id, len(shards), max_shards)

        kept: list[tuple[float, Shard]] = []
        filtered_deleted = 0
        filtered_silence = 0
        filtered_duration = 0

        for s in shards:
            analysis = s.analysis_json if isinstance(s.analysis_json, dict) else {}
            deleted = bool(analysis.get("deleted")) if isinstance(analysis.get("deleted"), (bool, int)) else False
            if deleted:
                filtered_deleted += 1
                continue

            features = s.features_json if isinstance(s.features_json, dict) else {}
            rms = features.get("rms") if isinstance(features.get("rms"), (int, float)) else None
            peak = features.get("peak") if isinstance(features.get("peak"), (int, float)) else None
            intensity = features.get("intensity") if isinstance(features.get("intensity"), (int, float)) else None
            duration = features.get("duration") if isinstance(features.get("duration"), (int, float)) else None

            if duration is not None and duration < 0.5:
                filtered_duration += 1
                continue

            # Silence heuristic: very low RMS and low peak -> treat as silence.
            if (rms is not None and rms < 300) or (peak is not None and peak < 600):
                filtered_silence += 1
                continue

            semantic_summary = ""
            semantic = analysis.get("semantic")
            if isinstance(semantic, dict):
                ss = semantic.get("summary")
                if isinstance(ss, str):
                    semantic_summary = ss.strip()

            primary_emotion = None
            emotion = analysis.get("emotion")
            if isinstance(emotion, dict):
                pe = emotion.get("primary")
                if isinstance(pe, str):
                    primary_emotion = pe.strip().lower()

            score = 0.0
            if isinstance(intensity, (int, float)):
                score += float(intensity)
            elif isinstance(rms, (int, float)):
                score += float(rms) / 1000.0

            if semantic_summary:
                score += 50.0

            if primary_emotion and primary_emotion not in {"neutro", "neutral"}:
                score += 25.0

            if duration is not None:
                if duration > 60:
                    score -= 10.0
                elif duration < 1.0:
                    score -= 5.0

            kept.append((score, s))

        kept.sort(key=lambda item: item[0], reverse=True)
        selected = [s for _score, s in kept[: max(0, int(max_shards or 0))]]
        selected.sort(key=lambda s: (s.start_time is None, s.start_time or 0.0, s.created_at))

        start_times = [s.start_time for s in shards if s.start_time is not None]
        end_times = [s.end_time for s in shards if s.end_time is not None]
        duration_seconds: Optional[float] = None
        if start_times and end_times:
            duration_seconds = max(end_times) - min(start_times)
            if duration_seconds < 0:
                duration_seconds = None

        primary_emotion: Optional[str] = None
        valence: Optional[str] = None
        arousal: Optional[str] = None
        if shards:
            latest = max(shards, key=lambda s: s.created_at)
            if isinstance(latest.analysis_json, dict):
                primary_emotion, valence, arousal = _extract_emotion_fields_from_analysis(latest.analysis_json)

        summary = EpisodeSummaryResponse(
            id=ep.id,
            createdAt=ep.created_at,
            title=ep.title,
            note=ep.note,
            shardCount=len(shards),
            durationSeconds=duration_seconds,
            primaryEmotion=primary_emotion,
            valence=valence,
            arousal=arousal,
        )

        shard_items: list[ShardWithAnalysisResponse] = []
        for s in selected:
            analysis = s.analysis_json if isinstance(s.analysis_json, dict) else {}
            publish_state = analysis.get("publishState") if isinstance(analysis.get("publishState"), str) else None
            deleted = bool(analysis.get("deleted")) if isinstance(analysis.get("deleted"), (bool, int)) else False
            deleted_reason = analysis.get("deletedReason") if isinstance(analysis.get("deletedReason"), str) else None
            deleted_at: Optional[datetime] = None
            deleted_at_raw = analysis.get("deletedAt")
            if isinstance(deleted_at_raw, str) and deleted_at_raw.strip():
                try:
                    deleted_at = datetime.fromisoformat(deleted_at_raw.replace("Z", "+00:00"))
                except Exception:
                    deleted_at = None

            shard_items.append(
                ShardWithAnalysisResponse(
                    id=s.id,
                    episodeId=s.episode_id,
                    startTime=s.start_time,
                    endTime=s.end_time,
                    source=s.source,
                    publishState=publish_state,
                    deleted=deleted,
                    deletedReason=deleted_reason,
                    deletedAt=deleted_at,
                    meta=s.meta_json or {},
                    features=s.features_json or {},
                    analysis=analysis or {},
                )
            )

        logger.info(
            "curate_episode_detail: done episode_id=%s selected=%s filtered_deleted=%s filtered_silence=%s filtered_duration=%s",
            episode_id,
            len(shard_items),
            filtered_deleted,
            filtered_silence,
            filtered_duration,
        )

        return EpisodeDetailResponse(summary=summary, shards=shard_items)


def get_episode_insights(episode_id: str) -> Optional[EpisodeInsightsByEpisodeResponse]:
    with Session(engine) as session:
        ep = session.get(Episode, episode_id)
        if ep is None:
            return None

        shards = session.exec(select(Shard).where(Shard.episode_id == episode_id)).all()

        total_shards = len(shards)
        start_times = [s.start_time for s in shards if s.start_time is not None]
        end_times = [s.end_time for s in shards if s.end_time is not None]

        duration_seconds: Optional[float] = None
        if start_times and end_times:
            duration_seconds = float(max(end_times)) - float(min(start_times))
            if duration_seconds < 0:
                duration_seconds = None

        first_shard_at = min(start_times) if start_times else None
        last_shard_at = max(end_times) if end_times else None

        primary_counts: dict[str, int] = {}
        valence_counts: dict[str, int] = {}
        activation_counts: dict[str, int] = {}

        shards_with_emotion = 0
        enriched: list[dict[str, Any]] = []

        for s in shards:
            analysis = s.analysis_json if isinstance(s.analysis_json, dict) else {}
            primary, valence, activation, headline = _extract_emotion_compact(analysis)

            if primary:
                shards_with_emotion += 1
                primary_counts[primary] = primary_counts.get(primary, 0) + 1

            if valence in {"positive", "neutral", "negative"}:
                valence_counts[valence] = valence_counts.get(valence, 0) + 1

            if activation in {"low", "medium", "high"}:
                activation_counts[activation] = activation_counts.get(activation, 0) + 1

            intensity_score: float = 0.0
            try:
                maybe = None
                if isinstance(s.features_json, dict):
                    maybe = s.features_json.get("intensity")
                if not isinstance(maybe, (int, float)):
                    signal = analysis.get("signalFeatures")
                    if isinstance(signal, dict):
                        maybe = signal.get("peak")
                if isinstance(maybe, (int, float)):
                    intensity_score = float(maybe)
            except Exception:
                intensity_score = 0.0

            user_block = analysis.get("user") if isinstance(analysis.get("user"), dict) else {}
            transcript_override = user_block.get("transcriptOverride")
            transcript = transcript_override if isinstance(transcript_override, str) and transcript_override.strip() else None
            if transcript is None:
                tr = analysis.get("transcript")
                transcript = tr if isinstance(tr, str) and tr.strip() else None

            enriched.append(
                {
                    "shard": s,
                    "primary": primary,
                    "valence": valence,
                    "activation": activation,
                    "headline": headline,
                    "intensity": intensity_score,
                    "transcript": transcript,
                }
            )

        stats = EpisodeInsightStats(
            totalShards=total_shards,
            durationSeconds=duration_seconds,
            shardsWithEmotion=shards_with_emotion,
            firstShardAt=first_shard_at,
            lastShardAt=last_shard_at,
        )

        emotion_summary = EpisodeEmotionSummary(
            primaryCounts=primary_counts,
            valenceCounts=valence_counts,
            activationCounts=activation_counts,
        )

        key_moments: list[EpisodeKeyMoment] = []
        used_shards: set[str] = set()

        def add_candidates(reason: str, items: list[dict[str, Any]]) -> None:
            items_sorted = sorted(items, key=lambda x: (float(x.get("intensity") or 0.0)), reverse=True)
            for item in items_sorted:
                if len(key_moments) >= 5:
                    return
                shard: Shard = item["shard"]
                if shard.id in used_shards:
                    continue
                used_shards.add(shard.id)

                emo = EpisodeKeyMomentEmotion(
                    primary=item.get("primary"),
                    valence=item.get("valence") if item.get("valence") in {"positive", "neutral", "negative"} else None,
                    activation=item.get("activation") if item.get("activation") in {"low", "medium", "high"} else None,
                    headline=item.get("headline"),
                )

                key_moments.append(
                    EpisodeKeyMoment(
                        shardId=shard.id,
                        episodeId=episode_id,
                        startTime=shard.start_time,
                        endTime=shard.end_time,
                        reason=reason,  # type: ignore[arg-type]
                        emotion=emo,
                        transcriptSnippet=item.get("transcript"),
                    )
                )

        highest_intensity = [
            it
            for it in enriched
            if it.get("activation") == "high" or (isinstance(it.get("intensity"), (int, float)) and float(it["intensity"]) >= 0.75)
        ]
        strong_negative = [it for it in enriched if it.get("valence") == "negative"]
        strong_positive = [it for it in enriched if it.get("valence") == "positive"]

        add_candidates("highestIntensity", highest_intensity)
        add_candidates("strongNegative", strong_negative)
        add_candidates("strongPositive", strong_positive)

        return EpisodeInsightsByEpisodeResponse(
            episodeId=episode_id,
            stats=stats,
            emotionSummary=emotion_summary,
            keyMoments=key_moments,
        )


def update_episode(
    episode_id: str,
    *,
    title: Optional[str] = None,
    note: Optional[str] = None,
) -> Optional[Episode]:
    with Session(engine) as session:
        episode = session.get(Episode, episode_id)
        if episode is None:
            return None

        if title is not None:
            episode.title = title
        if note is not None:
            episode.note = note

        session.add(episode)
        session.commit()
        session.refresh(episode)
        return episode


def update_shard(shard_id: str, updates: dict) -> Optional[Shard]:
    with Session(engine) as session:
        shard = session.get(Shard, shard_id)
        if shard is None:
            return None

        # IMPORTANT: JSON columns are not reliably change-tracked on in-place mutation.
        # Always work on copies and re-assign to the model so SQLAlchemy persists updates.
        analysis_json = dict(shard.analysis_json) if isinstance(shard.analysis_json, dict) else {}
        meta_json = dict(shard.meta_json) if isinstance(shard.meta_json, dict) else {}
        user_existing = analysis_json.get("user")
        user_block = dict(user_existing) if isinstance(user_existing, dict) else {}

        for key, value in (updates or {}).items():
            if value is None:
                continue
            user_block[key] = value

        # Align with A5 publish rule: PATCH {"status":"readyToPublish"} must persist to a place
        # that publish_shard_for_profile can reliably read.
        patched_status = user_block.get("status")
        if isinstance(patched_status, str) and patched_status == "readyToPublish":
            # Required readiness markers for publish flow.
            meta_json["status"] = "readyToPublish"
            meta_json["publishState"] = "ready"

        analysis_json["user"] = user_block
        shard.analysis_json = _json_safe(analysis_json)
        shard.meta_json = _json_safe(meta_json)

        session.add(shard)
        session.commit()
        session.refresh(shard)
        return shard


def _is_ready_to_publish(*, analysis: dict, meta: dict) -> bool:
    status, _tags = _extract_user_status_and_tags(analysis)
    if status == "readyToPublish":
        return True

    meta_status = meta.get("status") if isinstance(meta.get("status"), str) else None
    if meta_status in {"readyToPublish", "reviewed"}:
        return True

    meta_publish_state = meta.get("publishState") if isinstance(meta.get("publishState"), str) else None
    if meta_publish_state in {"ready", "readyToPublish"}:
        return True

    return False


def get_shard(shard_id: str) -> Optional[Shard]:
    with Session(engine) as session:
        return session.get(Shard, shard_id)


def _extract_user_status_and_tags(analysis: dict) -> tuple[Optional[str], list[str]]:
    user_block = analysis.get("user") if isinstance(analysis.get("user"), dict) else {}
    status = user_block.get("status")
    status_str = status if isinstance(status, str) and status.strip() else None

    tags_raw = user_block.get("userTags") or []
    tags: list[str] = []
    if isinstance(tags_raw, list):
        for t in tags_raw:
            if isinstance(t, str) and t.strip():
                tags.append(t)
    return status_str, tags


def _extract_transcript_snippet(analysis: dict) -> Optional[str]:
    user_block = analysis.get("user") if isinstance(analysis.get("user"), dict) else {}
    transcript_override = user_block.get("transcriptOverride")
    if isinstance(transcript_override, str) and transcript_override.strip():
        return transcript_override

    tr = analysis.get("transcript")
    if isinstance(tr, str) and tr.strip():
        return tr
    return None


def publish_shard_for_profile(*, profile_id: str, shard_id: str, force: bool = False) -> PublishedShard:
    with Session(engine) as session:
        shard = session.get(Shard, shard_id)
        if shard is None:
            raise ValueError("shard_not_found")

        analysis = shard.analysis_json if isinstance(shard.analysis_json, dict) else {}
        meta = shard.meta_json if isinstance(shard.meta_json, dict) else {}
        deleted = bool(analysis.get("deleted")) if isinstance(analysis.get("deleted"), (bool, int)) else False
        if deleted:
            raise ValueError("shard_deleted")

        if not force and not _is_ready_to_publish(analysis=analysis, meta=meta):
            user_status = None
            user_block = analysis.get("user") if isinstance(analysis.get("user"), dict) else {}
            if isinstance(user_block, dict):
                us = user_block.get("status")
                user_status = us if isinstance(us, str) else None
            meta_status = meta.get("status") if isinstance(meta.get("status"), str) else None
            meta_publish_state = meta.get("publishState") if isinstance(meta.get("publishState"), str) else None
            logger.info(
                "publish not ready shard_id=%s profile_id=%s user.status=%s meta.status=%s meta.publishState=%s",
                shard_id,
                profile_id,
                user_status,
                meta_status,
                meta_publish_state,
            )
            raise ValueError("not_ready_to_publish")

        # Ensure profile exists (local, offline)
        get_or_create_profile(profile_id)

        # Keep existing publishState lifecycle behavior (additive)
        publish_shard(shard_id=shard_id, force=force)

        episode_id = shard.episode_id or ""

        existing = session.exec(
            select(PublishedShard)
            .where(PublishedShard.profile_id == profile_id)
            .where(PublishedShard.shard_id == shard_id)
            .order_by(PublishedShard.published_at.desc())
        ).first()

        now = datetime.now(timezone.utc)
        if existing is None:
            ps = PublishedShard(
                profile_id=profile_id,
                shard_id=shard_id,
                episode_id=episode_id,
                published_at=now,
                deleted_at=None,
            )
            session.add(ps)
            session.commit()
            session.refresh(ps)
            return ps

        existing.episode_id = episode_id
        existing.published_at = now
        existing.deleted_at = None
        session.add(existing)
        session.commit()
        session.refresh(existing)
        return existing


def delete_published_shard_for_profile(*, profile_id: str, shard_id: str) -> None:
    with Session(engine) as session:
        existing = session.exec(
            select(PublishedShard)
            .where(PublishedShard.profile_id == profile_id)
            .where(PublishedShard.shard_id == shard_id)
            .where(PublishedShard.deleted_at.is_(None))
            .order_by(PublishedShard.published_at.desc())
        ).first()

        if existing is None:
            raise ValueError("not_published")

        existing.deleted_at = datetime.now(timezone.utc)
        session.add(existing)
        session.commit()


def get_feed_for_profile(profile_id: str) -> FeedResponse:
    with Session(engine) as session:
        items = session.exec(
            select(PublishedShard)
            .where(PublishedShard.profile_id == profile_id)
            .where(PublishedShard.deleted_at.is_(None))
            .order_by(PublishedShard.published_at.desc())
        ).all()

        out: list[FeedItem] = []
        for ps in items:
            shard = session.get(Shard, ps.shard_id)
            if shard is None:
                continue

            analysis = shard.analysis_json if isinstance(shard.analysis_json, dict) else {}
            status, tags = _extract_user_status_and_tags(analysis)
            transcript = _extract_transcript_snippet(analysis)

            primary, valence, activation, headline = _extract_emotion_compact(analysis)

            intensity: Optional[float] = None
            try:
                maybe = None
                if isinstance(shard.features_json, dict):
                    maybe = shard.features_json.get("intensity")
                if not isinstance(maybe, (int, float)):
                    signal = analysis.get("signalFeatures")
                    if isinstance(signal, dict):
                        maybe = signal.get("peak")
                if isinstance(maybe, (int, float)):
                    intensity = float(maybe)
            except Exception:
                intensity = None

            emo = FeedItemEmotion(
                primary=primary,
                valence=valence if valence in {"positive", "neutral", "negative"} else None,
                activation=activation if activation in {"low", "medium", "high"} else None,
                headline=headline,
                intensity=intensity,
            )

            out.append(
                FeedItem(
                    id=ps.id,
                    shardId=ps.shard_id,
                    episodeId=ps.episode_id,
                    publishedAt=ps.published_at,
                    startTimeSec=shard.start_time,
                    endTimeSec=shard.end_time,
                    status=status,
                    userTags=tags,
                    emotion=emo,
                    transcriptSnippet=transcript,
                )
            )

        return FeedResponse(items=out)


def publish_shard(*, shard_id: str, force: bool = False) -> Optional[Shard]:
    with Session(engine) as session:
        shard = session.get(Shard, shard_id)
        if shard is None:
            return None

        analysis = shard.analysis_json if isinstance(shard.analysis_json, dict) else {}
        deleted = bool(analysis.get("deleted")) if isinstance(analysis.get("deleted"), (bool, int)) else False
        if deleted:
            return shard

        current_state = analysis.get("publishState") if isinstance(analysis.get("publishState"), str) else None
        if current_state == "published" and not force:
            return shard

        analysis["publishState"] = "published"
        shard.analysis_json = _json_safe(analysis)

        session.add(shard)
        session.commit()
        session.refresh(shard)
        return shard


def soft_delete_shard(*, shard_id: str, reason: str) -> Optional[Shard]:
    with Session(engine) as session:
        shard = session.get(Shard, shard_id)
        if shard is None:
            return None

        analysis = shard.analysis_json if isinstance(shard.analysis_json, dict) else {}
        analysis["deleted"] = True
        analysis["deletedReason"] = reason
        analysis["deletedAt"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        shard.analysis_json = _json_safe(analysis)

        session.add(shard)
        session.commit()
        session.refresh(shard)
        return shard


def compute_episode_insights() -> EpisodeInsightsResponse:
    with Session(engine) as session:
        episodes = session.exec(select(Episode)).all()
        shards = session.exec(select(Shard)).all()

        total_episodes = len(episodes)
        total_shards = len(shards)

        durations: list[float] = []
        for s in shards:
            if s.start_time is not None and s.end_time is not None:
                delta = float(s.end_time) - float(s.start_time)
                if delta > 0:
                    durations.append(delta)
        total_duration = sum(durations) if durations else None

        tag_counts: dict[str, int] = {}
        status_counts: dict[str, int] = {}
        emotion_counts: dict[str, int] = {}

        for s in shards:
            analysis = s.analysis_json if isinstance(s.analysis_json, dict) else {}
            user_block = analysis.get("user") if isinstance(analysis.get("user"), dict) else {}

            tags = user_block.get("userTags") or []
            if isinstance(tags, list):
                for t in tags:
                    if not isinstance(t, str):
                        continue
                    tag_counts[t] = tag_counts.get(t, 0) + 1

            status = user_block.get("status")
            if isinstance(status, str):
                status_counts[status] = status_counts.get(status, 0) + 1

            primary = analysis.get("primaryEmotion")
            if not isinstance(primary, str):
                emotion_block = analysis.get("emotion")
                if isinstance(emotion_block, dict):
                    maybe = emotion_block.get("primary")
                    if isinstance(maybe, str):
                        primary = maybe
            if isinstance(primary, str):
                emotion_counts[primary] = emotion_counts.get(primary, 0) + 1

        tags_stats = [TagStat(tag=k, count=v) for k, v in sorted(tag_counts.items(), key=lambda kv: kv[1], reverse=True)]
        statuses_stats = [
            StatusStat(status=k, count=v) for k, v in sorted(status_counts.items(), key=lambda kv: kv[1], reverse=True)
        ]
        emotions_stats = [
            EmotionStat(emotion=k, count=v) for k, v in sorted(emotion_counts.items(), key=lambda kv: kv[1], reverse=True)
        ]

        last_episode_summary: Optional[EpisodeSummaryResponse] = None
        if episodes:
            latest = max(episodes, key=lambda e: e.created_at or datetime.min)
            summaries = list_episodes_with_stats()
            for ep in summaries:
                if ep.id == latest.id:
                    last_episode_summary = ep
                    break
            if last_episode_summary is None:
                last_episode_summary = EpisodeSummaryResponse(
                    id=latest.id,
                    createdAt=latest.created_at,
                    title=latest.title,
                    note=latest.note,
                    shardCount=0,
                    durationSeconds=None,
                    primaryEmotion=None,
                    valence=None,
                    arousal=None,
                )

        return EpisodeInsightsResponse(
            totalEpisodes=total_episodes,
            totalShards=total_shards,
            totalDurationSeconds=total_duration,
            tags=tags_stats,
            statuses=statuses_stats,
            emotions=emotions_stats,
            lastEpisode=last_episode_summary,
        )
