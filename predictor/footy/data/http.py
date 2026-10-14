"""HTTP-слой провайдеров: дисковый кэш + ретраи с backoff.

Зачем: бесплатные тарифы API жёстко лимитированы (football-data ~10 запросов
в минуту). Без кэша разбор целого тура упирается в 429. Здесь:
  * простой дисковый кэш ответов (ключ = url+params, TTL настраиваемый);
  * ретраи на 429/5xx с экспоненциальным backoff и учётом Retry-After.

Всё тестируемо: сетевой вызов (`fetch`) и `sleep` инъектируются, поэтому
логику кэша и ретраев можно проверить без сети и без реальных пауз.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass
from typing import Callable, Optional, Tuple

try:
    import requests
except ImportError:  # сеть не нужна для оффлайн-режима/тестов
    requests = None  # type: ignore

# fetch(url, headers, params, timeout) -> (status_code, json_or_none, retry_after_seconds)
Fetcher = Callable[[str, dict, dict, float], Tuple[int, Optional[dict], Optional[float]]]


class HttpError(RuntimeError):
    """Сетевой сбой, который не удалось вылечить ретраями."""


def _default_fetch(url: str, headers: dict, params: dict,
                   timeout: float) -> Tuple[int, Optional[dict], Optional[float]]:
    if requests is None:
        raise HttpError("Нужен пакет requests: pip install -r requirements.txt")
    resp = requests.get(url, headers=headers, params=params, timeout=timeout)
    ra = resp.headers.get("Retry-After")
    retry_after = None
    if ra is not None:
        try:
            retry_after = float(ra)
        except ValueError:
            retry_after = None
    data = None
    if resp.status_code < 400:
        data = resp.json()
    return resp.status_code, data, retry_after


def _cache_key(url: str, params: dict) -> str:
    raw = url + "?" + "&".join(f"{k}={params[k]}" for k in sorted(params))
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


@dataclass
class HttpClient:
    """Клиент с кэшем и ретраями. Один на провайдера."""

    cache_dir: Optional[str] = None     # None => кэш выключен
    ttl: float = 6 * 3600.0             # сколько секунд ответ считается свежим
    retries: int = 4                    # попыток на 429/5xx
    backoff: float = 2.0                # базовая пауза, сек (растёт ×2)
    timeout: float = 15.0
    fetch: Fetcher = _default_fetch
    sleep: Callable[[float], None] = time.sleep

    def _read_cache(self, key: str) -> Optional[dict]:
        if not self.cache_dir:
            return None
        path = os.path.join(self.cache_dir, key + ".json")
        try:
            with open(path, "r", encoding="utf-8") as f:
                blob = json.load(f)
        except (OSError, ValueError):
            return None
        if self.ttl <= 0 or (time.time() - blob.get("ts", 0)) > self.ttl:
            return None  # ttl<=0 => кэш не используется (всегда свежий запрос)
        return blob.get("data")

    def _write_cache(self, key: str, data: dict) -> None:
        if not self.cache_dir:
            return
        try:
            os.makedirs(self.cache_dir, exist_ok=True)
            path = os.path.join(self.cache_dir, key + ".json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"ts": time.time(), "data": data}, f)
        except OSError:
            pass  # кэш — лучшее усилие, не роняем запрос из-за него

    def get_json(self, url: str, *, headers: Optional[dict] = None,
                 params: Optional[dict] = None) -> dict:
        headers = headers or {}
        params = params or {}
        key = _cache_key(url, params)
        cached = self._read_cache(key)
        if cached is not None:
            return cached

        last_status = None
        for attempt in range(self.retries + 1):
            status, data, retry_after = self.fetch(url, headers, params, self.timeout)
            last_status = status
            if status < 400 and data is not None:
                self._write_cache(key, data)
                return data
            # ретраим только временные сбои: 429 и 5xx
            if status != 429 and status < 500:
                raise HttpError(f"HTTP {status} для {url}")
            if attempt < self.retries:
                wait = retry_after if retry_after is not None \
                    else self.backoff * (2 ** attempt)
                self.sleep(wait)
        raise HttpError(f"HTTP {last_status} для {url} после {self.retries} ретраев")


def client_from_env(prefix: str = "FOOTY") -> HttpClient:
    """Строит HttpClient из переменных окружения (кэш по умолчанию включён).

    {PREFIX}_CACHE_DIR (по умолчанию .footy_cache), {PREFIX}_CACHE_TTL (сек),
    {PREFIX}_NO_CACHE=1 — выключить кэш.
    """
    if os.environ.get(f"{prefix}_NO_CACHE") == "1":
        cache_dir = None
    else:
        cache_dir = os.environ.get(f"{prefix}_CACHE_DIR", ".footy_cache")
    ttl = float(os.environ.get(f"{prefix}_CACHE_TTL", 6 * 3600))
    return HttpClient(cache_dir=cache_dir, ttl=ttl)
