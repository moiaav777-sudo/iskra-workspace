"""Маршруты разбора документов: вставка текста, Искра, извлечение текста из файлов, справочник имён."""
from __future__ import annotations

import hashlib
import secrets
import threading
from urllib.parse import quote

from fastapi import APIRouter, File, Form, HTTPException, UploadFile

from . import agenda_changes, extract, llm_extract, parser, portal, readiness, storage, people_flow
from .deps import check_text, meeting_or_404, read_upload
from .models import AgendaItem, Order, Participant, renumber_agenda
from .protocol import protocol_rank
from .schemas import AgendaApplyChangesIn, AnalyzeIn, ApplyIn, ImportIn, NameReplaceIn, PendingResolveIn

router = APIRouter()


@router.post("/api/meetings/{mid}/import")
def import_text(mid: str, body: ImportIn):
    m = meeting_or_404(mid)
    lines = [ln for ln in body.text.splitlines() if ln.strip()]
    if len(lines) > 25:
        raise HTTPException(status_code=400, detail="Это похоже на полный документ — используйте «Умный разбор (Искра)».")
    if body.kind == "agenda":
        for item in parser.parse_agenda(body.text):
            m.agenda.append(AgendaItem(**item))
    elif body.kind == "participants":
        for p in parser.parse_participants(body.text):
            p["protocol_rank"] = protocol_rank(p.get("role", ""))
            m.participants.append(Participant(**p))
    elif body.kind == "orders":
        for o in parser.parse_orders(body.text):
            m.orders.append(Order(**o))
    else:
        raise HTTPException(status_code=400, detail="Неизвестный тип вставки")
    storage.save(m)
    return {"meeting": m.to_dict(), "readiness": readiness.readiness(m)}


# ── умный разбор через Искру: определение типа + извлечение → подтверждение ─


@router.post("/api/analyze")
def analyze(body: AnalyzeIn):
    """Искра определяет тип документа и извлекает данные. Ничего НЕ применяет."""
    check_text(body.text)
    if body.meeting_id:
        meeting_or_404(body.meeting_id)  # проверить доступ до обращения к Искре
    try:
        result = llm_extract.extract(body.text, hint=body.hint)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Искра не ответила: {e}")
    result = llm_extract.verify_names(result, body.text)
    return _with_diff(result, body.meeting_id)


@router.post("/api/analyze-file")
def analyze_file(file: UploadFile = File(...), meeting_id: str = "", hint: str = ""):
    """Файл целиком уходит в Искру, та разбирает его напрямую (без локального извлечения)."""
    if meeting_id:
        meeting_or_404(meeting_id)
    filename = file.filename or ""
    file_bytes = read_upload(file)
    _check_inbox_file(filename, file_bytes)
    file_bytes, filename = _for_iskra(filename, file_bytes)
    try:
        result = llm_extract.extract_with_file(file_bytes, filename, hint)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Искра не ответила: {e}")
    return _with_diff(result, meeting_id)


def _with_diff(result: dict, meeting_id: str) -> dict:
    diff = None
    conflicts = []
    changes = None
    if meeting_id:
        mt = meeting_or_404(meeting_id)
        diff = llm_extract.compute_diff(mt, result)
        conflicts = llm_extract.compute_registry_conflicts(mt, result)
        # непустая повестка/список участников — не «Заменить целиком» (решение 28.09, стирало
        # материалы/поручения/сценарий по номерам вопросов), а список изменений с галочками
        data = result.get("data") or {}
        if result.get("doc_type") == "повестка" and not agenda_changes.is_empty_agenda(mt):
            changes = agenda_changes.diff_agenda(mt, data)
        elif result.get("doc_type") == "участники" and not agenda_changes.is_empty_participants(mt):
            changes = agenda_changes.diff_participants(mt, data)
    return {"result": result, "diff": diff, "conflicts": conflicts, "changes": changes}


