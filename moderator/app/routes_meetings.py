"""Маршруты совещания целиком: список, создание, шапка, архив, приём повестки, выгрузка."""
from __future__ import annotations

import re
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import PlainTextResponse, Response

from . import agenda_docx, docx_export, exporter, llm_extract, people_flow, portal, readiness, storage, template_adapter
from .deps import admin_meeting_or_404, check_base, current_user, meeting_or_404
from .meeting_title import short_title
from .models import AgendaItem, Meeting, Participant, normalize_date
from .protocol import protocol_rank
from .schemas import AccessIn, AgendaImportIn, FindOrCreateIn, FolderIn, HeaderIn, MeetingIn

router = APIRouter()


@router.get("/api/meetings")
def list_meetings(archived: bool = False):
    return {"meetings": storage.list_ids(archived=archived)}


@router.get("/api/search")
def search(q: str = "", limit: int = 5):
    """Поиск для портала: он спрашивает все сервисы сразу и показывает общий список.

    Ищем по названию совещания и по вопросам повестки: так находится и «дети-сироты»,
    и совещание, где этот вопрос обсуждался.
    """
    query = (q or "").strip().lower()
    if len(query) < 2:
        return {"items": []}
    limit = max(1, min(int(limit or 5), 20))
    items = []
    # сначала действующие, потом архив: свежее нужнее
    for archived, rows in ((False, storage.list_ids()), (True, storage.list_ids(archived=True))):
        for row in rows:
            if len(items) >= limit:
                break
            m = storage.get(row["id"])
            if m is None:
                continue
            haystack = " ".join([m.title or "", m.subtitle or "",
                                 " ".join(a.title or "" for a in m.agenda),
                                 " ".join(p.name or "" for p in m.participants)]).lower()
            if query not in haystack:
                continue
            items.append({
                "title": (row["title"] or "Без названия") + (" · архив" if archived else ""),
                "url": f"/moderator/#meeting={row['id']}",
                "when": row.get("date") or (row.get("updated_at") or "")[:10],
                "who": row.get("owner") or "",
                "kind": "совещание",
            })
    return {"items": items}


@router.get("/api/meetings/{mid}/access")
def get_access(mid: str):
    """Кто работает над совещанием: владелец и те, кому его открыли."""
    m = meeting_or_404(mid)
    return {"owner": m.owner, "shared_with": list(m.shared_with), "people": portal.people()}


@router.put("/api/meetings/{mid}/access")
def set_access(mid: str, body: AccessIn, request: Request):
    """Открыть совещание коллегам или закрыть. Меняет владелец; администратор — тоже.

    Это не запрет, а круг показа: пространство открытое, но у коллеги совещание появится
    в его «Мои», и он не будет искать его среди чужих.
    """
    m = meeting_or_404(mid)
    login = request.state.login
    if m.owner and m.owner != login and not request.state.user["is_admin"]:
        raise HTTPException(status_code=403, detail="Открыть доступ может владелец совещания")
    known = {p["login"] for p in portal.people()}
    people = [x for x in dict.fromkeys(body.shared_with) if x in known and x != m.owner]
    was, m.shared_with = list(m.shared_with), people
    if body.owner and body.owner in known and body.owner != m.owner:
        portal.note_change(f"владелец: {m.owner or 'нет'} → {body.owner}")
        m.owner = body.owner
    storage.save(m)
    added = [x for x in people if x not in was]
    gone = [x for x in was if x not in people]
    if added:
        portal.note_change("открыт доступ: " + ", ".join(added))
    if gone:
        portal.note_change("доступ закрыт: " + ", ".join(gone))
    return {"owner": m.owner, "shared_with": m.shared_with}


