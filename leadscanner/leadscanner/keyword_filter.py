"""Ступень 1: дешёвый фильтр по ключевым словам и минус-словам."""

from __future__ import annotations

from dataclasses import dataclass, field

from .textnorm import compile_phrase, phrase_in_tokens, tokenize, word_count

POSITIVE_GROUPS = ("hot_ru", "warm_ru", "hot_en", "warm_en")
STEM_GROUPS = ("stems_ru", "stems_en")
DEFAULT_STEMS_CONTEXT = ["бот", "мини-апп", "телеграм", "посев", "закуп"]


@dataclass
class FilterResult:
    passed: bool
    reason: str = ""
    hot: list[str] = field(default_factory=list)
    warm: list[str] = field(default_factory=list)

    @property
    def matched(self) -> list[str]:
        return self.hot + self.warm


class KeywordFilter:
    """Сообщение проходит, если в нём есть фраза из hot/warm-групп, или корень из
    stems_* вместе со словом-контекстом (stems_context), нет минус-слов и слов
    не меньше min_words."""

    def __init__(self, dictionaries: dict, min_words: int = 6):
        self.min_words = min_words
        self._groups: dict[str, list[tuple[str, list[str]]]] = {}
        context = dictionaries.get("stems_context") or DEFAULT_STEMS_CONTEXT
        for group, phrases in [*((g, dictionaries.get(g)) for g in (*POSITIVE_GROUPS, *STEM_GROUPS, "minus")),
                               ("stems_context", context)]:
            self._groups[group] = [(str(p), compile_phrase(str(p))) for p in phrases or [] if str(p).strip()]

    def _find(self, group: str, tokens: list[str]) -> list[str]:
        return [phrase for phrase, stems in self._groups[group] if phrase_in_tokens(stems, tokens)]

    def check(self, text: str) -> FilterResult:
        if not text or word_count(text) < self.min_words:
            return FilterResult(False, "short")
        tokens = tokenize(text)
        minus = self._find("minus", tokens)
        if minus:
            return FilterResult(False, f"minus:{minus[0]}")
        hot = self._find("hot_ru", tokens) + self._find("hot_en", tokens)
        warm = self._find("warm_ru", tokens) + self._find("warm_en", tokens)
        if not hot and not warm:
            stems = [s for g in STEM_GROUPS for s in self._find(g, tokens)]
            if stems and self._find("stems_context", tokens):
                warm = [f"корень:{s}" for s in stems]
        if not hot and not warm:
            return FilterResult(False, "no_keywords")
        return FilterResult(True, "ok", hot=hot, warm=warm)


class CachedKeywordFilter:
    """Пересобирает фильтр, когда меняется keywords.yaml или min_words."""

    def __init__(self, config):
        self.config = config
        self._key = None
        self._filter: KeywordFilter | None = None

    def check(self, text: str) -> FilterResult:
        dictionaries = self.config.keywords
        min_words = int(self.config.get("telegram.min_words", 6))
        key = (id(dictionaries), min_words)
        if key != self._key:
            self._filter = KeywordFilter(dictionaries, min_words)
            self._key = key
        return self._filter.check(text)
