"""Посмотреть сервис у себя, без входа и паролей.

Зачем: стендовый шлюз (`portal/scripts/dev_gateway.py`) пока не знает про «Тезисы»
(в его списке только `a3` и `agenda`), поэтому `/theses/` на 8080 отдаёт 404.
Правит его общий трек — запрос записан в `../TRACK.md`. А пока сервис можно
посмотреть через этот маленький шлюз: он делает ровно то же, что nginx, —
подставляет заголовки портала и снимает префикс.

    .venv/bin/python -m uvicorn app.main:app --port 8961   # сам сервис
    .venv/bin/python tools/local_preview.py                # → http://127.0.0.1:8971/theses/

Что он делает:
- подставляет `X-Portal-*` и секрет, так что вход и пароли не нужны;
- отдаёт общие файлы портала (`/static/base.css` и прочие) из его папки — читает, не меняет;
- передаёт `/moderator/` и `/materials/` настоящим соседям стенда, если они подняты, —
  тогда кнопка «Приложить к совещанию» работает по-настоящему: «Модератор» находит
  или заводит совещание, «Материалы» принимают документ в папку. Учтите, что записи
  появятся в их настоящих базах.

Только для разработки: доверять заголовкам без проверки можно лишь на своей машине.
"""
from __future__ import annotations

import argparse
import http.client
import http.server
import json
from pathlib import Path
from urllib.parse import quote

PORTAL_STATIC = Path(__file__).resolve().parent.parent.parent / "portal" / "static"
HOP = {"connection", "keep-alive", "transfer-encoding", "host", "content-length"}
TYPES = {".css": "text/css", ".js": "application/javascript", ".html": "text/html",
         ".svg": "image/svg+xml", ".png": "image/png", ".ico": "image/x-icon"}
OPTS = argparse.Namespace()


def _handler():
    class Gate(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):
            if OPTS.verbose:
                super().log_message(fmt, *args)

        def _send(self, status, body: bytes, content_type: str):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")   # смотрим свежую правку
            self.end_headers()
            self.wfile.write(body)

        def _portal_file(self, path: str) -> bool:
            file = PORTAL_STATIC / path[len("/static/"):]
            if not file.is_file():
                return False
            self._send(200, file.read_bytes(), TYPES.get(file.suffix, "application/octet-stream"))
            return True

        def _proxy(self, upstream, path: str, body: bytes | None, extra: dict) -> None:
            headers = {k: v for k, v in self.headers.items()
                       if k.lower() not in HOP and not k.lower().startswith("x-portal-")}
            headers.update(extra)
            headers["Host"] = self.headers.get("Host", "127.0.0.1")
            try:
                conn = http.client.HTTPConnection(*upstream, timeout=600)
                conn.request(self.command, path, body=body, headers=headers)
                resp = conn.getresponse()
                data = resp.read()
            except OSError as error:
                self._send(502, json.dumps(
                    {"detail": f"Не отвечает {upstream[0]}:{upstream[1]} — {error}"},
                    ensure_ascii=False).encode(), "application/json")
                return
            self.send_response(resp.status)
            for key, value in resp.getheaders():
                if key.lower() not in HOP and key.lower() != "cache-control":
                    self.send_header(key, value)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            conn.close()

        def _handle(self):
            path = self.path
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else None
            portal_headers = {
                "X-Portal-Secret": OPTS.secret, "X-Portal-User": OPTS.login,
                "X-Portal-Role": OPTS.role, "X-Portal-Name": quote(OPTS.name),
            }
            for prefix, port in (("/moderator/", OPTS.moderator),
                                 ("/materials/", OPTS.materials)):
                if path.startswith(prefix):
                    # соседи живут за тем же шлюзом: префикс снимаем, как это делает nginx
                    self._proxy(("127.0.0.1", port), path[len(prefix) - 1:],
                                body, portal_headers)
                    return
            if path.startswith("/theses/"):
                path = path[len("/theses"):]
            elif path == "/theses":
                path = "/"
            if path.startswith("/static/") and self._portal_file(path):
                return
            self._proxy(("127.0.0.1", OPTS.port), path, body, portal_headers)

        do_GET = do_POST = do_PUT = do_DELETE = _handle

    return Gate


def main() -> int:
    parser = argparse.ArgumentParser(description="Локальный просмотр сервиса «Тезисы»")
    parser.add_argument("--port", type=int, default=8961, help="порт сервиса «Тезисы»")
    parser.add_argument("--listen", type=int, default=8975, help="порт этого шлюза")
    parser.add_argument("--moderator", type=int, default=8767, help="порт «Модератора» стенда")
    parser.add_argument("--materials", type=int, default=8971, help="порт «Материалов» стенда")
    parser.add_argument("--login", default="emp")
    parser.add_argument("--name", default="Сотрудникова Анна")
    parser.add_argument("--role", default="employee", choices=("employee", "admin"))
    parser.add_argument("--secret", default="dev-portal-secret",
                        help="PORTAL_SECRET, с которым запущен сервис")
    parser.add_argument("--verbose", action="store_true")
    parser.parse_args(namespace=OPTS)

    print(f"Откройте http://127.0.0.1:{OPTS.listen}/theses/")
    print(f"  сервис: 127.0.0.1:{OPTS.port} · «Модератор»: 127.0.0.1:{OPTS.moderator} · "
          f"«Материалы»: 127.0.0.1:{OPTS.materials}")
    print(f"  вход подставляется как {OPTS.login} ({OPTS.role})")
    try:
        http.server.ThreadingHTTPServer(("127.0.0.1", OPTS.listen), _handler()).serve_forever()
    except KeyboardInterrupt:
        print("\nостановлено")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
