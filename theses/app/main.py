"""Сервис «Тезисы»: тезисы выступления под конкретного человека.

Порт 8960 на 127.0.0.1, адрес за nginx — `/theses/`. Своего входа нет:
пользователь приходит в заголовках `X-Portal-*`, см. `portal.py`.

Два способа получить тезисы (`CLAUDE.md`): по теме, заданной руками, и из приложенных
документов. Правила написания берутся файлом из `docs/` по выбранному человеку.
"""
from __future__ import annotations

import re
import time
from pathlib import Path
from urllib.parse import quote, urlparse

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from starlette.concurrency import run_in_threadpool
from fastapi.responses import (FileResponse, JSONResponse, PlainTextResponse, RedirectResponse,
                               Response)
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import doc_text, docx_export, iskra, jobs, latin, moderator, portal, richtext, rules, storage
from .models import EMPHASIS, NORMAL, Bullet, Thesis

BASE_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = BASE_DIR / "static"

app = FastAPI(title="Тезисы", docs_url=None, redoc_url=None)
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


# ── вход, журнал, защита от чужих сайтов ─────────────────────────────────

@app.middleware("http")
async def portal_gate(request: Request, call_next):
    path = request.url.path
    if path == "/healthz":
        return await call_next(request)

    user = portal.user_from_headers(request.headers)
    if not user:
        if path.startswith("/api/"):
            return JSONResponse({"detail": "Нужен вход через портал"}, status_code=401)
        return RedirectResponse(portal.LOGIN_URL, status_code=303)

    if request.method not in ("GET", "HEAD", "OPTIONS"):
        origin = request.headers.get("origin", "")
        if origin and urlparse(origin).netloc != request.headers.get("host", ""):
            return JSONResponse({"detail": "Запрос с чужого сайта отклонён"}, status_code=403)

    request.state.user = user
    storage.set_current_login(user["login"])
    bucket = portal.start_request()
    response = await call_next(request)

    action = portal.describe(request.method, path)
    if action and response.status_code < 400 and not bucket.get("skip"):
        portal.send_audit(
            login=user["login"],
            action=action,
            # В журнале и на главной портала человек должен видеть название, а не внутренний номер
            object=_named(bucket["object"] or portal.thesis_id(path)),
            details="; ".join(bucket["changes"]),
            ip=request.client.host if request.client else "",
            tokens_in=bucket.get("tokens_in", 0), tokens_out=bucket.get("tokens_out", 0),
        )
    return response


def _named(obj: str) -> str:
    """Объект для журнала: «название» вместо идентификатора, если тезисы ещё на месте."""
    return storage.audit_name(obj)


@app.on_event("startup")
def _interrupted_jobs() -> None:
    """Перезапуск обрывает поток написания — человек не должен ждать вечно."""
    jobs.interrupt_stale()
    # слова вперемешку в старых текстах: что исправляется однозначно — исправить (22.09, п. 4)
    try:
        storage.heal_mixed(lambda t: latin.corpus(*(s.text for s in t.sources), t.topic, t.ask,
                                                  t.event, t._rules_text(), *t.texts()))
    except Exception:  # noqa: BLE001 — лечение старых текстов не должно мешать запуску
        pass


@app.get("/healthz", response_class=PlainTextResponse)
def healthz() -> str:
    return "ok"


@app.get("/")
def index():
    page = STATIC_DIR / "index.html"
    if not page.exists():
        return PlainTextResponse("Интерфейс ещё не собран", status_code=503)
    return FileResponse(page)


@app.get("/api/me")
def me(request: Request):
    return request.state.user


# ── вспомогательное ──────────────────────────────────────────────────────

def _stored_or_404(tid: str) -> storage.Stored:
    stored = storage.get(tid)
    if stored is None:
        raise HTTPException(status_code=404, detail="Тезисы не найдены")
    return stored


def _admin_only(request: Request) -> None:
    if not request.state.user["is_admin"]:
        raise HTTPException(status_code=403, detail="Действие доступно только администратору")


def _save(stored: storage.Stored, rev: int | None, summary: str) -> dict:
    """Сохранить с проверкой версии; при гонке — 409 и текущее состояние."""
    try:
        fresh = storage.save(stored.thesis, expected_rev=rev, summary=summary)
    except storage.Conflict as conflict:
        raise HTTPException(status_code=409, detail={
            "message": "Тезисы успели измениться — обновите страницу, набранное не потеряно",
            "current": conflict.stored.to_dict(),
        })
    portal.note_change(summary)
    return fresh.to_dict()


