"""Библиотека листов A3: сохранить, открыть, посмотреть, кто и когда менял.

Сервис живёт под префиксом `/a3/`, префикс снимает nginx, поэтому все адреса
в интерфейсе относительные. Вход и журнал — по контракту из ../../CLAUDE.md.
"""
from __future__ import annotations

import json
import threading
import time
import traceback
from html import escape
from typing import Any
from urllib.parse import quote

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import audit, config, db, extract, jobs, kit, portal, spark

app = FastAPI(title="Библиотека листов A3", docs_url=None, redoc_url=None)


class FreshStatic(StaticFiles):
    """Стили и скрипты браузер сверяет с сервисом каждый раз.

    Без этого после выкладки остаётся старый `library.js`, а он ждёт от сервиса
    прежних ответов — страница ломается на ровном месте. Сверка дешёвая: при
    совпадении ETag ответ приходит пустой, 304.
    """

    def file_response(self, *args: Any, **kwargs: Any) -> Response:
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        return response


app.mount("/static", FreshStatic(directory=config.APP_DIR / "static"), name="static")


@app.on_event("startup")
def startup() -> None:
    db.init()


def user_dep(request: Request) -> dict[str, str]:
    portal.check_origin(request)
    return portal.current_user(request)


class RestoreIn(BaseModel):
    version: int | None = None     # версия, которую человек видел на экране


class SheetIn(BaseModel):
    data: dict[str, Any]
    title: str = ""
    version: int | None = None
    note: str = Field(default="", max_length=200)


def sheet_or_404(sheet_id: int) -> dict[str, Any]:
    sheet = db.get_sheet(sheet_id)
    if not sheet:
        raise HTTPException(status_code=404, detail="Лист не найден")
    return sheet


# ── страницы ─────────────────────────────────────────────────────────────────

# Адреса соседей отдаёт сервер, в самой странице абсолютных адресов нет —
# так же это сделано у «Модератора» и «Брифа». Совещание — общая ось
# (../../CLAUDE.md, п. 9): карточку ведёт «Модератор», папку — «Материалы»,
# мы только просим найти совещание и кладём в папку готовый лист.
NEIGHBOURS = {
    "meetings": "/moderator/api/meetings",
    "find_or_create": "/moderator/api/meetings/find-or-create",
    "meeting_folder": "/moderator/api/meetings/{id}/folder",
    "meeting_page": "/moderator/#meeting=",
    "event_link": "/materials/api/events/link",
    "event_files": "/materials/api/events/{id}/files",
    "event_page": "/materials/#event=",
}


@app.get("/", response_class=HTMLResponse)
def library_page(request: Request, user: dict = Depends(user_dep)) -> HTMLResponse:
    html = (config.APP_DIR / "static" / "library.html").read_text(encoding="utf-8")
    # имя приходит из портала и попадает в атрибут — экранируем, иначе кавычка ломает страницу
    html = html.replace("{{ROLE}}", escape(user["role"], quote=True)).replace("{{NAME}}", escape(user["name"], quote=True))
    html = html.replace("{{LINKS}}", escape(json.dumps(NEIGHBOURS, ensure_ascii=False), quote=True))
    return HTMLResponse(html, headers={"Cache-Control": "no-store"})


@app.get("/editor", response_class=HTMLResponse)
def editor_page(request: Request, id: int | None = None, version: int | None = None,
                user: dict = Depends(user_dep)) -> HTMLResponse:
    """Лист в редакторе. Без id — новый лист из примера в конструкторе."""
    service_js = (config.APP_DIR / "static" / "service.js").read_text(encoding="utf-8")
    if id is None:
        state = {"id": None, "version": 0, "title": "", "role": user["role"], "readonly": False}
        return HTMLResponse(kit.service_page(kit.BLANK, state, service_js),
                            headers={"Cache-Control": "no-store"})
    sheet = sheet_or_404(id)
    data, readonly, shown = sheet["data"], False, sheet["version"]
    if version is not None:
        old = db.get_version(id, version)
        if not old:
            raise HTTPException(status_code=404, detail="Версия не найдена")
        data, readonly, shown = old["data"], True, version
    state = {
        "id": sheet["id"],
        "version": sheet["version"],
        "shown_version": shown,
        "fix_rounds": sheet["fix_rounds"],
        "max_rounds": MAX_ROUNDS,
        "sheet_state": sheet["state"],
        "title": sheet["title"],
        "role": user["role"],
        "readonly": readonly,
        "updated_at": sheet["updated_at"],
        "updated_name": sheet["updated_name"],
    }
    return HTMLResponse(kit.service_page(data, state, service_js), headers={"Cache-Control": "no-store"})


