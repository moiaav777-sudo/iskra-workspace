"""Сервис «Материалы»: папка мероприятия, раскладка документов и реестр.

Порт 8970 на 127.0.0.1, адрес за nginx — `/materials/`. Своего входа нет: пользователь
приходит в заголовках `X-Portal-*`, см. `portal.py`.

Замысел целиком — в `../STORAGE-SPEC.md`. Коротко: человек заводит мероприятие и
сбрасывает в него документы как есть. Сервис читает каждый, предлагает человеческое
название и номер вопроса, расставляет номера по общим правилам и собирает
«00. Реестр материалов.docx». Повестки и брифа может ещё не быть — номера 01–04
просто ждут, а в карточке и в реестре видно, чего не хватает.
"""
from __future__ import annotations

import io
import logging
import re
import shutil
import time
import zipfile
from datetime import date, datetime
from pathlib import Path
from urllib.parse import quote, unquote, urlparse

from fastapi import FastAPI, Form, HTTPException, Query, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import (FileResponse, JSONResponse, PlainTextResponse, RedirectResponse,
                               Response)
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import convert, doc_text, folder, inbox, meeting, naming, portal, registry, routing, similar, store, tree

log = logging.getLogger("materials")
BASE_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = BASE_DIR / "static"
MAX_UPLOAD = 200 * 1024 * 1024

app = FastAPI(title="Материалы", docs_url=None, redoc_url=None)
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
# есть ли LibreOffice — узнаём при старте (и пишем в лог); дальше ответ живёт несколько минут
convert.binary()


def _tidy_convert_cache() -> None:
    """Кэш переводов мероприятий, которых уже нет, — при старте (ревьюер RC-2026-10-02-9, Н-7)."""
    try:
        convert.prune_orphans({e.id for e in tree.events()})
    except Exception as e:      # хранилище недоступно — сервис всё равно должен подняться
        log.warning("кэш переводов не прибран: %s", e)


_tidy_convert_cache()


# ── вход, журнал, защита от чужих сайтов ─────────────────────────────────

@app.middleware("http")
async def portal_gate(request: Request, call_next):
    path = request.url.path
    if path == "/healthz":
        return await call_next(request)

    user = portal.user_from_headers(request.headers)
    if not user and path.startswith("/api/inbox"):
        # приёмка идёт мимо входа по cookie: телефон приходит с ключом в заголовке,
        # и проверяет его сам сервис, спросив портал (STORAGE-SPEC, «Договор о приёмке»)
        user = portal.user_by_inbox_key(request.headers.get("x-inbox-key", ""))
        if not user:
            # `message` здесь не для красоты: телефон показывает уведомлением именно
            # это поле, и без него человек увидит пустое окно вместо объяснения
            return JSONResponse({"detail": "Ключ приёмной не подошёл",
                                 "message": "Ключ приёмной не подошёл — выдайте новый "
                                            "в «Моём профиле» и впишите его в команду"},
                                status_code=401)
    if not user:
        if path.startswith("/api/"):
            return JSONResponse({"detail": "Нужен вход через портал"}, status_code=401)
        return RedirectResponse(portal.LOGIN_URL, status_code=303)

    if request.method not in ("GET", "HEAD", "OPTIONS"):
        origin = request.headers.get("origin", "")
        if origin and urlparse(origin).netloc != request.headers.get("host", ""):
            return JSONResponse({"detail": "Запрос с чужого сайта отклонён"}, status_code=403)

    request.state.user = user
    bucket = portal.start_request()
    response = await call_next(request)

    action = bucket.get("action") or portal.describe(request.method, path)
    if action and response.status_code < 400:
        portal.send_audit(
            login=user["login"],
            action=action,
            # в журнале человек должен видеть мероприятие, а не ключ папки
            object=bucket["object"] or _named(portal.event_id(path)),
            details="; ".join(bucket["changes"]),
            ip=request.client.host if request.client else "",
        )
    return response


def _named(eid: str) -> str:
    """Объект для журнала. Название — последним: портал показывает в «Вы недавно
    работали» именно хвост после « · » (журнал часто начинается с номера)."""
    if not eid:
        return ""
    event = tree.find(eid)
    return f"{event.date} · {event.title}" if event else eid


@app.get("/healthz", response_class=PlainTextResponse)
def healthz() -> str:
    return "ok"


@app.get("/")
def index():
    page = STATIC_DIR / "index.html"
    if not page.exists():
        return PlainTextResponse("Интерфейс ещё не собран", status_code=503)
    return FileResponse(page)


# ── встраивание в «Совещание» (решение пользователя 02.10) ───────────────
#
# Вкладка «Пакет» в «Совещании» — это наша карточка мероприятия в iframe, а не копия
# логики. Страница та же (`index.html`), отличие — флаг `data-embed` и id папки на
# `<html>`: по ним `app.js` рисует одну карточку без списка, приёмной и шапки портала.
# Права те же, что у обычной страницы: путь идёт через `portal_gate` и `location
# /materials/` с auth_request. Встраивать можно только себе же (same-origin): nginx
# ставит `X-Frame-Options: SAMEORIGIN`, здесь — ещё и `frame-ancestors 'self'`.

_EMBED_ID = re.compile(r"[0-9a-f]{12}")        # id папки — 12 знаков sha1, см. tree.event_id
_EMBED_DROP = (
    # общая шапка портала: в iframe она вторая на экране, а объявления — вторые сверху
    re.compile(r'\s*<link[^>]*href="/static/workspace\.css"[^>]*>'),
    re.compile(r'\s*<script[^>]*src="/static/workspace-header\.js"[^>]*></script>'),
)


def _embed_page(eid: str) -> str | None:
    page = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    for pattern in _EMBED_DROP:
        page = pattern.sub("", page)
    html_tag = '<html lang="ru" data-theme="light">'
    if html_tag not in page or "<head>" not in page:
        return None             # разметку поменяли — лучше честная ошибка, чем полстраницы
    page = page.replace(html_tag, f'<html lang="ru" data-theme="light" data-embed="1" data-event="{eid}">', 1)
    # адрес страницы — /materials/embed/<id>, а все адреса в ней относительные
    # (`static/…`, `api/…`): без <base> они ушли бы в /materials/embed/api/…
    return page.replace("<head>", '<head>\n<base href="../">', 1)


@app.get("/embed/{eid}/")
def embed_slash(eid: str):
    # с косой чертой на конце <base href="../"> вёл бы в /materials/embed/, а своя
    # переадресация Starlette — в /embed/… без префикса; уводим относительным адресом
    if not _EMBED_ID.fullmatch(eid):
        raise HTTPException(status_code=404, detail="Мероприятие не найдено")
    return RedirectResponse(f"../{eid}", status_code=307)


@app.get("/embed/{eid}")
def embed(eid: str):
    if not _EMBED_ID.fullmatch(eid):
        raise HTTPException(status_code=404, detail="Мероприятие не найдено")
    page = _embed_page(eid) if (STATIC_DIR / "index.html").exists() else None
    if page is None:
        return PlainTextResponse("Интерфейс ещё не собран", status_code=503)
    # папки может уже не быть — тогда страница сама скажет об этом в карточке
    return Response(page, media_type="text/html; charset=utf-8", headers={
        "Content-Security-Policy": "frame-ancestors 'self'",
        "Cache-Control": "no-cache",
    })