def _bullet_or_404(thesis: Thesis, bullet_id: str) -> Bullet:
    for bullet in thesis.bullets:
        if bullet.id == bullet_id:
            return bullet
    raise HTTPException(status_code=404, detail="Буллит не найден")


def _ask(text: str) -> str:
    """«Что нужно» — наговорённое голосом бывает длинным, но не бесконечным."""
    return latin.fix_mixed((text or "").strip()[:iskra.MAX_ASK_CHARS])[0]


def _mixed_or_409(thesis: Thesis, what: str) -> None:
    """Слово наполовину латиницей в документ и в совещание не уходит (задача 21.09).

    Однозначные («IТ», «прoграмма») исправляются сами при сохранении; сюда доходят
    только те, что исправить может лишь человек, — «zadанию».
    """
    words = thesis.mixed_words()
    if words:
        shown = ", ".join(f"«{w['word']}»" for w in words[:6])
        raise HTTPException(status_code=409, detail={
            "message": f"В тексте слова наполовину латиницей: {shown}. Поправьте их, "
                       f"и {what}.",
            "mixed_words": words,
        })


def _short(text: str, limit: int = 60) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[:limit].rstrip() + "…"


# ── правила и профили ────────────────────────────────────────────────────

@app.get("/api/rules")
def rules_state():
    """Что лежит в `docs/`: файлы правил и образцы оформления.

    Правила правит пользователь файлом — сервис их только читает.
    """
    return {
        "files": [f.to_dict() for f in rules.files()],
        "samples": rules.samples(),
        "folder": "theses/docs",
    }


@app.get("/api/rules/{name}")
def rules_text(name: str):
    if not rules.exists(name):
        raise HTTPException(status_code=404, detail="Файла правил нет в docs/")
    return {"name": name, "text": rules.read(name)}


@app.post("/api/rules/reload")
def rules_reload():
    """Правила поправили файлом — перечитать без перезапуска сервиса."""
    rules.reload()
    added = storage.adopt_new_rules_files()
    if added:
        portal.note_change("заведены карточки: " + ", ".join(p.name for p in added))
    return {"files": [f.to_dict() for f in rules.files()],
            "added": [p.to_dict() for p in added]}


class ProfileIn(BaseModel):
    name: str = ""
    role: str = ""
    base_file: str = ""
    own_file: str = ""
    note: str = ""


@app.get("/api/profiles")
def list_profiles():
    return {"profiles": [p.to_dict() for p in storage.profiles()]}


def _check_rules_files(body: ProfileIn) -> None:
    """Файлы правил в карточке — только из списка `docs/`: путь наружу не принимаем
    (аудит Security 21.09: чтение любого файла ПК через карточку)."""
    for field_name in ("base_file", "own_file"):
        name = getattr(body, field_name)
        if name and not rules.exists(name):
            raise HTTPException(status_code=400, detail=(
                "Файл правил выбирается из папки docs — такого файла там нет"))


@app.post("/api/profiles")
def create_profile(body: ProfileIn):
    prof = storage.Profile(**body.model_dump())
    if not prof.name.strip():
        raise HTTPException(status_code=400, detail="Нужно имя: для кого пишем")
    _check_rules_files(body)
    storage.save_profile(prof)
    portal.note_object(prof.id)
    portal.note_change(f"добавлен «{prof.name}»")
    return prof.to_dict()


@app.put("/api/profiles/{profile_id}")
def update_profile(profile_id: str, body: ProfileIn):
    prof = storage.profile(profile_id)
    if prof is None:
        raise HTTPException(status_code=404, detail="Карточка не найдена")
    _check_rules_files(body)
    changes = []
    for field_name, was in (("name", prof.name), ("role", prof.role),
                            ("base_file", prof.base_file), ("own_file", prof.own_file),
                            ("note", prof.note)):
        became = getattr(body, field_name)
        if became != was:
            changes.append(f"{field_name}: «{was}» → «{became}»")
        setattr(prof, field_name, became)
    storage.save_profile(prof)
    portal.note_object(prof.id)
    portal.note_change("; ".join(changes) or "без изменений")
    return prof.to_dict()


