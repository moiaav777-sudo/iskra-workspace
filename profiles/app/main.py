"""Сервис «Объективки»: краткая справка о человеке на одну страницу A4.

Порт 8990 на 127.0.0.1, адрес за nginx — `/profiles/` (префикс снимает nginx, поэтому в
интерфейсе все адреса относительные). Своего входа нет: пользователь — из заголовков
X-Portal-* (`portal.py`). Устройство и маршруты — в `../CLAUDE.md`.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool
from pydantic import BaseModel

from . import config, db, docx_build, extract, iskra, jobs, merge, model, portal

app = FastAPI(title="Объективки", docs_url=None, redoc_url=None)

DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
MAX_SOURCE_BYTES = 25 * 1024 * 1024
MAX_PHOTO_BYTES = 10 * 1024 * 1024
MAX_BATCH = 10
SCOPES = ("mine", "team", "all")
_KEY = re.compile(r"^[A-Za-z0-9_-]{8,64}$")

# Адреса соседей для «Приложить к мероприятию» — зовёт их браузер вошедшего человека (контракт п. 9).
MEETING_LINKS = {
    "meetings": "/moderator/api/meetings",
    "find_or_create": "/moderator/api/meetings/find-or-create",
    "link": "/materials/api/events/link",
    "folder": "/moderator/api/meetings/{id}/folder",
    "files": "/materials/api/events/{folder}/files",
}


class FreshStatic(StaticFiles):
    """Скрипты и стили браузер сверяет каждый раз: после выкладки не остаётся старого JS."""

    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        return response


if config.STATIC_DIR.exists():
    app.mount("/static", FreshStatic(directory=config.STATIC_DIR), name="static")


@app.on_event("startup")
def _startup() -> None:
    db.init()


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
    if portal.foreign_origin(request.method, request.headers):
        return JSONResponse({"detail": "Запрос с чужого сайта отклонён"}, status_code=403)
    request.state.user = user
    return await call_next(request)


@app.get("/healthz", response_class=PlainTextResponse)
def healthz() -> str:
    return "ok"


@app.get("/")
def index():
    page = config.STATIC_DIR / "index.html"
    if not page.exists():
        return PlainTextResponse("Интерфейс ещё не собран", status_code=503)
    return FileResponse(page, headers={"Cache-Control": "no-store"})


# ── помощники ───────────────────────────────────────────────────────────

def _ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for", "")
    return fwd.split(",")[0].strip() if fwd else (request.headers.get("x-real-ip")
                                                  or (request.client.host if request.client else ""))


def _writer(request: Request) -> dict:
    """Создавать и править может сотрудник. Администратор документы не ведёт (контракт п. 2),
    руководитель (chief) только смотрит — портал отклонит и сам, здесь — вторая линия."""
    user = request.state.user
    if user.get("is_admin"):
        raise HTTPException(403, "Администратор документы не ведёт — войдите под личной учётной записью")
    if user.get("is_chief"):
        raise HTTPException(403, "Роль руководителя — только просмотр")
    return user


def _admin(request: Request) -> dict:
    user = request.state.user
    if not user.get("is_admin"):
        raise HTTPException(403, "Действие доступно только администратору")
    return user


def _get(pid: str) -> dict:
    p = db.get(pid)
    if p is None:
        raise HTTPException(404, "Объективка не найдена")
    return p


def _not_archived(p: dict) -> None:
    if p["archived"]:
        raise HTTPException(409, "Объективка в архиве — сначала восстановите её")


def _short(text: str, limit: int = 60) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[:limit].rstrip() + "…"


def _label(p: dict) -> str:
    return p["data"].get("fio") or p.get("fio") or p.get("source_name") or "без имени"


def visible(item: dict, user: dict, scope: str) -> bool:
    """Круг показа (как у «Брифа»): mine — мои; team — мои и подчинённых (X-Portal-Team, «*» — всё);
    all — все. Фильтр показа, а не запрет: чужая объективка открывается по ссылке."""
    me = user.get("login") or ""
    if scope == "all" or not me:
        return True
    owner = item.get("owner") or ""
    if owner == me:
        return True
    if scope == "mine":
        return False
    team = user.get("team") or []
    return "*" in team or owner in team


def _photo_path(p: dict) -> Path | None:
    if not p.get("photo"):
        return None
    path = config.photos_dir() / p["photo"]
    return path if path.exists() else None


def _photo(p: dict) -> tuple[bytes, str] | None:
    path = _photo_path(p)
    if not path:
        return None
    return path.read_bytes(), path.suffix.lstrip(".")


def view(p: dict) -> dict:
    out = {k: v for k, v in p.items() if k not in ("source_text", "create_key")}
    out["source_chars"] = len(p.get("source_text") or "")
    out["has_photo"] = bool(_photo_path(p))
    out["job"] = jobs.view(jobs.of(p["id"]))
    out["running"] = p["status"] in (db.RUNNING, db.QUEUED) and jobs.active(p["id"])
    out["stale"] = p["built_rev"] != p["rev"]
    out["file_name"] = docx_build.file_name(p["data"])
    fio = p["data"].get("fio") or ""
    out["name_confirmed"] = ({"fio": fio, "by": p.get("name_ok_by", ""), "at": _ru_time(p.get("name_ok_at", ""))}
                             if fio and p.get("name_ok") == fio else None)
    return out


def _suggestions(item: dict) -> list[dict]:
    out = []
    for x in item.get("suggestions") or []:
        if isinstance(x, dict) and x.get("name"):
            out.append({"name": str(x["name"]), "role": str(x.get("role") or ""),
                        "organization": str(x.get("organization") or "")})
    return out[:3]


def _name_status(p: dict, got: dict | None) -> dict:
    """Итог сверки ФИО карточки по ответу портала (`portal.check_people`), контракт п. 5а.

    close — в документ не выпускается: заменить на запись реестра или подтвердить «это другой
    человек» (исключение для объективок, см. CLAUDE.md); confirmed — подтверждено вручную, кто и когда;
    unknown — предупреждение и предложение «Добавить в реестр»; failed — портал не ответил.
    """
    fio = p["data"].get("fio") or ""
    if not fio:
        return {"status": "empty", "message": "ФИО не заполнено"}
    if got is None:
        return {"status": "off"}
    if not got:
        return {"status": "failed", "message": "Сверка с реестром людей не прошла — портал не ответил. "
                                               "Проверьте написание ФИО сами."}
    item = got.get(fio) or {}
    status = item.get("status") or "unknown"
    if status == "close":
        sugg = _suggestions(item)
        names = [x["name"] for x in sugg]
        if p.get("name_ok") == fio:
            return {"status": "confirmed", "suggestions": sugg, "by": p.get("name_ok_by", ""),
                    "at": p.get("name_ok_at", ""),
                    "message": f"ФИО подтверждено вручную: {p.get('name_ok_by') or 'сотрудник'}, "
                               f"{_ru_time(p.get('name_ok_at', ''))} — это другой человек, не "
                               + ", ".join(f"«{n}»" for n in names)}
        return {"status": "close", "suggestions": sugg, "fio": fio,
                "message": f"«{fio}» — в реестре людей похожее: " + ", ".join(f"«{n}»" for n in names)
                           + ". Если это опечатка — замените; если другой человек — подтвердите."}
    if status == "unknown":
        return {"status": "unknown", "fio": fio,
                "message": f"«{fio}» нет в реестре людей портала — проверьте написание и добавьте в реестр."}
    return {"status": "exact"}


def _ru_time(iso: str) -> str:
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})", iso or "")
    return f"{m[3]}.{m[2]}.{m[1]} {m[4]}:{m[5]}" if m else ""


def _name_check(p: dict) -> dict:
    """Сверка ФИО с реестром людей перед выводом в документ (контракт п. 5а)."""
    fio = p["data"].get("fio") or ""
    return _name_status(p, portal.check_people([fio]) if fio else None)


def _block_close(check: dict, package: bool = False) -> None:
    """close в документ не выпускается; в пакет мероприятия — ещё и без состоявшейся сверки
    (портал не ответил): такой документ скачать можно, а в «Материалы» он не кладётся."""
    if check.get("status") == "close":
        raise HTTPException(409, {"message": check["message"], "name_check": check})
    if package and check.get("status") in ("failed", "empty"):
        raise HTTPException(409, {"message": "ФИО не сверено с реестром людей ("
                                  + (check.get("message") or "") + ") — в пакет мероприятия не кладём. "
                                  "Повторите позже.", "name_check": check})


def _write_file(path: Path, data: bytes) -> None:
    """Через временный файл и replace: обрыв не оставит половину документа."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    tmp.replace(path)


