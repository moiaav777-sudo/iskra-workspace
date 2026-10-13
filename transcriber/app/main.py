"""Сервис «Транскрибатор»: запись → «Искра» → три документа.

Решение пользователя 02.10: человек загружает файл записи и нажимает «Разобрать с Искрой»;
«Искра» расшифровывает запись дословно (`app/engine/iskra_engine.py`), затем пишет саммари
(`app/summarizer.py`) и перечень поручений (`app/orders.py`). Каждый документ можно поправить
(с версией) и скачать в Word; три документа можно приложить к совещанию («Совещание»,
вкладка «Результаты» — запросы идут из браузера человека, как у «Брифа» и A3).

Сервис живёт под префиксом `/transcriber/`, префикс снимает nginx, поэтому все адреса
в интерфейсе относительные. Вход и журнал — по контракту из `../../CLAUDE.md`.
"""
from __future__ import annotations

import json
import re
import threading
import time
import traceback
from html import escape
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import (audit, config, db, doc_html, docx_export, engine as engine_mod, jobs, media,
               orders as orders_mod, orders_docx, portal, richtext_common, summarizer)

app = FastAPI(title="Транскрибатор", docs_url=None, redoc_url=None)


class FreshStatic(StaticFiles):
    """Стили и скрипты браузер сверяет с сервисом каждый раз (как у A3 и «Модератора»):
    без этого после выкладки остаётся старый JS, который ждёт от сервиса прежних ответов."""

    def file_response(self, *args: Any, **kwargs: Any) -> Response:
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        return response


app.mount("/static", FreshStatic(directory=config.APP_DIR.parent / "static"), name="static")


@app.on_event("startup")
def startup() -> None:
    db.init()
    config.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    # задания живут только в памяти — перезапуск сервиса их всегда теряет (DEV-CHECKLIST, п. 3)
    db.interrupt_stale()
    media.clear_upload_dir()


def user_dep(request: Request) -> dict[str, str]:
    portal.check_origin(request)
    return portal.current_user(request)


def record_or_404(record_id: int) -> dict[str, Any]:
    record = db.get_record(record_id)
    if not record:
        raise HTTPException(status_code=404, detail="Запись не найдена")
    return record


# ── страницы ─────────────────────────────────────────────────────────────────

# Адреса соседей отдаёт сервер, как у A3 и «Брифа»: совещание — общая ось (../../CLAUDE.md,
# п. 9), карточку ведёт «Совещание» (moderator). Зовёт их браузер вошедшего человека
# (same-origin за порталом): прав это не добавляет, ошибку видит тот, кто нажал. На стенде
# без портала (`TRANSCRIBER_DEV=1`) соседей нет — адресов не отдаём, кнопка не видна.
NEIGHBOURS = {
    "meetings": "/moderator/api/meetings",
    "find_or_create": "/moderator/api/meetings/find-or-create",
    "results": "/moderator/api/meetings/{id}/results",
    "meeting_page": "/moderator/#meeting=",
}


