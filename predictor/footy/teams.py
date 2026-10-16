"""Сопоставление названий команд между источниками.

Один и тот же клуб в разных источниках зовут по-разному: football-data.org —
«Manchester City FC», The Odds API — «Manchester City», CSV football-data.co.uk —
«Man City». Без сопоставления кэфы не цепляются к матчам, а приор силы из CSV —
к командам из API.

Подход: нормализация (регистр, диакритика, служебные слова FC/CF/AC…, синонимы)
+ нечёткое сравнение с порогом. Ошибочная склейка хуже пропуска, поэтому
порог строгий, а при равенстве кандидатов матч не склеивается.
"""

from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher
from typing import Dict, Iterable, Optional

# служебные слова, не несущие идентичности клуба
_STOP = {
    "fc", "cf", "ac", "as", "sc", "afc", "ssc", "sv", "vfb", "vfl", "tsg",
    "fk", "sk", "bk", "if", "rc", "rcd", "ud", "cd", "sd", "ca", "club",
    "de", "del", "la", "le", "the", "calcio", "football", "futbol", "1",
    "07", "04", "05", "09", "1899", "1846", "1848", "1901", "1907", "1909",
    "1910", "1913", "balompie", "sad", "us", "ogc", "losc", "stade",
    "kv", "krc", "kaa", "rsc", "pae", "gs", "jk", "nk", "hnk", "pfc", "fc.",
}

# синонимы: нормализованная форма -> каноническая
_ALIASES = {
    "man city": "manchester city",
    "man united": "manchester united",
    "man utd": "manchester united",
    "nottm forest": "nottingham forest",
    "nott m forest": "nottingham forest",
    "wolves": "wolverhampton wanderers",
    "wolverhampton": "wolverhampton wanderers",
    "spurs": "tottenham hotspur",
    "tottenham": "tottenham hotspur",
    "brighton": "brighton hove albion",
    "brighton and hove albion": "brighton hove albion",
    "west ham": "west ham united",
    "newcastle": "newcastle united",
    "leeds": "leeds united",
    "sheffield united": "sheffield united",
    "sheffield utd": "sheffield united",
    "inter": "internazionale",
    "inter milan": "internazionale",
    "internazionale milano": "internazionale",
    "milan": "milan",
    "psg": "paris saint germain",
    "paris sg": "paris saint germain",
    "bayern munich": "bayern",
    "bayern munchen": "bayern",
    "bayern muenchen": "bayern",
    "dortmund": "borussia dortmund",
    "bvb": "borussia dortmund",
    "m gladbach": "borussia monchengladbach",
    "monchengladbach": "borussia monchengladbach",
    "gladbach": "borussia monchengladbach",
    "leverkusen": "bayer leverkusen",
    "bayer 04 leverkusen": "bayer leverkusen",
    "ein frankfurt": "eintracht frankfurt",
    "frankfurt": "eintracht frankfurt",
    "rb leipzig": "leipzig",
    "rasenballsport leipzig": "leipzig",
    "atletico madrid": "atletico madrid",
    "ath madrid": "atletico madrid",
    "atl madrid": "atletico madrid",
    "ath bilbao": "athletic bilbao",
    "athletic": "athletic bilbao",
    "athletic club": "athletic bilbao",
    "betis": "real betis",
    "sociedad": "real sociedad",
    "celta": "celta vigo",
    "celta de vigo": "celta vigo",
    "sporting cp": "sporting lisbon",
    "sporting": "sporting lisbon",
    "sporting clube de portugal": "sporting lisbon",
    "benfica": "benfica",
    "sl benfica": "benfica",
    "porto": "porto",
    "psv": "psv eindhoven",
    "ajax": "ajax",
    "afc ajax": "ajax",
    "club brugge": "club brugge",
    "brugge": "club brugge",
    "salzburg": "red bull salzburg",
    "rb salzburg": "red bull salzburg",
    "fc red bull salzburg": "red bull salzburg",
    "shakhtar": "shakhtar donetsk",
    "dinamo zagreb": "dinamo zagreb",
    "gnk dinamo zagreb": "dinamo zagreb",
    "crvena zvezda": "red star belgrade",
    "fk crvena zvezda": "red star belgrade",
    "red star": "red star belgrade",
    "slavia praha": "slavia prague",
    "sparta praha": "sparta prague",
    "olympiacos": "olympiakos",
    "olympiakos piraeus": "olympiakos",
    "olympiacos piraeus": "olympiakos",
    "qarabag": "qarabag",
    "qarabag agdam": "qarabag",
    "pafos": "pafos",
    "bodo glimt": "bodo glimt",
    "fk bodo glimt": "bodo glimt",
    "kobenhavn": "copenhagen",
    "fc kobenhavn": "copenhagen",
    "fc copenhagen": "copenhagen",
    "st etienne": "saint etienne",
    "paris fc": "paris fc",
    "marseille": "marseille",
    "olympique marseille": "marseille",
    "olympique de marseille": "marseille",
    "lyon": "lyon",
    "olympique lyonnais": "lyon",
    "olympique lyon": "lyon",
    "lille": "lille",
    "lille osc": "lille",
    "monaco": "monaco",
    "as monaco": "monaco",
    "nice": "nice",
    "roma": "roma",
    "lazio": "lazio",
    "napoli": "napoli",
    "juventus": "juventus",
    "atalanta": "atalanta",
    "atalanta bc": "atalanta",
    "verona": "hellas verona",
    "union berlin": "union berlin",
    "1 fc union berlin": "union berlin",
    "fc koln": "koln",
    "cologne": "koln",
    "fc cologne": "koln",
    "mainz": "mainz",
    "mainz 05": "mainz",
    "hoffenheim": "hoffenheim",
    "heidenheim": "heidenheim",
    "st pauli": "st pauli",
    "wolfsburg": "wolfsburg",
    "werder bremen": "werder bremen",
    "bremen": "werder bremen",
    "stuttgart": "stuttgart",
    "augsburg": "augsburg",
    "freiburg": "freiburg",
    "hamburg": "hamburger",
    "borussia m gladbach": "borussia monchengladbach",
    "espanyol barcelona": "espanyol",
    "espanol": "espanyol",
    "brestois": "brest",
    "brestois 29": "brest",
    "rennais": "rennes",
    "stade rennais": "rennes",
    "hamburger sv": "hamburger",
}