def _rebuild(p: dict) -> tuple[bytes, dict]:
    data, fit = docx_build.build(p["data"], _photo(p))
    _write_file(config.docx_dir() / f"{p['id']}.docx", data)
    db.set_fields(p["id"], built_rev=p["rev"], fit=fit)
    return data, fit


def _fit_words(fit: dict) -> str:
    if not fit:
        return ""
    line = str(fit["line"]).replace(".", ",")
    return (f"кегль {fit['size']:g}, интервал {line}, одна страница" if fit["fits"]
            else f"не уложилась в страницу (кегль {fit['size']:g}, интервал {line})")


def _upload_name(file: UploadFile) -> str:
    return Path(file.filename or "документ").name[:200]


async def _read_upload(file: UploadFile, limit: int, what: str) -> bytes:
    data = await file.read(limit + 1)
    if len(data) > limit:
        raise HTTPException(413, f"{what} больше {limit // (1024 * 1024)} МБ")
    if not data:
        raise HTTPException(400, f"{what} пустой")
    return data


def _iskra_or_503() -> None:
    if not iskra.key_ready():
        raise HTTPException(503, "Ключ «Искры» не настроен — его записывает портал при выкладке сервиса")


# ── кто я ───────────────────────────────────────────────────────────────

@app.get("/api/me")
def me(request: Request):
    user = request.state.user
    return {**user, "can_edit": not user.get("is_admin") and not user.get("is_chief"),
            "links": {} if config.dev() else MEETING_LINKS, "iskra": iskra.key_ready(),
            "limits": {"merge_min": merge.MIN_ITEMS, "merge_max": merge.MAX_ITEMS, "batch_max": MAX_BATCH,
                       "source_mb": MAX_SOURCE_BYTES // (1024 * 1024), "photo_mb": MAX_PHOTO_BYTES // (1024 * 1024)},
            # что принимают поля загрузки (решение пользователя 05.10, общий модуль docs_common)
            "accept": {"source": extract.ACCEPT, "merge": extract.MERGE_ACCEPT}}


# ── реестр ──────────────────────────────────────────────────────────────

@app.get("/api/profiles")
def list_profiles(request: Request, scope: str = "mine", q: str = "", archived: bool = False):
    user = request.state.user
    scope = scope if scope in SCOPES else "mine"
    query = " ".join((q or "").lower().split())
    items = []
    for row in db.listing(archived=archived):
        if not visible(row, user, scope):
            continue
        if query and query not in (row["fio"] or "").lower() and query not in row["position"].lower():
            continue
        row["running"] = row["status"] in (db.RUNNING, db.QUEUED) and jobs.active(row["id"])
        items.append(row)
    return {"items": items, "scope": scope}


@app.get("/api/search")
def search(request: Request, q: str = "", limit: int = 5):
    """Поиск для портала — по фамилии и должности (контракт п. 6а)."""
    query = " ".join((q or "").lower().split())
    if len(query) < 2:
        return {"items": []}
    limit = max(1, min(int(limit or 5), 20))
    items = []
    for row in db.listing():
        if query in (row["fio"] or "").lower() or query in row["position"].lower():
            items.append({"title": (row["fio"] or "Без имени") + (f" · {_short(row['position'], 50)}"
                                                                  if row["position"] else ""),
                          "url": f"/profiles/#profile={row['id']}", "when": (row["updated_at"] or "")[:10],
                          "who": row["updated_name"] or row["owner_name"] or "", "kind": "объективка"})
        if len(items) >= limit:
            break
    return {"items": items}


# ── создание и сбор «Искрой» ────────────────────────────────────────────

def _fill_work(pid: str, user: dict, ip: str):
    """Работа задания: «Искра» собирает сведения, итог — в карточку и в журнал (с расходом)."""
    def work(job: jobs.Job, ctl: iskra.Control) -> None:
        p = db.get(pid)
        if p is None:
            job.status, job.error = jobs.ERROR, "Объективку удалили"
            return
        label = _label(p)
        try:
            got = iskra.fill(ctl, p["mode"], p["fio"] or p["data"].get("fio", ""), p["hint"], p["web"],
                             p["source_name"], p["source_text"])
            data = got["data"]
            # фото — не дело «Искры»: оно из справки или загружено человеком
            fresh = db.save_data(pid, data, user, None, force_unlocked=True)
            _rebuild(fresh)
            notes = "; ".join(got["notes"])
            db.set_fields(pid, status=jobs.DONE, status_text=notes)
            job.status = jobs.DONE
            label = data.get("fio") or label
            what = (f"по имени «{_short(p['fio'])}»" if p["mode"] == "name"
                    else f"по справке «{_short(p['source_name'])}»" + (" с открытыми источниками" if p["web"] else ""))
            portal.audit(user, "Объективка собрана «Искрой»", label,
                         f"{what}; опыт работы: {len(data['career'])}, источников: {len(data['refs'])}"
                         + (f"; {notes}" if notes else ""), ip, ctl.spent["in"], ctl.spent["out"])
        except iskra.Cancelled as error:
            job.status, job.error = jobs.CANCELLED, str(error)
            db.set_fields(pid, status=jobs.CANCELLED, status_text="Сбор отменён")
            portal.audit(user, "Сбор объективки «Искрой» отменён", label, "", ip, ctl.spent["in"], ctl.spent["out"])
        except Exception as error:  # noqa: BLE001 — любой исход прогона: одна запись в журнал с расходом
            text = (str(error) if isinstance(error, iskra.IskraError) else "Объективку удалили во время сбора"
                    if isinstance(error, KeyError) else f"Сбой при разборе ответа «Искры» ({type(error).__name__})")
            job.status, job.error = jobs.ERROR, text
            db.set_fields(pid, status=jobs.ERROR, status_text=text)
            portal.audit(user, "«Искра» не собрала объективку", label, _short(text, 200), ip,
                         ctl.spent["in"], ctl.spent["out"])
    return work


class NameIn(BaseModel):
    fio: str = ""
    hint: str = ""
    key: str = ""


@app.post("/api/profiles")
def create_by_name(body: NameIn, request: Request):
    """По имени: человек пишет ФИО (и подсказку) — «Искра» ищет в открытых источниках."""
    user = _writer(request)
    fio = model.text_of(body.fio, 200)
    if len(fio) < 3:
        raise HTTPException(400, "Напишите фамилию, имя и отчество")
    _iskra_or_503()
    key = f"{user['login']}:{body.key}" if _KEY.match(body.key or "") else ""
    p, created = db.create(user, "name", fio=fio, hint=model.text_of(body.hint, 500), create_key=key)
    if created:
        portal.audit(user, "Заведена объективка", fio, "по имени" + (f"; подсказка: «{_short(body.hint)}»"
                                                                    if body.hint.strip() else ""), _ip(request))
        jobs.start(p["id"], user["login"], _fill_work(p["id"], user, _ip(request)))
    return view(db.get(p["id"]))


def _save_photo(pid: str, raw: bytes, kind: str) -> str:
    folder = config.photos_dir()
    name = f"{pid}.{kind}"
    _write_file(folder / name, raw)
    for old in folder.glob(f"{pid}.*"):
        if old.name != name and not old.name.endswith(".tmp"):
            old.unlink(missing_ok=True)
    return name


def _create_from_text(user: dict, ip: str, name: str, text: str, photo: tuple[bytes, str] | None, web: bool,
                      hint: str, key: str = "", batch: str = "") -> dict:
    """Карточка по прочитанной справке: завести, положить фото из справки, запустить «Искру»."""
    p, created = db.create(user, "doc", hint=hint, web=web, source_name=name, source_text=text,
                           create_key=key, batch=batch)
    if not created:
        return p
    if photo:
        db.set_fields(p["id"], photo=_save_photo(p["id"], *photo), photo_from="из справки")
    portal.audit(user, "Заведена объективка", name, "по справке" + (" с открытыми источниками" if web else "")
                 + (", фото из справки" if photo else "") + f"; знаков: {len(text)}", ip)
    jobs.start(p["id"], user["login"], _fill_work(p["id"], user, ip))
    return db.get(p["id"])


def _create_from_doc(user: dict, ip: str, name: str, raw: bytes, web: bool, hint: str,
                     key: str = "", batch: str = "") -> dict:
    text, photo = extract.read_source(name, raw)       # SourceError — наверх, с объяснением
    return _create_from_text(user, ip, name, text, photo, web, hint, key, batch)


@app.post("/api/profiles/from-doc")
async def create_by_doc(request: Request, file: UploadFile = File(...), web: str = Form("0"),
                        hint: str = Form(""), key: str = Form("")):
    """По справке: текст документа → «Искра» достаёт те же поля; фото — первая картинка Word.

    Чтение файла (pdftotext, LibreOffice — до минут) идёт в отдельном потоке: событийный цикл
    не занят, остальные запросы и /healthz отвечают (ревью RC-2026-10-05-2, Н-2)."""
    user = _writer(request)
    _iskra_or_503()
    raw = await _read_upload(file, MAX_SOURCE_BYTES, "Файл справки")
    k = f"{user['login']}:{key}" if _KEY.match(key or "") else ""
    try:
        p = await run_in_threadpool(_create_from_doc, user, _ip(request), _upload_name(file), raw,
                                    web in ("1", "true", "on"), model.text_of(hint, 500), k)
    except extract.SourceError as error:
        raise HTTPException(400, str(error))
    return view(p)


class RunIn(BaseModel):
    replace: bool = False


@app.post("/api/profiles/{pid}/iskra")
def rerun(pid: str, body: RunIn, request: Request):
    """Собрать заново. Готовые сведения молча не затираются — нужен явный «заменить»."""
    user = _writer(request)
    p = _get(pid)
    if jobs.active(pid):
        return view(p)
    d = p["data"]
    if any(d[k] for k in ("position", "career", "education", "awards", "key_facts")) and not body.replace:
        raise HTTPException(409, {"message": "Сведения уже есть. Собрать заново и заменить их?", "needs_replace": True})
    if p["mode"] == "name" and not (p["fio"] or d.get("fio")):
        raise HTTPException(400, "Без ФИО «Искре» искать нечего")
    _iskra_or_503()
    if d.get("fio") and p["mode"] == "name":
        db.set_fields(pid, fio=d["fio"])
    jobs.start(pid, user["login"], _fill_work(pid, user, _ip(request)))
    return view(db.get(pid))


@app.post("/api/profiles/{pid}/cancel")
def cancel(pid: str, request: Request):
    _writer(request)
    _get(pid)
    jobs.cancel(pid)
    return view(db.get(pid))


# ── карточка ────────────────────────────────────────────────────────────

@app.get("/api/profiles/{pid}")
def read(pid: str):
    return view(_get(pid))


class DataIn(BaseModel):
    data: dict = {}
    rev: int | None = None


def _changes(was: dict, now: dict) -> str:
    out = []
    names = {"fio": "ФИО", "birth_date": "дата рождения", "birth_place": "место рождения",
             "position": "должность", "organization": "организация", "note": "замечания", "photo_url": "ссылка на фото",
             "category": "вид"}
    for key, word in names.items():
        if was.get(key, "") != now.get(key, ""):
            out.append(f"{word}: «{_short(was.get(key, ''), 40)}» → «{_short(now.get(key, ''), 40)}»")
    for key, title in model.SECTION_TITLES.items():
        a, b = was.get(key) or [], now.get(key) or []
        if a != b:
            if len(a) != len(b):
                out.append(f"{title.lower()}: строк {len(a)} → {len(b)}")
            elif sorted(map(json.dumps, a)) == sorted(map(json.dumps, b)):
                out.append(f"{title.lower()}: изменён порядок")
            else:
                diff = next(i for i, (x, y) in enumerate(zip(a, b)) if x != y)
                fx = " ".join(v for k, v in a[diff].items() if k != "src")
                fy = " ".join(v for k, v in b[diff].items() if k != "src")
                out.append(f"{title.lower()}: «{_short(fx, 40)}» → «{_short(fy, 40)}»")
    if (was.get("refs") or []) != (now.get("refs") or []):
        out.append(f"источников {len(was.get('refs') or [])} → {len(now.get('refs') or [])}")
    return "; ".join(out)


@app.put("/api/profiles/{pid}")
def save(pid: str, body: DataIn, request: Request):
    """Правка на экране — сведения целиком, с версией: иначе затёрлась бы чужая правка."""
    user = _writer(request)
    if body.rev is None:
        raise HTTPException(400, "Нужна версия объективки, которую вы правите")
    p = _get(pid)
    if p["archived"]:
        raise HTTPException(409, "Объективка в архиве — сначала восстановите её")
    new = model.clean(body.data, sort=False)
    if new == p["data"]:
        return view(p)
    try:
        fresh = db.save_data(pid, new, user, body.rev)
    except db.Locked:
        raise HTTPException(409, "«Искра» ещё собирает объективку — дождитесь или отмените сбор")
    except db.Conflict as conflict:
        raise HTTPException(409, {"message": "Объективку успели изменить — обновите карточку, набранное не потеряно",
                                  "current": view(conflict.current)})
    portal.audit(user, "Изменена объективка", _label(fresh), _changes(p["data"], fresh["data"]), _ip(request))
    return view(fresh)


@app.delete("/api/profiles/{pid}")
def archive(pid: str, request: Request):
    """«Удалить» для сотрудника — перенос в архив (контракт п. 2)."""
    user = _writer(request)
    p = _get(pid)
    jobs.cancel(pid)
    db.set_archived(pid, True)
    portal.audit(user, "Объективка удалена (в архив)", _label(p), "в работе → архив", _ip(request))
    return {"ok": True}


@app.post("/api/profiles/{pid}/restore")
def restore(pid: str, request: Request):
    user = _writer(request)
    p = _get(pid)
    db.set_archived(pid, False)
    portal.audit(user, "Объективка восстановлена из архива", _label(p), "архив → в работе", _ip(request))
    return view(db.get(pid))


@app.delete("/api/profiles/{pid}/permanent")
def delete_forever(pid: str, request: Request):
    """Стереть навсегда — необратимо, только администратору (ADMIN_WRITE_PATHS портала)."""
    user = _admin(request)
    p = _get(pid)
    jobs.cancel(pid)
    for path in (_photo_path(p), config.docx_dir() / f"{pid}.docx"):
        if path:
            path.unlink(missing_ok=True)
    db.delete(pid)
    portal.audit(user, "Объективка удалена навсегда", _label(p), "", _ip(request))
    return {"ok": True}


# ── фото ────────────────────────────────────────────────────────────────

@app.get("/api/profiles/{pid}/photo")
def photo(pid: str):
    path = _photo_path(_get(pid))
    if not path:
        raise HTTPException(404, "Фото нет")
    return FileResponse(path, media_type=docx_build.MIME.get(path.suffix.lstrip("."), "image/jpeg"),
                        headers={"Cache-Control": "no-cache"})


@app.put("/api/profiles/{pid}/photo")
async def put_photo(pid: str, request: Request, file: UploadFile = File(...)):
    """Загрузить или заменить фото. Только JPEG и PNG: их Word вставляет без пересчёта."""
    user = _writer(request)
    p = _get(pid)
    _not_archived(p)
    raw = await _read_upload(file, MAX_PHOTO_BYTES, "Файл фото")
    info = extract.image_info(raw)
    if not info:
        raise HTTPException(400, "Фото — только JPEG или PNG")
    had = bool(_photo_path(p))
    # фото меняет документ — собранный Word устарел
    db.set_fields(pid, photo=_save_photo(pid, raw, info[0]), photo_from="загружено", built_rev=0)
    db.touch(pid, user)
    portal.audit(user, "Заменено фото объективки" if had else "Загружено фото объективки", _label(p),
                 f"{info[1]}×{info[2]} пикс., {info[0].upper()}", _ip(request))
    return view(db.get(pid))


@app.delete("/api/profiles/{pid}/photo")
def drop_photo(pid: str, request: Request):
    user = _writer(request)
    p = _get(pid)
    _not_archived(p)
    path = _photo_path(p)
    if not path:
        return view(p)
    path.unlink(missing_ok=True)
    db.set_fields(pid, photo="", photo_from="", built_rev=0)
    db.touch(pid, user)
    portal.audit(user, "Убрано фото объективки", _label(p), "фото → нет", _ip(request))
    return view(db.get(pid))


# ── Word ────────────────────────────────────────────────────────────────

@app.post("/api/profiles/{pid}/build")
def build(pid: str, request: Request):
    """«Пересобрать Word»: собрать, подобрать страницу, сверить ФИО с реестром людей."""
    user = _writer(request)
    p = _get(pid)
    _not_archived(p)
    if jobs.active(pid):
        raise HTTPException(409, "«Искра» ещё собирает объективку — дождитесь или отмените сбор")
    check = _name_check(p)
    _block_close(check)
    _, fit = _rebuild(p)
    portal.audit(user, "Собран Word объективки", _label(p), _fit_words(fit), _ip(request))
    return {**view(db.get(pid)), "name_check": check}


@app.get("/api/profiles/{pid}/docx")
def download(pid: str, request: Request, package: bool = False):
    """«Скачать Word»: собранный по текущей версии (устарел — пересобирается).
    package=1 — файл забирают в пакет мероприятия: без сверки ФИО не отдаём."""
    p = _get(pid)
    if package:
        _not_archived(p)
    _block_close(_name_check(p), package)
    path = config.docx_dir() / f"{pid}.docx"
    if p["built_rev"] != p["rev"] or not path.exists():
        data, _ = _rebuild(p)
    else:
        data = path.read_bytes()
    portal.audit(request.state.user, "Объективка выгружена в Word", _label(p), _fit_words(db.get(pid)["fit"]),
                 _ip(request))
    return Response(data, media_type=DOCX, headers={
        "Content-Disposition": f"attachment; filename*=UTF-8''{quote(docx_build.file_name(p['data']))}"})


class NameOkIn(BaseModel):
    fio: str = ""


@app.post("/api/profiles/{pid}/name-ok")
def name_ok(pid: str, body: NameOkIn, request: Request):
    """Человек подтвердил: ФИО верное, это другой человек, а не опечатка реестра.

    Принимается только при действующем расхождении (close) именно для этого ФИО — сверка
    повторяется здесь же; кто и когда подтвердил, видно в карточке и реестре (ревью RC-2026-10-05-2, Н-6).
    """
    user = _writer(request)
    p = _get(pid)
    fio = p["data"].get("fio") or ""
    if body.fio != fio:
        raise HTTPException(409, "ФИО успели изменить — обновите карточку")
    check = _name_status({**p, "name_ok": ""}, portal.check_people([fio]) if fio else None)
    if check["status"] != "close":
        raise HTTPException(409, {"message": "Подтверждать нечего: ФИО не расходится с реестром людей"
                                  + (" (сверка не прошла — повторите позже)" if check["status"] == "failed" else ""),
                                  "name_check": check})
    db.set_fields(pid, name_ok=fio, name_ok_by=user.get("name") or user["login"], name_ok_at=db.now())
    portal.audit(user, "Подтверждено ФИО, расходящееся с реестром людей", fio,
                 "это другой человек, не " + ", ".join(f"«{x['name']}»" for x in check["suggestions"]), _ip(request))
    fresh = view(db.get(pid))
    fresh["name_check"] = _name_check(db.get(pid))
    return fresh


class MeetingIn(BaseModel):
    id: str = ""
    url: str = ""
    folder: str = ""
    title: str = ""
    short_title: str = ""
    date: str = ""
    filed: bool = False


def _meeting(body: MeetingIn) -> dict:
    url = body.url if body.url.startswith("/") else ""
    return {"id": body.id[:100], "url": url[:300], "folder": body.folder[:100], "title": body.title[:300],
            "short_title": body.short_title[:200], "date": body.date[:20], "filed": body.filed}


@app.post("/api/profiles/{pid}/meeting")
def remember_meeting(pid: str, body: MeetingIn, request: Request):
    """Куда приложили: мероприятие запоминается в карточке (выбор всё равно доступен всегда)."""
    user = _writer(request)
    p = _get(pid)
    m = _meeting(body)
    db.set_fields(pid, meeting=m)
    portal.audit(user, "Объективка приложена к мероприятию" if m["filed"] else "Выбрано мероприятие для объективки",
                 _label(p), f"«{_short(m['short_title'] or m['title'])}» {m['date']}", _ip(request))
    return view(db.get(pid))


# ── объединение ─────────────────────────────────────────────────────────

def _merge_items(order: list, files: dict[int, tuple[str, bytes]]) -> tuple[list, list]:
    """Порядок объединения → [(имя, байты, страниц, заголовок)] и сведения о составе.

    ФИО всех объективок (из реестра и из файлов) сверяются с реестром людей одним запросом.
    Карточка из реестра идёт в содержание со своим ФИО как есть — по виду его не проверяем
    (иностранец латиницей, «Иванов И. И. (род. 1970)»; ревью RC-2026-10-05-2, Н-4).
    Файл .doc/.rtf/.odt переводится в .docx; PDF — понятный отказ.
    """
    out, meta, profiles = [], [], {}
    for item in order:
        if not isinstance(item, dict):
            raise HTTPException(400, "Неверный порядок объединения")
        if item.get("kind") == "profile":
            p = db.get(str(item.get("id") or ""))
            if p is None:
                raise HTTPException(404, "Объективка из списка не найдена — обновите реестр")
            if jobs.active(p["id"]):
                raise HTTPException(409, f"«Искра» ещё собирает «{_label(p)}» — дождитесь конца")
            if p["archived"]:
                # архивная в объединение не идёт — иначе через объединённый файл она попала бы в пакет (Н-8)
                raise HTTPException(409, f"Объективка «{_label(p)}» в архиве — восстановите её или уберите из списка")
            if not p["data"].get("fio"):
                raise HTTPException(400, f"У объективки по «{_short(p['source_name'] or p['id'])}» не заполнено ФИО — "
                                         "впишите его в карточку")
            profiles[p["id"]] = p
            meta.append({"kind": "profile", "id": p["id"], "fio": p["data"]["fio"], "title": p["data"]["fio"]})
        elif item.get("kind") == "file":
            idx = item.get("index")
            if not isinstance(idx, int) or idx not in files:
                raise HTTPException(400, "Неверный порядок объединения: нет такого файла")
            name, data = files[idx]
            try:
                data = extract.to_docx(name, data)
                title = merge.read_profile(name, data)["title"]
            except (extract.SourceError, merge.MergeError) as error:
                raise HTTPException(400, str(error))
            meta.append({"kind": "file", "name": name, "title": title, "data": data})
        else:
            raise HTTPException(400, "Неверный порядок объединения")
    got = portal.check_people([m["title"] for m in meta])
    for m in meta:
        if m["kind"] == "profile":
            p = profiles[m["id"]]
            check = _name_status(p, got)
            data, fit = docx_build.build(p["data"], _photo(p))
            out.append((docx_build.file_name(p["data"]), data, fit["pages"], p["data"]["fio"]))
            m.update(fits=fit["fits"], check=check["status"], message=check.get("message", ""))
        else:
            item = (got or {}).get(m["title"]) or {}
            m["check"] = "off" if got is None else ("failed" if not got else item.get("status") or "unknown")
            if m["check"] == "close":
                m["suggestions"] = [x["name"] for x in _suggestions(item)]
            out.append((m["name"], m.pop("data"), None, None))
    return out, meta


def _do_merge(user: dict, ip: str, spec: list, files: dict, title: str, batch: str) -> dict:
    items, meta = _merge_items(spec, files)
    close = [m["message"] for m in meta if m["kind"] == "profile" and m["check"] == "close"]
    if close:
        raise HTTPException(409, {"message": "ФИО расходится с реестром людей: " + " ".join(close)
                                  + " Откройте карточку, замените или подтвердите ФИО."})
    title = model.text_of(title, 120) or "Объективки"
    try:
        data, info = merge.merge_files(items, title=title)
    except merge.MergeError as error:
        raise HTTPException(400, str(error))
    for m, page in zip(meta, info["pages"]):
        m["page"] = page
        m.pop("message", None)
    file_name = f"{title} {time.strftime('%d.%m.%Y')}.docx"
    rec = db.add_merge(user["login"], title, meta, file_name)
    _write_file(config.merged_dir() / f"{rec['id']}.docx", data)
    if batch:
        b = db.get_batch(batch)
        if b and b["owner"] == user["login"]:
            db.set_batch(batch, merge_id=rec["id"])
    portal.audit(user, "Объединены объективки", title,
                 f"{len(meta)}: " + ", ".join(_short(t, 30) for t in info["titles"]), ip)
    return rec


@app.post("/api/merge")
async def merge_profiles(request: Request, order: str = Form("[]"), title: str = Form(""),
                         batch: str = Form(""), files: list[UploadFile] = File(default=[])):
    """«Объединить объективки»: из реестра и/или прикреплённых файлов Word, одним файлом с содержанием.
    Сборка и сверка ФИО — в отдельном потоке (Н-2)."""
    user = _writer(request)
    try:
        spec = json.loads(order or "[]")
    except ValueError:
        raise HTTPException(400, "Неверный порядок объединения")
    if not isinstance(spec, list) or not merge.MIN_ITEMS <= len(spec) <= merge.MAX_ITEMS:
        raise HTTPException(400, f"Объединяются от {merge.MIN_ITEMS} до {merge.MAX_ITEMS} объективок")
    if len(files) > merge.MAX_ITEMS:
        raise HTTPException(400, f"Не больше {merge.MAX_ITEMS} файлов")
    got = {}
    for i, f in enumerate(files):
        got[i] = (_upload_name(f), await _read_upload(f, MAX_SOURCE_BYTES, f"Файл «{_upload_name(f)}»"))
    rec = await run_in_threadpool(_do_merge, user, _ip(request), spec, got, title, batch)
    return {"merge": rec}


def _merge_or_404(mid: str) -> dict:
    rec = db.get_merge(mid)
    if rec is None or not (config.merged_dir() / f"{mid}.docx").exists():
        raise HTTPException(404, "Объединённый файл не найден")
    return rec


@app.get("/api/merges/{mid}")
def merge_info(mid: str):
    return {"merge": _merge_or_404(mid)}


@app.get("/api/merges/{mid}/docx")
def merge_download(mid: str, package: bool = False):
    rec = _merge_or_404(mid)
    bad = [m.get("title", "") for m in rec["items"] if m.get("check") in ("close", "failed")]
    if package and bad:
        raise HTTPException(409, "ФИО не сверено с реестром людей или расходится с ним: "
                            + ", ".join(f"«{t}»" for t in bad[:5]) + " — в пакет мероприятия не кладём. "
                            "Поправьте файлы или соберите заново.")
    return FileResponse(config.merged_dir() / f"{mid}.docx", media_type=DOCX, headers={
        "Content-Disposition": f"attachment; filename*=UTF-8''{quote(rec['file_name'])}"})


@app.post("/api/merges/{mid}/meeting")
def merge_meeting(mid: str, body: MeetingIn, request: Request):
    user = _writer(request)
    rec = _merge_or_404(mid)
    m = _meeting(body)
    db.set_merge_meeting(mid, m)
    portal.audit(user, "Объединённые объективки приложены к мероприятию" if m["filed"]
                 else "Выбрано мероприятие для объединённых объективок",
                 rec["title"], f"«{_short(m['short_title'] or m['title'])}» {m['date']}", _ip(request))
    return {"merge": db.get_merge(mid)}


# ── пакетная переработка ────────────────────────────────────────────────

def batch_view(b: dict) -> dict:
    """Пакет: по каждому файлу — карточка, её состояние и предупреждения."""
    rows, done, failed = [], 0, 0
    for item in b["items"]:
        row = {"file": item.get("file", ""), "error": item.get("error", ""), "profile": None, "warnings": []}
        p = db.get(item["profile"]) if item.get("profile") else None
        if item.get("profile") and p is None:
            row["error"] = "карточку удалили"
        if p is not None:
            row["profile"] = {"id": p["id"], "fio": p["data"].get("fio", ""), "status": p["status"],
                              "status_text": p["status_text"], "archived": p["archived"],
                              "running": p["status"] in (db.RUNNING, db.QUEUED) and jobs.active(p["id"]),
                              "job": jobs.view(jobs.of(p["id"])), "fit": p["fit"], "rev": p["rev"]}
            if p["status"] == jobs.DONE:
                if not p["data"].get("fio"):
                    row["warnings"].append("не нашлось ФИО — впишите его в карточку")
                if p["fit"] and not p["fit"].get("fits"):
                    row["warnings"].append("не уложилась в одну страницу")
                if p["archived"]:
                    row["warnings"].append("карточка в архиве")
            elif p["status"] in (jobs.ERROR, jobs.CANCELLED, "interrupted"):
                row["error"] = p["status_text"] or "сбор не удался"
        if row["profile"] and row["profile"]["status"] == jobs.DONE:
            done += 1
        elif row["error"]:
            failed += 1
        rows.append(row)
    left = len(rows) - done - failed
    ready = [r["profile"]["id"] for r in rows if r["profile"] and r["profile"]["status"] == jobs.DONE
             and r["profile"]["fio"] and not r["profile"]["archived"]]
    return {"id": b["id"], "created_at": b["created_at"], "web": b["web"], "items": rows,
            "done": done, "failed": failed, "left": left, "ready": ready, "merge_id": b["merge_id"],
            "merge": db.get_merge(b["merge_id"]) if b["merge_id"] else None}


def _read_batch_file(name: str, raw: bytes | None, error: str) -> dict:
    """Один файл пакета → прочитанный текст и фото или строка ошибки. Любая ошибка — в строку,
    а не 500 на весь пакет (ревью RC-2026-10-05-2, Н-1)."""
    if error:
        return {"file": name, "error": error}
    try:
        text, photo = extract.read_source(name, raw)
        return {"file": name, "text": text, "photo": photo}
    except extract.SourceError as e:
        return {"file": name, "error": str(e)}
    except Exception as e:  # noqa: BLE001 — неожиданный сбой чтения одного файла не роняет пакет
        return {"file": name, "error": f"Файл «{name}» не прочитан ({type(e).__name__})"}


def _make_batch(user: dict, ip: str, files: list[tuple[str, bytes | None, str]], web: bool, key: str) -> dict:
    """Сначала все файлы читаются, потом пакет заводится сразу со строками: пустого пакета и карточек
    без пакета не бывает; повтор из того же окна отдаёт уже собранный пакет."""
    if key:
        old = db.add_batch(user["login"], [], web, key, lookup_only=True)
        if old:
            return old
    read = [_read_batch_file(*f) for f in files]
    b, created = db.add_batch(user["login"], [{"file": r["file"], "error": r.get("error", "")} for r in read], web, key)
    if not created:
        return b
    items = []
    for r in read:
        if r.get("error"):
            items.append({"file": r["file"], "error": r["error"]})
            continue
        try:
            p = _create_from_text(user, ip, r["file"], r["text"], r["photo"], web, "", batch=b["id"])
            items.append({"file": r["file"], "profile": p["id"]})
        except Exception as e:  # noqa: BLE001
            items.append({"file": r["file"], "error": f"Карточку завести не удалось ({type(e).__name__})"})
        db.set_batch(b["id"], items=items + [{"file": x["file"], "error": "", "pending": True}
                                             for x in read[len(items):]])
    b = db.set_batch(b["id"], items=items)
    portal.audit(user, "Пакетная переработка объективок", f"{len(items)} документов",
                 f"принято {sum(1 for i in items if i.get('profile'))}, не прочитано "
                 f"{sum(1 for i in items if i.get('error'))}", ip)
    return b


@app.post("/api/batches")
async def create_batch(request: Request, files: list[UploadFile] = File(...), web: str = Form("0"),
                       key: str = Form("")):
    """«Переработать и объединить»: до 10 документов о людях → карточки по нашей форме (путь
    «по справке»), «Искра» разбирает их по очереди (`iskra.SLOTS`), потом — один файл.
    Чтение файлов — в отдельном потоке (Н-2)."""
    user = _writer(request)
    if not 1 <= len(files) <= MAX_BATCH:
        raise HTTPException(400, f"Приложите от 1 до {MAX_BATCH} документов")
    _iskra_or_503()
    k = f"{user['login']}:{key}" if _KEY.match(key or "") else ""
    got = []
    for f in files:
        name = _upload_name(f)
        try:
            got.append((name, await _read_upload(f, MAX_SOURCE_BYTES, f"Файл «{name}»"), ""))
        except HTTPException as error:
            got.append((name, None, str(error.detail)))
    b = await run_in_threadpool(_make_batch, user, _ip(request), got, web in ("1", "true", "on"), k)
    return batch_view(b)


@app.get("/api/batches")
def my_batches(request: Request):
    return {"items": [batch_view(b) for b in db.batches_of(request.state.user["login"], 10)]}


@app.get("/api/batches/{bid}")
def get_batch(bid: str):
    b = db.get_batch(bid)
    if b is None:
        raise HTTPException(404, "Пакет не найден")
    return batch_view(b)


class OrderIn(BaseModel):
    profiles: list[str] = []


@app.put("/api/batches/{bid}/order")
def batch_order(bid: str, body: OrderIn, request: Request):
    """Порядок карточек пакета перед сборкой (↑/↓, перетаскивание)."""
    _writer(request)
    b = db.get_batch(bid)
    if b is None:
        raise HTTPException(404, "Пакет не найден")
    by_profile = {i.get("profile"): i for i in b["items"] if i.get("profile")}
    if sorted(body.profiles) != sorted(by_profile):
        raise HTTPException(409, "Состав пакета изменился — обновите страницу")
    rest = [i for i in b["items"] if not i.get("profile")]
    return batch_view(db.set_batch(bid, items=[by_profile[pid] for pid in body.profiles] + rest))
