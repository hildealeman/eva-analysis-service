from __future__ import annotations

import json

from sqlmodel import Session, select

from src.db import PublishedShard, Shard, engine, init_db


DEBUG_SHARD_IDS = [
    "xLy9iGBCOcl4AANNetA08",
    "1gz5np8p4ibBjKhXtiMbD",
    "OK0o4rY6P_yE_uoPrFhwn",
    "Aoj326u3odhwOuLNE4IEQ",
    "NVMA77mRBCwze5F6xZPnX",
]


def _extract_publish_state(analysis: object) -> str | None:
    if isinstance(analysis, dict):
        v = analysis.get("publishState")
        if isinstance(v, str):
            return v
    return None


def _extract_deleted(analysis: object) -> bool | None:
    if isinstance(analysis, dict):
        v = analysis.get("deleted")
        if isinstance(v, bool):
            return v
        if isinstance(v, int):
            return bool(v)
    return None


def _extract_meta_status(meta: object) -> str | None:
    if isinstance(meta, dict):
        v = meta.get("status")
        if isinstance(v, str):
            return v
    return None


def _extract_meta_publish_state(meta: object) -> str | None:
    if isinstance(meta, dict):
        v = meta.get("publishState")
        if isinstance(v, str):
            return v
    return None


def _extract_emotion_headline(analysis: object) -> str | None:
    if isinstance(analysis, dict):
        emo = analysis.get("emotion")
        if isinstance(emo, dict):
            v = emo.get("headline")
            if isinstance(v, str):
                return v
    return None


def _extract_semantic_moment_type(analysis: object) -> str | None:
    if isinstance(analysis, dict):
        sem = analysis.get("semantic")
        if isinstance(sem, dict):
            v = sem.get("momentType")
            if isinstance(v, str):
                return v
    return None


def _shard_summary(*, s: Shard) -> dict:
    analysis = getattr(s, "analysis_json", None)
    meta = getattr(s, "meta_json", None)
    return {
        "id": getattr(s, "id", None),
        "episodeId": getattr(s, "episode_id", None),
        "meta_status": _extract_meta_status(meta),
        "meta_publishState": _extract_meta_publish_state(meta),
        "emotion_headline": _extract_emotion_headline(analysis),
        "semantic_momentType": _extract_semantic_moment_type(analysis),
    }


def main() -> None:
    init_db()

    legacy_shard_checks: list[dict] = []
    sample_existing_shards: list[dict] = []

    with Session(engine) as session:
        for shard_id in DEBUG_SHARD_IDS:
            s = session.get(Shard, shard_id)
            if s is None:
                legacy_shard_checks.append(
                    {
                        "id": shard_id,
                        "exists": False,
                        "episodeId": None,
                        "deleted": None,
                        "publishState": None,
                        "meta_status": None,
                        "meta_publishState": None,
                        "emotion_headline": None,
                        "semantic_momentType": None,
                        "publishedForLocalProfile": False,
                    }
                )
                continue

            analysis = getattr(s, "analysis_json", None)
            meta = getattr(s, "meta_json", None)

            is_published = (
                session.exec(
                    select(PublishedShard)
                    .where(PublishedShard.profile_id == "local_profile_1")
                    .where(PublishedShard.shard_id == shard_id)
                    .where(PublishedShard.deleted_at.is_(None))
                ).first()
                is not None
            )

            legacy_shard_checks.append(
                {
                    "id": shard_id,
                    "exists": True,
                    "episodeId": getattr(s, "episode_id", None),
                    "deleted": _extract_deleted(analysis),
                    "publishState": _extract_publish_state(analysis),
                    "meta_status": _extract_meta_status(meta),
                    "meta_publishState": _extract_meta_publish_state(meta),
                    "emotion_headline": _extract_emotion_headline(analysis),
                    "semantic_momentType": _extract_semantic_moment_type(analysis),
                    "publishedForLocalProfile": bool(is_published),
                }
            )

        existing = session.exec(select(Shard).order_by(Shard.created_at.desc()).limit(10)).all()
        for s in existing:
            sample_existing_shards.append(_shard_summary(s=s))

    out = {
        "legacyShardChecks": legacy_shard_checks,
        "sampleExistingShards": sample_existing_shards,
    }

    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