@app.get("/healthz", response_class=PlainTextResponse)
def healthz() -> str:
    return "ok"


# ── листы ────────────────────────────────────────────────────────────────────

@app.get("/api/sheets")
def api_list(q: str = "", archived: bool = False, sort: str = "updated",
             user: dict = Depends(user_dep)) -> dict[str, Any]:
    return {"sheets": db.list_sheets(q=q, archived=archived, sort=sort), "role": user["role"],
            "me": user["login"], "team": user.get("team", [])}


@app.get("/api/search")
def api_search(q: str = "", limit: int = 5, user: dict = Depends(user_dep)) -> dict[str, Any]:
    """Поиск для портала: он спрашивает все сервисы сразу и показывает общий список.

    Отдаём то же, что человек увидел бы в библиотеке: название, кто менял и когда.
    """
    query = (q or "").strip()
    if len(query) < 2:
        return {"items": []}
    limit = max(1, min(int(limit or 5), 20))
    found = db.list_sheets(q=query)[:limit]
    return {"items": [{
        "title": card["title"],
        "url": f"/a3/editor?id={card['id']}",
        "when": (card["updated_at"] or "")[:10],
        "who": card["updated_name"] or card["created_name"] or "",
        "kind": "лист A3",
    } for card in found]}


@app.get("/api/sheets/{sheet_id}")
def api_get(sheet_id: int, user: dict = Depends(user_dep)) -> dict[str, Any]:
    return sheet_or_404(sheet_id)


@app.post("/api/sheets", status_code=201)
def api_create(body: SheetIn, request: Request, user: dict = Depends(user_dep)) -> dict[str, Any]:
    sheet = db.create_sheet(body.data, user, body.title)
    audit.write(user, "создан лист A3", sheet["title"],
                f"версия 1, блоков: {len(body.data.get('blocks') or [])}", portal.client_ip(request))
    return sheet


@app.put("/api/sheets/{sheet_id}")
def api_save(sheet_id: int, body: SheetIn, request: Request,
             user: dict = Depends(user_dep)) -> dict[str, Any]:
    before = sheet_or_404(sheet_id)
    if body.version is None:
        raise HTTPException(status_code=400, detail="Не указана версия листа")
    try:
        sheet = db.save_sheet(sheet_id, body.data, user, body.version, body.title, body.note)
    except db.Conflict as conflict:
        raise HTTPException(
            status_code=409,
            detail={
                "message": f"Лист изменил {conflict.by} в {conflict.at}. Обновите страницу,"
                           f" чтобы не затереть чужую правку.",
                "version": conflict.version,
            },
        ) from None
    if not sheet:
        raise HTTPException(status_code=404, detail="Лист не найден")
    details = f"версия {before['version']} → {sheet['version']}"
    if before["title"] != sheet["title"]:
        details += f"; название «{before['title']}» → «{sheet['title']}»"
    n_before, n_after = len(before["data"].get("blocks") or []), len(body.data.get("blocks") or [])
    if n_before != n_after:
        details += f"; блоков {n_before} → {n_after}"
    audit.write(user, "сохранён лист A3", sheet["title"], details, portal.client_ip(request))
    return sheet


@app.post("/api/sheets/{sheet_id}/archive")
def api_archive(sheet_id: int, request: Request, archived: bool = True,
                user: dict = Depends(user_dep)) -> dict[str, Any]:
    sheet_or_404(sheet_id)
    sheet = db.set_archived(sheet_id, archived)
    if not sheet:
        raise HTTPException(status_code=404, detail="Лист не найден")
    action = "лист A3 убран в архив" if archived else "лист A3 возвращён из архива"
    audit.write(user, action, sheet["title"], "в работе → архив" if archived else "архив → в работе",
                portal.client_ip(request))
    return sheet


@app.delete("/api/sheets/{sheet_id}")
def api_delete(sheet_id: int, request: Request, user: dict = Depends(user_dep)) -> dict[str, Any]:
    """Удаление насовсем — только администратору. Сотруднику доступен архив."""
    portal.require_admin(user)
    sheet = sheet_or_404(sheet_id)
    versions = len(db.list_versions(sheet_id))
    db.delete_sheet(sheet_id)
    audit.write(user, "удалён лист A3 насовсем", sheet["title"],
                f"версий удалено: {versions}", portal.client_ip(request))
    return {"deleted": sheet_id}


