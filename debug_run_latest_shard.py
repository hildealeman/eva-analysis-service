from __future__ import annotations

from sqlmodel import Session, select

from src.db import Shard, engine, run_full_analysis_for_shard


def debug_run_latest_shard() -> None:
    with Session(engine) as session:
        shard = session.exec(select(Shard).order_by(Shard.created_at.desc())).first()
        if shard is None:
            print("NO_SHARDS_IN_DB")
            return

        print("DEBUG_LATEST_SHARD_ID", shard.id, "EPISODE", shard.episode_id)

    run_full_analysis_for_shard(shard.id)

    with Session(engine) as session:
        refreshed = session.get(Shard, shard.id)
        if refreshed is None:
            print("SHARD_NOT_FOUND_AFTER_ANALYSIS", shard.id)
            return

        print("META", refreshed.meta_json)
        print("ANALYSIS", refreshed.analysis_json)


if __name__ == "__main__":
    debug_run_latest_shard()