@router.get("/api/upcoming")
def upcoming(days: int = 14):
    """Ближайшие совещания с готовностью — для напоминания на главной портала.

    Дата у совещания есть, а напомнить о ней было некому: человек узнавал «материалов нет»
    в день совещания. Здесь и срок, и чего не хватает.
    """
    import datetime as dt
    today = dt.date.today()
    horizon = today + dt.timedelta(days=max(1, min(int(days or 14), 90)))
    out = []
    for row in storage.list_ids():
        m = storage.get(row["id"])
        if m is None or not m.date:
            continue
        parts = (normalize_date(m.date) or "").split(".")
        if len(parts) != 3 or not all(parts):
            continue
        when = dt.date(int(parts[2]), int(parts[1]), int(parts[0]))
        if not (today <= when <= horizon):
            continue
        state = readiness.readiness(m)
        not_done = [i["label"] for i in state["items"] if not i["done"]][:3]
        out.append({
            "id": m.id, "title": m.title or "без названия", "short_title": m.short_title or "без названия",
            "date": m.date,
            "days_left": (when - today).days, "percent": state["percent"],
            "not_done": not_done, "url": f"/moderator/#meeting={m.id}",
        })
    out.sort(key=lambda x: x["days_left"])
    return {"meetings": out}


@router.get("/api/orders")
def all_orders(who: str = "", overdue: bool = False):
    """Все поручения со всех совещаний — для общего реестра в портале.

    Поручения живут внутри совещаний, и «что там у нас по прошлому совещанию» приходилось
    искать руками. Здесь они собраны в один список с датой совещания и ссылкой на него.
    """
    import datetime as dt
    today = dt.date.today().isoformat()
    needle = (who or "").strip().lower()
    out = []
    for archived, rows in ((False, storage.list_ids()), (True, storage.list_ids(archived=True))):
        for row in rows:
            m = storage.get(row["id"])
            if m is None:
                continue
            for order in m.orders:
                if needle and needle not in (order.assignee or "").lower():
                    continue
                deadline = normalize_date(order.deadline or "") or (order.deadline or "")
                # normalize_date отдаёт ДД.ММ.ГГГГ — для сравнения со «сегодня» переводим в ГГГГ-ММ-ДД
                parts = deadline.split(".")
                iso = f"{parts[2]}-{parts[1]}-{parts[0]}" if len(parts) == 3 and all(parts) else ""
                late = bool(iso) and iso < today
                if overdue and not late:
                    continue
                out.append({
                    "id": order.id,
                    "text": order.text,
                    "assignee": order.assignee,
                    "deadline": deadline,
                    "deadline_iso": iso,
                    "overdue": late,
                    "meeting_id": m.id,
                    "meeting_title": m.title or "без названия",
                    "meeting_date": m.date or "",
                    "agenda_num": order.agenda_num or "",
                    "archived": archived,
                    "url": f"/moderator/#meeting={m.id}",
                })
    # сначала просроченные, потом ближайшие сроки, потом без срока
    out.sort(key=lambda o: (not o["overdue"], o["deadline_iso"] or "9999-99-99"))
    return {"orders": out}


@router.post("/api/meetings")
def create_meeting(body: MeetingIn, request: Request):
    d = body.model_dump()
    d["owner"] = request.state.login
    # из формы дата приходит как ГГГГ-ММ-ДД: без приведения она разойдётся с датами
    # из других мест (архив по прошедшей дате, поиск «дата + название» у соседей)
    d["date"] = normalize_date(d.get("date", ""))
    if body.id.strip():
        d["id"] = body.id.strip()
        m = Meeting(**d)
        if not storage.insert_new(m):
            raise HTTPException(status_code=409, detail="Совещание с таким ключом уже существует")
    else:
        d["id"] = ""
        m = Meeting(**d)
        storage.insert_with_next_id(m)
    portal.note_object(m.id)
    return {"meeting": m.to_dict(), "readiness": readiness.readiness(m)}