@app.get("/api/me")
def me(request: Request):
    return request.state.user


# ── приёмная ─────────────────────────────────────────────────────────────
#
# Документ прилетает из мессенджера и своего мероприятия ещё не знает. Он ложится
# в общую папку «Приёмная» и сразу читается; дальше человек в «Приёмной» на странице
# отправляет его в мероприятие одним нажатием. Договор — в `../STORAGE-SPEC.md`.
# Вход сюда не по cookie, а по ключу приёмной: проверка — в `portal_gate` выше.

ACTIVE = "active_event"
ACTIVE_HOURS = 24


def _active(login: str) -> tree.Event | None:
    """Мероприятие, в которое человек попросил складывать присланное.

    Выбор живёт сутки. Иначе документ через неделю улетит в позапрошлое совещание,
    о котором человек давно забыл, — а он и не узнает: телефон скажет «принято».
    """
    eid, left = _active_left(login)
    return tree.find(eid) if eid and left > 0 else None


def _active_left(login: str) -> tuple[str, float]:
    """Ключ мероприятия и сколько часов ему осталось. Просрочено — забываем."""
    raw = store.setting(login, ACTIVE)
    if not raw:
        return "", 0.0
    eid, _, chosen = raw.partition("|")
    try:
        left = ACTIVE_HOURS - (time.time() - float(chosen)) / 3600
    except ValueError:
        left = 0.0
    if left <= 0:
        store.set_setting(login, ACTIVE, "")
        return "", 0.0
    return eid, left


def _already(event: tree.Event, data: bytes) -> folder.Item | None:
    """Точно такой же файл уже лежит в папке? Тогда вопроса человеку нет."""
    mark = similar.digest(data)
    for item in folder.documents(folder.items(event)):
        if store.about(event.path / item.name)["hash"] == mark:
            return item
    return None


DISPOSITION = re.compile(r'filename\*?=(?:UTF-8'')?"?([^";]+)"?', re.I)


def _readable(name: str) -> str:
    """Имя файла из заголовка. Заголовки читаются латиницей, а телефон шлёт
    «Справка.docx» как есть — кириллица приезжает крякозябрами. Разворачиваем."""
    try:
        return name.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return name


async def _incoming(request: Request) -> tuple[list[tuple[str, bytes]], str, str]:
    """Что прислал телефон: список «имя, содержимое», мероприятие, если названо,
    и слова рядом с документом («напиши бриф») — полем `note` или заголовком
    `X-Inbox-Note` (URL-кодирование, как у имени).

    Принимаем двумя способами, потому что «Быстрые команды» на iPhone умеют
    по-разному. Форма с полем `files` — как в договоре. Но собрать в команде
    файловое поле формы получается не у всех: телефон требует выбрать у файла
    текстовое свойство и отправляет строку вместо документа. Поэтому принимаем
    и документ целиком телом запроса — тогда в команде достаточно выбрать
    «Тело запроса: Файл», а имя приходит заголовком `X-Inbox-Name`.
    """
    kind = request.headers.get("content-type", "")
    if kind.startswith(("multipart/", "application/x-www-form-urlencoded")):
        form = await request.form()
        found = []
        for value in form.getlist("files"):
            if hasattr(value, "filename"):
                found.append((Path(value.filename or "документ").name, await value.read()))
        note = str(form.get("note") or "") or _readable(unquote(request.headers.get("x-inbox-note", "").strip()))
        return found, str(form.get("event") or ""), note

    body = await request.body()
    note = _readable(unquote(request.headers.get("x-inbox-note", "").strip()))
    if not body:
        return [], request.query_params.get("event", ""), note
    named = request.headers.get("x-inbox-name", "").strip()
    if not named:
        found = DISPOSITION.search(request.headers.get("content-disposition", ""))
        named = found.group(1) if found else ""
    named = _readable(unquote(named))
    return ([(Path(named or "Документ").name, body)],
            request.query_params.get("event", ""), note)


@app.post("/api/inbox")
async def inbox_take(request: Request):
    """Приём документа с телефона. Ответ короткий: телефон покажет его уведомлением."""
    user = request.state.user
    came, event, note = await _incoming(request)
    target = tree.find(event.strip()) if event.strip() else _active(user["login"])
    meant = target
    if note.strip():
        # со словами документ идёт через приёмную: оттуда браузер отправителя
        # отдаст его соседу, а в папке совещания команду выполнить некому.
        # Куда направляли — помним: «Повестке» нужно совещание
        target = None
    saved, twice, waiting = [], [], []
    for origin, data in came:
        if not data:
            continue
        if len(data) > MAX_UPLOAD:
            return _phone_error(413, f"«{origin}» больше 200 МБ")
        if target is not None:
            same = _already(target, data)
            if same is not None:                 # тот же файл байт в байт — не плодим копии
                twice.append(same.name)
                continue
            twin = _looks_like_version(target, origin, data)
            if twin is not None:
                # похоже на прежнюю версию того, что уже лежит. Молча класть нельзя:
                # это или новая редакция, или дополнение, или другой документ, —
                # решает человек в портале, а телефон только сообщает
                kept = inbox.save(origin, data)
                inbox.meant_for(kept, target.id)
                inbox.sender_of(kept, user["login"])
                waiting.append((kept, twin))
                continue
            saved.append(_save(target, origin, data, as_is=True))
        else:
            twin = inbox.same_file(data)
            if twin is not None:        # тот же файл уже ждёт разбора — прислал коллега
                twice.append(twin)
                continue
            kept = inbox.save(origin, data)
            inbox.sender_of(kept, user["login"])
            if note.strip():
                inbox.note_for(kept, note)
                if meant is not None:
                    inbox.meant_for(kept, meant.id)
            saved.append(kept)
    if not saved and not twice and not waiting:
        # телефон показывает уведомлением поле message: без него человек увидит
        # пустое окно и не поймёт, что файл до сервиса вообще не доехал.
        # Говорим прямо, что приехало вместо файла, — иначе чинить нечего.
        return _phone_error(400, "Файл не дошёл. " + await _what_came(request))
    if waiting:
        for kept, _ in waiting:
            portal.note_object("Приёмная")
            portal.note_change(f"«{kept}» ждёт решения: похоже на прежнюю версию")
        return {"ok": True, "saved": [], "skipped": twice, "waiting": [k for k, _ in waiting],
                "where": inbox.DIR, "message": _needs_answer(waiting)}
    if target is not None:
        saved = _settle(target, saved)
        where = f"{target.date} {target.title}"
        portal.note_object(f"{target.date} · {target.title}")
        portal.note_change("принято из приёмной: " + ", ".join((saved + twice)[:10]))
    else:
        where = inbox.DIR
        portal.note_object("Приёмная")
        portal.note_change("принято: " + ", ".join(saved[:10])
                           + (" — с командой" if note.strip() else ""))
    message = _took(saved, twice, target)
    if saved and note.strip():
        message += f", команда: «{note.strip()[:60]}»"
    return {"ok": True, "saved": saved, "skipped": twice, "where": where, "message": message}


