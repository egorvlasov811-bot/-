"""Слой данных: загрузка форм команд из JSON или через API-провайдеров."""

from .base import DataProvider, load_match_file, team_from_dict

__all__ = ["DataProvider", "load_match_file", "team_from_dict",
           "get_provider"]


def get_provider(name: str, **kwargs):
    """Фабрика провайдеров по имени: 'football-data' или 'api-football'."""
    name = (name or "").lower()
    if name in ("football-data", "footballdata", "fd"):
        from .footballdata import FootballDataProvider
        return FootballDataProvider(**kwargs)
    if name in ("api-football", "apifootball", "af"):
        from .apifootball import ApiFootballProvider
        return ApiFootballProvider(**kwargs)
    raise ValueError(f"Неизвестный провайдер: {name!r}. "
                     "Доступно: 'football-data', 'api-football'.")
