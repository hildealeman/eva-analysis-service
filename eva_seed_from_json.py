from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from sqlmodel import Session, select

from src.db import Episode, Shard, engine, init_db


def _as_str(v: Any) -> Optional[str]:
    if v is None:
        return None
    if isinstance(v, str):
        s = v.strip()
        return s if s else None
    return str(v).strip() or None


def _as_float(v: Any) -> Optional[float]:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        s = v.strip()
        if not s:
            return None
        try:
            return float(s)
        except Exception:
            return None
    return None


def _as_dict(v: Any) -> dict:
    if isinstance(v, dict):
        return v
    return {}


def _parse_datetime(v: Any) -> Optional[datetime]:
    if v is None:
        return None
    if isinstance(v, datetime):
        return v
    if isinstance(v, str):
        s = v.strip()
        if not s:
            return None
        try:
            return datetime.fromisoformat(s.replace("Z", "+00:00"))
        except Exception:
            return None
    return None


def _upsert_episode(*, session: Session, episode_id: str, title: Any = None, note: Any = None, created_at: Any = None) -> bool:
    ep = session.get(Episode, episode_id)
    created = ep is None
    if ep is None:
        ep = Episode(id=episode_id)

    t = _as_str(title)
    n = _as_str(note)
    ca = _parse_datetime(created_at)

    if t is not None:
        ep.title = t
    if n is not None:
        ep.note = n
    if ca is not None and getattr(ep, "created_at", None) is not None:
        ep.created_at = ca

    session.add(ep)
    return created


def _upsert_shard(
    *,
    session: Session,
    shard_id: str,
    episode_id: Optional[str],
    start_time: Optional[float],
    end_time: Optional[float],
    source: Optional[str],
    meta_obj: dict,
    features_obj: dict,
    analysis_obj: dict,
) -> bool:
    existing = session.get(Shard, shard_id)
    created = existing is None

    if existing is None:
        shard = Shard(
            id=shard_id,
            episode_id=episode_id,
            start_time=start_time,
            end_time=end_time,
            source=source,
            meta_json=meta_obj,
            features_json=features_obj,
            analysis_json=analysis_obj,
        )
        session.add(shard)
        return True

    prev_analysis = existing.analysis_json if isinstance(existing.analysis_json, dict) else {}
    merged_analysis = analysis_obj
    if isinstance(prev_analysis.get("user"), dict) and "user" not in merged_analysis:
        merged_analysis["user"] = prev_analysis.get("user")
    for k in ("publishState", "deleted", "deletedReason", "deletedAt"):
        if k in prev_analysis and k not in merged_analysis:
            merged_analysis[k] = prev_analysis.get(k)

    existing.episode_id = episode_id
    existing.start_time = start_time
    existing.end_time = end_time
    existing.source = source
    existing.meta_json = meta_obj
    existing.features_json = features_obj
    existing.analysis_json = merged_analysis
    session.add(existing)
    return created


def _iter_episode_payloads(payload: Any) -> list[dict]:
    if isinstance(payload, dict):
        if isinstance(payload.get("episodes"), list):
            return [e for e in payload.get("episodes") if isinstance(e, dict)]
        if isinstance(payload.get("data"), dict) and isinstance(payload["data"].get("episodes"), list):
            return [e for e in payload["data"].get("episodes") if isinstance(e, dict)]
    if isinstance(payload, list):
        # IMPORTANT: a list payload is ambiguous (it could be a list of shards).
        # We only treat it as a list of episodes if it actually contains shard collections.
        if payload and all(
            isinstance(x, dict) and (isinstance(x.get("shards"), list) or isinstance(x.get("clips"), list))
            for x in payload
        ):
            return [x for x in payload if isinstance(x, dict)]
    return []


def _iter_shard_payloads(payload: Any) -> list[dict]:
    if isinstance(payload, dict):
        if isinstance(payload.get("shards"), list):
            return [s for s in payload.get("shards") if isinstance(s, dict)]
        if isinstance(payload.get("clips"), list):
            return [s for s in payload.get("clips") if isinstance(s, dict)]
        if isinstance(payload.get("data"), dict):
            d = payload.get("data")
            if isinstance(d.get("shards"), list):
                return [s for s in d.get("shards") if isinstance(s, dict)]
            if isinstance(d.get("clips"), list):
                return [s for s in d.get("clips") if isinstance(s, dict)]
    if isinstance(payload, list):
        if payload and all(isinstance(x, dict) and ("id" in x or "shardId" in x) for x in payload):
            return [x for x in payload if isinstance(x, dict)]
    return []


def seed_from_payload(payload: Any) -> dict[str, int]:
    init_db()

    episodes_payloads = _iter_episode_payloads(payload)
    shards_payloads: list[dict] = []

    episodes_seeded = 0
    episodes_updated = 0
    shards_inserted = 0
    shards_updated = 0
    skipped_shards = 0

    with Session(engine) as session:
        for ep_obj in episodes_payloads:
            ep_id = _as_str(ep_obj.get("id") or ep_obj.get("episodeId"))
            if not ep_id:
                continue

            created = _upsert_episode(
                session=session,
                episode_id=ep_id,
                title=ep_obj.get("title"),
                note=ep_obj.get("note"),
                created_at=ep_obj.get("createdAt") or ep_obj.get("created_at"),
            )
            if created:
                episodes_seeded += 1
            else:
                episodes_updated += 1

            if isinstance(ep_obj.get("shards"), list):
                shards_payloads.extend([s for s in ep_obj.get("shards") if isinstance(s, dict)])
            if isinstance(ep_obj.get("clips"), list):
                shards_payloads.extend([s for s in ep_obj.get("clips") if isinstance(s, dict)])

        session.commit()

    if not shards_payloads:
        shards_payloads = _iter_shard_payloads(payload)

    with Session(engine) as session:
        for s in shards_payloads:
            shard_id = _as_str(s.get("id") or s.get("shardId"))
            if not shard_id:
                skipped_shards += 1
                continue

            episode_id = _as_str(s.get("episodeId") or s.get("episode_id"))
            start_time = _as_float(s.get("startTime") or s.get("start_time") or s.get("startTimeSec"))
            end_time = _as_float(s.get("endTime") or s.get("end_time") or s.get("endTimeSec"))
            source = _as_str(s.get("source"))

            meta = _as_dict(s.get("meta") or s.get("meta_json"))
            features = _as_dict(s.get("features") or s.get("features_json"))
            analysis = _as_dict(s.get("analysis") or s.get("analysis_json"))

            if not episode_id:
                episode_id = _as_str(meta.get("episodeId"))

            if episode_id:
                _upsert_episode(session=session, episode_id=episode_id)

            created = _upsert_shard(
                session=session,
                shard_id=shard_id,
                episode_id=episode_id,
                start_time=start_time,
                end_time=end_time,
                source=source,
                meta_obj=meta,
                features_obj=features,
                analysis_obj=analysis,
            )
            if created:
                shards_inserted += 1
            else:
                shards_updated += 1

        session.commit()

    return {
        "episodesSeeded": int(episodes_seeded),
        "episodesUpdated": int(episodes_updated),
        "shardsInserted": int(shards_inserted),
        "shardsUpdated": int(shards_updated),
        "shardsSkipped": int(skipped_shards),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed EVA 2 DB from EVA 1 JSON export")
    parser.add_argument("json_path", type=str, help="Path to exported JSON")
    args = parser.parse_args()

    path = Path(args.json_path).expanduser()
    raw = path.read_text(encoding="utf-8")
    payload = json.loads(raw)

    result = seed_from_payload(payload)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
