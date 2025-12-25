from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv


@dataclass(frozen=True)
class EvaConfig:
    model_root: Optional[Path]
    whisper_model_root: Optional[Path]
    device: str
    use_real_whisper: bool
    use_real_emotion: bool
    whisper_model_name: str
    work_dir: Optional[Path]


def load_config() -> EvaConfig:
    # Load .env if present (does nothing if not found)
    load_dotenv()

    model_root_raw = os.getenv("EVA_MODEL_ROOT")
    whisper_model_root_raw = os.getenv("EVA_WHISPER_MODEL_ROOT")
    device = os.getenv("EVA_DEVICE", "cpu")
    use_real_whisper = os.getenv("EVA_USE_REAL_WHISPER", "0") == "1"
    use_real_emotion = os.getenv("EVA_USE_REAL_EMOTION", "0") == "1"
    whisper_model_name = os.getenv("EVA_WHISPER_MODEL_NAME", "medium")
    work_dir_raw = os.getenv("EVA_WORK_DIR")

    model_root = Path(model_root_raw).expanduser() if model_root_raw else None
    whisper_model_root = Path(whisper_model_root_raw).expanduser() if whisper_model_root_raw else None
    work_dir = Path(work_dir_raw).expanduser() if work_dir_raw else None

    return EvaConfig(
        model_root=model_root,
        whisper_model_root=whisper_model_root,
        device=device,
        use_real_whisper=use_real_whisper,
        use_real_emotion=use_real_emotion,
        whisper_model_name=whisper_model_name,
        work_dir=work_dir,
    )


def model_root_available(cfg: EvaConfig) -> bool:
    return bool(cfg.model_root and cfg.model_root.exists() and cfg.model_root.is_dir())


def ensure_hf_cache_dirs(cfg: EvaConfig) -> None:
    """Best-effort: point Hugging Face caches to the external model root.

    This prevents accidental downloads to the internal disk.
    """

    if not model_root_available(cfg):
        return

    assert cfg.model_root is not None
    cache_root = cfg.model_root / "_cache"
    try:
        cache_root.mkdir(parents=True, exist_ok=True)
    except Exception:
        return

    # huggingface_hub
    os.environ.setdefault("HF_HOME", str(cache_root / "hf"))

    # transformers / datasets (common env vars)
    os.environ.setdefault("TRANSFORMERS_CACHE", str(cache_root / "transformers"))
    os.environ.setdefault("HF_DATASETS_CACHE", str(cache_root / "datasets"))

    # some libs respect XDG_CACHE_HOME
    os.environ.setdefault("XDG_CACHE_HOME", str(cache_root / "xdg"))


def get_work_dir(cfg: EvaConfig) -> Path:
    if cfg.work_dir:
        cfg.work_dir.mkdir(parents=True, exist_ok=True)
        return cfg.work_dir

    if cfg.model_root:
        tmp = cfg.model_root / "tmp"
        try:
            tmp.mkdir(parents=True, exist_ok=True)
            return tmp
        except Exception:
            pass

    return Path("/tmp")
