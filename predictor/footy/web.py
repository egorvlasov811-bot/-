"""Локальный веб-интерфейс: `python -m footy serve` -> http://127.0.0.1:8050

Без внешних зависимостей (http.server). API:
  GET /api/competitions
  GET /api/upcoming?c=PL&days=7
  GET /api/match?c=PL&id=<fixture_id>
  GET /api/predict?c=PL&home=Arsenal&away=Chelsea
"""

from __future__ import annotations

import json
import os
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional
from urllib.parse import parse_qs, urlparse

from .service import FootyService, fixture_dict

STATIC = os.path.join(os.path.dirname(__file__), "static", "index.html")


def make_handler(service: FootyService):
    class Handler(BaseHTTPRequestHandler):
        server_version = "footy/1.0"

        def log_message(self, fmt, *args):  # тише в консоли
            pass

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, data, code: int = 200) -> None:
            self._send(code, json.dumps(data, ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8")

        def do_GET(self):  # noqa: N802
            url = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(url.query).items()}
            try:
                if url.path in ("/", "/index.html"):
                    with open(STATIC, "rb") as f:
                        self._send(200, f.read(), "text/html; charset=utf-8")
                elif url.path == "/api/competitions":
                    self._json(service.competitions())
                elif url.path == "/api/upcoming":
                    fx = service.upcoming(q.get("c", "CL"), int(q.get("days", 7)))
                    self._json([fixture_dict(f) for f in fx])
                elif url.path == "/api/match":
                    c = q.get("c", "CL")
                    f = service.fixture(c, int(q["id"]), int(q.get("days", 7)))
                    if f is None:
                        self._json({"error": "матч не найден"}, 404)
                    else:
                        self._json(service.analyze_fixture(f))
                elif url.path == "/api/predict":
                    self._json(service.analyze(q.get("c", "PL"), q["home"], q["away"]))
                else:
                    self._json({"error": "not found"}, 404)
            except (KeyError, ValueError, LookupError) as e:
                self._json({"error": str(e)}, 400)
            except Exception as e:  # сеть/лимиты API
                traceback.print_exc()
                self._json({"error": f"{type(e).__name__}: {e}"}, 502)

    return Handler


def serve(host: str = "127.0.0.1", port: int = 8050,
          service: Optional[FootyService] = None) -> None:
    service = service or FootyService()
    httpd = ThreadingHTTPServer((host, port), make_handler(service))
    print(f"footy web: http://{host}:{port}  (Ctrl+C — стоп)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