@router.post("/api/meetings/{mid}/agenda/apply-changes")
def apply_agenda_changes(mid: str, body: AgendaApplyChangesIn):
    """Внести отмеченные изменения повестки/участников (список с галочками, ../CLAUDE.md п. 5а
    и решение 28.09 — перенос из сервиса `agenda`, см. `app/agenda_changes.py`).

    Устаревшее (кто-то успел поправить совещание, пока человек отмечал галочки) не вносится
    молча: без `force` — 409 с раскладкой «что ещё применимо / что устарело», человек решает,
    вносить ли применимое; с `force` — вносится только применимое, а не то, что уже устарело.
    """
    if not body.ops:
        raise HTTPException(status_code=400, detail="Ничего не отмечено")
    m = meeting_or_404(mid)
    valid = [op for op in body.ops if agenda_changes.still_valid(m, op)]
    stale = [op for op in body.ops if op not in valid]
    if stale and not body.force:
        raise HTTPException(status_code=409, detail={
            "message": "Пока вы отмечали изменения, совещание поменяли. Можно внести только то, "
                       "чего эта правка не коснулась",
            "applicable": [op.get("id") for op in valid],
            "stale": [{"id": op.get("id"), "label": op.get("label", "")} for op in stale]})
    if not valid:
        raise HTTPException(status_code=409, detail={
            "message": "Ничего из отмеченного внести нельзя: совещание уже поменяли",
            "applicable": [], "stale": [{"id": op.get("id"), "label": op.get("label", "")} for op in stale]})
    done: list[str] = []
    skipped = list(stale)
    people: list[tuple[str, str, str]] = []
    has_agenda_op = has_participant_op = False
    for op in valid:
        # после удаления вопроса ссылки следующих изменений этого же пакета могли устареть —
        # перепроверяем на лету, а не только один раз в начале
        if not agenda_changes.still_valid(m, op):
            skipped.append(op)
            continue
        text, who = agenda_changes.apply(m, op)
        done.append(text)
        people.extend(who)
        if op["kind"] in ("add_question", "remove_question", "add_speaker", "replace_speaker",
                          "remove_speaker", "speaker_role", "speaker_minutes"):
            has_agenda_op = True
        else:
            has_participant_op = True
    if done:
        renumber_agenda(m)
        llm_extract.sync_agenda_participants(m)
        for name, role, org in people:
            portal.note_person(name, role, org)
        label = "повестка" if has_agenda_op else "участники"
        people_flow.queue_noted(m, people_flow.document_label(m, label))
        if has_agenda_op:   # кого реестр знает полностью — не добавлять, а заменить (окно сверки)
            people_flow.drop_known_from_pending(m)
        storage.save(m)
    return {"meeting": m.to_dict(), "readiness": readiness.readiness(m),
            "applied": done, "skipped": [{"id": op.get("id"), "label": op.get("label", "")} for op in skipped]}


@router.post("/api/meetings/{mid}/apply")
def apply_result(mid: str, body: ApplyIn):
    """Применить подтверждённый результат разбора (только добавление)."""
    m = meeting_or_404(mid)
    # Сюда приходит произвольный JSON из браузера, а не проверенный ответ Искры:
    # без проверки схемы в бриф попадал мусор, а «10 минут» строкой роняли сервер.
    try:
        result = llm_extract.check_schema(body.result)
    except Exception as e:
        raise HTTPException(status_code=422, detail=f"Данные не подходят по структуре: {e}")
    doc_type = result.get("doc_type", "")
    # «Заменить целиком» для непустой повестки/участников стирает материалы, поручения и
    # сценарий, привязанные к номерам вопросов (решение 28.09) — вместо неё интерфейс предлагает
    # список изменений с галочками (`agenda_changes`). Само поле в интерфейсе для этого случая
    # не показывается, но запрос можно собрать в обход (вкладка со старым скриптом, curl) —
    # ограничение только на клиенте не ограничение (ревьюер RC-2026-09-28-14, Н3), сервер тоже отказывает.
    if body.replace and doc_type == "повестка" and not agenda_changes.is_empty_agenda(m):
        raise HTTPException(status_code=409,
                             detail="Повестка не пуста — используйте список изменений с галочками, а не «Заменить целиком»")
    if body.replace and doc_type == "участники" and not agenda_changes.is_empty_participants(m):
        raise HTTPException(status_code=409,
                             detail="Список участников не пуст — используйте список изменений с галочками, а не «Заменить целиком»")
    llm_extract.apply_to_meeting(m, result, replace=body.replace, resolutions=body.resolutions)
    # найденных людей в реестр не пишем — ставим в очередь окна «Добавить в реестр?»
    people_flow.queue_noted(m, people_flow.document_label(m, result.get("doc_type", "")))
    if doc_type == "повестка":   # кого реестр знает полностью — не добавлять, а заменить (окно сверки)
        people_flow.drop_known_from_pending(m)
    storage.save(m)
    return {"meeting": m.to_dict(), "readiness": readiness.readiness(m)}