def _strip_accents(s: str) -> str:
    s = s.replace("ø", "o").replace("Ø", "O").replace("ß", "ss")
    s = s.replace("æ", "ae").replace("ı", "i")
    return "".join(c for c in unicodedata.normalize("NFKD", s)
                   if not unicodedata.combining(c))


def normalize(name: str) -> str:
    """Каноническая форма названия для сравнения."""
    s = _strip_accents(name or "").lower()
    s = s.replace("&", " and ").replace("'", " ").replace(".", " ")
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    if s in _ALIASES:
        return _ALIASES[s]
    words = [w for w in s.split() if w not in _STOP]
    core = " ".join(words) or s
    return _ALIASES.get(core, core)


def similarity(a: str, b: str) -> float:
    """Похожесть двух названий в [0, 1] (после нормализации)."""
    na, nb = normalize(a), normalize(b)
    if na == nb:
        return 1.0
    ta, tb = set(na.split()), set(nb.split())
    # одно название целиком содержится в другом: «Leipzig» ⊂ «RB Leipzig»
    if ta and tb and (ta <= tb or tb <= ta):
        return 0.92
    jacc = len(ta & tb) / len(ta | tb) if ta and tb else 0.0
    return max(SequenceMatcher(None, na, nb).ratio(), jacc)


def best_match(name: str, candidates: Iterable[str],
               threshold: float = 0.85) -> Optional[str]:
    """Лучшее совпадение среди кандидатов или None (если неуверенно).

    None также при ничьей двух разных кандидатов: лучше не склеить, чем
    склеить «Manchester United» с «Manchester City».
    """
    scored = sorted(((similarity(name, c), c) for c in candidates),
                    key=lambda x: -x[0])
    if not scored or scored[0][0] < threshold:
        return None
    if len(scored) > 1 and abs(scored[0][0] - scored[1][0]) < 1e-9 \
            and normalize(scored[0][1]) != normalize(scored[1][1]):
        return None
    return scored[0][1]


def match_pair(home: str, away: str, pairs: Dict[tuple, object],
               threshold: float = 0.80) -> Optional[object]:
    """Ищет значение по паре (хозяева, гости) с нечётким сравнением имён."""
    best, best_score = None, 0.0
    for (h, a), val in pairs.items():
        sh, sa = similarity(home, h), similarity(away, a)
        if sh >= threshold and sa >= threshold and sh + sa > best_score:
            best, best_score = val, sh + sa
    return best
