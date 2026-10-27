"""Нормализация текста и поиск по корням."""

from __future__ import annotations

import re
from difflib import SequenceMatcher

_WORD_RE = re.compile(r"[0-9a-zа-я+]+(?:[-'][0-9a-zа-я+]+)*", re.IGNORECASE)
_URL_RE = re.compile(r"(https?://\S+|www\.\S+|t\.me/\S+|telegram\.me/\S+)", re.IGNORECASE)
_MENTION_RE = re.compile(r"(?<!\w)@[A-Za-z0-9_]{4,}")


def normalize(text: str) -> str:
    return text.lower().replace("ё", "е")


def tokenize(text: str) -> list[str]:
    """Слова текста в нижнем регистре, ё→е. Дефисные слова дают и целое, и части."""
    tokens: list[str] = []
    for match in _WORD_RE.finditer(normalize(text)):
        word = match.group(0)
        if "-" in word:
            tokens.extend(word.split("-"))
            continue
        tokens.append(word)
    return tokens


def word_count(text: str) -> int:
    return len(_WORD_RE.findall(text))


def compile_phrase(phrase: str) -> list[str]:
    """Фраза словаря → список корней. «Мини-апп» → ['мини', 'апп']."""
    return tokenize(phrase)


def phrase_in_tokens(stems: list[str], tokens: list[str]) -> bool:
    """Есть ли в tokens подряд идущие слова, начинающиеся с каждого из stems."""
    if not stems:
        return False
    n = len(stems)
    for i in range(len(tokens) - n + 1):
        if all(tokens[i + j].startswith(stems[j]) for j in range(n)):
            return True
    return False


def similarity(a: str, b: str) -> float:
    """Доля совпадения двух текстов, 0..1 (без учёта регистра и пробелов)."""
    na = " ".join(tokenize(a))
    nb = " ".join(tokenize(b))
    if not na or not nb:
        return 0.0
    return SequenceMatcher(None, na, nb, autojunk=False).ratio()


def strip_links(text: str) -> str:
    """Убирает ссылки и @упоминания — первое сообщение уходит без них."""
    text = _URL_RE.sub("", text)
    text = _MENTION_RE.sub("", text)
    return re.sub(r"[ \t]{2,}", " ", text).strip()


def truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    cut = text[: limit - 1]
    # Режем по границе предложения или слова, чтобы не обрывать на полуслове.
    for sep in (". ", "! ", "? ", "\n"):
        pos = cut.rfind(sep)
        if pos > limit * 0.6:
            return cut[: pos + 1].strip()
    pos = cut.rfind(" ")
    if pos > 0:
        cut = cut[:pos]
    return cut.rstrip(",;:- ") + "…"
