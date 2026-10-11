"""Портал рабочего пространства workspace.example.ru.

Вход и пользователи, права на сервисы, журнал действий, стартовая страница.
Сами сервисы («Модератор», «Бриф», A3) стоят за nginx: перед каждым запросом nginx
спрашивает портал (/auth/check/<сервис>), и сервис получает проверенного пользователя
в заголовках X-Portal-User / X-Portal-Role вместе с общим секретом X-Portal-Secret.
"""
from __future__ import annotations

import hmac
import re
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote

from anyio import to_thread
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import (a3, accounts, ask, auth, config, db, iskra_key, mail, notices, package, people, people_import,
               search, status)

THREADPOOL_SIZE = 200   # умолчание anyio (40) держало обычные def-обработчики в очереди друг
# за другом при обычной, не аварийной нагрузке — /internal/auth (вход во всё пространство) ждал
# позади них (СРЕ 28.09, P1, остаток после коротких таймаутов у ping/waiting-count/upcoming/
# orders). Портал — один процесс: несколько воркеров uvicorn тихо сломали бы внутрипроцессные
# счётчики (ограничение частоты вопросов «Искре», кэш её живости) — они не разделяются между
# процессами. Больше потоков в том же процессе эту память не дробит.


@asynccontextmanager
async def _lifespan(app: FastAPI):
    to_thread.current_default_thread_limiter().total_tokens = THREADPOOL_SIZE
    yield


app = FastAPI(title="Рабочее пространство", docs_url=None, redoc_url=None, openapi_url=None,
              lifespan=_lifespan)
STATIC = config.BASE_DIR / "static"
app.mount("/static", StaticFiles(directory=STATIC), name="static")

UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
LOGIN_RE = re.compile(r"^[a-zA-Z0-9._-]{2,40}$")
CSP = ("default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self'; "
       "frame-ancestors 'none'; base-uri 'self'; form-action 'self'")


# ── общее ───────────────────────────────────────────────────────────────

def client_ip(request: Request) -> str:
    return request.headers.get("x-real-ip") or (request.client.host if request.client else "")


def origin_ok(request: Request) -> bool:
    """Защита от чужих сайтов. Host ставит nginx; X-Forwarded-Host не используем —
    его может прислать кто угодно и тем самым отключить проверку."""
    origin = request.headers.get("origin")
    if not origin:
        return True
    host = request.headers.get("host") or ""
    return origin.split("://", 1)[-1] == host


def err(msg: str, code: int = 400) -> JSONResponse:
    return JSONResponse({"detail": msg}, status_code=code)


def page(name: str) -> FileResponse:
    return FileResponse(STATIC / name, media_type="text/html; charset=utf-8",
                        headers={"Cache-Control": "no-store", "Content-Security-Policy": CSP})


def current_user(request: Request) -> dict | None:
    return auth.verify_session(request.cookies.get(auth.SESSION_COOKIE))


def public_user(u: dict) -> dict:
    return {
        "login": u["login"], "full_name": u["full_name"], "role": u["role"], "role_name": config.ROLES.get(u["role"], ""),
        "services": db.allowed_services(u), "active": u["active"], "must_change_password": u["must_change_password"],
        "manager": u.get("manager", ""), "position": u.get("position", ""), "email": u.get("email", ""),
        "totp_enabled": bool(u["totp_secret"]), "created_at": u["created_at"], "last_login_at": u["last_login_at"],
    }


def log(request: Request, action: str, user: dict | None = None, **kw) -> None:
    db.audit(action, login=(user or {}).get("login", kw.pop("login", "")), ip=client_ip(request), **kw)


def safe_next(nxt: str | None) -> str:
    if nxt and nxt.startswith("/") and not nxt.startswith("//") and "\\" not in nxt:
        return nxt
    return "/"


@app.middleware("http")
async def guard(request: Request, call_next):
    if request.method in UNSAFE_METHODS and not request.url.path.startswith("/internal/") and not origin_ok(request):
        return err("Запрос с чужого сайта отклонён", 403)
    response = await call_next(request)
    # После выкладки браузер не должен показывать старые стили и скрипты: разрешаем кэш,
    # но с обязательной проверкой у сервера (no-cache = «спроси, не изменилось ли»).
    if request.url.path.startswith("/static/"):
        response.headers.setdefault("Cache-Control", "no-cache")
    return response


@app.get("/healthz", response_class=PlainTextResponse)
def healthz():
    db.conn().close()
    return "ok"


# ── вход ────────────────────────────────────────────────────────────────

class LoginIn(BaseModel):
    login: str = Field(max_length=100)
    password: str = Field(max_length=200)
    code: str = Field(default="", max_length=20)


@app.get("/login")
def login_page(request: Request):
    return page("login.html")


@app.post("/api/login")
def api_login(body: LoginIn, request: Request):
    ip, login = client_ip(request), body.login.strip()
    if auth.login_blocked(ip, login):
        return err("Слишком много неудачных попыток. Подождите 15 минут.", 429)
    user = auth.check_credentials(login, body.password)
    if not user:
        auth.register_failure(ip, login)
        # В поле логина легко набрать пароль, поэтому неизвестный логин в журнал не пишем.
        known = bool(db.get_user(login))
        db.audit("Неудачная попытка входа", login=login[:40] if known else "", ip=ip,
                 details="" if known else "логин не найден")
        return err("Неверный логин или пароль", 401)
    if user["totp_secret"]:
        if not body.code.strip():
            return JSONResponse({"need_code": True, "detail": "Введите код из приложения-аутентификатора"}, status_code=401)
        if not auth.verify_totp(user["totp_secret"], body.code, user["login"]):
            auth.register_failure(ip, login)
            db.audit("Неверный код 2FA", login=user["login"], ip=ip)
            return JSONResponse({"need_code": True, "detail": "Неверный код"}, status_code=401)
    auth.clear_failures(login)
    db.update_user(user["login"], last_login_at=db.now_iso())
    log(request, "Вход", user)
    resp = JSONResponse({"ok": True, "must_change_password": user["must_change_password"]})
    resp.set_cookie(auth.SESSION_COOKIE, auth.make_session(user), max_age=auth.SESSION_TTL, path="/",
                    httponly=True, samesite="lax", secure=config.secure_cookies())
    return resp


@app.post("/api/logout")
def api_logout(request: Request):
    user = current_user(request)
    if user:
        log(request, "Выход", user)
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(auth.SESSION_COOKIE, path="/")
    return resp


# ── проверка для nginx ──────────────────────────────────────────────────

