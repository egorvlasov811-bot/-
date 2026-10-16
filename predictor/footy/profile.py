"""Профиль калибровки: сохранить лучшие параметры модели и применять их.

`calibrate --save` кладёт найденные rho/decay/shrink_k в JSON-профиль, а
predict/fetch/round/sgp при старте его подхватывают — не нужно каждый раз
копировать параметры руками. Профиль не перекрывает значения, которые ты задал
явно (в матч-файле или флагами): применяется только там, где стоят дефолты.
"""

from __future__ import annotations

import json
import os
from typing import Optional

DEFAULT_PATH = ".footy_profile.json"

# дефолты модели — профиль применяется только если значение равно дефолту
MODEL_DEFAULTS = {"rho": -0.08, "decay": 0.85, "shrink_k": 3.0}


def save_profile(params: dict, path: str = DEFAULT_PATH) -> str:
    """Записывает профиль (rho/decay/shrink_k + метаданные) в JSON."""
    keep = {}
    for k in ("rho", "decay", "shrink_k", "half_life_days", "prior_games"):
        if k in params and params[k] is not None:
            keep[k] = float(params[k])
    for meta in ("metric", "n", "log_loss", "brier", "model",
                 "market_log_loss"):
        if params.get(meta) is not None:
            keep[meta] = params[meta]
    with open(path, "w", encoding="utf-8") as f:
        json.dump(keep, f, ensure_ascii=False, indent=2)
    return path


def load_profile(path: str = DEFAULT_PATH) -> Optional[dict]:
    """Читает профиль; None, если файла нет или он битый."""
    if not path or not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def apply_to_ctx(ctx, profile: dict) -> list:
    """Применяет rho/decay/shrink_k к ctx там, где стоят дефолты модели.

    Возвращает список применённых ключей (пусто, если ничего не тронуто).
    """
    applied = []
    for k, default in MODEL_DEFAULTS.items():
        if k in profile and abs(getattr(ctx, k) - default) < 1e-12:
            setattr(ctx, k, float(profile[k]))
            applied.append(k)
    return applied
