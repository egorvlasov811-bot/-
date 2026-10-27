"""Проверка критерия приёмки: LLM правильно определяет «лид / не лид» в ≥80% случаев.

Формат выборки (JSONL, одна строка — одно сообщение, размеченное вручную):
  {"text": "...", "chat": "Название чата", "is_lead": true, "language": "ru"}

Запуск (по одному запросу к LLM на строку; провайдер — как в settings.yaml / LLM_PROVIDER):
  LLM_API_KEY=... python scripts/eval_llm.py tests/data/labeled_sample.jsonl
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from leadscanner.config import Config, Secrets  # noqa: E402
from leadscanner.llm import TemporaryLLMError, make_scorer  # noqa: E402
from leadscanner.models import Candidate  # noqa: E402
from leadscanner.textnorm import tokenize  # noqa: E402

CYRILLIC = set("абвгдежзийклмнопрстуфхцчшщъыьэюяіїєґ")


def draft_language(text: str) -> str:
    letters = [ch for ch in text.lower() if ch.isalpha()]
    if not letters:
        return ""
    return "ru" if sum(ch in CYRILLIC for ch in letters) / len(letters) > 0.5 else "en"


def quotes_author(draft: str, text: str) -> bool:
    """Черновик ссылается на фразу автора: есть общие содержательные корни (5+ букв)."""
    stems = {t[:5] for t in tokenize(text) if len(t) >= 5}
    return any(t[:5] in stems for t in tokenize(draft) if len(t) >= 5)


async def main(path: str, concurrency: int) -> None:
    rows = [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]
    config = Config()
    scorer = make_scorer(config, Secrets.from_env())
    sem = asyncio.Semaphore(concurrency)

    async def one(row):
        async with sem:
            c = Candidate(source=row.get("chat", "test"), source_type=row.get("source_type", "chat"),
                          text=row["text"], chat_title=row.get("chat", ""))
            for attempt in range(5):
                try:
                    return row, await scorer.assess(c)
                except TemporaryLLMError as exc:
                    print(f"… лимит/сбой LLM ({exc}), повтор через {20 * (attempt + 1)} с")
                    await asyncio.sleep(20 * (attempt + 1))
            return row, None

    results = await asyncio.gather(*(one(r) for r in rows))
    tp = fp = tn = fn = 0
    lang_ok = lang_total = quote_ok = 0
    for row, a in results:
        predicted = bool(a and a.is_lead)
        expected = bool(row["is_lead"])
        tp += predicted and expected
        fp += predicted and not expected
        tn += not predicted and not expected
        fn += not predicted and expected
        if predicted and expected and a.draft_message:
            lang_total += 1
            want = row.get("language") or ("ru" if draft_language(row["text"]) == "ru" else "en")
            lang_ok += draft_language(a.draft_message) == ("ru" if want in ("ru", "uk") else "en")
            quote_ok += quotes_author(a.draft_message, row["text"])
        if predicted != expected:
            print(f"✗ ожидали {expected}, получили {predicted}: {row['text'][:100]!r}")

    total = len(results)
    accuracy = (tp + tn) / total if total else 0
    print(f"\nВсего: {total}  TP={tp} FP={fp} TN={tn} FN={fn}")
    print(f"Точность лид/не лид: {accuracy:.0%}  (критерий ≥80%: {'OK' if accuracy >= 0.8 else 'НЕ ВЫПОЛНЕН'})")
    if tp + fp:
        print(f"Доля мусора среди найденных: {fp / (tp + fp):.0%}  (цель <30%)")
    if lang_total:
        print(f"Черновик на языке автора: {lang_ok}/{lang_total}; ссылается на фразу автора: {quote_ok}/{lang_total}")
    if total < 100:
        print(f"⚠️ В выборке {total} строк — для приёмки нужно 100 реальных размеченных сообщений.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("path")
    parser.add_argument("--concurrency", type=int, default=4)
    args = parser.parse_args()
    asyncio.run(main(args.path, args.concurrency))