@router.post("/api/meetings/find-or-create")
def find_or_create(body: FindOrCreateIn, request: Request):
    """Совещание — общая ось пространства, его карточку ведём мы (`../CLAUDE.md` п. 9).

    Соседние сервисы своих карточек не заводят, а спрашивают отсюда. Ответ идемпотентен
    по паре «дата + название»: два сервиса, нажавшие кнопку подряд, получат одно совещание.
    Запрос приходит из браузера пользователя тем же доменом — права обычные, из заголовков портала.
    """
    date = normalize_date(body.date)
    title = " ".join((body.title or "").split())
    if not (date and title):
        raise HTTPException(status_code=400, detail="Нужны дата и название совещания")

    service = str((body.source or {}).get("service") or "")
    src_id = str((body.source or {}).get("id") or "")
    with storage.find_or_create_lock:
        m = storage.find_by_date_title(date, title)
        created = m is None
        if created:
            m = Meeting(id="", owner=request.state.login, date=date, title=title)
            if service and src_id:
                m.source = {"service": service, "meeting_id": src_id}
            storage.insert_with_next_id(m)

    portal.note_object(m.id)
    portal.note_change(
        (f"заведено «{title}» на {date}" if created else f"найдено «{title}» на {date}")
        + (f" (запрос из сервиса «{service}»)" if service else "")
    )
    return {"id": m.id, "url": f"/moderator/#meeting={quote(m.id)}", "created": created,
            "folder": m.folder, "short_title": m.short_title}


@router.put("/api/meetings/{mid}/folder")
def set_folder(mid: str, body: FolderIn):
    """Папку совещания ведут «Материалы» — здесь запоминаем только её id.

    Своей раскладки файлов у нас нет: «Скачать пакет» ведёт в папку, правила
    нумерации и реестр — там же.
    """
    m = meeting_or_404(mid)
    folder = (body.folder or "").strip()
    if not folder:
        raise HTTPException(status_code=400, detail="Нужен id папки в «Материалах»")
    if m.folder == folder:
        return {"folder": m.folder, "changed": False}
    was = m.folder
    m.folder = folder
    storage.save(m)
    portal.note_change(f"папка материалов: «{was}» → «{folder}»")
    return {"folder": m.folder, "changed": True}


@router.post("/api/meetings/import-from-agenda")
def import_from_agenda(body: AgendaImportIn, request: Request):
    """Приём готовой повестки: заменяем повестку и участников, работу в «Модераторе» не трогаем.

    Запрос приходит из браузера пользователя тем же доменом, поэтому пользователь и права
    обычные, из заголовков портала; общих секретов между сервисами не появляется.
    """
    service = str(body.source.get("service") or "")
    src_id = str(body.source.get("meeting_id") or "")
    created = False
    if body.target_id:
        m = meeting_or_404(body.target_id)
    else:
        m = storage.find_by_source(service, src_id)
        if m is None:
            m = Meeting(id="", owner=request.state.login)
            storage.insert_with_next_id(m)
            created = True
    if service and src_id:
        m.source = {"service": service, "meeting_id": src_id}

    # title — ключ совещания: при явном target_id (человек выбрал совещание из списка) его не трогаем —
    # иначе передача повестки переименует чужое совещание (ревьюер RC-22-3, Н3). Новое совещание и
    # совещание по своему source (та же повестка) — название ведёт «Повестка», это её «Переименовать».
    if not body.target_id or not m.title.strip():
        m.title = body.title
    # subtitle: пустой в запросе — это «не прислали», а не «стереть»: полное наименование или
    # подзаголовок, записанные раньше, остаются (ревьюер RC-22-3)
    if body.subtitle.strip():
        m.subtitle = body.subtitle
    for field_name in ("chair", "location_primary", "location_secondary", "time_start", "time_end"):
        setattr(m, field_name, getattr(body, field_name))
    m.date = normalize_date(body.date)

    was = (len(m.agenda), len(m.participants))
    m.agenda = [AgendaItem(**item.model_dump(exclude={"speaker_kinds"})) for item in body.agenda]
    m.participants = []
    for p in body.participants:
        data = p.model_dump()
        if not data.get("protocol_rank"):
            data["protocol_rank"] = protocol_rank(p.role)
        m.participants.append(Participant(**data))
    # порядок пришёл протокольным — дальше список ведём как обычно, по старшинству
    m.participants_manual = False
    llm_extract.sync_agenda_participants(m)
    # люди из «Повестки» — в очередь окна «Добавить в реестр?»: решит человек, открыв совещание
    for person in m.participants:
        portal.note_person(person.name, person.role, person.organization)
    people_flow.queue_noted(m, people_flow.document_label(m, "повестка"))
    people_flow.drop_known_from_pending(m)   # известных реестру полностью — заменять, не добавлять (Н-3)
    storage.save(m)
    portal.note_object(m.id)
    portal.note_change(
        f"повестка: {was[0]}→{len(m.agenda)}; участники: {was[1]}→{len(m.participants)}"
        + (" (создано совещание)" if created else " (заменены повестка и участники)")
    )
    return {"id": m.id, "url": f"/moderator/#meeting={quote(m.id)}", "created": created}