def admin_write_allowed(service: str, method: str, request: Request) -> bool:
    """Изменяющий запрос администратора: только необратимые действия и настройки сервиса.

    Документы создаются и правятся под личными учётными записями — так в журнале
    остаётся конкретный человек, а не «admin».
    """
    uri = request.headers.get("x-original-uri", "").split("?")[0]
    path = uri[len(f"/{service}"):] if uri.startswith(f"/{service}/") else uri
    return any(m == method and re.fullmatch(pattern, path)
               for m, pattern in config.ADMIN_WRITE_PATHS.get(service, []))


@app.get("/auth/check/{service}")
def auth_check(service: str, request: Request):
    """nginx auth_request: 200 — пропустить (с данными пользователя), 401 — на вход, 403 — нет доступа."""
    user = current_user(request)
    if not user:
        return Response(status_code=401)
    # Причина отказа уезжает кодом в заголовке: nginx подставляет по ней понятный текст,
    # иначе человек видит «нет доступа» там, где на самом деле «под админом не правят».
    if service not in config.SERVICE_IDS or service not in db.allowed_services(user):
        return Response(status_code=403, headers={"X-Deny-Reason": "no-access"})
    if user["must_change_password"]:
        return Response(status_code=403, headers={"X-Deny-Reason": "change-password"})
    # Кто и что может менять — решается здесь, одним правилом на все сервисы.
    # Метод и адрес исходного запроса nginx кладёт в X-Original-Method и X-Original-URI.
    method = request.headers.get("x-original-method", "GET").upper()
    if method not in ("GET", "HEAD", "OPTIONS"):
        if user["role"] in config.READ_ONLY_ROLES:
            return Response(status_code=403, headers={"X-Deny-Reason": "read-only"})
        if user["role"] == "admin" and not admin_write_allowed(service, method, request):
            # админ администрирует, а не ведёт документы
            return Response(status_code=403, headers={"X-Deny-Reason": "admin-readonly"})
    # Круг показа: кто под этим человеком по цепочке. Сервисы по нему делят списки
    # на «мои / отдела / все». Это фильтр показа, а не запрет (решение пользователя 19.09).
    team = "*" if user["role"] == "admin" else ",".join(db.team_of(user["login"]))
    return Response(status_code=200, headers={
        "X-Portal-User": user["login"],
        "X-Portal-Role": user["role"],
        "X-Portal-Name": quote(user["full_name"] or user["login"]),
        "X-Portal-Team": team,
    })


# ── журнал от сервисов ──────────────────────────────────────────────────

class AuditIn(BaseModel):
    service: str = Field(max_length=40)
    login: str = Field(default="", max_length=100)
    action: str = Field(max_length=200)
    object: str = Field(default="", max_length=300)
    details: str = Field(default="", max_length=4000)
    ip: str = Field(default="", max_length=64)
    # Расход «Искры» кладётся в ту же запись, что и само действие: тогда видно, кто и на что потратил.
    tokens_in: int = Field(default=0, ge=0, le=100_000_000)
    tokens_out: int = Field(default=0, ge=0, le=100_000_000)


@app.get("/internal/users")
def internal_users(request: Request):
    """Список сотрудников для сервисов: окно «Доступ» и подпись «чьё это».

    Отдаём только логин, имя и признак «работает» — ни паролей, ни прав, ни журнала.
    """
    if not hmac.compare_digest(request.headers.get("x-portal-secret", ""), config.portal_secret()):
        return err("forbidden", 403)
    return {"users": [{"login": u["login"], "name": u["full_name"], "active": u["active"],
                       "manager": u.get("manager", "")}
                      for u in db.list_users()]}


# ── реестр людей: один на всё пространство (решение пользователя 17.09) ───
# Сервисы обращаются к нему со своего сервера — разбор документа идёт там, браузера
# у этого места нет (контракт, п. 9: секрет и 127.0.0.1 — там, где браузера нет).

def _secret_ok(request: Request) -> bool:
    return hmac.compare_digest(request.headers.get("x-portal-secret", ""), config.portal_secret())


class PersonIn(BaseModel):
    name: str = Field(max_length=300)
    role: str = Field(default="", max_length=500)
    organization: str = Field(default="", max_length=500)
    force: bool = False
    login: str = Field(default="", max_length=100)
    source: str = Field(default="", max_length=40)
    document: str = Field(default="", max_length=300)   # «Повестка … от ДД.ММ.ГГГГ» — откуда человек взят


@app.get("/internal/people")
def internal_people_search(request: Request, q: str = "", limit: int = 20):
    if not _secret_ok(request):
        return err("forbidden", 403)
    return {"items": people.search(q, limit)}


@app.get("/internal/people/one")
def internal_people_get(request: Request, name: str = ""):
    if not _secret_ok(request):
        return err("forbidden", 403)
    card = people.get(name)
    return card if card else err("Нет такого человека в реестре", 404)


@app.post("/internal/people")
def internal_people_remember(body: PersonIn, request: Request):
    """Запомнить человека. `force` — правка руками, без него только дозаполняется пустое."""
    if not _secret_ok(request):
        return err("forbidden", 403)
    if body.force and not body.login.strip():
        return err("Запись реестра подтверждает человек: передайте login того, кто нажал «Добавить»")
    suggest = people.suggest(body.name, body.role, body.organization)
    summary = people.remember(body.name, body.role, body.organization, force=body.force,
                              login=body.login, source=body.source, document=body.document)
    return {"summary": summary, "suggest": "" if body.force else suggest}


class PersonForgetIn(BaseModel):
    name: str = Field(max_length=300)
    login: str = Field(max_length=100)


@app.post("/internal/people/forget")
def internal_people_forget(body: PersonForgetIn, request: Request):
    """Стереть запись — необратимо, поэтому только от имени администратора."""
    if not _secret_ok(request):
        return err("forbidden", 403)
    user = db.get_user(body.login)
    if not user or user["role"] != "admin":
        return err("Стереть запись реестра может только администратор", 403)
    return {"forgotten": people.forget(body.name, login=body.login)}


@app.get("/api/people")
def api_people(request: Request, q: str = ""):
    user, e = need_api_user(request)
    if e:
        return e
    items = people.search(q, 50) if q.strip() else people.entries()
    return {"items": items, "total": people.count()}


class PersonEditIn(BaseModel):
    name: str = Field(max_length=300)
    role: str = Field(default="", max_length=500)
    organization: str = Field(default="", max_length=500)
    document: str = Field(default="", max_length=300)   # откуда человек: сервис передаёт из окна «Добавить в реестр?»
    source: str = Field(default="portal", max_length=40)