@app.delete("/api/profiles/{profile_id}")
def remove_profile(profile_id: str, request: Request):
    """Удаление карточки необратимо, поэтому только администратору."""
    _admin_only(request)
    prof = storage.profile(profile_id)
    if prof is None:
        raise HTTPException(status_code=404, detail="Карточка не найдена")
    used = storage.profile_in_use(profile_id)
    if used:
        raise HTTPException(status_code=409,
                            detail=f"Для этого человека написаны тезисы ({used}) — карточка нужна")
    storage.delete_profile(profile_id)
    portal.note_object(profile_id)
    portal.note_change(f"удалён «{prof.name}»")
    return {"ok": True}


# ── тезисы ───────────────────────────────────────────────────────────────

_CREATE_KEY = re.compile(r"^[A-Za-z0-9_-]{8,64}$")


class ThesisIn(BaseModel):
    """Всё, что спрашиваем у человека: для кого, о чём и на сколько минут.

    Откуда берём содержание, сервис определяет сам: приложили документы — из них,
    не приложили — по теме (`models.sync_mode`). «Где выступление» необязательно:
    оно нужно только как подсказка названия при создании совещания в «Модераторе».
    """
    profile: str = ""
    topic: str = ""
    minutes: int = 7
    event: str = ""
    # что нужно в тезисах — своими словами, голосом или текстом; уходит «Искре»
    ask: str = ""
    # одноразовый ключ окна «Новые тезисы»: повтор той же отправки не заводит вторые тезисы
    key: str = ""
    rev: int | None = None


@app.get("/api/theses")
def list_theses(archived: bool = False):
    # у каких тезисов «Искра» сейчас пишет — в списке это видно, даже если ушли в другие
    running = jobs.active_theses()
    rows = storage.listing(archived=archived)
    for row in rows:
        row["running"] = row["id"] in running
    return {"theses": rows, "profiles": [p.to_dict() for p in storage.profiles()]}


@app.get("/api/search")
def search(q: str = "", limit: int = 5):
    """Поиск для портала: он спрашивает все сервисы сразу и показывает общий список.

    Ищем по теме, событию и тексту буллитов — человек помнит «про молодых педагогов»,
    а не дату создания.
    """
    query = (q or "").strip().lower()
    if len(query) < 2:
        return {"items": []}
    limit = max(1, min(int(limit or 5), 20))
    names = {p.id: p.name for p in storage.profiles()}
    items = []
    for row in storage.listing() + storage.listing(archived=True):
        stored = storage.get(row["id"])
        if stored is None:
            continue
        thesis = stored.thesis
        haystack = " ".join([thesis.topic or "", thesis.event or "", thesis.ask or "",
                             " ".join(b.text for b in thesis.bullets)]).lower()
        if query not in haystack:
            continue
        who = names.get(thesis.profile, "")
        items.append({
            "title": (f"{thesis.topic or 'Без темы'}"
                      + (f" · для {who}" if who else "")
                      + (" · архив" if row["archived"] else "")),
            "url": f"/theses/#thesis={row['id']}",
            "when": (row.get("updated_at") or "")[:10],
            "who": row.get("updated_by") or "",
            "kind": "тезисы",
        })
        if len(items) >= limit:
            break
    return {"items": items}


@app.post("/api/theses")
def create_thesis(body: ThesisIn, request: Request):
    if body.profile and storage.profile(body.profile) is None:
        raise HTTPException(status_code=400, detail="Не найдена карточка человека")
    if not body.topic.strip():
        raise HTTPException(status_code=400, detail="Напишите, о чём тезисы")
    key = body.key if _CREATE_KEY.match(body.key or "") else ""
    if key:
        # ключ окна — свой у каждого человека: чужой ключ не отдаёт чужих тезисов (Н3)
        key = f"{request.state.user['login']}:{key}"
        for _ in range(40):
            mine, existing = storage.claim_create_key(key)
            if mine:
                break                   # ключ наш (новый или брошенный) — создаём сами
            if existing:
                stored = storage.get(existing)
                if stored is not None:
                    # та же отправка второй раз: отдаём уже созданное
                    portal.skip_audit()
                    return stored.to_dict()
            time.sleep(0.05)            # первый запрос ещё создаёт — ждём его
        else:
            raise HTTPException(status_code=409, detail="Эти тезисы уже создаются — обновите страницу")
    thesis = Thesis(
        profile=body.profile, topic=latin.fix_mixed(body.topic.strip())[0],
        event=latin.fix_mixed(body.event)[0],
        minutes=max(1, min(int(body.minutes or 7), 180)),
        ask=_ask(body.ask), author=request.state.user["login"],
    )
    try:
        stored = storage.create(thesis)
    except Exception:
        if key:
            storage.release_create_key(key)
        raise
    if key:
        storage.bind_create_key(key, thesis.id)
    portal.note_object(thesis.id)
    portal.note_change(f"«{_short(thesis.topic) or 'без темы'}», {thesis.minutes} мин"
                       + (f"; что нужно: «{_short(thesis.ask)}»" if thesis.ask else ""))
    return stored.to_dict()