@router.get("/api/meetings/{mid}")
def get_meeting(mid: str):
    m = meeting_or_404(mid)
    m.fill_agenda_times()
    _before = _parts_signature(m)
    # состав участников — по повестке; должности из реестра здесь не ищем: открытие не ждёт портал
    llm_extract.sync_agenda_participants(m, fill_roles=False)
    # Дозаполнение меняет состав/данные — сразу фиксируем на диск, иначе следующее
    # действие (например, стрелки ↑/↓) работало бы с устаревшим порядком участников.
    if _parts_signature(m) != _before:
        storage.save(m)
    return {"meeting": m.to_dict(), "readiness": readiness.readiness(m), "timeline": m.timeline(),
            "block_times": m.block_times(), "editors": _other_editors(mid)}


def _other_editors(mid: str) -> list[dict]:
    """Коллеги, менявшие это совещание последние минуты: показываем предупреждение в шапке."""
    user = current_user.get() or {}
    return storage.active_editors(mid, user.get("login", ""))


@router.get("/api/meetings/{mid}/editors")
def meeting_editors(mid: str):
    """Опрос из интерфейса: кто ещё правит это совещание прямо сейчас."""
    meeting_or_404(mid)
    return {"editors": _other_editors(mid)}


def _parts_signature(m) -> list:
    """Слепок состава участников: имена в текущем порядке + должности/ранги."""
    return [(p.name, p.role, p.organization, p.protocol_rank, p.is_speaker) for p in m.participants]


@router.put("/api/meetings/{mid}/header")
def update_header(mid: str, body: HeaderIn):
    m = meeting_or_404(mid)
    check_base(m, body.base, mid, "Данные совещания")
    for k, v in body.model_dump(exclude={"base", "short_title"}).items():
        if k == "date":
            v = normalize_date(v)
        setattr(m, k, v)
    if body.short_title is not None:
        typed = " ".join(body.short_title.split())
        # совпадает с тем, что считает правило, — не закрепляем: поправят полное название, короткое пойдёт следом
        m.short_title_manual = "" if typed == short_title(m.title) else typed
    storage.save(m)
    return {"meeting": m.to_dict(), "readiness": readiness.readiness(m)}


@router.delete("/api/meetings/{mid}")
def delete_meeting(mid: str):
    # мягкое удаление — перемещение в архив
    meeting_or_404(mid)
    if not storage.archive(mid):
        raise HTTPException(status_code=404, detail="Совещание не найдено")
    return {"archived": True}


@router.post("/api/meetings/{mid}/restore")
def restore_meeting(mid: str):
    meeting_or_404(mid)
    if not storage.restore(mid):
        raise HTTPException(status_code=404, detail="Совещание не найдено")
    return {"restored": True}


@router.delete("/api/meetings/{mid}/permanent")
def delete_meeting_permanent(mid: str):
    admin_meeting_or_404(mid, "удалить совещание навсегда")
    if not storage.delete(mid):
        raise HTTPException(status_code=404, detail="Совещание не найдено")
    return {"deleted": True}


UNCHECKED_MARK = "ФИО не сверены с реестром людей"


