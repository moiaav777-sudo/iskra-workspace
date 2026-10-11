"""Локальная имитация nginx для проверки портала на Mac (только для разработки).

    python scripts/dev_gateway.py            → http://127.0.0.1:8080

Повторяет схему продуктива: перед сервисом спрашивает портал /auth/check/<сервис>,
передаёт X-Portal-* и общий секрет, снимает префикс /moderator/ и /brief/.
"""
import http.client
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORTAL = ("127.0.0.1", int(os.environ.get("DEV_PORTAL_PORT", 8701)))
SERVICES = {
    "moderator": ("127.0.0.1", int(os.environ.get("DEV_MODERATOR_PORT", 8767))),
    "brief": ("127.0.0.1", int(os.environ.get("DEV_BRIEF_PORT", 8932))),
}
# Сервисы в разработке подключаются, когда их порт задан: A3 и «Повестка» на стенде идут своими портами,
# а без них /a3/ по-прежнему отдаёт портал (конструктор листа).
for _id, _var in (("a3", "DEV_A3_PORT"), ("agenda", "DEV_AGENDA_PORT"), ("theses", "DEV_THESES_PORT"), ("materials", "DEV_MATERIALS_PORT"), ("transcriber", "DEV_TRANSCRIBER_PORT"), ("profiles", "DEV_PROFILES_PORT")):
    _port = os.environ.get(_var)
    if _port:
        SERVICES[_id] = ("127.0.0.1", int(_port))
SECRET = os.environ["PORTAL_SECRET"]
HOP = {"connection", "keep-alive", "transfer-encoding", "te", "trailer", "upgrade", "proxy-authorization", "proxy-authenticate"}


class Gateway(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else None

    def _redirect(self, where):
        self.send_response(302)
        self.send_header("Location", where)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _check(self, service):
        c = http.client.HTTPConnection(*PORTAL, timeout=10)
        c.request("GET", f"/auth/check/{service}", headers={"Cookie": self.headers.get("Cookie", ""), "Host": self.headers.get("Host", "")})
        r = c.getresponse(); r.read()
        headers = {k: r.getheader(k) for k in ("X-Portal-User", "X-Portal-Role", "X-Portal-Name")}
        c.close()
        return r.status, headers

    def _proxy(self, upstream, path, extra=None):
        body = self._body()
        headers = {k: v for k, v in self.headers.items() if k.lower() not in HOP and not k.lower().startswith("x-portal-")}
        headers["X-Real-IP"] = self.client_address[0]
        headers["X-Forwarded-Proto"] = "http"
        headers.update(extra or {})
        c = http.client.HTTPConnection(*upstream, timeout=900)
        c.request(self.command, path, body=body, headers=headers)
        r = c.getresponse()
        data = r.read()
        self.send_response(r.status)
        for k, v in r.getheaders():
            if k.lower() not in HOP and k.lower() != "content-length":
                self.send_header(k, v)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)
        c.close()

    def handle_any(self):
        path = self.path
        if path.startswith("/internal/") or path.startswith("/auth/"):
            self._body()
            return self._redirect("/")
        for service, upstream in SERVICES.items():
            prefix = f"/{service}"
            if path == prefix:
                return self._redirect(prefix + "/")
            if path.startswith(prefix + "/"):
                status, user = self._check(service)
                if status == 401:
                    self._body(); return self._redirect(f"/login?next={prefix}/")
                if status == 403:
                    self._body(); return self._redirect(f"/?denied={service}")
                extra = {k: v for k, v in user.items() if v}
                extra["X-Portal-Secret"] = SECRET
                return self._proxy(upstream, path[len(prefix):] or "/", extra)
        return self._proxy(PORTAL, path)

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = handle_any


if __name__ == "__main__":
    port = int(os.environ.get("DEV_GATEWAY_PORT", 8080))
    host = os.environ.get("DEV_GATEWAY_HOST", "127.0.0.1")   # 0.0.0.0 — в контейнере (run.sh / docker compose)
    print(f"gateway http://{host}:{port}", flush=True)
    ThreadingHTTPServer((host, port), Gateway).serve_forever()