@app.post("/api/people")
def api_people_edit(body: PersonEditIn, request: Request):
    """Правка руками или выбор варианта из расхождений: решение человека, оно главнее разбора."""
    user, e = need_api_user(request)
    if e:
        return e
    # Реестр людей — справочник, а не документ: его чистит и администратор
    # (решение пользователя 21.09 — «выводи в админку реестр всех ФИО с должностями»).
    source = body.source if body.source in config.SERVICE_IDS else "portal"
    summary = people.remember(body.name, body.role, body.organization, force=True,
                              login=user["login"], source=source, document=body.document)
    return {"summary": summary, "person": people.get(body.name)}


@app.post("/api/admin/people/parse")
async def admin_people_parse(request: Request):
    """Разобрать список участников (.docx) и показать, что нашлось. В реестр ничего не пишет:
    записывает только «Добавить отмеченные» — решение администратора."""
    admin, e = need_admin(request)
    if e:
        return e
    data = await request.body()
    if len(data) > 20 * 1024 * 1024:
        return err("Файл больше 20 МБ — это точно список участников?")
    try:
        found = people_import.parse_docx(data)
    except ValueError as ex:
        return err(str(ex))
    for p in found:
        # сверка та же, что перед выводом в документ: «Иванов И.И.» узнаётся, опечатка — тоже
        ch = people.check(p["name"])
        card = ch.get("person") if ch["status"] == "exact" else None
        p["current"] = {"name": card["name"], "role": card["role"], "organization": card["organization"]} if card else None
        if card:
            p["status"] = people.suggest(card["name"], p["role"], p["organization"])
        elif ch["status"] == "close":
            p["status"] = "close"
            p["similar"] = [s["name"] for s in ch.get("suggestions", [])][:3]
        else:
            p["status"] = "new"
    return {"items": found}


class PeopleImportIn(BaseModel):
    items: list[PersonEditIn] = Field(default_factory=list, max_length=1000)


@app.post("/api/admin/people/import")
def admin_people_import(body: PeopleImportIn, request: Request):
    """Записать отмеченных. Каждый — от имени администратора, с файлом-источником."""
    admin, e = need_admin(request)
    if e:
        return e
    added = changed = 0
    for p in body.items:
        existed = people.get(p.name) is not None
        if people.remember(p.name, p.role, p.organization, force=True, login=admin["login"],
                           source="portal", document=p.document):
            changed += existed
            added += not existed
    return {"added": added, "changed": changed, "total": people.count()}


class NamesIn(BaseModel):
    names: list[str] = Field(default_factory=list, max_length=500)


@app.post("/internal/people/check")
def internal_people_check(body: NamesIn, request: Request):
    """Обязательная сверка ФИО с реестром перед выводом в документ (решение пользователя 21.09).

    exact — такой человек в реестре есть (в том числе в короткой записи «Фамилия И.О.»);
    close — точного нет, но есть похожий: почти наверняка опечатка, выводить нельзя, пока
            человек не выберет; unknown — никого похожего, новый человек.
    """
    if not _secret_ok(request):
        return err("forbidden", 403)
    return {"items": [people.check(n) for n in body.names if (n or "").strip()]}


@app.post("/api/people/check")
def api_people_check(body: NamesIn, request: Request):
    user, e = need_api_user(request)
    if e:
        return e
    return {"items": [people.check(n) for n in body.names if (n or "").strip()]}


@app.get("/api/people/duplicates")
def api_people_duplicates(request: Request):
    """Записи, похожие на одного человека: разбор дал опечатку, и она осела рядом с правильной."""
    user, e = need_api_user(request)
    if e:
        return e
    return {"items": people.duplicates()}


class MergeIn(BaseModel):
    keep: str = Field(max_length=300)
    drop: str = Field(max_length=300)


@app.post("/api/people/merge")
def api_people_merge(body: MergeIn, request: Request):
    """Слить две записи одного человека. Необратимо — поэтому только администратору."""
    user, e = need_api_user(request)
    if e:
        return e
    if user["role"] != "admin":
        return err("Объединять записи реестра может только администратор", 403)
    summary = people.merge(body.keep, body.drop, login=user["login"])
    if not summary:
        return err("Не нашлось одной из записей", 404)
    return {"summary": summary}


@app.delete("/api/people")
def api_people_forget(request: Request, name: str = ""):
    user, e = need_api_user(request)
    if e:
        return e
    if user["role"] != "admin":
        return err("Стереть запись может только администратор", 403)
    return {"forgotten": people.forget(name, login=user["login"])}


@app.post("/internal/audit")
def internal_audit(body: AuditIn, request: Request):
    if not hmac.compare_digest(request.headers.get("x-portal-secret", ""), config.portal_secret()):
        return err("forbidden", 403)
    db.audit(body.action, login=body.login, service=body.service, obj=body.object, details=body.details, ip=body.ip,
             tokens_in=body.tokens_in, tokens_out=body.tokens_out)
    return {"ok": True}


# ── страницы ────────────────────────────────────────────────────────────

def need_page_user(request: Request, allow_must_change: bool = False):
    user = current_user(request)
    if not user:
        return None, RedirectResponse("/login?next=" + quote(request.url.path), status_code=302)
    if user["must_change_password"] and not allow_must_change:
        return None, RedirectResponse("/account?force=1", status_code=302)
    return user, None


@app.get("/")
def home(request: Request):
    user, redirect = need_page_user(request)
    return redirect or page("home.html")


@app.get("/account")
def account_page(request: Request):
    user, redirect = need_page_user(request, allow_must_change=True)
    return redirect or page("account.html")


@app.get("/admin")
def admin_page(request: Request):
    user, redirect = need_page_user(request)
    if redirect:
        return redirect
    if user["role"] != "admin":
        return RedirectResponse("/", status_code=302)
    return page("admin.html")


@app.get("/summary")
def summary_page(request: Request):
    """Сводка за месяц: кто сколько сделал. Для администратора."""
    user, redirect = need_page_user(request)
    if redirect:
        return redirect
    if user["role"] != "admin":
        # Молча уводить на главную — значит оставить человека гадать, что произошло.
        return RedirectResponse("/?denied=summary", status_code=302)
    return page("summary.html")


# ── API: текущий пользователь ───────────────────────────────────────────

def need_api_user(request: Request, allow_must_change: bool = False):
    user = current_user(request)
    if not user:
        return None, err("Требуется вход", 401)
    if user["must_change_password"] and not allow_must_change:
        return None, err("Сначала смените пароль", 403)
    return user, None


def _place_named(login: str) -> dict:
    """Место человека в лестнице — с именами, а не логинами: логин человеку ничего не говорит."""
    place = db.place_of(login)
    people = {u["login"]: u for u in db.list_users()}

    def card(who: str) -> dict:
        u = people.get(who) or {}
        return {"login": who, "name": u.get("full_name") or who, "position": u.get("position", "")}

    return {
        "level": place["level"], "levels": place["levels"],
        "manager": card(place["manager"]) if place["manager"] else None,
        "chain": [card(x) for x in place["chain"]],
        "direct": [card(x) for x in place["direct"]],
        "team_size": len(place["team"]),
    }


