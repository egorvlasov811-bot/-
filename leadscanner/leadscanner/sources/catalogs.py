"""Каталоги, вакансии и фриланс-ленты. Каждый адаптер отдаёт только НОВЫЕ записи.

Все адаптеры построены одинаково: забрать список → отбросить уже виденное
(catalog_seen) → превратить в Candidate. При первом запуске каталога берём
не больше initial_import записей, остальные просто помечаем виденными.
"""

from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from html.parser import HTMLParser

from ..models import Candidate
from ..textnorm import compile_phrase, phrase_in_tokens, tokenize
from .http import PoliteHTTP

log = logging.getLogger(__name__)

TGADSSPY = "Telegram Ads Spy"
HH = "hh.ru"
TME_RE = re.compile(r"(?:https?://)?(?:t|telegram)\.me/([A-Za-z][A-Za-z0-9_]{3,31})(?:/([A-Za-z0-9_]+))?", re.I)
USERNAME_RE = re.compile(r"@([A-Za-z][A-Za-z0-9_]{3,31})")
SERVICE_NAMES = {"share", "joinchat", "addstickers", "proxy", "socks", "iv", "c", "s", "addemoji", "boost"}


def _username(value) -> str:
    if not value or not isinstance(value, str):
        return ""
    m = TME_RE.search(value)
    if m:
        return m.group(1)
    value = value.strip().lstrip("@")
    return value if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{3,31}", value) else ""


def _flatten(item: dict, skip: set[str] = frozenset()) -> str:
    lines = []
    for key, value in item.items():
        if key in skip or value in (None, "", [], {}):
            continue
        if isinstance(value, dict):
            value = ", ".join(f"{k}: {v}" for k, v in value.items() if isinstance(v, (str, int, float)) and v != "")
        elif isinstance(value, list):
            value = ", ".join(str(v) for v in value if isinstance(v, (str, int, float)))
        if value != "":
            lines.append(f"{key}: {value}")
    return "\n".join(lines)


async def _only_new(db, source: str, keyed: list[tuple[str, int | None, Candidate]], initial: int) -> list[Candidate]:
    seen = await db.catalog_seen(source)
    first_run = not seen
    result = []
    for key, position, candidate in keyed:
        is_new = key not in seen
        await db.catalog_mark(source, key, position)
        if not is_new:
            continue
        if first_run and len(result) >= initial:
            continue
        result.append(candidate)
    return result


# --- Telegram Ads Spy ---------------------------------------------------------------

async def fetch_tgadsspy(http: PoliteHTTP, db, cfg: dict, initial: int = 20) -> list[Candidate]:
    """Публичный API tgadsspy.com (лицензия CC-BY-4.0, атрибуция в карточке)."""
    resp = await http.get(cfg.get("url", "https://tgadsspy.com/api/v1/bots"), api=True)
    if resp is None:
        return []
    data = resp.json()
    if isinstance(data, dict):
        data = next((data[k] for k in ("data", "items", "bots", "results", "advertisers") if isinstance(data.get(k), list)), [])
    keyed = []
    for pos, item in enumerate(data[: int(cfg.get("max_items", 200))], 1):
        if not isinstance(item, dict):
            continue
        username = next(
            (u for u in (_username(item.get(k)) for k in ("username", "bot_username", "handle", "bot", "link", "url")) if u),
            "",
        )
        if not username:
            continue
        contact = next(
            (u for u in (_username(item.get(k)) for k in ("contact", "owner", "admin", "creator", "developer")) if u),
            "",
        )
        title = item.get("title") or item.get("name") or username
        text = f"Проект из Telegram Ads Spy (рекламируется в Telegram Ads): {title}\n" + _flatten(item)
        keyed.append(
            (
                username.lower(),
                pos,
                Candidate(
                    source=TGADSSPY, source_type="catalog", text=text[:3000], link=f"https://t.me/{username}",
                    username=username, project=str(title), contact=contact, chat_title=TGADSSPY,
                ),
            )
        )
    return await _only_new(db, TGADSSPY, keyed, initial)


# --- hh.ru (официальный API) -----------------------------------------------------------

async def fetch_hh(http: PoliteHTTP, db, cfg: dict, initial: int = 50) -> list[Candidate]:
    keyed = []
    for query in cfg.get("queries") or []:
        params = {"text": query, "per_page": 50, "period": int(cfg.get("period_days", 1)), "order_by": "publication_time"}
        if cfg.get("area"):
            params["area"] = cfg["area"]
        resp = await http.get(
            "https://api.hh.ru/vacancies", params=params,
            headers={"HH-User-Agent": http.config.get("web.user_agent", "LeadScanner/1.0")}, api=True,
        )
        if resp is None:
            continue
        for item in resp.json().get("items", []):
            employer = item.get("employer") or {}
            salary = item.get("salary") or {}
            snippet = item.get("snippet") or {}
            budget = ""
            if salary:
                budget = f"{salary.get('from') or ''}–{salary.get('to') or ''} {salary.get('currency') or ''}".strip()
            text = "\n".join(
                x for x in (
                    f"Вакансия: {item.get('name', '')}",
                    f"Компания: {employer.get('name', '')}",
                    f"Город: {(item.get('area') or {}).get('name', '')}",
                    f"Зарплата: {budget}" if budget else "",
                    f"Требования: {snippet.get('requirement') or ''}",
                    f"Обязанности: {snippet.get('responsibility') or ''}",
                    f"Поисковый запрос: {query}",
                ) if x
            )
            text = re.sub(r"</?highlighttext>", "", text)
            keyed.append(
                (
                    str(item.get("id")),
                    None,
                    Candidate(
                        source=HH, source_type="jobs", text=text, link=item.get("alternate_url", ""),
                        project=employer.get("name") or item.get("name", ""), chat_title=HH,
                        extra={"employer_url": employer.get("alternate_url", "")},
                    ),
                )
            )
    return await _only_new(db, HH, keyed, initial)