# ── версии ───────────────────────────────────────────────────────────────────

@app.get("/api/sheets/{sheet_id}/versions")
def api_versions(sheet_id: int, user: dict = Depends(user_dep)) -> dict[str, Any]:
    sheet = sheet_or_404(sheet_id)
    return {"sheet": {"id": sheet["id"], "title": sheet["title"], "version": sheet["version"]},
            "versions": db.list_versions(sheet_id)}


@app.post("/api/sheets/{sheet_id}/versions/{version}/restore")
def api_restore(sheet_id: int, version: int, request: Request, body: RestoreIn | None = None,
                user: dict = Depends(user_dep)) -> dict[str, Any]:
    """Восстановление — это новая версия поверх текущей, история не теряется."""
    current = sheet_or_404(sheet_id)
    old = db.get_version(sheet_id, version)
    if not old:
        raise HTTPException(status_code=404, detail="Версия не найдена")
    seen = (body.version if body and body.version is not None else current["version"])
    try:
        sheet = db.save_sheet(sheet_id, old["data"], user, seen, old["title"],
                              f"восстановлена версия {version}")
    except db.Conflict as conflict:
        raise HTTPException(status_code=409, detail={
            "message": f"Лист изменил {conflict.by} в {conflict.at}. Обновите страницу.",
            "version": conflict.version}) from None
    if not sheet:
        raise HTTPException(status_code=404, detail="Лист не найден")
    audit.write(user, "восстановлена версия листа A3", sheet["title"],
                f"версия {current['version']} → {sheet['version']} (копия версии {version})",
                portal.client_ip(request))
    return sheet


# ── скачивание ───────────────────────────────────────────────────────────────

@app.get("/api/sheets/{sheet_id}/file", response_class=HTMLResponse)
def api_file(sheet_id: int, request: Request, version: int | None = None, purpose: str = "",
             user: dict = Depends(user_dep)) -> Response:
    """Самодостаточный HTML: работает без сервиса и без интернета.

    Скачивание пишется в журнал: без этого воронка «разбор → лист» выглядела
    брошенной — лист сделали, а что с ним было дальше, не видно. Файл, который
    страница берёт, чтобы положить в папку совещания (`purpose=attach`),
    скачиванием не считается: то событие пишется отдельно, как приложение.
    """
    sheet = sheet_or_404(sheet_id)
    data, title = sheet["data"], sheet["title"]
    if version is not None:
        old = db.get_version(sheet_id, version)
        if not old:
            raise HTTPException(status_code=404, detail="Версия не найдена")
        data, title = old["data"], old["title"]
    if purpose.strip().lower() != "attach":
        audit.write(user, "скачан лист A3", sheet["title"],
                    f"файл HTML, версия {version if version is not None else sheet['version']}",
                    portal.client_ip(request))
    name = kit.file_name(title)
    return Response(
        content=kit.plain_page(data),
        media_type="text/html; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}"},
    )


# ── «Искра»: справка → лист, замечания → исправленный лист ───────────────────

MAX_ROUNDS = 2        # столько кругов доработки лист проходит сам, дальше — руками
MAX_TOTAL_ROUNDS = 6  # общий предел обращений к «Искре» на один лист: бесконечный «ещё раз» стоит денег


class FixIn(BaseModel):
    data: dict[str, Any]
    warnings: list[str] = []
    version: int
    again: bool = False   # человек нажал «Доработать ещё раз»


class StateIn(BaseModel):
    """Итог проверки вёрстки — её делает браузер, сервер только запоминает."""

    state: str = ""
    note: str = ""