@app.get("/api/me")
def api_me(request: Request):
    user, e = need_api_user(request, allow_must_change=True)
    if e:
        return e
    allowed = db.allowed_services(user)
    # сервис в разработке (hidden) виден на стартовой только администратору
    visible = [s for s in config.SERVICES if not s.get("hidden") or user["role"] == "admin"]
    return {**public_user(user), "all_services": config.SERVICES,
            "service_list": [s for s in visible if s["id"] in allowed],
            "place": _place_named(user["login"])}


# ── объявления и версии: общая шапка всех сервисов (решение пользователя 02.10) ──

@app.get("/api/notice")
def api_notice(request: Request):
    """Действующие объявления: от выкладки (каталог уведомлений) и от администратора (база).

    Шапка спрашивает раз в 30 секунд из любого сервиса. `now` — время сервера: по нему
    страница считает «через N с» до начала работ, а не по часам компьютера.
    """
    user, e = need_api_user(request, allow_must_change=True)
    if e:
        return e
    return JSONResponse({"items": notices.active(), "now": notices.now().isoformat(timespec="seconds")},
                        headers={"Cache-Control": "no-store"})


@app.get("/api/version")
def api_version(request: Request):
    """Какие коммиты выложены (пишет выкладка в versions.json) — по ним открытая страница
    узнаёт, что её код устарел, и предлагает обновиться."""
    user, e = need_api_user(request, allow_must_change=True)
    if e:
        return e
    versions = notices.versions()
    return JSONResponse({"versions": versions,
                         "portal": {**versions.get("portal", {"commit": "", "at": ""}), "started": notices.BOOT}},
                        headers={"Cache-Control": "no-store"})


class NoticeIn(BaseModel):
    text: str = Field(max_length=2000)
    kind: str = Field(default="info", max_length=20)
    service: str = Field(default="", max_length=40)    # пусто или «all» — все сервисы
    until: str = Field(default="", max_length=40)      # пусто — до снятия


@app.get("/api/admin/notices")
def admin_notices_list(request: Request):
    admin, e = need_admin(request)
    if e:
        return e
    return {"items": notices.admin_notices(include_ended=20), "deploy": notices.file_notices(),
            "kinds": notices.KIND_NAMES,
            "services": [{"id": "portal", "name": "Портал"}] + [{"id": s["id"], "name": s["name"]}
                                                                  for s in config.SERVICES]}


@app.post("/api/admin/notices")
def admin_notices_create(body: NoticeIn, request: Request):
    admin, e = need_admin(request)
    if e:
        return e
    text = " ".join(body.text.split())
    if not text:
        return err("Впишите текст объявления")
    if len(text) > notices.MAX_TEXT:
        return err(f"Объявление длиннее {notices.MAX_TEXT} знаков — сократите: оно должно помещаться в строку")
    if body.kind not in notices.KINDS:
        return err("Неизвестный вид объявления")
    service = "" if body.service in ("", "all") else body.service
    if service and service != "portal" and service not in config.SERVICE_IDS:
        return err("Неизвестный сервис")
    until = ""
    if body.until.strip():
        t = notices.parse_time(body.until)
        if t is None:
            return err("Срок показа записан с ошибкой")
        if t <= notices.now():
            return err("Срок показа уже прошёл — выберите время в будущем")
        until = t.isoformat(timespec="seconds")
    item, created = notices.create(text, body.kind, service, until, admin["login"])
    if created:
        log(request, "Объявление добавлено", admin, obj=f"объявление {item['number']}: {text[:150]}",
            details=f"вид: {notices.KIND_NAMES[body.kind]}; для: {notices.service_name(service)}; "
                    f"до: {until or 'снятия'}")
    return {"ok": True, "created": created, "item": item}


@app.delete("/api/admin/notices/{number}")
def admin_notices_remove(number: int, request: Request):
    admin, e = need_admin(request)
    if e:
        return e
    item, removed = notices.remove(number, admin["login"])
    if item is None:
        return err("Такого объявления нет", 404)
    if removed:
        log(request, "Объявление снято", admin, obj=f"объявление {number}: {item['text'][:150]}",
            details=f"вид: {notices.KIND_NAMES.get(item['kind'], item['kind'])}; для: {item['service_name']}")
    return {"ok": True, "removed": removed}


class PasswordChangeIn(BaseModel):
    old_password: str = Field(max_length=200)
    new_password: str = Field(max_length=200)


@app.post("/api/me/password")
def api_change_password(body: PasswordChangeIn, request: Request):
    user, e = need_api_user(request, allow_must_change=True)
    if e:
        return e
    if not auth.verify_password(body.old_password, user["password_hash"]):
        # Не засчитываем в блокировку входа: иначе человек запирает сам себя на 15 минут.
        return err("Текущий пароль указан неверно")
    problem = auth.password_problem(body.new_password, user["login"])
    if problem:
        return err(problem)
    if auth.verify_password(body.new_password, user["password_hash"]):
        return err("Новый пароль совпадает с текущим")
    db.update_user(user["login"], password_hash=auth.hash_password(body.new_password), must_change_password=False)
    db.bump_session_gen(user["login"])
    fresh = db.get_user(user["login"])
    log(request, "Смена своего пароля", user)
    resp = JSONResponse({"ok": True})
    resp.set_cookie(auth.SESSION_COOKIE, auth.make_session(fresh), max_age=auth.SESSION_TTL, path="/",
                    httponly=True, samesite="lax", secure=config.secure_cookies())
    return resp


class MyProfileIn(BaseModel):
    full_name: str = Field(default="", max_length=200)
    position: str = Field(default="", max_length=200)


@app.post("/api/me/profile")
def api_my_profile(body: MyProfileIn, request: Request):
    """Своё имя и должность человек пишет сам.

    Должность нигде больше не заводится: администратор создаёт учётную запись, а кем человек
    работает — знает он сам, и по должности его узнают в списках быстрее, чем по фамилии.
    """
    user, e = need_api_user(request, allow_must_change=True)
    if e:
        return e
    name = " ".join((body.full_name or "").split())
    position = " ".join((body.position or "").split())
    if not name:
        return err("Имя не может быть пустым")
    db.update_user(user["login"], full_name=name, position=position)
    log(request, "Изменён свой профиль", user,
        details=f"имя: {user['full_name']} → {name}; должность: {user.get('position','')} → {position}")
    return {"ok": True, "full_name": name, "position": position}