@app.exception_handler(Exception)
async def _failed(request: Request, exc: Exception):
    """Непойманная ошибка — с объяснением, а не голое 500.

    Соседний сервис, которому мы не смогли помочь, показывает человеку наш ответ.
    «Internal Server Error» ему ничего не говорит; «не удалось записать файл на диск:
    слишком длинный путь» — говорит, и чинить понятно что.
    """
    log.exception("сбой на %s %s", request.method, request.url.path)
    if isinstance(exc, OSError):
        why = f"не удалось записать файл на диск ({exc.strerror or type(exc).__name__})"
    else:
        why = f"внутренняя ошибка ({type(exc).__name__})"
    return JSONResponse({"detail": f"«Материалы»: {why}. Подробности — в журнале сервиса.",
                         "message": f"«Материалы»: {why}"}, status_code=500)


@app.exception_handler(RequestValidationError)
async def _malformed(request: Request, exc: RequestValidationError):
    """Кривой запрос в приёмную объясняем телефону, остальное — как обычно.

    Команда на телефоне показывает уведомлением поле `message`, а FastAPI на такой
    запрос отвечает разбором ошибок валидации — человек видит невнятицу и не знает,
    что чинить в команде.
    """
    if request.url.path.startswith("/api/inbox"):
        return _phone_error(400, "Файл не дошёл. " + await _what_came(request))
    return JSONResponse({"detail": exc.errors()}, status_code=422)


async def _what_came(request: Request) -> str:
    """Что на самом деле прислала команда. Подсказка человеку, а не отладка."""
    try:
        form = await request.form()
    except Exception:
        return "Команда прислала не форму — проверьте «Тело запроса: Форма»"
    fields = []
    for name, value in form.multi_items():
        kind = "файл" if hasattr(value, "filename") else "текст"
        fields.append(f"{name} ({kind})")
    if not fields:
        return "Форма пришла пустой — в команде не заполнено поле files"
    if not any(name == "files" for name, _ in form.multi_items()):
        return f"Поля files нет, пришло: {', '.join(fields[:4])}"
    return ("Поле files пришло текстом, а не файлом: в команде у него "
            "должен быть тип «Файл», значение — «Входные данные команды»")


def _phone_error(code: int, says: str) -> JSONResponse:
    """Отказ, который человек прочитает на телефоне, а не в журнале сервера."""
    return JSONResponse({"detail": says, "message": says}, status_code=code)


def _looks_like_version(event: tree.Event, origin: str, data: bytes) -> dict | None:
    """Похоже ли присланное на прежнюю версию того, что уже лежит в папке."""
    return inbox.same_as(event, {"title": doc_text.propose(origin, data),
                                 "text": doc_text.text(origin, data),
                                 "origin": naming.safe_file(origin),
                                 "hash": similar.digest(data)})


def _needs_answer(waiting: list[tuple[str, dict]]) -> str:
    """Строка для телефона: спрашивать там нечего, но сказать надо прямо."""
    if len(waiting) == 1:
        kept, twin = waiting[0]
        title = naming.split_ext(kept)[0]       # в приёмной документ зовётся, как прислали
        return f"Принято: {title} — похоже на «{twin['number']}. {twin['title']}». Решите в портале"
    return f"Принято {len(waiting)} док. — похожи на уже лежащие. Решите в портале"


def _took(saved: list[str], twice: list[str], target: tree.Event | None) -> str:
    """Строка для телефона: человек видит её уведомлением и больше ничего не открывает."""
    if not saved and twice:
        if target is None:              # лежит в приёмной: там имя ещё исходное
            title = store.cached(inbox.path() / twice[0])["title"] or twice[0]
        else:
            title = naming.parse(twice[0]).title
        return f"Это уже есть: {title}"
    where = target.title if target is not None else "Приёмная — разберите в портале"
    if len(saved) != 1:
        return f"Принято {len(saved)} док. → {where}"
    name = saved[0]
    if target is not None:
        title = naming.parse(name).title
    else:   # в приёмной документ зовётся, как прислали: так его и узнают
        title = naming.split_ext(name)[0]
    return f"Принято: {title} → {where}"


def _near(event: tree.Event) -> int:
    year, month, day = (int(x) for x in event.iso.split("-"))
    return abs((date(year, month, day) - date.today()).days)


@app.get("/api/inbox/events")
def inbox_events(request: Request):
    """Список для телефона: ближайшие к сегодняшнему дню мероприятия, ближайшее сверху."""
    near = sorted(tree.events(), key=_near)[:20]
    return {"items": [{"id": e.id, "title": e.title, "date": e.date} for e in near]}


# Ниже — адреса для браузера, и они нарочно НЕ под «/api/inbox»: этот префикс nginx
# пускает мимо проверки входа (там ключ вместо cookie). Лента и перенос — обычная
# работа вошедшего человека, им проверка нужна.

@app.get("/api/waiting")
def inbox_waiting(request: Request):
    """«Приёмная» на странице: что в ней лежит и куда сейчас идёт присланное."""
    login = request.state.user["login"]
    active, (_, left) = _active(login), _active_left(login)
    return {"items": inbox.items(), "me": login,
            "active": {"id": active.id, "title": active.title, "date": active.date,
                       "hours": round(left)} if active else None}


class RouteStep(BaseModel):
    to: str = ""            # agenda | participants | brief | a3 | keep
    state: str = "claim"    # claim — берусь отправить; done — отправил; release — не вышло
    url: str = ""           # где документ у соседа (при done)
    why: str = ""           # почему не вышло (при release) — в журнал


@app.post("/api/waiting/{name}/route")
def inbox_route(name: str, body: RouteStep, request: Request):
    """Отметка «ушло в…». Сама отправка идёт из браузера под отправителем (п. 9),
    а здесь — заявка, чтобы двойное нажатие и вторая вкладка не отправили дважды:
    второй `claim` получает 409 с тем, что уже есть."""
    path = inbox.file(name)
    if path is None:
        raise HTTPException(status_code=404, detail="Документ в приёмной не найден")
    user = request.state.user
    login = user["login"]
    admin = bool(user.get("is_admin")) or user.get("role") == "admin"
    portal.note_object("Приёмная")
    # Единственное действие, которое отдаёт документ соседу и запускает там платный
    # разбор, — каждый его шаг в журнале, неудача тоже (контракт, п. 4; ревьюер К3)
    if body.state == "claim":
        if body.to not in routing.TARGETS:
            raise HTTPException(status_code=400, detail="Неизвестно, куда отправлять")
        try:
            now = inbox.claim(path.name, body.to, login)
        except inbox.Busy as e:
            raise HTTPException(status_code=409, detail="Документ уже отправлен или отправляется") from e
        if body.to == "keep":
            portal.note_action("Присланное решено просто сохранить")
            portal.note_change(f"«{path.name}»: без отправки")
        else:
            portal.note_action(f"Документ отправляется в «{routing.TARGETS[body.to]}»")
            portal.note_change(f"«{path.name}» → {routing.TARGETS[body.to]}")
        return {"ok": True, "sent": now}
    if body.state == "done":
        now = inbox.finish(path.name, body.url)
        if now is None:
            raise HTTPException(status_code=409, detail="Отправка не заявлена")
        label = routing.TARGETS.get(now["to"], now["to"])
        portal.note_action(f"Документ отправлен в «{label}»")
        portal.note_change(f"«{path.name}» → {label}: {now['url'] or 'без ссылки'}")
        return {"ok": True, "sent": now}
    if body.state == "release":
        was = inbox.sent(path.name)
        if not inbox.release(path.name, login, admin):
            raise HTTPException(status_code=403, detail="Заявку снимает тот, кто отправлял")
        if was and was.get("state") == "sending":
            label = routing.TARGETS.get(was["to"], was["to"])
            portal.note_action(f"Отправка в «{label}» не удалась")
            portal.note_change(f"«{path.name}» → {label}: {(body.why or 'причина не названа')[:200]}")
        return {"ok": True, "sent": inbox.sent(path.name)}
    raise HTTPException(status_code=400, detail="Неизвестный шаг")