@app.get("/api/theses/{tid}")
def read_thesis(tid: str, request: Request):
    stored = _stored_or_404(tid)
    user = request.state.user
    storage.touch_editor(tid, user["login"], user["name"])
    out = stored.to_dict()
    out["editors"] = storage.active_editors(tid, user["login"])
    prof = storage.profile(stored.thesis.profile) if stored.thesis.profile else None
    out["profile_card"] = prof.to_dict() if prof else None
    return out


@app.put("/api/theses/{tid}")
def update_thesis(tid: str, body: ThesisIn):
    stored = _stored_or_404(tid)
    thesis = stored.thesis
    changes = []
    if body.profile and storage.profile(body.profile) is None:
        raise HTTPException(status_code=400, detail="Не найдена карточка человека")
    topic, ask = latin.fix_mixed(body.topic)[0], _ask(body.ask)
    event = latin.fix_mixed(body.event)[0]
    for name, was, became in (("тема", thesis.topic, topic),
                              ("событие", thesis.event, event),
                              ("что нужно", thesis.ask, ask)):
        if became != was:
            changes.append(f"{name}: «{_short(was)}» → «{_short(became)}»")
    minutes = max(1, min(int(body.minutes or 7), 180))
    if minutes != thesis.minutes:
        changes.append(f"хронометраж: {thesis.minutes} → {minutes} мин")
    if body.profile != thesis.profile:
        names = {p.id: p.name for p in storage.profiles()}
        changes.append(f"для кого: «{names.get(thesis.profile, '—')}» → «{names.get(body.profile, '—')}»")
    thesis.topic, thesis.event, thesis.minutes, thesis.profile, thesis.ask = (
        topic, event, minutes, body.profile, ask)
    portal.note_object(tid)
    return _save(stored, body.rev, "; ".join(changes) or "без изменений")


@app.delete("/api/theses/{tid}")
def archive_thesis(tid: str):
    """Удаление для сотрудника — перенос в архив (контракт портала, п. 2)."""
    if not storage.set_archived(tid, True):
        raise HTTPException(status_code=404, detail="Тезисы не найдены")
    portal.note_object(tid)
    return {"ok": True}


@app.post("/api/theses/{tid}/restore")
def restore_thesis(tid: str):
    if not storage.set_archived(tid, False):
        raise HTTPException(status_code=404, detail="Тезисы не найдены")
    portal.note_object(tid)
    return {"ok": True}


@app.delete("/api/theses/{tid}/permanent")
def delete_thesis(tid: str, request: Request):
    _admin_only(request)
    if not storage.delete_permanent(tid):
        raise HTTPException(status_code=404, detail="Тезисы не найдены")
    portal.note_object(tid)
    return {"ok": True}


@app.get("/api/theses/{tid}/history")
def thesis_history(tid: str):
    _stored_or_404(tid)
    return {"history": storage.history(tid)}


# ── текст тезисов ────────────────────────────────────────────────────────

class LineIn(BaseModel):
    id: str = ""
    text: str = ""
    emphasis: str = NORMAL


class TextIn(BaseModel):
    """Текст тезисов из редактора целиком (задача 22.09: редактор как в «Модераторе»).

    `html` — что человек видит, с оформлением; оно и уходит в Word. `bullets` — прежний
    вид (строки с выделением), принимается для совместимости и превращается в HTML.
    """
    html: str | None = None
    bullets: list[LineIn] = []
    rev: int | None = None