def _require_checked(checked: dict, unchecked: bool) -> dict:
    """Обязательная сверка ФИО перед любым документом совещания (../CLAUDE.md п. 5а).

    close — 409, документ не выпускаем. Реестр не ответил — 409 `X-Names-Check: unavailable`,
    пока не пришёл флаг `unchecked` (интерфейс ставит его только после окна «Выгрузить без сверки?»;
    прямая ссылка тоже его ставит, поэтому в журнале — факт, а не «человек подтвердил»).
    """
    if checked["unavailable"] and not unchecked:
        raise HTTPException(status_code=409, headers={"X-Names-Check": "unavailable"},
                            detail="Сверка ФИО не выполнена: реестр людей не ответил. Выгрузить без сверки?")
    if checked["unavailable"]:
        portal.note_change("выгружено без сверки ФИО (реестр не ответил, флаг unchecked)")
    close = [x for x in checked["items"] if x.get("status") == "close"]
    if close:
        names = ", ".join(x.get("name", "") for x in close)
        raise HTTPException(status_code=409, detail=f"В реестре записаны иначе: {names} — проверьте ФИО "
                                                    "перед выгрузкой (вероятно, опечатка)")
    return checked


def _names_checked(m, unchecked: bool) -> dict:
    """Сверка ФИО документа ведущего — Word и HTML."""
    return _require_checked(people_flow.check(m), unchecked)


@router.get("/api/meetings/{mid}/export.docx")
def export_docx(mid: str, unchecked: bool = False):
    """Документ «Модератор» в Word по вёрстке образца пользователя (../MODERATOR-DOCX-SPEC.md).

    Без времени начала не собираем: ремарки «ЧЧ:ММ–ЧЧ:ММ» считать не от чего, а подменять
    их на «5 минут» пользователь не хочет. Интерфейс в этом случае просит поставить время.
    """
    m = meeting_or_404(mid)
    checked = _names_checked(m, unchecked)
    m.refresh_speaker_phrases()      # ФИО во фразах сценария — общим склонением, без «Павела»
    try:
        data = docx_export.render(m, unchecked=checked["unavailable"])
    except docx_export.NoStartTime:
        raise HTTPException(status_code=409, detail="Укажите время начала совещания — "
                                                    "по нему считаются часы в документе")
    name = docx_export.filename(m, unchecked=checked["unavailable"])
    return Response(
        data,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={"Content-Disposition": f"attachment; filename=\"moderator.docx\"; filename*=UTF-8''{quote(name)}",
                 "X-Filename": quote(name),
                 # портал не ответил — документ собран без сверки ФИО, интерфейс предупредит
                 "X-Names-Check": "skipped" if checked["unavailable"] else "done"},
    )


@router.get("/api/meetings/{mid}/export", response_class=PlainTextResponse)
def export(mid: str, unchecked: bool = False):
    m = meeting_or_404(mid)
    checked = _names_checked(m, unchecked)      # та же обязательная сверка, что у Word
    m.refresh_speaker_phrases()
    try:
        html = template_adapter.render(m)
    except FileNotFoundError:
        html = exporter.render(m)  # fallback: чистая вёрстка, если шаблона нет
    if checked["unavailable"]:
        html = _mark_unchecked(html)
    filename = (m.id or "brief") + (" (ФИО не сверены)" if checked["unavailable"] else "") + ".html"
    return PlainTextResponse(
        html,
        media_type="text/html",
        headers={"Content-Disposition": f"attachment; filename=\"brief.html\"; filename*=UTF-8''{quote(filename)}",
                 "X-Names-Check": "skipped" if checked["unavailable"] else "done"},
    )


def _mark_unchecked(html: str) -> str:
    """Несверенный документ помечен в нём самом: пометка видна любому, кто откроет файл."""
    mark = ('<div style="background:#fff4e5;color:#8a4b00;border:1px solid #f0b060;padding:8px 12px;'
            f'font-weight:600;font-family:sans-serif;">{UNCHECKED_MARK}</div>')
    return re.sub(r"(<body[^>]*>)", r"\1" + mark, html, count=1) if re.search(r"<body[^>]*>", html) \
        else mark + html