class ToEvent(BaseModel):
    event: str = ""
    how: str = "new"        # new — обычный следующий номер, replace — новая редакция,
    target: str = ""        # append — дополнение к документу `target`


@app.post("/api/waiting/{name}/move")
def inbox_move(name: str, body: ToEvent, request: Request):
    """Разобрать документ из приёмной: обычным номером, новой редакцией или дополнением."""
    path = inbox.file(name)
    if path is None:
        raise HTTPException(status_code=404, detail="Документ в приёмной не найден")
    event = _event_or_404(body.event.strip())
    data = path.read_bytes()
    same = _already(event, data)
    if same is not None and body.how != "append":
        # тот же файл байт в байт уже лежит в папке: второй копии там не место
        store.forget(path)
        path.unlink()
        portal.note_object(f"{event.date} · {event.title}")
        portal.note_change(f"«{path.name}» не положен: это уже есть («{same.name}»)")
        return {"ok": True, "event": event.to_dict(), "name": same.name, "how": "same",
                "message": f"Это уже есть: {same.parsed.title}"}
    if body.how in ("replace", "append"):
        target = event.path / body.target
        # именно в этой папке: «внутри хранилища» пропускало бы «../соседняя/файл»
        if target.parent != event.path or not target.is_file():
            raise HTTPException(status_code=404, detail="Документ, к которому относится, не найден")
        if body.how == "replace":       # новая редакция встаёт под прежним номером и названием
            work = _new_version(event, target.name, path.name, data)
        else:
            work = _as_appendix(event, target.name, path.name, data, as_is=True)
        note = ("новая редакция" if body.how == "replace" else "дополнение к") + f" «{target.name}»"
    else:
        work = _save(event, path.name, data, as_is=True)
        note = "новым документом"
    store.forget(path)
    inbox.forget_all(path.name)
    path.unlink()
    work = _settle(event, [work])[0]
    portal.note_object(f"{event.date} · {event.title}")
    portal.note_change(f"«{path.name}» из приёмной → «{work}» ({note})")
    return {"ok": True, "event": event.to_dict(), "name": work, "how": body.how}


class WaitingTitle(BaseModel):
    title: str = ""


@app.get("/api/waiting/{name}/download")
def waiting_download(name: str):
    """Скачать присланное, не раскладывая: иногда иначе не понять, что это."""
    path = inbox.file(name)
    if path is None:
        raise HTTPException(status_code=404, detail="Документ в приёмной не найден")
    return FileResponse(path, filename=path.name)


@app.post("/api/waiting/{name}/title")
def waiting_title(name: str, body: WaitingTitle, request: Request):
    """Поправить название присланного, не выходя из ленты.

    Имя доезжает не всегда — бывает «Документ» и ничего больше. Заставлять
    человека сначала раскладывать документ по папкам, чтобы только потом его
    переименовать, незачем.
    """
    path = inbox.file(name)
    if path is None:
        raise HTTPException(status_code=404, detail="Документ в приёмной не найден")
    title = naming.safe_title(body.title or "")
    if not title:
        raise HTTPException(status_code=400, detail="Нужно название документа")
    ext = naming.split_ext(path.name)[1]
    target = tree.unique(path.with_name(f"{title}.{ext}" if ext else title))
    if target.name == path.name:
        return {"ok": True, "name": path.name}
    path.rename(target)
    store.rename(path, target)
    store.set_title(target, title)      # слово человека главнее заголовка в документе
    inbox.carry(path.name, target.name)
    portal.note_object("Приёмная")
    portal.note_change(f"«{path.name}» → «{target.name}»")
    return {"ok": True, "name": target.name}


@app.post("/api/waiting/{name}/drop")
def inbox_put_aside(name: str, request: Request):
    """Убрать ненужное из приёмной. Файл уходит в «Убранное», а не с диска:
    человек мог ошибиться кнопкой, а документ бывает единственным экземпляром."""
    path = inbox.file(name)
    if path is None:
        raise HTTPException(status_code=404, detail="Документ в приёмной не найден")
    target = tree.unique(inbox.dropped_dir() / path.name)
    path.rename(target)
    store.rename(path, target)
    inbox.forget_all(path.name)
    portal.note_object("Приёмная")
    portal.note_change(f"«{path.name}» → «Убранное»")
    return {"ok": True}


@app.delete("/api/waiting/{name}")
def inbox_erase(name: str, request: Request):
    """Стереть присланное с диска. Необратимо, поэтому только администратор."""
    _admin_only(request)
    path = inbox.file(name)
    if path is None:
        raise HTTPException(status_code=404, detail="Документ в приёмной не найден")
    store.forget(path)
    inbox.forget_all(path.name)
    path.unlink()
    portal.note_object("Приёмная")
    portal.note_change(f"«{path.name}» стёрт из приёмной")
    return {"ok": True}


@app.post("/api/active")
def inbox_active(body: ToEvent, request: Request):
    """Складывать присланное сразу в это мероприятие. Пусто — снова в приёмную."""
    login = request.state.user["login"]
    eid = body.event.strip()
    if not eid:
        store.set_setting(login, ACTIVE, "")
        portal.note_object("Приёмная")
        portal.note_change("присланное снова идёт в приёмную")
        return {"active": None}
    event = _event_or_404(eid)
    store.set_setting(login, ACTIVE, f"{event.id}|{time.time()}")
    portal.note_object(f"{event.date} · {event.title}")
    portal.note_change(f"присланное идёт сюда ближайшие {ACTIVE_HOURS} ч")
    return {"active": {"id": event.id, "title": event.title, "date": event.date,
                       "hours": ACTIVE_HOURS}}


# ── вспомогательное ──────────────────────────────────────────────────────

def _event_or_404(eid: str) -> tree.Event:
    event = tree.find(eid)
    if event is None:
        raise HTTPException(status_code=404, detail="Мероприятие не найдено")
    return event