@app.post("/api/me/logout-all")
def api_logout_all(request: Request):
    user, e = need_api_user(request, allow_must_change=True)
    if e:
        return e
    db.bump_session_gen(user["login"])
    log(request, "Выход на всех устройствах", user)
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(auth.SESSION_COOKIE, path="/")
    return resp


# ── ключ приёмной: документы с телефона ─────────────────────────────────
# 98 % документов приходит в мессенджере, часто с телефона. Ключ позволяет быстрой
# команде положить файл в «Приёмную» хранилища, не вводя пароль и не проходя 2FA.
# Он только кладёт документы: ничего не читает, ничего не меняет, отзывается одной кнопкой.

@app.get("/api/me/inbox-key")
def api_inbox_key(request: Request):
    user, e = need_api_user(request)
    if e:
        return e
    return {"key": user.get("inbox_key", ""), "login": user["login"],
            "url": "https://workspace.example.ru/materials/api/inbox"}


@app.post("/api/me/inbox-key")
def api_inbox_key_new(request: Request):
    user, e = need_api_user(request)
    if e:
        return e
    # Администратор документы не ведёт (контракт, п. 2) — значит и складывать ему некуда.
    if user["role"] == "admin":
        return err("Документы ведут под личными учётными записями — ключ приёмной "
                   "администратору не выдаётся", 403)
    key = secrets.token_hex(24)
    db.update_user(user["login"], inbox_key=key)
    log(request, "Выдан ключ приёмной", user)
    return {"key": key}


@app.delete("/api/me/inbox-key")
def api_inbox_key_drop(request: Request):
    user, e = need_api_user(request)
    if e:
        return e
    db.update_user(user["login"], inbox_key="")
    log(request, "Отозван ключ приёмной", user)
    return {"ok": True}


@app.get("/internal/inbox-user")
def internal_inbox_user(key: str = "", request: Request = None):
    """Кому принадлежит ключ приёмной. Спрашивает сервис «Материалы» с общим секретом.

    Отдаём ровно то, что сервис положил бы в заголовки X-Portal-*, если бы человек
    пришёл через nginx: этот путь идёт мимо проверки доступа, поэтому доступ к сервису
    проверяем здесь же.
    """
    if not hmac.compare_digest(request.headers.get("x-portal-secret", ""), config.portal_secret()):
        return err("forbidden", 403)
    user = db.user_by_inbox_key(key)
    if not user or "materials" not in db.allowed_services(user):
        return err("Ключ не подошёл", 404)
    return {"login": user["login"], "role": user["role"], "name": user["full_name"] or user["login"],
            "team": "*" if user["role"] == "admin" else ",".join(db.team_of(user["login"]))}


@app.post("/api/me/totp/start")
def api_totp_start(request: Request):
    user, e = need_api_user(request)
    if e:
        return e
    secret = auth.new_totp_secret()
    db.update_user(user["login"], totp_pending=secret)
    uri = auth.totp_uri(secret, user["login"])
    return {"secret": secret, "uri": uri, "qr": qr_data_url(uri)}


class CodeIn(BaseModel):
    code: str = Field(max_length=20)


@app.post("/api/me/totp/confirm")
def api_totp_confirm(body: CodeIn, request: Request):
    user, e = need_api_user(request)
    if e:
        return e
    if auth.login_blocked(client_ip(request), user["login"]):
        return err("Слишком много попыток. Подождите 15 минут.", 429)
    if not user["totp_pending"] or not auth.verify_totp(user["totp_pending"], body.code, user["login"]):
        auth.register_failure(client_ip(request), user["login"])
        return err("Код не подошёл. Проверьте время на телефоне и введите свежий код.")
    db.update_user(user["login"], totp_secret=user["totp_pending"], totp_pending="")
    log(request, "Включена двухфакторная проверка", user)
    return {"ok": True}


@app.post("/api/me/totp/disable")
def api_totp_disable(body: CodeIn, request: Request):
    user, e = need_api_user(request)
    if e:
        return e
    if auth.login_blocked(client_ip(request), user["login"]):
        return err("Слишком много попыток. Подождите 15 минут.", 429)
    if not user["totp_secret"] or not auth.verify_totp(user["totp_secret"], body.code, user["login"]):
        auth.register_failure(client_ip(request), user["login"])
        return err("Код не подошёл")
    db.update_user(user["login"], totp_secret="", totp_pending="")
    log(request, "Отключена двухфакторная проверка", user)
    return {"ok": True}


def qr_data_url(text: str) -> str:
    try:
        import segno
    except ImportError:
        return ""
    return segno.make(text, error="m").svg_data_uri(scale=5, border=2)


@app.get("/api/summary")
def api_summary(request: Request):
    """Сводка за месяц по журналу: для администратора."""
    user, e = need_api_user(request)
    if e:
        return e
    if user["role"] != "admin":
        return err("Только для администратора", 403)
    months = db.months_with_work()
    month = request.query_params.get("month", "")
    if not re.fullmatch(r"\d{4}-\d{2}", month or ""):
        month = months[0] if months else db.now_iso()[:7]
    data = db.summary(month)
    data["months"] = months
    names = {u["login"]: u["full_name"] for u in db.list_users()}
    service_names = {s["id"]: s["name"] for s in config.SERVICES}
    for row in data["by_login"]:
        row["name"] = names.get(row["key"], "")
    for row in data["by_service"]:
        row["name"] = service_names.get(row["key"], row["key"])
    data["iskra"] = db.iskra_usage(month)["total"]
    return data


@app.get("/api/waiting-count")
def api_waiting_count(request: Request):
    """Сколько документов ждёт разбора в «Приёмной» — для значка на плитке «Материалов».

    Документы приходят по одному и в разное время, а человек заходит в портал не ради
    хранилища. Значок на плитке — единственное место, где он увидит, что накопилось.
    Спрашиваем сервис от имени того же человека: прав это не расширяет. Сервис молчит
    или не умеет — значка просто нет, главная из-за этого не ломается.
    """
    user, e = need_api_user(request)
    if e:
        return e
    if "materials" not in db.allowed_services(user):
        return {"count": 0}
    data = package.ask_json("materials", "/api/waiting", user, timeout=3)
    return {"count": len(data.get("items") or []) if isinstance(data, dict) else 0}


@app.get("/api/me/recent")
def api_recent(request: Request):
    """Над чем человек работал последним — для главной страницы."""
    user, e = need_api_user(request)
    if e:
        return e
    names = {s["id"]: s["name"] for s in config.SERVICES}
    urls = {s["id"]: s["url"] for s in config.SERVICES}
    allowed = set(db.allowed_services(user))
    return {"items": [{**row, "name": names.get(row["service"], row["service"]),
                       "url": urls.get(row["service"], "/")}
                      for row in db.recent_work(user["login"]) if row["service"] in allowed]}


