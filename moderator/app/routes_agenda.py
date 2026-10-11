"""Маршруты повестки, докладчиков и участников."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from . import llm_extract, people_flow, portal, readiness, storage
from .deps import STALE, at, check_base, meeting_or_404, pos, speaker_pos
from .models import AgendaItem, Participant, renumber_agenda
from .protocol import protocol_rank, sort_key
from .schemas import (AgendaCommandIn, AgendaIn, AgendaTitleIn, MinutesIn, MoveIn, ParticipantIn,
                     SpeakerAddIn)

router = APIRouter()


@router.post("/api/meetings/{mid}/agenda")
def add_agenda(mid: str, body: AgendaIn):
    m = meeting_or_404(mid)
    if not (body.title or "").strip():
        raise HTTPException(status_code=400, detail="Введите формулировку вопроса")
    m.agenda.append(AgendaItem(**body.model_dump()))
    llm_extract.sync_agenda_participants(m)
    storage.save(m)
    return {"meeting": m.to_dict(), "readiness": readiness.readiness(m)}


@router.put("/api/meetings/{mid}/agenda/{key}")
def set_agenda_title(mid: str, key: str, body: AgendaTitleIn):
    """Правка формулировки уже добавленного вопроса — саму позицию (номер, докладчиков) не трогает."""
    m = meeting_or_404(mid)
    item = at(m.agenda, key, "Пункт повестки")
    check_base(item, body.base, mid, "Формулировка вопроса")
    title = (body.title or "").strip()
    if not title:
        raise HTTPException(status_code=400, detail="Введите формулировку вопроса")
    item.title = title
    storage.save(m)
    return {"meeting": m.to_dict(), "readiness": readiness.readiness(m)}


@router.delete("/api/meetings/{mid}/agenda/{key}")
def remove_agenda(mid: str, key: str, cascade: bool = False):
    m = meeting_or_404(mid)
    idx = pos(m.agenda, key, "Пункт повестки")
    item = m.agenda[idx]
    num = item.num
    if cascade and num:
        m.materials = [x for x in m.materials if x.question_num != num]
        m.orders = [x for x in m.orders if x.agenda_num != num]
        m.scenario = [b for b in m.scenario if b.agenda_num != num]
    m.agenda.pop(idx)
    renumber_agenda(m)
    llm_extract.sync_agenda_participants(m)
    storage.save(m)
    return {"meeting": m.to_dict(), "readiness": readiness.readiness(m)}


@router.post("/api/meetings/{mid}/agenda/{key}/move")
def move_agenda(mid: str, key: str, body: MoveIn):
    m = meeting_or_404(mid)
    n = len(m.agenda)
    idx = pos(m.agenda, key, "Пункт повестки")
    target = idx - 1 if body.direction == "up" else idx + 1
    if 0 <= target < n:
        m.agenda[idx], m.agenda[target] = m.agenda[target], m.agenda[idx]
        renumber_agenda(m)   # номера идут по порядку, а поручения и материалы едут вместе со своим вопросом
        storage.save(m)
    return {"meeting": m.to_dict(), "readiness": readiness.readiness(m)}


@router.put("/api/meetings/{mid}/agenda/{key}/minutes")
def set_agenda_minutes(mid: str, key: str, body: MinutesIn):
    m = meeting_or_404(mid)
    item = at(m.agenda, key, "Пункт повестки")
    # минуты приходят списком «по позициям»: если состав докладчиков сменился, они лягут не тем
    if body.speakers is not None and body.speakers != item.speakers:
        raise HTTPException(status_code=409, detail=f"Состав докладчиков {STALE}")
    item.speaker_minutes = body.minutes
    storage.save(m)
    return {"meeting": m.to_dict(), "readiness": readiness.readiness(m)}


@router.put("/api/meetings/{mid}/agenda/{key}/item-minutes")
def set_agenda_item_minutes(mid: str, key: str, body: MinutesIn):
    """Минуты пункта без докладчиков (напр. «Обсуждение»)."""
    m = meeting_or_404(mid)
    item = at(m.agenda, key, "Пункт повестки")
    item.minutes = body.minutes[0] if body.minutes else 0
    storage.save(m)
    return {"meeting": m.to_dict(), "readiness": readiness.readiness(m)}


@router.post("/api/meetings/{mid}/agenda/{key}/speakers")
def add_agenda_speaker(mid: str, key: str, body: SpeakerAddIn):
    m = meeting_or_404(mid)
    item = at(m.agenda, key, "Пункт повестки")
    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Укажите ФИО")
    item.speakers.append(name)
    item.speaker_roles.append(body.role or "")
    item.speaker_minutes.append(int(body.minutes or 0))
    llm_extract.sync_agenda_participants(m)
    # докладчика вписали вручную (не выбрали из подсказки реестра на форме) — предложим то же
    # окно «Добавить в реестр?», что показывают разбор и импорт: в реестр пишет только человек
    # (../CLAUDE.md п. 5а), а тихо игнорировать нового/расходящегося докладчика нельзя
    role = next((p.role for p in m.participants if p.name == name), body.role or "")
    portal.note_person(name, role)
    people_flow.queue_noted(m, people_flow.document_label(m, "повестка"))
    # короткую запись («Кравцов»), которую реестр знает полностью, не добавлять, а заменить — как после
    # разбора (ревьюер RC-2026-10-02-2, Н-3); сверку перед Word человек увидит с «Заменить на …»
    people_flow.drop_known_from_pending(m)
    storage.save(m)
    return {"meeting": m.to_dict(), "readiness": readiness.readiness(m)}


@router.delete("/api/meetings/{mid}/agenda/{key}/speakers/{sidx}")
def remove_agenda_speaker(mid: str, key: str, sidx: int, name: str | None = None):
    m = meeting_or_404(mid)
    item = at(m.agenda, key, "Пункт повестки")
    speaker_pos(item, sidx, name)
    item.speakers.pop(sidx)
    if sidx < len(item.speaker_roles):
        item.speaker_roles.pop(sidx)
    if sidx < len(item.speaker_minutes):
        item.speaker_minutes.pop(sidx)
    llm_extract.sync_agenda_participants(m)
    storage.save(m)
    return {"meeting": m.to_dict(), "readiness": readiness.readiness(m)}


@router.post("/api/meetings/{mid}/agenda/{key}/speakers/{sidx}/move")
def move_agenda_speaker(mid: str, key: str, sidx: int, body: MoveIn, name: str | None = None):
    m = meeting_or_404(mid)
    item = at(m.agenda, key, "Пункт повестки")
    n = len(item.speakers)
    speaker_pos(item, sidx, name)
    target = sidx - 1 if body.direction == "up" else sidx + 1
    if 0 <= target < n:
        item.speakers[sidx], item.speakers[target] = item.speakers[target], item.speakers[sidx]
        if len(item.speaker_roles) == n:
            item.speaker_roles[sidx], item.speaker_roles[target] = item.speaker_roles[target], item.speaker_roles[sidx]
        if len(item.speaker_minutes) == n:
            item.speaker_minutes[sidx], item.speaker_minutes[target] = item.speaker_minutes[target], item.speaker_minutes[sidx]
        storage.save(m)
    return {"meeting": m.to_dict(), "readiness": readiness.readiness(m)}


# ── участники ───────────────────────────────────────────────────────────


@router.post("/api/meetings/{mid}/participants")
def add_participant(mid: str, body: ParticipantIn):
    m = meeting_or_404(mid)
    if not (body.name or "").strip():
        raise HTTPException(status_code=400, detail="Введите имя участника")
    data = body.model_dump()
    if not data.get("protocol_rank"):
        data["protocol_rank"] = protocol_rank(body.role)
    m.participants.append(Participant(**data))
    # по умолчанию список идёт по протокольному старшинству; если порядок задан
    # вручную (стрелки ↑/↓), новый участник просто встаёт в конец
    if not m.participants_manual:
        m.participants.sort(key=lambda p: sort_key(p.protocol_rank, p.name))
    # участника вписали вручную — предложим то же окно «Добавить в реестр?», что показывают
    # разбор и импорт (../CLAUDE.md п. 5а); если человека взяли из подсказки реестра, портал
    # ответит «same» и очередь не пополнится — окно не всплывёт зря
    portal.note_person(data.get("name", ""), data.get("role", ""), data.get("organization", ""))
    people_flow.queue_noted(m, people_flow.document_label(m, "участники"))
    people_flow.drop_known_from_pending(m)   # участник, он же докладчик коротко — заменять, не добавлять (Н-5)
    storage.save(m)
    return {"meeting": m.to_dict(), "readiness": readiness.readiness(m)}


@router.post("/api/meetings/{mid}/participants/sort")
def sort_participants(mid: str):
    """Отсортировать участников по протокольному старшинству."""
    m = meeting_or_404(mid)
    m.participants.sort(key=lambda p: sort_key(p.protocol_rank, p.name))
    m.participants_manual = False  # вернулись к протокольному порядку
    storage.save(m)
    return {"meeting": m.to_dict(), "readiness": readiness.readiness(m)}


@router.get("/api/meetings/{mid}/history")
def meeting_history(mid: str):
    """Журнал изменений совещания."""
    meeting_or_404(mid)
    return {"history": storage.get_history(mid)}


@router.delete("/api/meetings/{mid}/participants/{key}")
def remove_participant(mid: str, key: str, cascade: bool = False):
    m = meeting_or_404(mid)
    idx = pos(m.participants, key, "Участник")
    p = m.participants[idx]
    name = p.name
    if cascade:
        for a in m.agenda:
            keep = [i for i, sp in enumerate(a.speakers) if sp != name]
            a.speakers = [a.speakers[i] for i in keep]
            a.speaker_roles = [a.speaker_roles[i] for i in keep if i < len(a.speaker_roles)]
            a.speaker_minutes = [a.speaker_minutes[i] for i in keep if i < len(a.speaker_minutes)]
    m.participants.pop(idx)
    storage.save(m)
    return {"meeting": m.to_dict(), "readiness": readiness.readiness(m)}


@router.post("/api/meetings/{mid}/participants/{key}/move")
def move_participant(mid: str, key: str, body: MoveIn):
    m = meeting_or_404(mid)
    n = len(m.participants)
    idx = pos(m.participants, key, "Участник")
    target = idx - 1 if body.direction == "up" else idx + 1
    if 0 <= target < n:
        m.participants[idx], m.participants[target] = m.participants[target], m.participants[idx]
        m.participants_manual = True  # порядок задан вручную — не пересортировывать
        storage.save(m)
    return {"meeting": m.to_dict(), "readiness": readiness.readiness(m)}


@router.post("/api/meetings/{mid}/agenda/command")
def agenda_command(mid: str, body: AgendaCommandIn):
    """Команда на естественном языке → Искра добавляет вопрос в повестку."""
    m = meeting_or_404(mid)
    cmd = (body.command or "").strip()
    if not cmd:
        raise HTTPException(status_code=400, detail="Команда пуста")
    try:
        action = llm_extract.parse_agenda_command(llm_extract.agenda_context_text(m), cmd)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Искра не смогла выполнить команду: {e}")
    m = meeting_or_404(mid)  # перечитать после долгого ответа Искры
    try:
        item = llm_extract.apply_add_question(m, action)
    except Exception as e:
        raise HTTPException(status_code=422, detail=f"Не удалось применить команду: {e}")
    storage.save(m)
    who = ", ".join(item.speakers) if item.speakers else "без докладчика"
    msg = "Добавлен вопрос «" + item.title + "» (" + who + ")"
    return {"meeting": m.to_dict(), "readiness": readiness.readiness(m), "message": msg}


@router.post("/api/meetings/{mid}/participants/fill-roles")
def fill_roles(mid: str):
    """«Дозаполнить должности из реестра»: докладчикам без должности — из реестра портала.

    При открытии совещания в портал не ходим (SRE 21.09), поэтому если при добавлении
    докладчика реестр не ответил, должность дозаполняется этой кнопкой.
    """
    m = meeting_or_404(mid)
    empty = {p.name for p in m.participants if not p.role}
    llm_extract.sync_agenda_participants(m, fill_roles=True)
    filled = sum(1 for p in m.participants if p.name in empty and p.role)
    if filled:
        storage.save(m)
    return {"meeting": m.to_dict(), "readiness": readiness.readiness(m), "filled": filled}