def _file_or_404(event: tree.Event, name: str, sub: str = "") -> Path:
    """Файл в папке мероприятия по имени из адреса.

    Раскрывать процентную запись здесь нельзя: FastAPI уже раскрыл её сам. Второй
    раз — и «Макет экрана %22Стоимость обучения%22.pdf» превращается в имя с кавычками,
    которого на диске нет. У обычных имён это незаметно, у такого ломается всё разом.
    """
    path = event.path / sub / name
    if not tree.inside(path) or not path.is_file():
        raise HTTPException(status_code=404, detail="Файл не найден")
    return path


def _admin_only(request: Request) -> None:
    if not request.state.user["is_admin"]:
        raise HTTPException(status_code=403, detail="Действие доступно только администратору")


def _card(event: tree.Event) -> dict:
    items = folder.items(event)
    files = []
    for item in items:
        row = item.to_dict()
        was = store.origin(event.path / item.name)
        # первой строкой показываем, под каким именем файл прислали, — но только
        # если имя действительно менялось: иначе строка лишняя
        row["origin"] = was if was and was != item.name else ""
        files.append(row)
    return {"event": {**event.to_dict(), "meeting": meeting.meeting_of(event)},
            "files": files,
            "questions": folder.questions(items),
            "state": folder.state(event, items),
            "archive": _archive(event),
            # можно ли скачать документ в PDF/Word (`download?as=`): «Совещание» по нему
            # решает, рисовать ли кнопки активными
            "convert": convert.status()}


def _archive(event: tree.Event) -> list[dict]:
    """Первичка: всё, что прислали, под своими именами. Видно, что уже в пакете."""
    path = event.path / "Архив"
    if not path.is_dir():
        return []
    used = {store.origin(event.path / i.name) for i in folder.items(event)}
    out = []
    for p in sorted(path.iterdir(), key=lambda x: x.name.lower()):
        if p.is_file() and not p.name.startswith((".", "~$")):
            out.append({"name": p.name, "size": p.stat().st_size, "in_package": p.name in used})
    return out


def _work_copy(event: tree.Event, origin: str, data: bytes, keep: str,
               as_is: bool = False) -> str:
    """Рабочая копия документа в пакете: под человеческим названием, без номера.

    Номер ей даст пересчёт — он зависит от того, что ещё лежит в папке.
    `as_is` — присланное с телефона: название то, под которым прислали, сервис
    ставит только номер (решение пользователя 21.09: «не менять название с Макса»).
    """
    stem = naming.split_ext(origin)[0]
    title = (naming.safe_title(stem) if as_is else "") or doc_text.propose(origin, data)
    ext = naming.split_ext(origin)[1] or doc_text.guess_ext(data)
    work = tree.unique(event.path / naming.compose(None, None, title, None, ext))
    work.write_bytes(data)
    store.remember(work, data, origin=keep)
    if as_is:
        store.set_title(work, title)        # иначе «Привести в порядок» предложит своё
    return work.name


def _save(event: tree.Event, origin: str, data: bytes, as_is: bool = False) -> str:
    """Положить присланный документ: первичку — в «Архив», рабочую копию — в папку."""
    keep = tree.unique(tree.archive_dir(event) / naming.safe_file(origin))
    keep.write_bytes(data)
    return _work_copy(event, origin, data, keep.name, as_is)


def _new_version(event: tree.Event, target: str, origin: str, data: bytes) -> str:
    """Новая редакция документа: встаёт на место прежней, номер и название те же.

    Прежняя версия уезжает в «Архив» с датой в имени — ссылки и номера в переписке
    не ломаются, а достать вчерашний вариант всё равно можно. Расширение рабочей
    копии следует за присланным файлом, а не за прежним: разные сервисы кладут один
    жанр в разных форматах (docx от «Повестки», html от «Совещания»), и писать чужие
    байты под старым расширением нельзя — файл станет нечитаемым (К1, ревьюер
    RC-2026-09-28-6).
    """
    old = event.path / target
    stem, ext = naming.split_ext(old.name)
    stamp = date.today().strftime("%d.%m.%Y")
    kept = tree.unique(tree.archive_dir(event)
                       / f"{stem} (версия от {stamp}){'.' + ext if ext else ''}")
    old.rename(kept)
    store.rename(old, kept)
    keep = tree.unique(tree.archive_dir(event) / naming.safe_file(origin))
    keep.write_bytes(data)
    new_ext = naming.split_ext(origin)[1] or doc_text.guess_ext(data)
    work = tree.unique(event.path / f"{stem}{'.' + new_ext if new_ext else ''}")
    work.write_bytes(data)
    store.remember(work, data, origin=keep.name)
    return work.name


def _as_appendix(event: tree.Event, target: str, origin: str, data: bytes,
                 as_is: bool = False) -> str:
    """Дополнение к документу: получает номер «NN.1» и наследует его вопрос."""
    work = event.path / _save(event, origin, data, as_is)
    head = naming.parse(target)
    used = [i.parsed.sub for i in folder.documents(folder.items(event))
            if i.parsed.num == head.num and i.parsed.sub]
    mine = naming.parse(work.name)
    dst = tree.unique(event.path / naming.compose(head.num, max(used, default=0) + 1,
                                                  mine.title, head.question, mine.ext))
    work.rename(dst)
    store.rename(work, dst)
    return dst.name


def _same_kind(event: tree.Event, origin: str, data: bytes) -> folder.Item | None:
    """Документ того же вида уже в папке? Тогда присланное — его новая редакция.

    Повестка, список участников, модератор и бриф бывают в папке по одному:
    сервис, приславший свой документ второй раз, правит его, а не добавляет второй
    (контракт `../CLAUDE.md`, п. 9 — «заменяя прежнюю версию, если она была»).
    """
    step = (naming.role_of(doc_text.propose(origin, data))
            or naming.role_of(origin)
            or naming.role_of("\n".join(doc_text.lines(origin, data)[:3])))
    if not step:
        return None
    return next((i for i in folder.documents(folder.items(event))
                 if folder.role(event, i) == step), None)


def _replace(event: tree.Event, origin: str, data: bytes) -> str:
    """Новая редакция того же документа: место в пакете и название остаются прежними.

    Название человек мог поправить руками — сбрасывать его из-за того, что прислали
    уточнённую редакцию, нельзя. Расширение рабочей копии следует за присланным файлом:
    писать байты другого формата под старым расширением нельзя — файл станет
    нечитаемым (К1, ревьюер RC-2026-09-28-6).
    """
    keep = tree.archive_dir(event) / naming.safe_file(origin)
    old = next((i for i in folder.items(event)
                if store.origin(event.path / i.name) == keep.name), None)
    if old is None:
        # по первичке не нашли: документ мог уже смениться новой редакцией того же вида
        # (`_same_kind` → `_new_version`), и у рабочей копии первичка теперь «Модератор (2).docx».
        # Повестка, участники, модератор и бриф бывают в папке по одному — заменяем его,
        # а не кладём второй рядом (07.10, «Модератор_И61.docx» дважды в пакете)
        old = _same_kind(event, origin, data)
    if keep.is_file():
        # прежняя редакция уходит в сторону с датой: из приёмной так и делается,
        # и отсюда должно быть так же — иначе вчерашний вариант не достать
        stem, ext = naming.split_ext(keep.name)
        stamp = date.today().strftime("%d.%m.%Y")
        kept = tree.unique(keep.with_name(f"{stem} (версия от {stamp})"
                                          + (f".{ext}" if ext else "")))
        keep.rename(kept)
        store.rename(keep, kept)
    keep.write_bytes(data)
    if old is None:
        return _work_copy(event, origin, data, keep.name)
    path = event.path / old.name
    stem, ext = naming.split_ext(old.name)
    new_ext = naming.split_ext(origin)[1] or doc_text.guess_ext(data)
    if new_ext and new_ext != ext:
        path.unlink(missing_ok=True)
        store.forget(path)
        path = tree.unique(event.path / f"{stem}.{new_ext}")
    path.write_bytes(data)
    store.remember(path, data, origin=keep.name)
    return path.name