# --- фриланс: RSS/Atom ленты ---------------------------------------------------------

def _rss_items(xml_text: str) -> list[dict]:
    root = ET.fromstring(xml_text)
    items = []
    for node in root.iter():
        tag = node.tag.rsplit("}", 1)[-1]
        if tag not in ("item", "entry"):
            continue
        fields = {}
        for child in node:
            name = child.tag.rsplit("}", 1)[-1]
            if name == "link" and child.get("href"):
                fields["link"] = child.get("href")
            elif child.text:
                fields.setdefault(name, child.text.strip())
        items.append(fields)
    return items


def _strip_html(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", text or "")).strip()


async def fetch_freelance(http: PoliteHTTP, db, cfg: dict, initial: int = 30) -> list[Candidate]:
    queries = [compile_phrase(q) for q in cfg.get("queries") or []]
    result = []
    for feed in cfg.get("feeds") or []:
        resp = await http.get(feed["url"])
        if resp is None:
            continue
        try:
            items = _rss_items(resp.text)
        except ET.ParseError as exc:
            log.warning("RSS %s не разобран: %s", feed["name"], exc)
            continue
        keyed = []
        for item in items:
            title = _strip_html(item.get("title", ""))
            body = _strip_html(item.get("description") or item.get("summary") or item.get("content") or "")
            tokens = tokenize(f"{title} {body}")
            if queries and not any(phrase_in_tokens(q, tokens) for q in queries):
                continue
            link = item.get("link") or item.get("guid") or ""
            keyed.append(
                (
                    item.get("guid") or link or title,
                    None,
                    Candidate(
                        source=feed["name"], source_type="freelance", text=f"Заказ: {title}\n{body}"[:3000],
                        link=link, project=f"{feed['name']}: {title}"[:200], chat_title=feed["name"],
                    ),
                )
            )
        result += await _only_new(db, feed["name"], keyed, initial)
    return result


# --- страницы каталогов мини-аппов (topapps.tg, Apps Center, FindMini) ---------------

class _LinkCollector(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links: list[tuple[str, str]] = []
        self._href: str | None = None
        self._text: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            attrs = dict(attrs)
            self._href = attrs.get("href") or ""
            self._text = [attrs.get("title") or attrs.get("aria-label") or ""]

    def handle_data(self, data):
        if self._href is not None:
            self._text.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self._href is not None:
            self.links.append((self._href, " ".join(t.strip() for t in self._text if t.strip())))
            self._href = None


def extract_tme_links(html: str) -> list[tuple[str, str, str]]:
    """[(username, app_name, текст ссылки)] в порядке появления на странице, без повторов."""
    parser = _LinkCollector()
    parser.feed(html)
    found, seen = [], set()
    for href, text in parser.links:
        m = TME_RE.search(href)
        if not m or m.group(1).lower() in SERVICE_NAMES:
            continue
        key = m.group(1).lower()
        if key in seen:
            continue
        seen.add(key)
        found.append((m.group(1), m.group(2) or "", text))
    return found


async def fetch_catalog_page(http: PoliteHTTP, db, page: dict, initial: int = 20) -> list[Candidate]:
    resp = await http.get(page["url"])
    if resp is None:
        return []
    links = extract_tme_links(resp.text)
    if not links:
        log.warning(
            "%s: на странице нет ссылок t.me — вероятно, каталог рендерится скриптом; адаптер нужно донастроить",
            page["name"],
        )
        return []
    seen = await db.catalog_seen(page["name"])
    keyed = []
    for pos, (username, app, text) in enumerate(links, 1):
        rating = page.get("kind") == "rating"
        line = f"Позиция в рейтинге: {pos}" if rating else "Новая карточка в каталоге"
        if rating and username.lower() in seen and seen[username.lower()]:
            line += f" (было {seen[username.lower()]})"
        keyed.append(
            (
                username.lower(),
                pos,
                Candidate(
                    source=page["name"], source_type="catalog",
                    text=f"Мини-апп/бот из каталога {page['name']}: {text or username}\n{line}",
                    link=f"https://t.me/{username}" + (f"/{app}" if app else ""),
                    username=username, project=text or username, chat_title=page["name"],
                ),
            )
        )
    return await _only_new(db, page["name"], keyed, initial)
