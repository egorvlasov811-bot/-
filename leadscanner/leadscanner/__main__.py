"""python -m leadscanner [run|login|chats]"""

import asyncio
import logging
import sys

from . import app


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("telethon").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    command = sys.argv[1] if len(sys.argv) > 1 else "run"
    commands = {"run": app.run, "login": app.login, "chats": app.list_chats}
    if command not in commands:
        raise SystemExit(f"Неизвестная команда {command!r}. Доступно: {', '.join(commands)}")
    asyncio.run(commands[command]())


if __name__ == "__main__":
    main()
