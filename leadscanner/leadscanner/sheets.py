"""Зеркало ключевых полей в Google Sheets (только для просмотра). Тексты сообщений не выгружаем."""

from __future__ import annotations

import asyncio
import json
import logging

log = logging.getLogger(__name__)

LEAD_COLUMNS = [
    "id", "created_at", "source", "source_type", "username", "project", "segment", "geo", "language",
    "temperature", "score", "status", "link", "contacted_at",
]


class SheetsMirror:
    def __init__(self, credentials_json: str, sheet_id: str):
        import gspread  # опциональная зависимость

        info = json.loads(credentials_json)
        self._book = gspread.service_account_from_dict(info).open_by_key(sheet_id)

    def _worksheet(self, title: str, cols: int):
        import gspread

        try:
            return self._book.worksheet(title)
        except gspread.WorksheetNotFound:
            return self._book.add_worksheet(title=title, rows=1000, cols=cols)

    def _sync_leads(self, rows: list[list]) -> None:
        ws = self._worksheet("leads", len(LEAD_COLUMNS))
        ws.clear()
        ws.update(values=[LEAD_COLUMNS] + rows, range_name="A1")

    def _append_report(self, row: list) -> None:
        ws = self._worksheet("weekly", 12)
        if not ws.get_values("A1:A1"):
            ws.append_row(["date", "found", "shown", "sent", "followups", "replied", "demo", "won", "skipped", "junk_reasons"])
        ws.append_row(row)

    async def sync_leads(self, leads) -> None:
        rows = [[("" if lead[c] is None else lead[c]) for c in LEAD_COLUMNS] for lead in leads]
        await asyncio.to_thread(self._sync_leads, rows)

    async def append_report(self, date: str, data: dict) -> None:
        f = data["funnel"]
        junk = sum(v for k, v in data["skip_reasons"].items() if k in ("not_lead", "junk"))
        row = [date, f["found"], f["shown"], f["sent"], f["followups"], f["replied"], f["demo"], f["won"], f["skipped"], junk]
        await asyncio.to_thread(self._append_report, row)


def make_mirror(secrets) -> SheetsMirror | None:
    if not (secrets.sheets_credentials_json and secrets.sheets_id):
        return None
    try:
        return SheetsMirror(secrets.sheets_credentials_json, secrets.sheets_id)
    except Exception:  # noqa: BLE001 — зеркало необязательно, сервис работает без него
        log.exception("Google Sheets недоступен — зеркало отключено")
        return None
