"""Нормализация текста и поиск по корням."""

from __future__ import annotations

import re
from difflib import SequenceMatcher

_WORD_RE = re.compile(r"[0-9a-zа-я+]+(?:[-'][0-9a-zа-я+]+)*", re.IGNORECASE)
_URL_RE = re.compile(r"(https?://\S+|www\.\S+|t\.me/\S+|telegram\.me/\S+)", re.IGNORECASE)
_MENTION_RE = re.compile(r"(?<!\w)@[A-Za-z0-9_]{4,}")


_CYRILLIC_RE = re.compile(r"^[а-я]+$")
# Окончания, которые отрезаются у слов словаря: «аналитика бота» → «аналитик бот»
# и находит «аналитику ботов». Самые длинные — первыми.
_RU_ENDINGS = sorted(
    """ются ется ость ами ями ого его ому ему ать ять ить ешь ют ут ят ет ит ат
    ых их ой ей ий ый ая яя ое ее ые ие ов ев ам ям ах ях ом ем
    а я ы и у ю е о ь""".split(),
    key=len,
    reverse=True,
)


def normalize(text: str) -> str:
    return text.lower().replace("ё", "е").replace("’", "'")


def stem_word(word: str) -> str:
    """Грубый стемминг слова из словаря: отрезает окончание, оставляя основу.

    Однобуквенное окончание — если остаётся 3+ буквы («бота» → «бот»), длинное — если 4+
    («посевов» → «посев», но «посев» не превращается в «пос»).
    """
    if _CYRILLIC_RE.match(word) and len(word) >= 4:
        for ending in _RU_ENDINGS:
            if word.endswith(ending) and len(word) - len(ending) >= (3 if len(ending) == 1 else 4):
                return word[: -len(ending)]
    elif word.isascii() and len(word) >= 4 and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def tokenize(text: str) -> list[str]:
    """Слова текста в нижнем регистре, ё→е. Дефисное слово разбивается на части."""
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
    """Фраза словаря → список корней. «Посевы не окупаются» → ['посев', 'не', 'окупа']."""
    return [stem_word(word) for word in tokenize(phrase)]


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
