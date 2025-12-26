from __future__ import annotations

import os
from datetime import datetime
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
            existing.episode_id = episode_id
            existing.start_time = start_time
            existing.end_time = end_time
            existing.source = source
            existing.meta_json = meta_obj
            existing.features_json = features_obj
            existing.analysis_json = analysis_dict
            session.add(existing)

        session.commit()


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
            shard_items.append(
                ShardWithAnalysisResponse(
                    id=s.id,
                    episodeId=s.episode_id,
                    startTime=s.start_time,
                    endTime=s.end_time,
                    source=s.source,
                    meta=s.meta_json or {},
                    features=s.features_json or {},
                    analysis=s.analysis_json or {},
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