@app.post("/api/spark/draft", status_code=202)
def api_draft(request: Request, file: UploadFile | None = File(default=None),
              text: str = Form(default=""), user: dict = Depends(user_dep)) -> dict[str, Any]:
    """Справка уходит в «Искру» фоном: запрос отдаёт номер разбора, а не ждёт ответа.

    Разбор идёт минутами, на такое соединение не рассчитан ни браузер, ни шлюз
    портала. Ход дела браузер спрашивает через `api/spark/jobs/{id}`.
    Сам текст справки нигде не сохраняется.
    """
    if file is not None:
        raw = file.file.read(10 * 1024 * 1024 + 1)
        if len(raw) > 10 * 1024 * 1024:
            raise HTTPException(status_code=413, detail="Файл больше 10 МБ")
        try:
            source = extract.from_file(file.filename or "", raw)
        except extract.Unsupported as e:
            raise HTTPException(status_code=400, detail=str(e)) from None
    else:
        source = text.strip()
    if len(source) < 200:
        raise HTTPException(status_code=400, detail="Текста маловато: пришлите справку целиком")
    ip = portal.client_ip(request)      # запрос закончится раньше работы, адрес берём сейчас

    def work() -> dict[str, Any]:
        try:
            data, spent = spark.draft(source)
        except spark.SparkError as e:
            # неудачный разбор тоже стоил токенов — контракт с порталом, п. 4
            audit.write(user, "«Искра» не разобрала справку в лист A3", "справка",
                        f"знаков в справке: {len(source)}; причина: {e}", ip,
                        tokens_in=e.spent.get("in", 0), tokens_out=e.spent.get("out", 0))
            raise
        sheet = db.create_sheet(data, user)
        db.add_tokens(sheet["id"], spent["total"])
        sheet = db.set_state(sheet["id"], "checking", "лист собран, идёт проверка вёрстки") or sheet
        audit.write(user, "разобрана справка в лист A3", sheet["title"],
                    f"знаков в справке: {len(source)}, блоков: {len(data.get('blocks') or [])},"
                    f" токенов «Искры»: {spent['total']}", ip,
                    tokens_in=spent.get("in", 0), tokens_out=spent.get("out", 0))
        return {"sheet": db.get_sheet(sheet["id"]) or sheet, "tokens": spent["total"]}

    try:
        job = jobs.submit("draft", user, work)
    except jobs.Busy as e:
        raise HTTPException(status_code=429, detail=str(e)) from None
    return jobs.view(job)