# ── загрузка файлов (Word/PPT/PDF → текст) ───────────────────────────────


@router.post("/api/extract")
def extract_upload(file: UploadFile = File(...)):
    """Текст файла: Word, Excel, PowerPoint, PDF, RTF, ODT, текст — формат по содержимому."""
    name = file.filename or ""
    content = read_upload(file)
    if extract.problem(name, content) and not extract.listed(name):
        raise HTTPException(status_code=400, detail=f"Поддерживаются {extract.WHAT}")
    try:
        text = extract.extract_bytes(content, name)
    except Exception as e:
        raise HTTPException(status_code=422, detail=f"Не удалось извлечь текст: {e}")
    return {"filename": file.filename, "text": text}


# ── подсказка по людям: общий реестр портала ───────────────────────────


@router.get("/api/names/resolve")
def resolve_name(q: str):
    """Подсказка по людям — из общего реестра портала (../CLAUDE.md п. 5а).

    Люди лежали в трёх местах и расходились: своя таблица здесь, реестр «Повестки» и
    `names.json`. Теперь реестр один, в портале; своего у «Модератора» нет.
    """
    return {"matches": [{**person, "source": "реестр людей"} for person in portal.people_search(q)]}


@router.post("/api/meetings/{mid}/people/check")
def people_check(mid: str):
    """Сверка ФИО документа ведущего с реестром: exact | close | unknown по каждому (../CLAUDE.md п. 5а)."""
    return people_flow.check(meeting_or_404(mid))


@router.post("/api/meetings/{mid}/people/check-agenda")
def people_check_agenda(mid: str):
    """Та же обязательная сверка, но для докладчиков документа «Повестка»."""
    return people_flow.check_agenda(meeting_or_404(mid))


@router.post("/api/meetings/{mid}/people/check-participants")
def people_check_participants(mid: str):
    """Та же обязательная сверка, но для всех ФИО документа «Список участников»."""
    return people_flow.check_participants(meeting_or_404(mid))


@router.post("/api/meetings/{mid}/people/check-orders")
def people_check_orders(mid: str):
    """Та же обязательная сверка для «Перечня поручений»: подпись председателя."""
    return people_flow.check_orders(meeting_or_404(mid))


@router.post("/api/meetings/{mid}/people/replace")
def people_replace(mid: str, body: NameReplaceIn):
    """«Кадошников П.А. — в реестре Кадочников П.А.»: заменить одним нажатием во всём совещании."""
    m = meeting_or_404(mid)
    same = " ".join(body.old.split()) == " ".join(body.new.split())
    count = people_flow.replace_name(m, body.old, body.new, body.role)
    if not count and not same:
        raise HTTPException(status_code=409, detail=f"«{body.old}» в совещании уже нет — данные обновлены")
    if not count:   # то же ФИО, должность уже стояла — менять нечего, и это не ошибка (ревьюер RC-2026-10-02-1, Н2)
        return {"meeting": m.to_dict(), "replaced": 0}
    portal.note_change((f"ФИО: «{body.old}» → «{body.new}»" if not same else f"должность: «{body.new}»")
                       + (f" ({body.role})" if body.role else ""))
    storage.save(m)
    people_flow.forget_checks(m.id)      # следующая сверка — по свежему реестру
    return {"meeting": m.to_dict(), "replaced": count}