@app.get("/", response_class=HTMLResponse)
def index(request: Request, user: dict = Depends(user_dep)) -> HTMLResponse:
    html = (config.APP_DIR.parent / "static" / "index.html").read_text(encoding="utf-8")
    html = html.replace("{{ROLE}}", escape(user["role"], quote=True))
    html = html.replace("{{NAME}}", escape(user["name"], quote=True))
    links = {} if config.DEV else NEIGHBOURS
    html = html.replace("{{LINKS}}", escape(json.dumps(links, ensure_ascii=False), quote=True))
    html = html.replace("{{MAX_MB}}", str(config.MAX_FILE_BYTES // (1024 * 1024)))
    return HTMLResponse(html, headers={"Cache-Control": "no-store"})


@app.get("/healthz", response_class=PlainTextResponse)
def healthz() -> str:
    return "ok"


@app.get("/api/engine")
def api_engine(user: dict = Depends(user_dep)) -> dict[str, Any]:
    """Какой движок распознавания сейчас работает: «Искра», резерв или подмена (см. app/engine)."""
    out = dict(engine_mod.engine_status(config.ENGINE))
    out["max_mb"] = config.MAX_FILE_BYTES // (1024 * 1024)
    return out


# ── список и карточка записи ──────────────────────────────────────────────────

@app.get("/api/records")
def api_list(q: str = "", archived: bool = False, user: dict = Depends(user_dep)) -> dict[str, Any]:
    return {"records": db.list_records(q=q, archived=archived), "role": user["role"]}


@app.get("/api/search")
def api_search(q: str = "", limit: int = 5, user: dict = Depends(user_dep)) -> dict[str, Any]:
    """Поиск для портала: он спрашивает все сервисы сразу и показывает общий список.

    По содержимому записи не ищем — оно не должно утекать даже в подсказки поиска
    (приватность записи, `../CLAUDE.md` → задача, п. 7); ищем только по названию.
    """
    query = (q or "").strip()
    if len(query) < 2:
        return {"items": []}
    limit = max(1, min(int(limit or 5), 20))
    found = db.list_records(q=query)[:limit]
    return {"items": [{
        "title": card["title"],
        "url": f"/transcriber/#record={card['id']}",
        "when": (card["updated_at"] or "")[:10],
        "who": card["updated_name"] or card["created_name"] or "",
        "kind": "запись",
    } for card in found]}


@app.get("/api/records/{record_id}")
def api_get(record_id: int, user: dict = Depends(user_dep)) -> dict[str, Any]:
    return record_or_404(record_id)


@app.post("/api/records/{record_id}/archive")
def api_archive(record_id: int, request: Request, archived: bool = True,
               user: dict = Depends(user_dep)) -> dict[str, Any]:
    record = record_or_404(record_id)
    updated = db.set_archived(record_id, archived)
    if not updated:
        raise HTTPException(status_code=404, detail="Запись не найдена")
    action = "запись убрана в архив" if archived else "запись возвращена из архива"
    audit.write(user, action, record["title"], "в работе → архив" if archived else "архив → в работе",
               portal.client_ip(request))
    return updated


@app.delete("/api/records/{record_id}")
def api_delete(record_id: int, request: Request, user: dict = Depends(user_dep)) -> dict[str, Any]:
    """Удаление насовсем — необратимое действие, только администратору (контракт п. 2)."""
    portal.require_admin(user)
    record = record_or_404(record_id)
    if db.delete_record(record_id):
        audit.write(user, "запись удалена насовсем", record["title"], "", portal.client_ip(request))
    return {"deleted": record_id}


class TextIn(BaseModel):
    text: str = ""
    # единый формат блоков (решение пользователя 13.10): правка с оформлением; задан — текст
    # выводится из него, `text` не читается. Не задан — правка простым текстом, как раньше.
    html: str | None = None
    version: int | None = None


# Предел HTML правки (ревью RC-2026-10-13-2, Н-3): транскрипция с оформлением — обычно десятки КБ,
# а разбор мегабайтов разметки занимает поток запроса на секунды
MAX_DOC_HTML = 1_000_000


def _styled(body: TextIn, sep: str) -> tuple[str, str]:
    """(текст, очищенный HTML) правки. HTML чистится на сервере всегда — что бы ни прислал
    браузер (`richtext_common.clean`, белый список общей панели); текст — его абзацы через `sep`."""
    if body.html is None:
        return body.text, ""
    if len(body.html) > MAX_DOC_HTML:
        raise HTTPException(status_code=413, detail=(
            f"Текст с оформлением слишком большой: {len(body.html):,} знаков разметки, предел — "
            f"{MAX_DOC_HTML:,}. Снимите часть оформления (кнопка «Снять оформление») или разделите "
            "документ.").replace(",", " "))
    html = richtext_common.clean(body.html)
    text = sep.join(richtext_common.paragraphs(html))
    return text, html if text.strip() else ""


class OrdersIn(BaseModel):
    orders: list[Any] = []
    version: int | None = None


class MetaIn(BaseModel):
    chair: str = ""
    chair_role: str = ""
    meeting_date: str = ""


def _conflict(conflict: db.Conflict, what: str) -> HTTPException:
    return HTTPException(status_code=409, detail={
        "message": f"{what} изменил {conflict.by} в {conflict.at}. Обновите карточку,"
                   f" чтобы не затереть чужую правку.",
        "version": conflict.version,
    })


@app.put("/api/records/{record_id}/text")
def api_save_text(record_id: int, body: TextIn, request: Request,
                  user: dict = Depends(user_dep)) -> dict[str, Any]:
    """Правка транскрипции руками. Версия обязательна — иначе можно затереть то, что
    дописала «Искра» или другой человек в другой вкладке (DEV-CHECKLIST, п. 2)."""
    if body.version is None:
        raise HTTPException(status_code=400, detail="Не указана версия черновика")
    before = record_or_404(record_id)
    # абзацы — через пустую строку: по ним docx_export ищет таймкод и говорящего
    text, html = _styled(body, "\n\n")
    html = doc_html.mark_stamps(html) if html else ""      # таймкод — <span class="tc">, Quill его снимает
    try:
        record = db.save_draft_text(record_id, text, user, body.version, html=html)
    except db.Conflict as conflict:
        raise _conflict(conflict, "Транскрипцию") from None
    if not record:
        raise HTTPException(status_code=404, detail="Запись не найдена")
    audit.write(user, "исправлена транскрипция записи", before["title"],
               f"версия {before['draft_version']} → {record['draft_version']}, знаков: {len(text)}"
               + (", с оформлением" if html else ""), portal.client_ip(request))
    return record


@app.put("/api/records/{record_id}/summary")
def api_save_summary(record_id: int, body: TextIn, request: Request,
                     user: dict = Depends(user_dep)) -> dict[str, Any]:
    """Правка саммари руками — с версией, как у транскрипции."""
    if body.version is None:
        raise HTTPException(status_code=400, detail="Не указана версия саммари")
    before = record_or_404(record_id)
    text, html = _styled(body, "\n")
    try:
        record = db.save_summary_text(record_id, text, user, body.version, html=html)
    except db.Conflict as conflict:
        raise _conflict(conflict, "Саммари") from None
    if not record:
        raise HTTPException(status_code=404, detail="Запись не найдена")
    audit.write(user, "исправлено саммари записи", before["title"],
               f"версия {before['summary_version']} → {record['summary_version']},"
               f" знаков: {len(text)}" + (", с оформлением" if html else ""), portal.client_ip(request))
    return record


@app.put("/api/records/{record_id}/orders")
def api_save_orders(record_id: int, body: OrdersIn, request: Request,
                    user: dict = Depends(user_dep)) -> dict[str, Any]:
    """Правка перечня поручений руками — с версией. Поручения без текста отбрасываются,
    срок приводится к ДД.ММ.ГГГГ (или остаётся словами, если так вписан)."""
    if body.version is None:
        raise HTTPException(status_code=400, detail="Не указана версия перечня поручений")
    before = record_or_404(record_id)
    try:
        cleaned = orders_mod.from_human(body.orders)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from None
    try:
        record = db.save_orders(record_id, cleaned, user, body.version)
    except db.Conflict as conflict:
        raise _conflict(conflict, "Перечень поручений") from None
    if not record:
        raise HTTPException(status_code=404, detail="Запись не найдена")
    audit.write(user, "исправлен перечень поручений записи", before["title"],
               f"версия {before['orders_version']} → {record['orders_version']},"
               f" поручений: {len(before['orders'])} → {len(cleaned)}", portal.client_ip(request))
    return record


def _meeting_date(value: str) -> str:
    text = (value or "").strip()
    if not text:
        return ""
    parsed = orders_docx.parse_deadline(text)
    if not parsed:
        raise HTTPException(status_code=400, detail="Дата совещания — в виде ДД.ММ.ГГГГ")
    return f"{parsed:%d.%m.%Y}"


@app.put("/api/records/{record_id}/meta")
def api_save_meta(record_id: int, body: MetaIn, request: Request,
                  user: dict = Depends(user_dep)) -> dict[str, Any]:
    """Председатель, должность и дата совещания: шапка и подпись перечня, даты в документах."""
    before = record_or_404(record_id)
    chair = " ".join(orders_docx.dash(body.chair).split())[:200]
    role = orders_docx.dash(body.chair_role).replace("\r", "").strip()[:300]
    when = _meeting_date(body.meeting_date)
    record = db.set_meta(record_id, user, chair, role, when)
    if not record:
        raise HTTPException(status_code=404, detail="Запись не найдена")
    changes = [f"{label}: «{before[key] or '—'}» → «{record[key] or '—'}»"
               for key, label in (("chair", "председатель"), ("chair_role", "должность"),
                                  ("meeting_date", "дата"))
               if before[key] != record[key]]
    if changes:
        audit.write(user, "изменены сведения для документов записи", before["title"],
                   "; ".join(changes)[:300], portal.client_ip(request))
    return record


# ── загрузка и фоновая обработка ───────────────────────────────────────────────

ZERO = {"in": 0, "out": 0, "total": 0}


def _duration_of(segments: list) -> float:
    marks = [x for s in segments for x in (getattr(s, "end", None), getattr(s, "start", None))
             if isinstance(x, (int, float))]
    return float(max(marks)) if marks else 0.0


def _spent_of(eng: Any) -> dict[str, int]:
    spent = getattr(eng, "spent", None)
    return dict(spent) if isinstance(spent, dict) else dict(ZERO)


def _documents(record_id: int, title: str, user: dict[str, str], ip: str,
               cancelled, stage, note: str = "") -> dict[str, Any]:
    """Саммари и поручения; любой непредвиденный сбой не оставляет запись «в обработке»
    (RC-19, Н-14: иначе «Пересобрать» отвечало 409 до перезапуска сервиса)."""
    try:
        return _documents_steps(record_id, title, user, ip, cancelled, stage, note)
    except Exception as e:
        current = db.get_record(record_id)
        if current and current["state"] in db.WORKING:
            db.set_state(record_id, "error", "Сбой при разборе ответа «Искры» — попробуйте «Пересобрать»")
            audit.write(user, "сбой при подготовке документов записи", title,
                       f"ошибка: {type(e).__name__}", ip)
            raise RuntimeError("Сбой при разборе ответа «Искры» — попробуйте «Пересобрать»") from None
        raise


def _documents_steps(record_id: int, title: str, user: dict[str, str], ip: str,
                     cancelled, stage, note: str = "") -> dict[str, Any]:
    """Саммари и перечень поручений по текущей транскрипции записи — два прогона «Искры»,
    у каждого своё событие в журнале со своим расходом (контракт п. 4, DEV-CHECKLIST п. 4).

    Версии саммари и перечня берутся со старта: человек успел их поправить, пока «Искра»
    работала, — его правку не затираем, а говорим об этом (DEV-CHECKLIST п. 2).
    """
    record = db.get_record(record_id)
    draft_text = record["draft_text"]
    base_summary, base_orders = record["summary_version"], record["orders_version"]
    facts = f"знаков транскрипции: {len(draft_text)}"
    notes = [note] if note else []

    stage("«Искра» пишет саммари…")
    db.set_state(record_id, "summarizing", "«Искра» пишет саммари")
    try:
        summary, spent = summarizer.summarize(draft_text, cancelled)
    except summarizer.Cancelled as e:
        db.add_tokens(record_id, e.spent.get("total", 0))
        db.set_state(record_id, "cancelled", "саммари отменено")
        audit.write(user, "саммари записи отменено", title, facts, ip,
                   tokens_in=e.spent.get("in", 0), tokens_out=e.spent.get("out", 0))
        raise jobs.Cancelled(str(e)) from None
    except summarizer.SummaryError as e:
        db.add_tokens(record_id, e.spent.get("total", 0))
        db.set_state(record_id, "error", f"«Искра» не сделала саммари: {e}"[:200])
        audit.write(user, "«Искра» не сделала саммари записи", title,
                   f"{facts}, причина: {e}"[:280], ip,
                   tokens_in=e.spent.get("in", 0), tokens_out=e.spent.get("out", 0))
        raise RuntimeError(str(e)) from None
    kept = db.set_summary(record_id, summary, spent.get("total", 0), state="ordering",
                          note="«Искра» составляет перечень поручений", base_version=base_summary)
    if not kept:
        notes.append("саммари поправили, пока «Искра» работала, — новое не записано")
    audit.write(user, "саммари записи готово", title,
               f"{facts}, токенов «Искры»: {spent.get('total', 0)}"
               + ("" if kept else "; правка человека сохранена, ответ «Искры» не записан"), ip,
               tokens_in=spent.get("in", 0), tokens_out=spent.get("out", 0))

    stage("«Искра» составляет перечень поручений…")
    try:
        found, spent = orders_mod.extract(draft_text, cancelled)
    except orders_mod.Cancelled as e:
        db.add_tokens(record_id, e.spent.get("total", 0))
        db.set_state(record_id, "cancelled", "перечень поручений отменён")
        audit.write(user, "перечень поручений записи отменён", title, facts, ip,
                   tokens_in=e.spent.get("in", 0), tokens_out=e.spent.get("out", 0))
        raise jobs.Cancelled(str(e)) from None
    except orders_mod.OrdersError as e:
        db.add_tokens(record_id, e.spent.get("total", 0))
        db.set_state(record_id, "error", f"«Искра» не составила перечень поручений: {e}"[:200])
        audit.write(user, "«Искра» не составила перечень поручений записи", title,
                   f"{facts}, причина: {e}"[:280], ip,
                   tokens_in=e.spent.get("in", 0), tokens_out=e.spent.get("out", 0))
        raise RuntimeError(str(e)) from None
    kept = db.set_orders(record_id, found, spent.get("total", 0), state="done",
                         note="; ".join(notes), base_version=base_orders)
    if not kept:
        notes.append("перечень поручений поправили, пока «Искра» работала, — новый не записан")
        db.set_state(record_id, "done", "; ".join(notes))
    audit.write(user, "перечень поручений записи готов", title,
               f"поручений: {len(found)}, токенов «Искры»: {spent.get('total', 0)}"
               + ("" if kept else "; правка человека сохранена, ответ «Искры» не записан"), ip,
               tokens_in=spent.get("in", 0), tokens_out=spent.get("out", 0))
    return {"record": db.get_record(record_id)}


def _work(record_id: int, path: Path, title: str, user: dict[str, str], ip: str, job_id: str,
         probed_duration: float | None = None):
    def cancelled() -> bool:
        job = jobs.get(job_id)
        return bool(job and job.cancel_event.is_set())

    def stage(text: str) -> None:
        jobs.set_stage(job_id, text)

    def work_fn() -> dict[str, Any]:
        iskra = config.ENGINE in engine_mod.ISKRA
        stage("«Искра» расшифровывает запись…" if iskra else "Распознаю речь…")
        db.set_state(record_id, "transcribing",
                     "«Искра» расшифровывает запись" if iskra else "идёт распознавание речи")
        try:
            eng = engine_mod.get_engine(config.ENGINE)
        except engine_mod.TranscribeUnavailable as e:
            media.discard(path)
            db.set_state(record_id, "error", str(e)[:200])
            audit.write(user, "запись не разобрана: распознавание не настроено", title,
                       str(e)[:200], ip)
            raise RuntimeError(str(e)) from None
        if hasattr(eng, "progress"):
            eng.progress = stage                 # ход прогона «Искры» — в строку задания
        try:
            segments = eng.transcribe(path, cancelled)
        except engine_mod.Cancelled as e:
            spent = _spent_of(eng)
            db.add_tokens(record_id, spent["total"])
            db.set_state(record_id, "cancelled", "расшифровка отменена")
            audit.write(user, "расшифровка записи отменена", title, "", ip,
                       tokens_in=spent["in"], tokens_out=spent["out"])
            raise jobs.Cancelled(str(e)) from None
        except engine_mod.TranscribeError as e:
            spent = _spent_of(eng)
            db.add_tokens(record_id, spent["total"])
            db.set_state(record_id, "error", f"Запись не расшифрована: {e}"[:200])
            audit.write(user, "запись не расшифрована", title, str(e)[:200], ip,
                       tokens_in=spent["in"], tokens_out=spent["out"])
            raise RuntimeError(str(e)) from None
        finally:
            # запись на сервере больше не нужна — приватность (п. 7), файл на диске недолговечен
            media.discard(path)

        spent = _spent_of(eng)
        note = str(getattr(eng, "note", "") or "")
        duration = probed_duration if probed_duration else _duration_of(segments)
        seg_dicts = [s.to_dict() for s in segments]
        db.set_result(record_id, seg_dicts, duration, eng.name, state="summarizing",
                      note=note or "«Искра» пишет саммари")
        db.add_tokens(record_id, spent["total"])
        draft_text = db.get_record(record_id)["draft_text"]
        parts = getattr(eng, "parts", 0)
        audit.write(user, "запись расшифрована", title,
                   f"длительность {duration:.0f} с, знаков транскрипции: {len(draft_text)}"
                   + (f", прогонов «Искры»: {parts}" if parts else "")
                   + (", текст — файлом из черновиков «Искры»"
                      if getattr(eng, "source", "") == "file" else "")
                   + (f"; {note}" if note else ""), ip,
                   tokens_in=spent["in"], tokens_out=spent["out"])

        if not draft_text.strip():
            db.set_state(record_id, "done", "речь не найдена — саммари и поручения не делали")
            return {"record": db.get_record(record_id)}
        return _documents(record_id, title, user, ip, cancelled, stage, note=note)

    return work_fn


_rebuild_lock = threading.Lock()


@app.post("/api/records/{record_id}/rebuild", status_code=202)
def api_rebuild(record_id: int, request: Request, user: dict = Depends(user_dep)) -> dict[str, Any]:
    """Саммари и перечень поручений заново по текущей транскрипции — после правки текста
    или если «Искра» сорвалась на этих шагах (расшифровку повторять не нужно)."""
    ip = portal.client_ip(request)
    job_id = jobs.new_id()

    def cancelled() -> bool:
        job = jobs.get(job_id)
        return bool(job and job.cancel_event.is_set())

    def stage(text: str) -> None:
        jobs.set_stage(job_id, text)

    with _rebuild_lock:     # двойное нажатие — одно задание (DEV-CHECKLIST, п. 2)
        record = record_or_404(record_id)
        active = jobs.by_record(record_id)
        if active and active.status in (jobs.QUEUED, jobs.RUNNING):
            out = jobs.view(active)
            out["record"] = record
            return out
        if record["state"] in db.WORKING:
            raise HTTPException(status_code=409, detail="Запись ещё обрабатывается — дождитесь конца")
        if not (record["draft_text"] or "").strip():
            raise HTTPException(status_code=400, detail="Транскрипции нет — пересобирать не из чего")
        title = record["title"]

        def work(_cancelled) -> dict[str, Any]:
            return _documents(record_id, title, user, ip, cancelled, stage)

        db.set_state(record_id, "summarizing", "«Искра» пишет саммари")
        try:
            job = jobs.submit(user["login"], record_id, work, job_id=job_id)
        except jobs.Busy as e:
            db.set_state(record_id, record["state"], record["state_note"])
            raise HTTPException(status_code=429, detail=str(e)) from None
    out = jobs.view(job)
    out["record"] = db.get_record(record_id)
    return out


MAX_TITLE = 200

# ── повтор одной и той же отправки не заводит вторую запись (DEV-CHECKLIST, п. 2) ──
# Двойной клик или повтор браузера после обрыва шлёт тот же `client_key` — второй
# запрос получает ту же запись и то же задание, а не заводит дубликат.
_UPLOAD_KEY = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
_UPLOAD_KEY_TTL = 180.0
_recent_uploads: dict[str, dict[str, Any]] = {}
_recent_lock = threading.Lock()


def _claim_upload_key(key: str) -> tuple[bool, dict[str, Any] | None]:
    """Первый с этим ключом получает `(True, None)` и делает загрузку сам.
    Опоздавший — `(False, None)`, пока первый ещё работает, либо `(False, готовый_ответ)`."""
    with _recent_lock:
        entry = _recent_uploads.get(key)
        if entry is None or (entry.get("out") is not None
                             and time.monotonic() - entry["at"] > _UPLOAD_KEY_TTL):
            _recent_uploads[key] = {"out": None, "at": time.monotonic()}
            return True, None
        return False, entry.get("out")


def _finish_upload_key(key: str, out: dict[str, Any] | None) -> None:
    with _recent_lock:
        if out is None:
            _recent_uploads.pop(key, None)   # не удалось — освобождаем ключ для повтора
        else:
            _recent_uploads[key] = {"out": out, "at": time.monotonic()}
        if len(_recent_uploads) > 1000:
            now = time.monotonic()
            for k, v in list(_recent_uploads.items()):
                if v.get("out") is not None and now - v["at"] > _UPLOAD_KEY_TTL:
                    _recent_uploads.pop(k, None)


@app.post("/api/records", status_code=202)
async def api_upload(request: Request, file: UploadFile = File(...),
                     client_key: str = Form(default=""),
                     user: dict = Depends(user_dep)) -> dict[str, Any]:
    """Загрузка записи: сохраняем файл, заводим карточку и запускаем обработку фоном.

    Ответ приходит сразу с номером задания — распознавание длинной записи идёт
    минутами, держать на нём HTTP-соединение нельзя (как у A3 и «Тезисов»).
    """
    key = f"{user['login']}:{client_key}" if _UPLOAD_KEY.match(client_key or "") else ""
    if key:
        for _ in range(40):
            mine, existing = _claim_upload_key(key)
            if mine:
                break
            if existing is not None:
                return existing
            time.sleep(0.05)
        else:
            raise HTTPException(status_code=429, detail="Эта запись уже загружается — подождите")

    try:
        title = (file.filename or "Запись без названия")[:MAX_TITLE]
        try:
            path = media.save_upload(file)
            duration = media.check_duration(path)
        except media.BadUpload as e:
            raise HTTPException(status_code=413 if "МБ" in str(e) else 400, detail=str(e)) from None
        ip = portal.client_ip(request)
        record = db.create_record(user, title)
        audit.write(user, "загружена запись", record["title"],
                   f"длительность {duration:.0f} с" if duration else "длительность не определена", ip)

        job_id = jobs.new_id()
        work_fn = _work(record["id"], path, record["title"], user, ip, job_id, probed_duration=duration)
        try:
            job = jobs.submit(user["login"], record["id"], lambda _cancelled: work_fn(), job_id=job_id)
        except jobs.Busy as e:
            media.discard(path)
            db.delete_record(record["id"])
            raise HTTPException(status_code=429, detail=str(e)) from None
        out = jobs.view(job)
        out["record"] = record
    except Exception:
        if key:
            _finish_upload_key(key, None)
        raise
    if key:
        _finish_upload_key(key, out)
    return out


@app.get("/api/jobs/{job_id}")
def api_job(job_id: str, user: dict = Depends(user_dep)) -> dict[str, Any]:
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Задание не найдено: похоже, сервис "
                                                    "перезапускали. Откройте список записей — "
                                                    "результат мог успеть сохраниться.")
    return jobs.view(job)


@app.get("/api/records/{record_id}/job")
def api_record_job(record_id: int, user: dict = Depends(user_dep)) -> dict[str, Any]:
    """Последнее задание по записи — страница восстанавливает индикатор после ухода."""
    record_or_404(record_id)
    job = jobs.by_record(record_id)
    return {"job": jobs.view(job) if job else None}


@app.post("/api/jobs/{job_id}/cancel")
def api_cancel(job_id: str, user: dict = Depends(user_dep)) -> dict[str, Any]:
    job = jobs.cancel(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Задание не найдено")
    return jobs.view(job)


# ── скачивание: три документа в Word ─────────────────────────────────────────

DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def _attachment(content: bytes, name: str, media_type: str = DOCX, fallback: str = "document.docx") -> Response:
    """Имя по-русски — в `filename*`; старым клиентам — латинский запасной вариант."""
    ascii_name = fallback
    return Response(content=content, media_type=media_type, headers={
        "Content-Disposition": f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(name)}",
        "Cache-Control": "no-store",
    })


EXPORTS = {
    # адрес → (сборка документа, что записать в журнал)
    "transcript": (docx_export.build_transcript, "транскрипция записи скачана в Word"),
    "summary": (docx_export.build_summary, "саммари записи скачано в Word"),
    "orders": (docx_export.build_orders, "перечень поручений записи скачан в Word"),
}


@app.get("/api/records/{record_id}/export/{kind}.docx")
def api_export_docx(record_id: int, kind: str, request: Request,
                    user: dict = Depends(user_dep)) -> Response:
    """Транскрипция, саммари, перечень поручений — каждый своим файлом Word.

    Этот же адрес отдаётся «Совещанию» как `docx_url` (кнопка «Скачать Word» на вкладке
    «Результаты»): человек скачивает его из браузера через портал, заголовки X-Portal-*
    ставит nginx; без них — 403, как у любого адреса сервиса."""
    if kind not in EXPORTS:
        raise HTTPException(status_code=404, detail="Нет такого документа")
    record = record_or_404(record_id)
    build, action = EXPORTS[kind]
    content = build(record)
    audit.write(user, action, record["title"], "", portal.client_ip(request))
    return _attachment(content, docx_export.names(record)[kind], fallback=f"{kind}-{record_id}.docx")


@app.get("/api/records/{record_id}/export/transcript.txt")
def api_export_txt(record_id: int, request: Request, user: dict = Depends(user_dep)) -> Response:
    record = record_or_404(record_id)
    audit.write(user, "запись скачана текстом", record["title"], "", portal.client_ip(request))
    parts = [record["title"], ""]
    if record.get("summary"):
        parts += ["Саммари", record["summary"], ""]
    if record.get("orders"):
        parts += ["Перечень поручений", _orders_text(record["orders"]), ""]
    parts += ["Транскрипция", record.get("draft_text") or "(текста нет)"]
    name = docx_export.names(record)["transcript"].replace(".docx", ".txt")
    return _attachment(("\n".join(parts)).encode("utf-8"), name, "text/plain; charset=utf-8",
                       fallback=f"transcript-{record_id}.txt")


# ── приложить к совещанию («Совещание», вкладка «Результаты») ────────────────────
# Решение пользователя 02.10: результаты транскрибатора ложатся в «Совещание». Документы
# кладёт браузер человека (`static/meeting.js`) запросом `POST /moderator/api/meetings/{id}/results`
# — по одному на каждый вид (transcript, summary, decisions); повтор с тем же `source.id`
# заменяет документы того же вида. Сервер готовит тела запросов и запоминает, куда приложено.

def _orders_text(orders: list[dict[str, str]]) -> str:
    lines = []
    for n, order in enumerate(orders_docx.clean_orders(orders), start=1):
        lines.append(f"{n}. {order['text']}")
        term = orders_docx.deadline_line(order["deadline"])
        if term:
            lines.append(term)
        lines.append("")
    return "\n".join(lines).strip()


def _orders_html(orders: list[dict[str, Any]]) -> str:
    """Перечень для «Совещания»: пункт с оформлением человека — его HTML, иначе текст (как до 13.10)."""
    items = []
    for raw in orders or []:
        cleaned = orders_docx.clean_orders([raw])
        if not cleaned:
            continue
        order = cleaned[0]
        term = orders_docx.deadline_line(order["deadline"])
        if isinstance(raw, dict) and doc_html.order_styled(raw):
            items.append(f"<li>{raw['html']}" + (f"<p>{escape(term)}</p>" if term else "") + "</li>")
        else:
            items.append(f"<li>{escape(order['text'])}" + (f"<br>{escape(term)}" if term else "") + "</li>")
    return f"<ol>{''.join(items)}</ol>" if items else ""


def results_payload(record: dict[str, Any]) -> list[dict[str, Any]]:
    """Три тела для `POST /moderator/api/meetings/{id}/results` — по контракту «Совещания»."""
    when = docx_export.date_ru(record)
    tail = f" {when}" if when else ""
    rid = str(record["id"])
    source = {"service": "transcriber", "id": rid, "url": f"/transcriber/#record={rid}"}
    base = f"/transcriber/api/records/{rid}/export"
    draft = record.get("draft_text") or ""
    summary = orders_docx.dash(record.get("summary") or "")
    orders = record.get("orders") or []
    # правка с оформлением (решение пользователя 13.10) уходит своим HTML, иначе — как до неё
    draft_html = record["draft_html"] if record.get("draft_styled") else doc_html.transcript_html(draft, mark=False)
    # тире в «Совещании» — как в Word и в тексте (ревью RC-2026-10-13-2, Н-5)
    summary_html = (orders_docx.dash(record["summary_html"]) if record.get("summary_styled")
                    else doc_html.paragraphs_html(summary))
    return [
        {"kind": "transcript", "title": f"Транскрипция совещания{tail}",
         "html": draft_html, "text": draft,
         "docx_url": f"{base}/transcript.docx", "source": source},
        {"kind": "summary", "title": f"Саммари совещания{tail}",
         "html": summary_html, "text": summary,
         "docx_url": f"{base}/summary.docx", "source": source},
        {"kind": "decisions", "title": f"Перечень поручений{tail}",
         "html": _orders_html(orders), "text": _orders_text(orders),
         "docx_url": f"{base}/orders.docx", "source": source},
    ]


@app.get("/api/records/{record_id}/results")
def api_results(record_id: int, user: dict = Depends(user_dep)) -> dict[str, Any]:
    record = record_or_404(record_id)
    if record["state"] in db.WORKING or not (record.get("draft_text") or "").strip():
        raise HTTPException(status_code=409, detail="Запись ещё не разобрана — прикладывать нечего")
    return {"date": docx_export.date_ru(record), "title": record["title"],
            "documents": results_payload(record)}


class MeetingIn(BaseModel):
    id: str = ""
    title: str = ""
    date: str = ""
    url: str = ""
    kinds: list[str] = []


KIND_WORDS = {"transcript": "транскрипция", "summary": "саммари", "decisions": "перечень поручений"}


@app.post("/api/records/{record_id}/meeting")
def api_meeting(record_id: int, body: MeetingIn, request: Request,
                user: dict = Depends(user_dep)) -> dict[str, Any]:
    """Браузер приложил документы к совещанию — запоминаем куда (ссылка «Открыть совещание»)
    и пишем в журнал. Сами документы сервер не видит: их кладёт браузер в «Совещание»."""
    record = record_or_404(record_id)
    meeting_id = (body.id or "").strip()[:100]
    if not meeting_id:
        raise HTTPException(status_code=400, detail="Не указано совещание")
    url = (body.url or "").strip()[:300]
    if not url.startswith("/moderator/"):
        url = NEIGHBOURS["meeting_page"] + quote(meeting_id)
    kinds = [k for k in body.kinds if k in KIND_WORDS]
    meeting = {"id": meeting_id, "title": " ".join((body.title or "").split())[:300],
               "date": (body.date or "").strip()[:10], "url": url, "kinds": kinds,
               "at": db.now(), "by": user["name"]}
    updated = db.set_meeting(record_id, meeting)
    if not updated:
        raise HTTPException(status_code=404, detail="Запись не найдена")
    audit.write(user, "документы записи приложены к совещанию", record["title"],
               f"совещание «{meeting['title'] or meeting_id}» {meeting['date']}: "
               + (", ".join(KIND_WORDS[k] for k in kinds) or "—"), portal.client_ip(request))
    return updated


@app.exception_handler(HTTPException)
def http_error(request: Request, exc: HTTPException) -> JSONResponse:
    detail = exc.detail if isinstance(exc.detail, dict) else {"message": str(exc.detail)}
    return JSONResponse(detail, status_code=exc.status_code)


@app.exception_handler(Exception)
def any_error(request: Request, exc: Exception) -> JSONResponse:
    """Даже неожиданная ошибка возвращается как JSON, а не «Internal Server Error»."""
    traceback.print_exception(type(exc), exc, exc.__traceback__)
    return JSONResponse(
        {"message": f"Внутренняя ошибка сервиса: {type(exc).__name__}: {exc}"[:300]},
        status_code=500,
    )