@app.get("/people")
def people_page(request: Request):
    """Реестр людей: участники и докладчики совещаний, один на всё пространство."""
    user, redirect = need_page_user(request)
    return redirect or page("people.html")


@app.get("/orders")
def orders_page(request: Request):
    """Реестр поручений: все поручения со всех совещаний в одном списке."""
    user, redirect = need_page_user(request)
    if redirect:
        return redirect
    if "moderator" not in db.allowed_services(user):
        return RedirectResponse("/", status_code=302)
    return page("orders.html")


@app.get("/api/upcoming")
def api_upcoming(request: Request):
    """Ближайшие совещания с готовностью — блок напоминания на главной."""
    user, e = need_api_user(request)
    if e:
        return e
    if "moderator" not in db.allowed_services(user):
        return {"meetings": []}
    data = package.ask_json("moderator", "/api/upcoming?days=14", user, timeout=3)
    return data if data is not None else {"meetings": []}


@app.get("/api/orders")
def api_orders(request: Request):
    """Поручения из «Модератора» — портал только пересказывает их человеку."""
    user, e = need_api_user(request)
    if e:
        return e
    if "moderator" not in db.allowed_services(user):
        return err("Нет доступа к сервису «Совещание»", 403)
    query = ""
    if request.query_params.get("who") == "me":
        query = "?who=" + quote(user["full_name"] or user["login"])
    elif request.query_params.get("overdue") == "1":
        query = "?overdue=true"
    data = package.ask_json("moderator", "/api/orders" + query, user, timeout=3)
    if data is None:
        return err("Сервис «Совещание» не ответил — попробуйте позже", 503)
    return data


@app.get("/api/package/{meeting_id}")
def api_package(meeting_id: str, request: Request):
    """Пакет к совещанию одним архивом: бриф совещания, повестка, участники, список материалов."""
    user, e = need_api_user(request)
    if e:
        return e
    if "moderator" not in db.allowed_services(user):
        return err("Нет доступа к сервису «Совещание»", 403)
    try:
        name, body, missing = package.build(meeting_id, user)
    except LookupError as ex:
        return err(str(ex), 404)
    log(request, "Скачан пакет к совещанию", user, obj=meeting_id[:300],
        details=("не вошло: " + "; ".join(missing)) if missing else "полный пакет")
    return Response(body, media_type="application/zip", headers={
        "Content-Disposition": "attachment; filename=\"package.zip\"; filename*=UTF-8''" + quote(name),
    })


@app.get("/api/search")
def api_search(request: Request):
    """Поиск по всем сервисам, к которым у человека есть доступ."""
    user, e = need_api_user(request)
    if e:
        return e
    return search.search(user, request.query_params.get("q", "")[:100])


@app.get("/api/iskra")
def api_iskra(request: Request):
    """Лампочка «Искра на связи» на главной: состояние на сейчас, одно на всех."""
    user, e = need_api_user(request)
    if e:
        return e
    return ask.health()


class AskIn(BaseModel):
    question: str = Field(max_length=ask.MAX_QUESTION)


@app.post("/api/ask")
def api_ask(body: AskIn, request: Request):
    """Вопрос «Искре» о рабочем пространстве — с главной страницы."""
    user, e = need_api_user(request)
    if e:
        return e
    if ask.rate_limited(user["login"]):
        return err("Слишком много вопросов за час. Попробуйте позже.", 429)
    try:
        answer = ask.ask(body.question)
    except ask.AskInputError as ex:
        return err(str(ex), 400)                   # отказ до Искры — лампочку не трогает (ревью RC-15)
    except ask.AskError as ex:
        ask.note_result(False, str(ex)[:120])      # живой отказ — самая точная проверка для лампочки
        return err(str(ex), 503)
    ask.note_result(True)
    log(request, "Вопрос Искре", user, obj=body.question.strip()[:300],
        tokens_in=answer["tokens_in"], tokens_out=answer["tokens_out"])
    return answer


# ── API: администрирование ──────────────────────────────────────────────

def need_admin(request: Request):
    user, e = need_api_user(request)
    if e:
        return None, e
    if user["role"] != "admin":
        return None, err("Только для администратора", 403)
    return user, None


@app.get("/api/admin/users")
def admin_users(request: Request):
    admin, e = need_admin(request)
    if e:
        return e
    return {"users": [public_user(u) for u in db.list_users()], "services": config.SERVICES, "roles": config.ROLES}


class UserCreateIn(BaseModel):
    login: str = Field(default="", max_length=40)       # пусто — из фамилии
    full_name: str = Field(default="", max_length=200)
    password: str = Field(default="", max_length=200)   # пусто — придумает портал
    email: str = Field(default="", max_length=200)
    role: str = "employee"
    services: list[str] = Field(default_factory=list)
    manager: str = Field(default="", max_length=40)


def _services_text(services: list[str]) -> str:
    names = {s["id"]: s["name"] for s in config.SERVICES}
    return ", ".join(names[s] for s in services if s in names) or "нет"


def _email_problem(email: str) -> str:
    return "" if not email or mail.EMAIL_RE.match(email) else "Почта записана с ошибкой"


def _deliver(user: dict, password: str, *, reset: bool) -> dict:
    """Отправить логин и временный пароль на почту. Не ушло — пароль возвращается
    администратору, чтобы он передал его сам; ушло — на экране его нет."""
    email = user.get("email", "")
    if not email:
        return {"sent": False, "password": password, "why": "почта сотрудника не указана"}
    if not mail.configured():
        return {"sent": False, "password": password, "why": "почта портала не настроена (вкладка «Почта»)"}
    try:
        mail.send_credentials(email, user.get("full_name", ""), user["login"], password, reset=reset)
    except Exception as ex:          # письмо — не повод ронять создание пользователя
        return {"sent": False, "password": password, "why": mail.human_error(ex)}
    return {"sent": True, "email": email}


@app.get("/api/admin/users/suggest-login")
def admin_suggest_login(request: Request, full_name: str = ""):
    admin, e = need_admin(request)
    if e:
        return e
    return {"login": accounts.suggest_login(full_name)}