@app.post("/api/sheets/{sheet_id}/spark/fix", status_code=202)
def api_fix(sheet_id: int, body: FixIn, request: Request,
            user: dict = Depends(user_dep)) -> dict[str, Any]:
    """Замечания проверки вёрстки уходят обратно в «Искру» фоном, лист перезаписывается.

    Пределы кругов проверяются сразу: в очередь попадает только разрешённый круг.
    """
    sheet = sheet_or_404(sheet_id)
    if not body.warnings:
        raise HTTPException(status_code=400, detail="Нет замечаний для исправления")
    if db.fix_calls(sheet_id) >= MAX_TOTAL_ROUNDS:
        spent_so_far = sheet.get("tokens") or 0
        raise HTTPException(status_code=409, detail={
            "message": f"Лист уже отправляли в «Искру» {MAX_TOTAL_ROUNDS} раз"
                       + (f" (потрачено {spent_so_far} токенов)" if spent_so_far else "")
                       + " — дальше правьте руками",
            "fix_rounds": sheet["fix_rounds"]})
    if body.again:
        db.reset_fix_rounds(sheet_id)   # человек сам попросил ещё попытку
    elif sheet["fix_rounds"] >= MAX_ROUNDS:
        raise HTTPException(
            status_code=409,
            detail={"message": f"Лист уже прошёл {sheet['fix_rounds']} круга доработки — "
                               "дальше правьте вручную или запустите ещё круг кнопкой",
                    "fix_rounds": sheet["fix_rounds"]})
    db.bump_fix_call(sheet_id)    # обращение считаем до прогона: сорвётся — всё равно стоило денег
    n = db.bump_fix_round(sheet_id)
    db.set_state(sheet_id, "fixing",
                 f"«Искра» дорабатывает, круг {n} из {MAX_ROUNDS}: замечаний {len(body.warnings)}")
    ip = portal.client_ip(request)

    def work() -> dict[str, Any]:
        try:
            fixed, spent = spark.fix(body.data, body.warnings)
        except spark.SparkError as e:
            db.set_state(sheet_id, "warn", f"«Искра» не помогла: {e}")
            # неудачный круг тоже стоил токенов — контракт с порталом, п. 4
            db.add_tokens(sheet_id, e.spent.get("total", 0))
            audit.write(user, "«Искра» не смогла исправить лист A3", sheet["title"],
                        f"замечаний было: {len(body.warnings)}; причина: {e}", ip,
                        tokens_in=e.spent.get("in", 0), tokens_out=e.spent.get("out", 0))
            raise
        if not spark.is_fix_of(body.data, fixed):
            # заглушка вместо листа: сохранить её поверх — значит потерять работу человека
            db.set_state(sheet_id, "warn", "«Искра» вернула не тот лист — правка не сохранена")
            total_spent = db.add_tokens(sheet_id, spent["total"])
            audit.write(user, "«Искра» вернула не тот лист A3", sheet["title"],
                        f"блоков было {len(body.data.get('blocks') or [])},"
                        f" вернулось {len(fixed.get('blocks') or [])};"
                        f" токенов «Искры»: {spent['total']} (всего по листу {total_spent})", ip,
                        tokens_in=spent.get("in", 0), tokens_out=spent.get("out", 0))
            raise RuntimeError("«Искра» прислала вместо доработанного листа другой — лист оставлен"
                               " как был. Попробуйте ещё раз или поправьте вручную.")
        try:
            saved = db.save_sheet(sheet_id, fixed, user, body.version, "", "правка по замечаниям")
        except db.Conflict as conflict:
            # «Искра» отработала и токены ушли, но пока она думала, лист поправили
            # руками — правку не сохраняем, а расход считаем (контракт п. 4)
            db.set_state(sheet_id, "warn", f"лист изменил {conflict.by}")
            total_spent = db.add_tokens(sheet_id, spent["total"])
            audit.write(user, "правка «Искрой» не сохранена: лист изменили", sheet["title"],
                        f"лист изменил {conflict.by} в {conflict.at}; версия {body.version} →"
                        f" {conflict.version}; токенов «Искры»: {spent['total']}"
                        f" (всего по листу {total_spent})", ip,
                        tokens_in=spent.get("in", 0), tokens_out=spent.get("out", 0))
            raise RuntimeError(f"Лист изменил {conflict.by} в {conflict.at}. Обновите страницу.") from None
        if not saved:
            raise RuntimeError("Лист не найден")
        total = db.add_tokens(sheet_id, spent["total"])
        audit.write(user, "лист A3 исправлен «Искрой»", saved["title"],
                    f"замечаний было: {len(body.warnings)}; версия {body.version} → {saved['version']};"
                    f" токенов «Искры»: {spent['total']} (всего по листу {total})", ip,
                    tokens_in=spent.get("in", 0), tokens_out=spent.get("out", 0))
        return {"sheet": db.get_sheet(sheet_id) or saved, "tokens": spent["total"]}

    try:
        job = jobs.submit("fix", user, work, sheet_id=sheet_id,
                          beat=lambda: db.touch_state(sheet_id))
    except jobs.Busy as e:
        db.set_state(sheet_id, "warn", "очередь разборов заполнена — попробуйте позже")
        raise HTTPException(status_code=429, detail=str(e)) from None
    return jobs.view(job)


@app.get("/api/spark/jobs/{job_id}")
def api_job(job_id: str, user: dict = Depends(user_dep)) -> dict[str, Any]:
    """Как идёт разбор: очередь, работа, готовый лист или понятная ошибка."""
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Разбор не найден: похоже, сервис перезапускали. "
                                                    "Откройте библиотеку — лист мог успеть сохраниться.")
    return jobs.view(job)


class EventIn(BaseModel):
    kind: str = Field(max_length=20)
    meeting: str = Field(default="", max_length=300)   # короткое название совещания
    how: str = Field(default="", max_length=40)        # «файлом в папку» или «ссылкой»
    format: str = Field(default="", max_length=10)     # html или json — для скачивания из редактора
    version: int | None = None                         # какую версию смотрят: старую тоже печатают


HOW = {"файлом в папку", "ссылкой"}
FORMATS = {"html", "json"}
EVENT_PAUSE = 10.0      # секунд: одно и то же событие того же человека по тому же листу
_last_event: dict[tuple, float] = {}
# Общий предел поверх точного ключа: разные названия совещаний — законно разные
# события, но скрипт с тысячей выдуманных названий обходил бы точный ключ. Живой
# человек за 10 секунд не приложит один лист больше чем к пяти совещаниям.
EVENT_BURST = 5
_burst: dict[tuple, list[float]] = {}
_event_lock = threading.Lock()   # запросы идут из пула потоков, а чистка перебирает словари