@router.post("/api/meetings/{mid}/people/pending/resolve")
def people_pending_resolve(mid: str, body: PendingResolveIn):
    """Человек решил по окну «Добавить в реестр?» — убираем из очереди (запись в реестр он сделал сам)."""
    m = meeting_or_404(mid)
    if body.added:
        people_flow.forget_checks(m.id)  # человек добавил запись в реестр — старая сверка устарела
    if people_flow.resolve_pending(m, body.name):
        portal.note_change(f"реестр людей: {body.name} — {'добавлен' if body.added else 'не добавлять'}")
        storage.save(m)
    return {"pending": m.registry_pending}


# ── приём из «Приёмной» «Материалов» (шаг 2 перехода «Повестка → Совещание», решение
# пользователя 29.09) ────────────────────────────────────────────────────────────────────
#
# Контракт списан с `agenda/app/main.py::/api/inbox` (только читали, не трогали): «Материалы»
# зовут из браузера человека, нажавшего кнопку в «Приёмной» (п. 9, `credentials: same-origin`,
# без общих секретов) — он и автор действия, из заголовков портала. В отличие от «Повестки»,
# своё совещание заводить не приходится (п. 9: карточку ведём мы сами и она уже есть) —
# только `meeting_or_404`. Разбор — тот же, что у карточки «Вставить документ»/`smartImport` (шаг 1): непустая
# повестка/список участников → список изменений через `agenda_changes.py`, иначе — предложение
# целиком. Ничего не применяется само: ответ несёт ссылку на страницу совещания с готовым
# предложением, человек подтверждает его там же, где обычный «Умный разбор».

_inbox_locks: dict[str, threading.Lock] = {}
_inbox_locks_guard = threading.Lock()


def _inbox_lock(fingerprint: str) -> threading.Lock:
    with _inbox_locks_guard:
        return _inbox_locks.setdefault(fingerprint, threading.Lock())


# Что «Искра» уже принимала файлом (проверено на боевом); остальное — Excel, старые .doc/.xls/.ppt,
# .rtf, .odt, .csv, веб-страница — уходит ей текстом, который читаем сами (`extract.extract_bytes`).
ISKRA_FILE_KINDS = ("docx", "pptx", "pdf", "txt", "md")


def _check_inbox_file(name: str, data: bytes) -> None:
    """В «Искру» уходят только документы — по содержимому файла, а не по расширению (тот же
    приём, что и у «Повестки», ревьюер RC-2026-09-27-1, Н1: иначе `virus.exe` уехал бы туда целиком).
    Принимаются Word, Excel, PowerPoint, PDF, RTF, ODT и текст (решение пользователя 05.10)."""
    reason = extract.problem(name, data)
    if reason:
        raise HTTPException(status_code=400, detail=reason)


def _for_iskra(name: str, data: bytes) -> tuple[bytes, str]:
    """Файл для «Искры»: знакомый ей формат — как есть, остальное — его текстом (.txt)."""
    if extract.kind_of(data, name) in ISKRA_FILE_KINDS:
        return data, name
    try:
        text = extract.extract_bytes(data, name)
    except Exception as e:      # не читается — понятная причина, а не 500
        raise HTTPException(status_code=400, detail=f"Не удалось прочитать «{name}»: {e}")
    if not (text or "").strip():
        raise HTTPException(status_code=400, detail=f"В файле «{name}» нет текста")
    return text.encode("utf-8"), f"{name}.txt"


def _inbox_summary(mode: str, doc_type: str, result: dict, changes: list[dict] | None) -> str:
    kind = {"повестка": "Повестка", "участники": "Список участников"}.get(doc_type, doc_type.capitalize())
    if mode == "changes":
        return f"{kind}: изменений {len(changes or [])}"
    data = result.get("data") or {}
    if doc_type == "повестка":
        return f"{kind}: вопросов {len(data.get('agenda') or [])}, участников {len(data.get('participants') or [])}"
    if doc_type == "участники":
        return f"{kind}: участников {len(data.get('participants') or [])}"
    return f"{kind}: разобрано"