def _settle(event: tree.Event, fresh: list[str] | None = None) -> list[str]:
    """Пересчитать номера и пересобрать реестр.

    Возвращает, как в итоге называются только что добавленные файлы: до пересчёта
    у них ещё нет номера, и показывать человеку это промежуточное имя незачем.
    """
    done = {m.src: m.dst for m in folder.renumber(event)}
    registry.write(event, folder.items(event))
    convert.prune(event.path, event.id)     # переводы документов, которых уже нет или которые сменили имя
    return [done.get(name, name) for name in (fresh or [])]


def _saved(event: tree.Event, note: str) -> dict:
    """Общий хвост всякого изменения: пересчёт номеров, реестр, запись в журнал."""
    _settle(event)
    portal.note_object(f"{event.date} · {event.title}")
    portal.note_change(note)
    return _card(event)


# ── мероприятия ──────────────────────────────────────────────────────────

@app.get("/api/years")
def years():
    return {"items": tree.years()}


@app.get("/api/events")
def events(q: str = "", year: str = ""):
    query = (q or "").strip().lower()
    out = []
    for event in tree.events():
        if year and not event.date.endswith(year):
            continue
        if query and query not in event.title.lower():
            continue
        st = folder.state(event, folder.items(event))
        out.append({**event.to_dict(), "files": st["count"], "have": st["have"],
                    "meeting": meeting.meeting_of(event)})
    return {"items": out}


class NewEvent(BaseModel):
    date: str = ""
    title: str = ""