# Что делают с готовым листом, сервер сам не видит: печать идёт из браузера, файл
# из редактора собирается на странице, а к совещанию лист кладёт браузер прямо
# соседям (контракт п. 9). Страница сообщает об этом сюда, запись уходит в журнал —
# иначе воронка «разбор → лист → совещание» обрывалась на сохранении.
EVENTS = {
    "print": "лист A3 отправлен на печать",
    "download": "скачан лист A3",
    "attach": "лист A3 приложен к совещанию",
}


@app.post("/api/sheets/{sheet_id}/event", status_code=204)
def api_event(sheet_id: int, body: EventIn, request: Request,
              user: dict = Depends(user_dep)) -> Response:
    action = EVENTS.get(body.kind)
    if not action:
        raise HTTPException(status_code=400, detail="Неизвестное событие листа")
    # в журнал уходит только знакомое: свободный текст здесь — это чужой текст в журнале
    how = body.how.strip()
    fmt = (body.format or "html").strip().lower()
    if how and how not in HOW:
        raise HTTPException(status_code=400, detail="Неизвестный способ приложить лист")
    if fmt not in FORMATS:
        raise HTTPException(status_code=400, detail="Неизвестный формат файла")
    sheet = sheet_or_404(sheet_id)

    # Серверный предел: страница и так не шлёт одно событие дважды подряд, но журнал
    # не должен раздуваться от скрипта, который шлёт их пачкой. Повтор — не ошибка,
    # просто не пишем.
    version = body.version if body.version and 0 < body.version <= sheet["version"] else sheet["version"]
    # Повтор — это то же самое событие: тот же человек, лист, версия, формат и
    # совещание. Приложить лист к двум разным совещаниям или напечатать две версии
    # подряд — законные разные события, их предел не глушит.
    key = (user["login"], sheet_id, body.kind, version,
           fmt if body.kind == "download" else "",
           body.meeting.strip().lower() if body.kind == "attach" else "")
    now = time.monotonic()
    coarse = (user["login"], sheet_id, body.kind)
    with _event_lock:
        if now - _last_event.get(key, -EVENT_PAUSE) < EVENT_PAUSE:
            return Response(status_code=204)
        recent = [s for s in _burst.get(coarse, []) if now - s < EVENT_PAUSE]
        if len(recent) >= EVENT_BURST:
            _burst[coarse] = recent
            return Response(status_code=204)
        recent.append(now)
        _burst[coarse] = recent
        _last_event[key] = now
        if len(_last_event) > 5000:          # память не копится: старые метки не нужны
            for k in [k for k, v in _last_event.items() if now - v > EVENT_PAUSE]:
                _last_event.pop(k, None)
        if len(_burst) > 5000:
            for k in [k for k, v in _burst.items() if not v or now - v[-1] > EVENT_PAUSE]:
                _burst.pop(k, None)

    if body.kind == "attach":
        details = (f"совещание «{body.meeting.strip()}»" if body.meeting.strip() else "совещание") + \
                  (f", {how}" if how else "")
    elif body.kind == "download":
        details = f"файл {fmt.upper()} из редактора, версия {version}"
    else:
        details = f"версия {version}"
    audit.write(user, action, sheet["title"], details, portal.client_ip(request))
    return Response(status_code=204)


@app.post("/api/sheets/{sheet_id}/state")
def api_state(sheet_id: int, body: StateIn, user: dict = Depends(user_dep)) -> dict[str, Any]:
    """Браузер сообщает, чем кончилась проверка: это видно в списке листов."""
    sheet_or_404(sheet_id)
    sheet = db.set_state(sheet_id, body.state, body.note)
    if not sheet:
        raise HTTPException(status_code=404, detail="Лист не найден")
    return sheet


@app.exception_handler(HTTPException)
def http_error(request: Request, exc: HTTPException) -> JSONResponse:
    detail = exc.detail if isinstance(exc.detail, dict) else {"message": str(exc.detail)}
    return JSONResponse(detail, status_code=exc.status_code)


@app.exception_handler(Exception)
def any_error(request: Request, exc: Exception) -> JSONResponse:
    """Даже неожиданная ошибка возвращается как JSON: страница показывает
    человеческое сообщение, а не «Internal Server Error» в разборе ответа."""
    traceback.print_exception(type(exc), exc, exc.__traceback__)
    return JSONResponse(
        {"message": f"Внутренняя ошибка сервиса: {type(exc).__name__}: {exc}"[:300]},
        status_code=500,
    )