@router.get("/api/meetings/{mid}/export/agenda.html", response_class=PlainTextResponse)
def export_agenda(mid: str, unchecked: bool = False):
    """Документ «Повестка» — для отправки в пакет совещания (../CLAUDE.md п. 9).

    Номер и место в пакете расставляют «Материалы» по имени файла (ровно «Повестка»),
    здесь — только содержимое и обязательная сверка ФИО докладчиков.
    """
    m = meeting_or_404(mid)
    checked = _require_checked(people_flow.check_agenda(m), unchecked)
    html = exporter.render_agenda(m)
    if checked["unavailable"]:
        html = _mark_unchecked(html)
    name = exporter.agenda_filename()
    return PlainTextResponse(
        html,
        media_type="text/html",
        headers={"Content-Disposition": f"attachment; filename=\"agenda.html\"; filename*=UTF-8''{quote(name)}",
                 "X-Filename": quote(name),
                 "X-Names-Check": "skipped" if checked["unavailable"] else "done"},
    )


@router.get("/api/meetings/{mid}/export/participants.html", response_class=PlainTextResponse)
def export_participants(mid: str, unchecked: bool = False):
    """Документ «Список участников» — для отправки в пакет совещания (../CLAUDE.md п. 9)."""
    m = meeting_or_404(mid)
    checked = _require_checked(people_flow.check_participants(m), unchecked)
    html = exporter.render_participants(m)
    if checked["unavailable"]:
        html = _mark_unchecked(html)
    name = exporter.participants_filename()
    return PlainTextResponse(
        html,
        media_type="text/html",
        headers={"Content-Disposition": f"attachment; filename=\"participants.html\"; filename*=UTF-8''{quote(name)}",
                 "X-Filename": quote(name),
                 "X-Names-Check": "skipped" if checked["unavailable"] else "done"},
    )


DOCX_MEDIA = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


@router.get("/api/meetings/{mid}/export/agenda.docx")
def export_agenda_docx(mid: str, unchecked: bool = False):
    """«Повестка» по утверждённому образцу в Word (решение пользователя 18.09/28.09, перенос
    из сервиса «Повестка» — `app/agenda_docx.py`) — для отправки в пакет совещания
    (../CLAUDE.md п. 9), заменяет собой прежний простой HTML в этой роли (`export/agenda.html`
    остаётся, но пакет теперь получает именно этот файл)."""
    m = meeting_or_404(mid)
    checked = _require_checked(people_flow.check_agenda(m), unchecked)
    data = agenda_docx.to_bytes(agenda_docx.build_agenda(m, unchecked=checked["unavailable"]))
    name = agenda_docx.agenda_filename(unchecked=checked["unavailable"])
    return Response(
        data, media_type=DOCX_MEDIA,
        headers={"Content-Disposition": f"attachment; filename=\"agenda.docx\"; filename*=UTF-8''{quote(name)}",
                 "X-Filename": quote(name),
                 "X-Names-Check": "skipped" if checked["unavailable"] else "done"},
    )


@router.get("/api/meetings/{mid}/export/participants.docx")
def export_participants_docx(mid: str, unchecked: bool = False):
    """«Список участников» по утверждённому образцу в Word — та же замена HTML на docx
    в пакете, что и у «Повестки» выше."""
    m = meeting_or_404(mid)
    checked = _require_checked(people_flow.check_participants(m), unchecked)
    data = agenda_docx.to_bytes(agenda_docx.build_participants(m, unchecked=checked["unavailable"]))
    name = agenda_docx.participants_filename(unchecked=checked["unavailable"])
    return Response(
        data, media_type=DOCX_MEDIA,
        headers={"Content-Disposition": f"attachment; filename=\"participants.docx\"; filename*=UTF-8''{quote(name)}",
                 "X-Filename": quote(name),
                 "X-Names-Check": "skipped" if checked["unavailable"] else "done"},
    )