def _inbox_answer(meeting_id: str, key: str, payload: dict) -> dict:
    return {"ok": True, "meeting_id": meeting_id, "mode": payload["mode"], "doc_type": payload["doc_type"],
            "summary": _inbox_summary(payload["mode"], payload["doc_type"], payload["result"], payload.get("changes")),
            "url": f"/moderator/#meeting={quote(meeting_id, safe='')}&proposal={key}"}


@router.post("/api/inbox")
def inbox(file: list[UploadFile] = File(...), meeting: str = Form(""), title: str = Form(""),
          date: str = Form(""), short_title: str = Form(""), folder: str = Form(""),
          hint: str = Form(""), sender: str = Form("")):
    """Документ из «Приёмной»: разобрать «Искрой» и показать предложение в совещании.

    `meeting`, `file` — обязательны (совещание у нас уже заведено, вторые заводить не нужно);
    `title`/`date`/`short_title`/`folder` в форме есть для совместимости контракта с «Повесткой»
    (её нашей карточке заводить не пришлось бы), но нами не используются. Ответ:
    `{ok, meeting_id, mode, doc_type, summary, url}` — `url` ведёт на страницу с готовым
    предложением. Ошибки — обычные `{detail}`: 400 нет файла/совещания или не тот тип,
    413 велик, 502 «Искра» не ответила. Тот же документ к тому же совещанию второй раз —
    прежнее предложение без нового расхода «Искры» (защита от двойного нажатия/вкладки).
    """
    meeting_id = (meeting or "").strip()
    if not meeting_id:
        raise HTTPException(status_code=400, detail="Не указано совещание, к которому относится документ")
    m = meeting_or_404(meeting_id)
    if len(file) != 1:
        raise HTTPException(status_code=400, detail="Присылайте по одному документу за вызов")
    upload = file[0]
    filename = upload.filename or ""
    data_bytes = read_upload(upload)
    if not data_bytes:
        raise HTTPException(status_code=400, detail=f"Файл «{filename or 'без имени'}» пустой")
    _check_inbox_file(filename, data_bytes)
    hint = (hint or "").strip()
    sender = (sender or "").strip()

    digest = hashlib.sha256()
    for part in (meeting_id, hint, filename):
        digest.update(part.encode("utf-8"))
        digest.update(b"\0")
    digest.update(data_bytes)
    fingerprint = digest.hexdigest()

    with _inbox_lock(fingerprint):
        found = storage.proposal_by_fingerprint(fingerprint)
        if found is not None:
            key, found_mid, payload = found
            if found_mid == meeting_id:
                portal.note_object(meeting_id)
                portal.note_change("повтор — отдано прежнее предложение, «Искра» не звалась")
                return _inbox_answer(meeting_id, key, payload)
        iskra_bytes, iskra_name = _for_iskra(filename, data_bytes)
        try:
            result = llm_extract.extract_with_file(iskra_bytes, iskra_name, hint)
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"Искра не ответила: {e}")
        payload = _with_diff(result, meeting_id)
        payload["mode"] = "changes" if payload.get("changes") is not None else "full"
        payload["doc_type"] = result.get("doc_type", "неизвестно")
        key = secrets.token_urlsafe(12)
        storage.remember_proposal(key, meeting_id, fingerprint, payload)
        portal.note_object(meeting_id)
        portal.note_change(f"из «Приёмной»: «{filename or 'документ'}»"
                           + (f", отправитель по данным «Материалов»: {sender}" if sender else "")
                           + f"; {_inbox_summary(payload['mode'], payload['doc_type'], result, payload.get('changes'))}")
        return _inbox_answer(meeting_id, key, payload)


@router.get("/api/meetings/{mid}/iskra/proposal/{key}")
def inbox_proposal(mid: str, key: str):
    """Предложение, составленное по документу из «Приёмной», — по ссылке из ответа `/api/inbox`."""
    meeting_or_404(mid)
    data = storage.proposal(key, mid)
    if data is None:
        raise HTTPException(status_code=404, detail="Предложение устарело или не найдено — пришлите документ заново")
    return data