@app.post("/api/admin/users")
def admin_create_user(body: UserCreateIn, request: Request):
    admin, e = need_admin(request)
    if e:
        return e
    login = body.login.strip().lower() or accounts.suggest_login(body.full_name)
    if not login:
        return err("Впишите ФИО — логин составится из фамилии")
    if not LOGIN_RE.match(login):
        return err("Логин: 2–40 символов, латиница, цифры, точка, дефис, подчёркивание")
    if db.get_user(login):
        return err("Такой логин уже есть")
    if body.role not in config.ROLES:
        return err("Неизвестная роль")
    password = body.password or accounts.generate_password(login)
    problem = auth.password_problem(password, login)
    if problem:
        return err("Временный пароль: " + problem)
    email = body.email.strip()
    if _email_problem(email):
        return err(_email_problem(email))
    services = [s for s in body.services if s in config.SERVICE_IDS]
    manager = body.manager.strip()
    if manager:
        problem = db.manager_problem(login, manager)     # проверяем до создания, чтобы не откатывать
        if problem:
            return err(problem)
    db.create_user(login, auth.hash_password(password), full_name=body.full_name.strip(), role=body.role,
                   services=services, must_change_password=True)
    db.update_user(login, manager=manager, email=email)
    sent = _deliver(db.get_user(login), password, reset=False)
    log(request, "Создан пользователь", admin, obj=login,
        details=f"{body.full_name.strip()}; роль: {config.ROLES[body.role]}; сервисы: {_services_text(services)}"
                + (f"; руководитель: {manager}" if manager else "")
                + (f"; учётные данные отправлены на {email}" if sent["sent"] else f"; письмо не ушло: {sent['why']}"))
    return {"ok": True, "login": login, **sent}


class UserUpdateIn(BaseModel):
    full_name: str | None = Field(default=None, max_length=200)
    email: str | None = Field(default=None, max_length=200)
    role: str | None = None
    services: list[str] | None = None
    active: bool | None = None
    manager: str | None = Field(default=None, max_length=40)


@app.patch("/api/admin/users/{login}")
def admin_update_user(login: str, body: UserUpdateIn, request: Request):
    admin, e = need_admin(request)
    if e:
        return e
    target = db.get_user(login)
    if not target:
        return err("Пользователь не найден", 404)
    changes, fields = [], {}
    if body.full_name is not None and body.full_name.strip() != target["full_name"]:
        fields["full_name"] = body.full_name.strip()
        changes.append(f"ФИО: «{target['full_name']}» → «{fields['full_name']}»")
    if body.email is not None and body.email.strip() != target.get("email", ""):
        if _email_problem(body.email.strip()):
            return err(_email_problem(body.email.strip()))
        fields["email"] = body.email.strip()
        changes.append(f"почта: {target.get('email') or '—'} → {fields['email'] or '—'}")
    if body.role is not None and body.role != target["role"]:
        if body.role not in config.ROLES:
            return err("Неизвестная роль")
        if target["role"] == "admin" and db.count_admins() <= 1:
            return err("Нельзя снять роль с последнего администратора")
        fields["role"] = body.role
        changes.append(f"роль: {config.ROLES[target['role']]} → {config.ROLES[body.role]}")
    if body.services is not None:
        new = [s for s in config.SERVICE_IDS if s in body.services]
        if new != target["services"]:
            fields["services"] = new
            changes.append(f"сервисы: {_services_text(target['services'])} → {_services_text(new)}")
    if body.active is not None and body.active != target["active"]:
        if not body.active and target["role"] == "admin" and db.count_admins() <= 1:
            return err("Нельзя заблокировать последнего администратора")
        if not body.active and login == admin["login"]:
            return err("Нельзя заблокировать самого себя")
        fields["active"] = body.active
        changes.append("разблокирован" if body.active else "заблокирован")
    if body.manager is not None and body.manager.strip() != target.get("manager", ""):
        manager = body.manager.strip()
        problem = db.manager_problem(login, manager)
        if problem:
            return err(problem)
        fields["manager"] = manager
        changes.append(f"руководитель: {target.get('manager') or 'нет'} → {manager or 'нет'}")
    if not fields:
        return {"ok": True}
    db.update_user(login, **fields)
    if fields.get("active") is False:
        db.bump_session_gen(login)
    log(request, "Изменён пользователь", admin, obj=login, details="; ".join(changes))
    return {"ok": True}


class AdminPasswordIn(BaseModel):
    password: str = Field(default="", max_length=200)   # пусто — придумает портал


@app.post("/api/admin/users/{login}/password")
def admin_set_password(login: str, body: AdminPasswordIn, request: Request):
    admin, e = need_admin(request)
    if e:
        return e
    target = db.get_user(login)
    if not target:
        return err("Пользователь не найден", 404)
    password = body.password or accounts.generate_password(login)
    problem = auth.password_problem(password, login)
    if problem:
        return err("Временный пароль: " + problem)
    db.update_user(login, password_hash=auth.hash_password(password), must_change_password=True)
    db.bump_session_gen(login)
    sent = _deliver(target, password, reset=True)
    log(request, "Выдан временный пароль", admin, obj=login,
        details="пользователь сменит его при входе"
                + (f"; отправлен на {sent['email']}" if sent["sent"] else f"; письмо не ушло: {sent['why']}"))
    return {"ok": True, **sent}


class MailSettingsIn(BaseModel):
    user: str = Field(max_length=200)
    host: str = Field(default="", max_length=200)
    port: str = Field(default="465", max_length=5)
    from_name: str = Field(default="", max_length=100)
    password: str = Field(default="", max_length=200)   # пусто — оставить прежний


@app.get("/api/admin/mail")
def admin_mail_get(request: Request):
    admin, e = need_admin(request)
    if e:
        return e
    return mail.settings()


@app.post("/api/admin/mail")
def admin_mail_set(body: MailSettingsIn, request: Request):
    admin, e = need_admin(request)
    if e:
        return e
    user = body.user.strip()
    if not mail.EMAIL_RE.match(user):
        return err("Ящик отправителя записан с ошибкой")
    host = body.host.strip() or mail.guess_host(user)
    if not host:
        return err("Для этого ящика впишите сервер SMTP")
    if not body.port.strip().isdigit():
        return err("Порт — число, обычно 465")
    config.write_env("SMTP_USER", user)
    config.write_env("SMTP_HOST", host)
    config.write_env("SMTP_PORT", body.port.strip())
    config.write_env("SMTP_FROM_NAME", body.from_name.strip())
    if body.password:
        config.write_env("SMTP_PASSWORD", "".join(body.password.split()))
    log(request, "Изменены настройки почты", admin,
        details=f"ящик {user}, сервер {host}" + ("; пароль заменён" if body.password else ""))
    return {"ok": True, **mail.settings()}


class MailTestIn(BaseModel):
    to: str = Field(max_length=200)


@app.post("/api/admin/mail/test")
def admin_mail_test(body: MailTestIn, request: Request):
    admin, e = need_admin(request)
    if e:
        return e
    if not mail.EMAIL_RE.match(body.to.strip()):
        return err("Адрес записан с ошибкой")
    if not mail.configured():
        return err("Сначала сохраните ящик и пароль")
    try:
        mail.send_test(body.to.strip())
    except Exception as ex:
        return err("Письмо не ушло: " + mail.human_error(ex))
    log(request, "Отправлено проверочное письмо", admin, details=body.to.strip())
    return {"ok": True}