MAX_LINES, MAX_HTML_CHARS = 300, 400_000
_EMPHASIS_TITLES = {"normal": "обычный", "strong": "акцент", "aside": "справочное"}


@app.put("/api/theses/{tid}/text")
def save_text(tid: str, body: TextIn):
    """Сохранить текст из редактора целиком.

    Версия обязательна: редактор присылает весь текст, и без проверки версии он
    затёр бы чужую правку или вариант «Искры», пришедший за это время.
    """
    if body.rev is None:
        raise HTTPException(status_code=400, detail=(
            "Нужна версия тезисов, которую вы правите: без неё можно затереть чужую правку"))
    if body.html is not None:
        if len(body.html) > MAX_HTML_CHARS:
            raise HTTPException(status_code=400, detail="Текст слишком большой для тезисов")
        html = body.html
    else:
        if len(body.bullets) > MAX_LINES:
            raise HTTPException(status_code=400, detail=f"Слишком много строк: больше {MAX_LINES}")
        html = richtext.html_from_bullets([
            Bullet(text=line.text, emphasis=line.emphasis if line.emphasis in EMPHASIS else NORMAL)
            for line in body.bullets])
    stored = _stored_or_404(tid)
    thesis = stored.thesis
    before_html = thesis.html
    before_heads = thesis.headings()
    old = list(thesis.bullets)
    thesis.set_html(_heal_text(thesis, html))
    if thesis.html == before_html:
        portal.skip_audit()                       # ничего не поменялось — и в журнал нечего
        return stored.to_dict()

    fresh = thesis.bullets
    old_ids = {b.id for b in old}
    kept = {b.id for b in fresh} & old_ids
    parts = [f"пунктов {len(old)} → {len(fresh)}"]
    if len(fresh) - len(kept):
        parts.append(f"добавлено {len(fresh) - len(kept)}")
    if len(old) - len(kept):
        parts.append(f"убрано {len(old) - len(kept)}")
    by_id = {b.id: b for b in old}
    changed = [(by_id[b.id], b) for b in fresh if b.id in by_id and by_id[b.id].text != b.text]
    marked = [(by_id[b.id], b) for b in fresh
              if b.id in by_id and by_id[b.id].emphasis != b.emphasis]
    if changed:
        was, now_ = changed[0]
        parts.append(f"изменено {len(changed)}: «{_short(was.text)}» → «{_short(now_.text)}»"
                     + (" и другие" if len(changed) > 1 else ""))
    if marked:
        was, now_ = marked[0]
        parts.append(f"выделение «{_short(now_.text, 30)}»: {_EMPHASIS_TITLES[was.emphasis]} → "
                     f"{_EMPHASIS_TITLES[now_.emphasis]}"
                     + (f" и ещё {len(marked) - 1}" if len(marked) > 1 else ""))
    if [b.id for b in fresh if b.id in kept] != [b.id for b in old if b.id in kept]:
        parts.append("изменён порядок")
    # разделы — заголовки текста (решение пользователя 13.10): их правка в журнале словами
    heads = thesis.headings()
    if heads != before_heads:
        parts.append("разделы: " + _heads_list(before_heads) + " → " + _heads_list(heads))
    if not changed and not marked and len(old) == len(fresh) and heads == before_heads and not (
            [b.id for b in fresh if b.id in kept] != [b.id for b in old if b.id in kept]):
        parts.append("изменено оформление")
    portal.note_object(tid)
    return _save(stored, body.rev, "; ".join(parts))


def _heads_list(heads: list[str], limit: int = 5) -> str:
    """Названия разделов для журнала: первые `limit` и «и ещё N» — при 20 разделах запись
    не разрастается (ревью RC-2026-10-13-3, Н-6)."""
    if not heads:
        return "нет"
    shown = ", ".join(f"«{_short(h, 30)}»" for h in heads[:limit])
    return shown + (f" и ещё {len(heads) - limit}" if len(heads) > limit else "")


