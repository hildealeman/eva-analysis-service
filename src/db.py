from __future__ import annotations

import os
import secrets
import string
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import Column
from sqlalchemy.types import JSON
from sqlmodel import Field, SQLModel, Session, create_engine, select

from src.schemas.episodes import EpisodeDetailResponse, EpisodeSummaryResponse, ShardWithAnalysisResponse
from src.schemas.insights import EmotionStat, EpisodeInsightsResponse, StatusStat, TagStat

EVA_DB_URL = os.getenv("EVA_DB_URL", "sqlite:///./eva.db")

engine = create_engine(
    EVA_DB_URL,
    echo=False,
    connect_args={"check_same_thread": False} if EVA_DB_URL.startswith("sqlite") else {},
)


class Episode(SQLModel, table=True):
    id: str = Field(primary_key=True, index=True)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    title: Optional[str] = None
    note: Optional[str] = None


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

        existing = session.exec(select(Shard).where(Shard.id == shard_id)).first()
        if existing is None:
            shard = Shard(
                id=shard_id,
                episode_id=episode_id,
                start_time=start_time,
                end_time=end_time,
                source=source,
                meta_json=meta_obj,
                features_json=features_obj,
                analysis_json=analysis_dict,
            )
            session.add(shard)
        else:
            prev_analysis = existing.analysis_json if isinstance(existing.analysis_json, dict) else {}
            if isinstance(prev_analysis.get("user"), dict) and "user" not in analysis_dict:
                analysis_dict["user"] = prev_analysis.get("user")
            for k in ("publishState", "deleted", "deletedReason", "deletedAt"):
                if k in prev_analysis and k not in analysis_dict:
                    analysis_dict[k] = prev_analysis.get(k)

            existing.episode_id = episode_id
            existing.start_time = start_time
            existing.end_time = end_time
            existing.source = source
            existing.meta_json = meta_obj
            existing.features_json = features_obj
            existing.analysis_json = analysis_dict
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

        analysis_json = shard.analysis_json if isinstance(shard.analysis_json, dict) else {}
        user_block = analysis_json.get("user")
        if not isinstance(user_block, dict):
            user_block = {}

        for key, value in (updates or {}).items():
            if value is None:
                continue
            user_block[key] = value

        analysis_json["user"] = user_block
        shard.analysis_json = analysis_json

        session.add(shard)
        session.commit()
        session.refresh(shard)
        return shard


def get_shard(shard_id: str) -> Optional[Shard]:
    with Session(engine) as session:
        return session.get(Shard, shard_id)


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
        shard.analysis_json = analysis

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
        shard.analysis_json = analysis

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