@app.post("/api/admin/users/{login}/reset-2fa")
def admin_reset_2fa(login: str, request: Request):
    admin, e = need_admin(request)
    if e:
        return e
    if not db.update_user(login, totp_secret="", totp_pending=""):
        return err("Пользователь не найден", 404)
    log(request, "Сброшена двухфакторная проверка", admin, obj=login)
    return {"ok": True}


class UserDeleteIn(BaseModel):
    confirm: str = Field(max_length=40)   # логин, вписанный администратором руками


@app.post("/api/admin/users/{login}/delete")
def admin_delete_user(login: str, body: UserDeleteIn, request: Request):
    """Удалить насовсем — необратимо, поэтому логин подтверждается вводом, а не одной кнопкой."""
    admin, e = need_admin(request)
    if e:
        return e
    target = db.get_user(login)
    if not target:
        return err("Пользователь не найден", 404)
    if body.confirm.strip() != login:
        return err("Для подтверждения впишите логин точно: " + login)
    if login == admin["login"]:
        return err("Себя удалить нельзя — попросите другого администратора")
    if target["role"] == "admin" and target["active"] and db.count_admins() <= 1:
        return err("Нельзя удалить последнего администратора")
    orphans = db.delete_user(login)
    log(request, "Удалён пользователь", admin, obj=login,
        details=f"{target['full_name'] or '—'}; роль: {config.ROLES.get(target['role'], target['role'])}"
                + (f"; остались без руководителя: {', '.join(orphans)}" if orphans else ""))
    return {"ok": True, "orphans": orphans}


@app.post("/api/admin/users/{login}/logout-all")
def admin_logout_all(login: str, request: Request):
    admin, e = need_admin(request)
    if e:
        return e
    if not db.get_user(login):
        return err("Пользователь не найден", 404)
    db.bump_session_gen(login)
    log(request, "Завершены все сессии пользователя", admin, obj=login)
    return {"ok": True}


@app.get("/api/admin/audit")
def admin_audit(request: Request, login: str = "", service: str = "", q: str = "", date_from: str = "",
                date_to: str = "", limit: int = 100, offset: int = 0):
    admin, e = need_admin(request)
    if e:
        return e
    items, total = db.audit_list(login=login, service=service, q=q, date_from=date_from, date_to=date_to,
                                 limit=limit, offset=offset)
    return {"items": items, "total": total}


@app.get("/api/admin/audit.csv")
def admin_audit_csv(request: Request, login: str = "", service: str = "", q: str = "", date_from: str = "",
                    date_to: str = ""):
    admin, e = need_admin(request)
    if e:
        return e
    import csv
    import io
    items, _ = db.audit_list(login=login, service=service, q=q, date_from=date_from, date_to=date_to, limit=500)
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    def safe(v: str) -> str:
        # Excel считает формулой всё, что начинается с = + - @ — обезвреживаем.
        s = str(v or "")
        return "'" + s if s[:1] in ("=", "+", "-", "@") else s

    w.writerow(["Время", "Пользователь", "Сервис", "Действие", "Объект", "Подробности", "IP"])
    for it in items:
        w.writerow([safe(it["ts"]), safe(it["login"]), safe(it["service"]), safe(it["action"]),
                    safe(it["object"]), safe(it["details"]), safe(it["ip"])])
    log(request, "Выгружен журнал", admin)
    return Response("﻿" + buf.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": "attachment; filename=\"audit.csv\""})


@app.get("/api/admin/iskra-usage")
def admin_iskra_usage(request: Request):
    admin, e = need_admin(request)
    if e:
        return e
    months = db.iskra_usage_months()
    month = request.query_params.get("month", "")
    if not re.fullmatch(r"\d{4}-\d{2}", month or ""):
        month = months[0] if months else db.now_iso()[:7]
    data = db.iskra_usage(month)
    data["months"] = months
    names = {u["login"]: u["full_name"] for u in db.list_users()}
    for row in data["by_login"]:
        row["name"] = names.get(row["key"], "")
    return data


@app.get("/api/admin/status")
def admin_status(request: Request):
    admin, e = need_admin(request)
    if e:
        return e
    return status.collect(force=request.query_params.get("force") == "1")


@app.get("/api/admin/iskra-key")
def admin_iskra_key_get(request: Request):
    admin, e = need_admin(request)
    if e:
        return e
    return iskra_key.status()


class IskraKeyIn(BaseModel):
    key: str = Field(min_length=10, max_length=300)
    expires: str = Field(default="", max_length=20)   # дата окончания ключа (справка Искры: 7–365 дней, по умолчанию 30)


@app.post("/api/admin/iskra-key")
def admin_iskra_key_set(body: IskraKeyIn, request: Request):
    admin, e = need_admin(request)
    if e:
        return e
    try:
        iskra_key.parse_expires(body.expires)          # сначала проверяем дату — ключ не трогаем, если дата кривая
        result = iskra_key.set_key(body.key.strip())
        iskra_key.set_expires(body.expires)
    except iskra_key.KeyError_ as ex:
        return err(str(ex))
    log(request, "Заменён ключ Искры", admin, details="записан в: " + ", ".join(result["written"])
        + (f"; действует до {body.expires.strip()}" if body.expires.strip() else "; срок не указан"))
    return {"ok": True, **iskra_key.status()}


class IskraExpiresIn(BaseModel):
    expires: str = Field(default="", max_length=20)


@app.post("/api/admin/iskra-key/expires")
def admin_iskra_key_expires(body: IskraExpiresIn, request: Request):
    """Дата окончания действующего ключа — без замены самого ключа (ключ уже стоит, срок узнали в Искре)."""
    admin, e = need_admin(request)
    if e:
        return e
    try:
        info = iskra_key.set_expires(body.expires)
    except iskra_key.KeyError_ as ex:
        return err(str(ex))
    log(request, "Срок ключа Искры", admin, details=("до " + info["expires"]) if info["expires"] else "срок снят")
    return {"ok": True, **iskra_key.status()}


@app.post("/api/admin/iskra-key/spread")
def admin_iskra_key_spread(request: Request):
    """Свести разошедшиеся ключи: всем — тот, что стоит в портале."""
    admin, e = need_admin(request)
    if e:
        return e
    try:
        result = iskra_key.spread()
    except iskra_key.KeyError_ as ex:
        return err(str(ex))
    log(request, "Ключ Искры разослан всем сервисам", admin, details="записан в: " + ", ".join(result["written"]))
    return {"ok": True, **iskra_key.status()}


# ── A3 ──────────────────────────────────────────────────────────────────

app.include_router(a3.router)