def _heal_text(thesis: Thesis, html: str) -> str:
    """Слова вперемешку, которые исправляются однозначно, — исправить при приходе текста
    (задача 22.09, п. 4). Однозначно — если после замены латинской части получается слово,
    которое есть в материалах, теме, пожеланиях, правилах или в самом тексте."""
    corpus = latin.corpus(*(s.text for s in thesis.sources), thesis.topic, thesis.ask,
                          thesis.event, thesis._rules_text(), *richtext.lines(html))
    items = richtext.blocks(html)
    changed = False
    for block in items:
        for run in block.runs:
            fixed = latin.fix_mixed(run.text, corpus)[0]
            if fixed != run.text:
                run.text, changed = fixed, True
    return richtext.render(items) if changed else html


# ── выгрузка в Word ──────────────────────────────────────────────────────

@app.get("/api/theses/{tid}/export/theses.docx")
def export_docx(tid: str):
    """Документ по формату из образца.

    Числа не проверяются — решение пользователя 21.09: «всё будет на моё решение».
    Не выпускаем только слова наполовину латиницей. Недостающая фактура выгрузке
    не мешает: она идёт в документ красным маркером и повторяется блоком
    «Что необходимо доуточнить».
    """
    stored = _stored_or_404(tid)
    thesis = stored.thesis
    if not thesis.has_text:
        raise HTTPException(status_code=400, detail="Тезисы пустые — выгружать нечего")
    _mixed_or_409(thesis, "документ соберётся")
    prof = storage.profile(thesis.profile) if thesis.profile else None
    name = docx_export.file_name(thesis, prof.name if prof else "")
    portal.note_object(tid)
    portal.note_change(f"«{_short(thesis.topic)}», пунктов: {len(thesis.bullets)}")
    return Response(
        content=docx_export.to_bytes(docx_export.build(thesis)),
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8\'\'{quote(name)}"},
    )


@app.get("/api/theses/{tid}/rules")
def thesis_rules(tid: str):
    """По каким правилам пишется этот текст — чтобы человек видел закон целиком."""
    stored = _stored_or_404(tid)
    prof = storage.profile(stored.thesis.profile) if stored.thesis.profile else None
    if prof is None:
        return {"profile": None, "text": "", "files": []}
    return {"profile": prof.to_dict(), "text": prof.rules_text(),
            "files": [f for f in (prof.base_file, prof.own_file) if f]}


# ── передача в «Модератор» ───────────────────────────────────────────────

@app.get("/api/theses/{tid}/moderator-payload")
def moderator_payload(tid: str):
    """Адреса и тело для кнопки «Приложить к совещанию».

    Совещание — общая ось (`../CLAUDE.md`, п. 9): его карточку ведёт «Модератор»,
    папку — «Материалы», а мы отдаём только своё готовое. Сами запросы делает браузер
    пользователя: между сервисами общих секретов нет.
    Слова наполовину латиницей не уезжают — то же правило, что и для выгрузки в Word.
    """
    stored = _stored_or_404(tid)
    thesis = stored.thesis
    if not thesis.has_text:
        raise HTTPException(status_code=400, detail="Тезисы пустые — передавать нечего")
    _mixed_or_409(thesis, "тезисы можно будет передать")
    portal.note_object(tid)
    portal.note_change(f"«{_short(thesis.topic)}» → совещание, {thesis.minutes} мин")
    return moderator.envelope(thesis)


# ── приложенные документы ────────────────────────────────────────────────

def _add_source(stored: storage.Stored, name: str, text: str, rev: int | None, kind: str = "") -> dict:
    from .models import Source
    source = Source(name=name, kind=kind or doc_text.kind_of(name) or "txt", text=text)
    stored.thesis.sources.append(source)
    portal.note_object(stored.thesis.id)
    out = _save(stored, rev, f"приложен документ «{name}», знаков: {len(text)}")
    out["source_id"] = source.id
    return out


MAX_SOURCE_BYTES = 25 * 1024 * 1024      # один документ; файл читается в память целиком
MAX_SOURCES = 2                           # документов у одних тезисов (решение пользователя 22.09)