@app.post("/api/events")
def create_event(body: NewEvent, request: Request):
    title = (body.title or "").strip()
    if not title:
        raise HTTPException(status_code=400, detail="Нужно название мероприятия")
    try:
        when = datetime.strptime((body.date or "").strip(), "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(status_code=400, detail="Нужна дата мероприятия")
    event = tree.create(when, title)
    registry.write(event, folder.items(event))
    portal.note_object(f"{event.date} · {event.title}")
    portal.note_change(f"папка «{event.rel.name}»")
    return _card(event)


class Rename(BaseModel):
    title: str = ""
    date: str = ""              # ГГГГ-ММ-ДД; не прислали — дата остаётся прежней


@app.put("/api/events/{eid}")
def rename_event(eid: str, body: Rename, request: Request):
    """Дата и название мероприятия.

    Дату двигают часто: пакет собирают под совещание, а совещание переносят.
    Папка переезжает вместе с датой — в свой месяц и год, — иначе дерево врёт.
    """
    event = _event_or_404(eid)
    title = naming.safe_title(body.title or "") or event.title
    if not title:
        raise HTTPException(status_code=400, detail="Нужно название мероприятия")
    when = date(*(int(x) for x in reversed(event.date.split("."))))
    if (body.date or "").strip():
        try:
            when = datetime.strptime(body.date.strip(), "%Y-%m-%d").date()
        except ValueError:
            raise HTTPException(status_code=400, detail="Нужна дата мероприятия")
    fresh = _relocate(event, when, title)
    if fresh is event:
        return _card(event)
    portal.note_object(f"{fresh.date} · {title}")
    portal.note_change(f"«{event.date} {event.title}» → «{fresh.date} {title}»")
    return _card(fresh)


def _relocate(event: tree.Event, when: date, title: str) -> tree.Event:
    """Перенести папку под новую дату и название. Ничего не менялось — вернём как было."""
    target = tree.root() / tree.relative(when, title)
    if target == event.path:
        return event
    if target.exists():
        raise HTTPException(status_code=409,
                            detail="Мероприятие с таким названием уже есть на эту дату")
    was, was_rel = event.path, str(event.rel).replace("\\", "/")
    target.parent.mkdir(parents=True, exist_ok=True)
    was.rename(target)
    store.move_folder(was, target)
    tree.prune(was.parent)
    fresh = tree.from_rel(target.relative_to(tree.root()))
    meeting.moved(was_rel, fresh)
    registry.write(fresh, folder.items(fresh))
    convert.move_event(event.id, fresh.id)  # кэш переводов едет вместе с папкой (ревьюер RC-9, Н-7)
    return fresh


@app.get("/api/events/{eid}")
def event_card(eid: str):
    return _card(_event_or_404(eid))


class Link(BaseModel):
    meeting: str = ""
    date: str = ""              # ДД.ММ.ГГГГ, как в карточке «Модератора»
    title: str = ""


@app.post("/api/events/link")
def link_meeting(body: Link, request: Request):
    """Папка совещания: найти или завести и привязать (контракт `../CLAUDE.md`, п. 9).

    Зовёт «Модератор», когда совещание появилось или его переименовали. Вызов
    идемпотентный: одно совещание — одна папка, сколько раз ни позови. Папка
    заводится сразу, даже пустой: реестр с первого дня показывает, чего не хватает.
    """
    meeting_id = (body.meeting or "").strip()
    if not meeting_id:
        raise HTTPException(status_code=400, detail="Нужен ключ совещания")
    title = naming.safe_title(body.title or "")
    if not title:
        raise HTTPException(status_code=400, detail="Нужно название совещания")
    when = _when_of(body.date)
    stamp = when.strftime("%d.%m.%Y")

    event = meeting.folder_of(meeting_id)
    if event is None:
        # папка того же дня с тем же названием — берём её, а не заводим вторую
        event = next((e for e in tree.events() if e.date == stamp and e.title == title), None)
    if event is not None:
        bound = meeting.meeting_of(event)
        if bound and bound != meeting_id:
            raise HTTPException(status_code=409,
                                detail="У этой папки уже есть совещание")
        event = _relocate(event, when, title)      # совещание перенесли или переименовали
    else:
        event = tree.create(when, title)
        registry.write(event, folder.items(event))
    meeting.bind(meeting_id, event)
    portal.note_object(f"{event.date} · {event.title}")
    portal.note_change(f"папка привязана к совещанию «{meeting_id}»")
    return {"id": event.id, **_card(event)}      # «Модератор» читает и плоский id


def _when_of(text: str) -> date:
    """Дата совещания. «Модератор» шлёт ДД.ММ.ГГГГ, браузер — ГГГГ-ММ-ДД."""
    for shape in ("%d.%m.%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime((text or "").strip(), shape).date()
        except ValueError:
            continue
    raise HTTPException(status_code=400, detail="Нужна дата совещания")


@app.post("/api/events/{eid}/drop")
def drop_event(eid: str, request: Request):
    """Убрать мероприятие. Папка уезжает в «Убранное» целиком, вместе с первичкой.

    Не удаляем: в папке бывают единственные экземпляры документов, а завести
    мероприятие по ошибке легко. Убранное лежит рядом с годами, в списке его нет,
    в проводнике — есть.
    """
    event = _event_or_404(eid)
    target = tree.unique(tree.dropped_dir() / event.path.name)
    was = event.path
    meeting.unbind(event)
    was.rename(target)
    store.move_folder(was, target)
    tree.prune(was.parent)
    convert.forget_event(event.id)          # убранного мероприятия в списке нет — и переводов его нет
    portal.note_object(f"{event.date} · {event.title}")
    portal.note_change(f"мероприятие убрано: папка «{event.rel.name}» → «{tree.DROPPED}»")
    return {"ok": True}


@app.delete("/api/events/{eid}")
def erase_event(eid: str, request: Request):
    """Стереть мероприятие с диска вместе со всеми документами. Только администратор."""
    _admin_only(request)
    event = _event_or_404(eid)
    count = len(folder.documents(folder.items(event)))
    meeting.unbind(event)
    shutil.rmtree(event.path, ignore_errors=True)
    tree.prune(event.path.parent)
    convert.forget_event(event.id)
    portal.note_object(f"{event.date} · {event.title}")
    portal.note_change(f"мероприятие стёрто с диска вместе с документами ({count})")
    return {"ok": True}


# ── документы ────────────────────────────────────────────────────────────

@app.post("/api/events/{eid}/files")
async def upload(eid: str, request: Request, files: list[UploadFile] = [],
                 mode: str = Form("add")):
    """Загруженный документ раскладывается сразу: первичка в «Архив», копия — в пакет.

    Ждать подтверждения названий не заставляем (решение пользователя от 20.09):
    название правится по месту, нажатием прямо на него.
    """
    event = _event_or_404(eid)
    saved = []
    for upload_file in files:
        data = await upload_file.read()
        if not data:
            continue
        if len(data) > MAX_UPLOAD:
            raise HTTPException(status_code=413, detail=f"«{upload_file.filename}» больше 200 МБ")
        origin = Path(upload_file.filename or "документ").name
        keep = event.path / "Архив" / naming.safe_file(origin)
        if mode == "replace" and keep.is_file():
            saved.append(_replace(event, origin, data))
            continue
        older = _same_kind(event, origin, data)
        if older is not None:       # повестка, участники, модератор, бриф — по одному
            saved.append(_new_version(event, older.name, origin, data))
            continue
        saved.append(_save(event, origin, data))
    if not saved:
        raise HTTPException(status_code=400, detail="Не пришло ни одного файла")
    names = _settle(event, saved)
    portal.note_object(f"{event.date} · {event.title}")
    portal.note_change("загружено: " + ", ".join(names[:10])
                       + (" и ещё…" if len(names) > 10 else ""))
    return _card(event)


class Title(BaseModel):
    title: str = ""
    question: int | None = None


@app.get("/api/events/{eid}/files/{name}/proposal")
def proposal(eid: str, name: str):
    """Как сервис назвал бы этот документ сегодня.

    Правила именования меняются, а имена на диске — нет: документ, разложенный
    вчера, так и останется со вчерашним названием. Кнопка «предложить заново»
    в окне правки показывает свежее предложение, а решает по-прежнему человек.
    """
    event = _event_or_404(eid)
    path = _file_or_404(event, name)
    origin = store.origin(path) or path.name
    return {"title": doc_text.propose(origin, path.read_bytes())}


@app.post("/api/events/{eid}/files/{name}/title")
def set_title(eid: str, name: str, body: Title, request: Request):
    """Название и вопрос документа. Номер ставит сервис — он зависит от места в пакете."""
    event = _event_or_404(eid)
    path = _file_or_404(event, name)
    title = naming.safe_title(body.title or "")
    if not title:
        raise HTTPException(status_code=400, detail="Нужно название документа")
    parsed = naming.parse(path.name)
    # в имени файла номер вопроса читается как «\d{1,2}»: больший туда впишется,
    # но обратно уже не прочитается — и потеряется при первом же пересчёте
    question = body.question if 0 < (body.question or 0) < 100 else None
    wanted = naming.compose(parsed.num, parsed.sub, title, question, parsed.ext)
    if wanted == path.name:
        return _card(event)             # ничего не поменялось — и файл трогать незачем
    target = tree.unique(event.path / wanted)
    path.rename(target)
    store.rename(path, target)
    return _saved(event, f"«{path.name}» → «{target.name}»")


@app.post("/api/events/{eid}/files/{name}/attach")
def attach(eid: str, name: str, request: Request):
    """Сделать документ приложением к тому, что стоит перед ним: 05 и 05.1."""
    event = _event_or_404(eid)
    path = _file_or_404(event, name)
    docs = folder.documents(folder.items(event))
    where = next((i for i, item in enumerate(docs) if item.name == path.name), -1)
    if where <= 0:
        raise HTTPException(status_code=400,
                            detail="Приложение не бывает первым: над ним нужен документ")
    head_num = docs[where - 1].parsed.num
    if head_num is None:                # документ над ним сам ещё не разложен
        raise HTTPException(status_code=409,
                            detail="Сначала разложите документ, к которому это приложение")
    question = docs[where - 1].parsed.question
    used = [i.parsed.sub for i in docs if i.parsed.num == head_num and i.parsed.sub]
    parsed = naming.parse(path.name)
    target = tree.unique(event.path / naming.compose(head_num, max(used, default=0) + 1,
                                                     parsed.title, question, parsed.ext))
    path.rename(target)
    store.rename(path, target)
    return _saved(event, f"«{path.name}» стал приложением «{target.name}»")


@app.post("/api/events/{eid}/files/{name}/detach")
def detach(eid: str, name: str, request: Request):
    """Вернуть приложению самостоятельность — встанет следом за своим документом."""
    event = _event_or_404(eid)
    path = _file_or_404(event, name)
    parsed = naming.parse(path.name)
    if not parsed.sub:
        raise HTTPException(status_code=400, detail="Это и так самостоятельный документ")
    head = next((i for i in folder.documents(folder.items(event))
                 if i.parsed.num == parsed.num and not i.parsed.sub), None)
    free = tree.unique(event.path / naming.compose(None, None, parsed.title,
                                                   parsed.question, parsed.ext))
    path.rename(free)
    store.rename(path, free)
    order: list[str] = []
    for chain in folder.chains(folder.items(event)):
        if chain.head.name == free.name:
            continue
        order.append(chain.head.name)
        if head is not None and chain.head.name == head.name:
            order.append(free.name)
    done = folder.renumber(event, order)
    registry.write(event, folder.items(event))
    portal.note_object(f"{event.date} · {event.title}")
    mine = next((m.dst for m in done if m.src == free.name), free.name)
    portal.note_change(f"«{path.name}» больше не приложение → «{mine}»")
    return _card(event)


class Order(BaseModel):
    names: list[str] = []


@app.post("/api/events/{eid}/order/preview")
def order_preview(eid: str, body: Order):
    """«Было → стало» целиком, до единой правки на диске: человек видит план и решает."""
    event = _event_or_404(eid)
    return {"moves": [m.to_dict() for m in folder.plan_numbering(event, body.names)]}


@app.post("/api/events/{eid}/order")
def reorder(eid: str, body: Order, request: Request):
    """Порядок документов в пакете: номер документа — это его место."""
    event = _event_or_404(eid)
    done = folder.renumber(event, body.names)
    if not done:
        return _card(event)
    registry.write(event, folder.items(event))
    convert.prune(event.path, event.id)     # имена сменились — старые переводы не нужны (ревьюер RC-9, Н-8)
    portal.note_object(f"{event.date} · {event.title}")
    portal.note_change("; ".join(f"«{m.src}» → «{m.dst}»" for m in done[:10])
                       + (" и ещё…" if len(done) > 10 else ""))
    return {**_card(event), "moved": [m.to_dict() for m in done]}


@app.post("/api/events/{eid}/registry")
def rebuild_registry(eid: str, request: Request):
    """Привести папку в порядок: пересчитать номера и пересобрать реестр.

    Обычно это делается само при каждом изменении. Кнопка нужна, когда папку
    поправили мимо сервиса — через сетевой диск — или когда поменялись правила.
    """
    event = _event_or_404(eid)
    done = folder.renumber(event)
    registry.write(event, folder.items(event))
    convert.prune(event.path, event.id)
    portal.note_object(f"{event.date} · {event.title}")
    if done:
        portal.note_change("; ".join(f"«{m.src}» → «{m.dst}»" for m in done[:10])
                           + (" и ещё…" if len(done) > 10 else ""))
    return _card(event)


@app.get("/api/events/{eid}/files/{name}/download")
def download(eid: str, name: str, archived: int = 0, as_: str = Query("", alias="as")):
    """Файл как есть, а с `?as=pdf|docx` — переведённый в этот формат (`convert.py`).

    Скачивание в журнал не пишется — ни оригинала, ни перевода: данные не меняются.
    """
    fmt = (as_ or "").strip().lower()
    if fmt and fmt not in convert.FORMATS:
        raise HTTPException(status_code=400, detail="Скачать можно только в PDF или Word: as=pdf или as=docx")
    event = _event_or_404(eid)
    path = _file_or_404(event, name, "Архив" if archived else "")
    stem, ext = naming.split_ext(path.name)
    if not fmt or fmt == ext:
        return FileResponse(path, filename=path.name)
    try:
        done = convert.convert(path, event.id, fmt, archived=bool(archived))
    except convert.Unsupported as e:
        raise HTTPException(status_code=400, detail=str(e))
    except convert.NotInstalled as e:
        raise HTTPException(status_code=501, detail=str(e))
    except convert.Failed as e:
        raise HTTPException(status_code=502, detail=str(e))
    except convert.Busy as e:
        raise HTTPException(status_code=503, detail=str(e))
    return FileResponse(done, filename=f"{stem}.{fmt}", media_type=convert.MEDIA[fmt])


@app.delete("/api/events/{eid}/files/{name}")
def drop(eid: str, name: str, request: Request):
    """Убрать документ из пакета. Первичка остаётся в «Архиве» — вернуть можно всегда.

    Первички нет (файл положили мимо сервиса) — не стираем, а уводим в «Архив»:
    удалять то, чего больше нигде нет, сервис не станет.
    """
    event = _event_or_404(eid)
    path = _file_or_404(event, name)
    if path.name == naming.REGISTRY:
        raise HTTPException(status_code=400, detail="Реестр собирает сервис, убрать его нельзя")
    origin = store.origin(path)
    first = event.path / "Архив" / origin if origin else None
    if first is not None and first.is_file():
        store.forget(path)
        path.unlink()
        note = f"«{path.name}» убран из пакета, первичка цела"
    else:
        target = tree.unique(tree.archive_dir(event) / path.name)
        path.rename(target)
        store.rename(path, target)
        note = f"«{path.name}» убран из пакета в «Архив»"
    return _saved(event, note)


@app.post("/api/events/{eid}/archive/{name}/restore")
def restore(eid: str, name: str, request: Request):
    """Вернуть документ из первички в пакет — новой копией, с предложенным названием."""
    event = _event_or_404(eid)
    path = _file_or_404(event, name, "Архив")
    # первичка уже лежит в «Архиве» — делаем только рабочую копию
    work = _work_copy(event, path.name, path.read_bytes(), path.name)
    return _saved(event, f"«{path.name}» возвращён в пакет как «{work}»")


@app.delete("/api/events/{eid}/archive/{name}")
def purge(eid: str, name: str, request: Request):
    """Стереть первичку с диска — необратимо, поэтому только администратор."""
    _admin_only(request)
    event = _event_or_404(eid)
    path = _file_or_404(event, name, "Архив")
    store.forget(path)
    path.unlink()
    convert.prune(event.path, event.id)     # и её переводы: «стёрта с диска» значит нигде
    portal.note_object(f"{event.date} · {event.title}")
    portal.note_change(f"первичка «{path.name}» стёрта с диска")
    return {"ok": True}


@app.get("/api/events/{eid}/export/package.zip")
def package(eid: str):
    """Вся папка одним архивом — то же, что открыть её и выделить всё."""
    event = _event_or_404(eid)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for item in folder.items(event):
            try:
                zf.write(event.path / item.name, item.name)
            except OSError:
                continue        # файл убрали с сетевого диска прямо сейчас — не роняем выгрузку
    # имя файла по-русски: в заголовке оно передаётся только в процентной записи (RFC 5987)
    name = quote(f"{event.date} {naming.safe_title(event.title)}.zip")
    return Response(buffer.getvalue(), media_type="application/zip", headers={
        "Content-Disposition": f"attachment; filename*=UTF-8''{name}"})


# ── поиск для портала ────────────────────────────────────────────────────

@app.get("/api/search")
def search(q: str = "", limit: int = 5):
    """Поиск по названиям мероприятий, названиям документов и тексту внутри них.

    Текст уже прочитан при загрузке, поэтому мероприятие находится по фразе из
    справки, а не только по названию папки, — ради этого хранилище и заводилось.
    """
    query = (q or "").strip().lower()
    if len(query) < 2:
        return {"items": []}
    limit = max(1, min(int(limit or 5), 20))
    items = []
    for event in tree.events():
        if len(items) >= limit:
            break
        hit = ""
        if query in event.title.lower():
            hit = "название мероприятия"
        else:
            # реестр — отчёт о папке, и в нём переписаны все названия: без этого
            # любой запрос находил бы «текст документа „Реестр материалов"»
            for item in folder.documents(folder.items(event)):
                if query in item.parsed.title.lower():
                    hit = f"документ «{item.parsed.title}»"
                    break
                if query in store.cached(event.path / item.name)["text"].lower():
                    hit = f"текст документа «{item.parsed.title}»"
                    break
        if not hit:
            continue
        items.append({"title": f"{event.title} · {event.date} — {hit}",
                      "url": f"/materials/#event={event.id}",
                      "when": event.iso, "who": "", "kind": "материалы", "same_as": ""})
    return {"items": items}