@app.post("/api/theses/{tid}/sources/file")
async def upload_source(tid: str, rev: int | None = None, file: UploadFile = File(...)):
    """Файл читается из памяти: временный файл на Windows уже ломал загрузку в A3.
    Поэтому размер и число документов ограничены (ревьюер RC-2026-09-22-2)."""
    stored = _stored_or_404(tid)
    if len(stored.thesis.sources) >= MAX_SOURCES:
        raise HTTPException(status_code=400, detail=(
            f"К одним тезисам можно приложить не больше {MAX_SOURCES} документов — "
            "открепите один, чтобы приложить другой"))
    data = await file.read(MAX_SOURCE_BYTES + 1)
    if len(data) > MAX_SOURCE_BYTES:
        raise HTTPException(status_code=413, detail=(
            f"Файл больше {MAX_SOURCE_BYTES // (1024 * 1024)} МБ — приложите текстовую версию"))
    if not data:
        raise HTTPException(status_code=400, detail="Файл пустой")
    # Чтение файла (в том числе через LibreOffice, до 120 с) — в пуле потоков, а не в цикле событий:
    # иначе один .doc держал весь сервис вместе с /healthz (ревьюер RC-2026-10-05-5, К-3).
    try:
        text = await run_in_threadpool(doc_text.extract, file.filename or "", data)
    except doc_text.BadDocument as error:
        raise HTTPException(status_code=400, detail=str(error))
    return _add_source(stored, file.filename or "документ", text, rev,
                       doc_text.kind_of(file.filename or "", data))


@app.delete("/api/theses/{tid}/sources/{source_id}")
def drop_source(tid: str, source_id: str, rev: int | None = None):
    stored = _stored_or_404(tid)
    found = [s for s in stored.thesis.sources if s.id == source_id]
    if not found:
        raise HTTPException(status_code=404, detail="Документ не найден")
    stored.thesis.sources = [s for s in stored.thesis.sources if s.id != source_id]
    portal.note_object(tid)
    return _save(stored, rev, f"откреплён документ «{found[0].name}»")


# ── «Искра» пишет тезисы ─────────────────────────────────────────────────

class WriteIn(BaseModel):
    replace: bool = False
    # текст уже есть: не заменять, а предложить новую редакцию — человек примет по версии
    propose: bool = False
    rev: int | None = None


@app.get("/api/iskra")
def iskra_state():
    """Есть ли ключ. Сам ключ наружу не отдаём — его пишет портал при выкладке.

    `demo` — сервис запущен с подменным ответом (`tools/fake_iskra.py`), чтобы показать
    работу, пока «Искра» недоступна. В продуктиве такого запуска нет, и флага тоже.
    """
    return {"ready": iskra.key_ready(), "demo": bool(portal.env("THESES_DEMO_ISKRA"))}


@app.post("/api/theses/{tid}/write", status_code=202)
def write_theses(tid: str, body: WriteIn, request: Request):
    """Запустить написание тезисов по правилам выбранного человека — фоном.

    Ответ приходит сразу, с заданием; страница опрашивает его и показывает, что
    происходит. Раньше запрос держался, пока «Искра» думала, и страница выглядела
    зависшей. Второе нажатие, пока идёт первое, получает то же задание.

    Готовый текст молча не затирается: если он уже есть, нужен явный «заменить».
    Ответ модели приводится к схеме; английские слова и слова вперемешку с латиницей
    остаются видимыми человеку. Чисел сервис не перепроверяет (решение 21.09).
    """
    stored = _stored_or_404(tid)
    thesis = stored.thesis
    running = jobs.latest(tid)
    if running and running["active"]:
        return {"job": running, "started": False}
    if thesis.has_text and not body.replace and not body.propose:
        raise HTTPException(status_code=409, detail={
            "message": "Текст уже есть. Заменить его целиком?", "needs_replace": True})
    if not thesis.topic.strip():
        raise HTTPException(status_code=400, detail="Без темы «Искре» писать не о чем")
    if not iskra.key_ready():
        raise HTTPException(status_code=503, detail=(
            "Ключ «Искры» не настроен — его записывает портал при выкладке сервиса"))

    def prepare():
        # первый шаг, который видит человек: «Читаю документы» / «Готовлю задание»
        fresh = storage.get(tid)
        if fresh is None:
            raise iskra.IskraError("Тезисы удалили, пока шла подготовка")
        prof = storage.profile(fresh.thesis.profile) if fresh.thesis.profile else None
        sources_text = "\n\n".join(f"=== {s.name} ===\n{s.text}" for s in fresh.thesis.sources)
        return (fresh.thesis, prof.rules_text() if prof else "", prof.name if prof else "",
                sources_text)

    user = request.state.user
    # версия тезисов на момент нажатия: результат ляжет только поверх неё
    job, started = jobs.start(tid, stored.rev, user["login"],
                              request.client.host if request.client else "", prepare,
                              propose=bool(thesis.has_text and body.propose))
    return {"job": job, "started": started}


@app.get("/api/theses/{tid}/job")
def thesis_job(tid: str):
    """Последнее задание по тезисам — страница восстанавливает индикатор после ухода."""
    _stored_or_404(tid)
    return {"job": jobs.latest(tid), "typical": jobs.typical(),
            "deadline": int(iskra.DEADLINE)}


@app.get("/api/jobs/{job_id}")
def job_state(job_id: str):
    job = jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Задание не найдено")
    return {"job": job, "typical": jobs.typical(), "deadline": int(iskra.DEADLINE)}


@app.get("/api/jobs/{job_id}/draft")
def job_draft(job_id: str):
    """Вариант «Искры», не применённый из-за правок за время прогона, — посмотреть."""
    data = jobs.draft(job_id)
    if data is None:
        raise HTTPException(status_code=404, detail="Непримененного варианта нет")
    return {"bullets": data.get("bullets", []), "html": data.get("html", ""),
            "gaps": data.get("gaps", []), "notes": data.get("notes", [])}


class DraftIn(BaseModel):
    # версия обязательна: без неё вариант «Искры» затёр бы более новую правку (ревьюер, П1)
    rev: int | None = None


@app.post("/api/jobs/{job_id}/apply")
def job_apply(job_id: str, body: DraftIn):
    """Человек посмотрел вариант «Искры» и решил его взять — поверх того, что видит сейчас.

    Версию проверяем снова: если тезисы опять изменили, пока он смотрел, — 409.
    """
    if body.rev is None:
        raise HTTPException(status_code=400, detail=(
            "Нужна версия тезисов, которую вы видите: без неё вариант «Искры» мог бы "
            "затереть более новую правку"))
    job = jobs.get(job_id)
    data = jobs.draft(job_id)
    if job is None or data is None:
        raise HTTPException(status_code=404, detail="Непримененного варианта нет")
    stored = _stored_or_404(job["thesis_id"])
    if stored.archived:
        raise HTTPException(status_code=409, detail="Тезисы в архиве — сначала восстановите их")
    portal.note_object(job["thesis_id"])
    try:
        fresh = jobs.apply_result(stored, jobs.unpack(data), body.rev,
                                  "применён вариант «Искры», написанный во время правок")
    except storage.Conflict as conflict:
        raise HTTPException(status_code=409, detail={
            "message": "Тезисы снова изменились — обновите страницу и решите ещё раз",
            "current": conflict.stored.to_dict()})
    jobs.decide_draft(job_id, "applied", "Вариант «Искры» применён")
    portal.note_change(f"пунктов {len(fresh.thesis.bullets)}")
    out = fresh.to_dict()
    out["notes"] = data.get("notes", [])
    return out


@app.post("/api/jobs/{job_id}/discard")
def job_discard(job_id: str):
    """Человек оставил свою редакцию — вариант «Искры» больше не предлагаем."""
    job = jobs.get(job_id)
    if job is None or jobs.draft(job_id) is None:
        raise HTTPException(status_code=404, detail="Варианта «Искры» нет — отклонять нечего")
    if job["active"]:
        raise HTTPException(status_code=409, detail="«Искра» ещё пишет — решать пока не о чем")
    # вариант не стираем: он остаётся среди отложенных, к нему можно вернуться (П3)
    jobs.decide_draft(job_id, "kept", "Оставлены правки человека; вариант «Искры» отложен")
    portal.note_object(job["thesis_id"])
    portal.note_change("вариант «Искры» отложен, правки человека оставлены")
    return {"job": jobs.get(job_id)}


@app.get("/api/theses/{tid}/drafts")
def thesis_drafts(tid: str):
    """Варианты «Искры», не вошедшие в текст: ждущие решения и отложенные."""
    _stored_or_404(tid)
    return {"drafts": jobs.drafts(tid)}


@app.post("/api/jobs/{job_id}/cancel")
def job_cancel(job_id: str):
    """Отмена снимает прогон у «Искры»; исход и расход задание пишет в журнал само."""
    job = jobs.cancel(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Задание не найдено")
    return {"job": job}
