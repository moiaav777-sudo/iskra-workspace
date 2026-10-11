"""Точечная правка повестки и участников через «Искру» — список изменений с галочками.

Решение пользователя 28.09 (перенос из сервиса `agenda/app/changes.py`, который со временем
закроют, — читайте детали в `../CLAUDE.md` и `../TRACK.md` → «Модератор»): полная замена
(`llm_extract.apply_to_meeting(replace=True)`) при непустой повестке стирала материалы,
поручения и сценарий, привязанные к номерам вопросов, — реальный риск потери данных, если
Искрой правят уже заполненное совещание, а не только с нуля.

Портировано **не дословно**, а по образцу: у `agenda` докладчик — отдельный объект `Speaker`
со своим стабильным `id`; у «Модератора» докладчики лежат параллельными списками
(`AgendaItem.speakers`/`speaker_roles`/`speaker_minutes`) без собственного id — поэтому
внутри вопроса они адресуются именем (как уже делают `remove_agenda_speaker`/`speaker_pos`
в `deps.py`), а не отдельным ключом.

Принцип тот же: «Искра» по-прежнему возвращает состояние целиком (так ей проще и надёжнее),
но применяет его не сервис, а человек — сервер сравнивает с тем, что есть сейчас, и предлагает
список именованных действий («добавить вопрос», «заменить докладчика», «убрать участника Х»);
удаления по умолчанию **не отмечены** — Искра могла просто не упомянуть пункт, а не имела в
виду его убрать. Каждое действие помнит, что было на момент предложения (`was`), и вносится,
только если это всё ещё так (`still_valid`) — иначе чужая или более поздняя правка не затирается.

**Без счётчика `rev`, в отличие от `agenda`.** Там `rev` — короткий путь «ничего не менялось,
можно не гадать»; здесь у совещания и так нет глобальной версии (правки идут по стабильным id
и `base`-сверке, `deps.check_base`), а `still_valid` по каждой операции сам по себе точен —
отдельного счётчика заводить не стал: 409 наступает ровно тогда, когда что-то из отмеченного
на самом деле устарело, а не при любой чужой правке совещания.

**Убрать вопрос — тоже по слепку, не только по формулировке** (ревьюер RC-2026-09-28-14, К1).
Каскад удаляет материалы, поручения и блоки сценария, привязанные к номеру вопроса, — если между
предложением и нажатием коллега положил к этому вопросу материал (например, «Бриф» прислал
саммари доклада), формулировка вопроса не изменится, а каскад молча унёс бы и его. Поэтому
`diff_agenda` запоминает в `was_materials`/`was_orders`/`was_scenario` id того, что привязано
к вопросу на момент предложения, а `still_valid` сверяет их с тем, что привязано сейчас.
"""
from __future__ import annotations

from .models import AgendaItem, Meeting, Participant
from .protocol import protocol_rank

# Шапку (дата, председатель, место…) в список изменений не включаем: `apply_to_meeting` и так
# заполняет её только когда поле пусто (`_fillable`) — там нечего терять, «Заменить целиком»
# не требовался и остаётся отдельным путём («Дополнить»/шапка правится формой совещания).


def _norm(value: str) -> str:
    return " ".join((value or "").strip().lower().replace("ё", "е").split())


def _attendance(v: str) -> str:
    v = (v or "").strip().lower()
    if "вкс" in v or "видео" in v or "удал" in v:
        return "remote"
    if "очн" in v:
        return "in_person"
    return "unknown"


def _short(name: str) -> str:
    """«Иванов Иван Иванович» → «Иванов И.И.» — короче в списке изменений."""
    parts = (name or "").split()
    if len(parts) < 2:
        return name or ""
    return parts[0] + " " + "".join(p[0] + "." for p in parts[1:3])


def is_empty_agenda(m: Meeting) -> bool:
    return not m.agenda


def is_empty_participants(m: Meeting) -> bool:
    return not m.participants


# ── сравнение: повестка ─────────────────────────────────────────────────

def diff_agenda(m: Meeting, data: dict) -> list[dict]:
    """Изменения, которые превратят повестку в предложенную «Искрой». Ничего не меняет."""
    ops: list[dict] = []

    def op(kind: str, label: str, checked: bool = True, **fields) -> None:
        ops.append({"id": f"op{len(ops) + 1}", "kind": kind, "label": label, "checked": checked, **fields})

    current = {_norm(i.title): i for i in m.agenda if i.title.strip()}
    wanted = set()
    for pos, item in enumerate(data.get("agenda") or [], 1):
        title = item.get("title", "")
        key = _norm(title)
        if not key:
            continue
        wanted.add(key)
        speakers = [s for s in (item.get("speakers") or []) if s]
        roles = list(item.get("speaker_roles") or [])
        minutes = [int(x or 0) for x in (item.get("speaker_minutes") or [])]
        spk = [{"name": speakers[i], "role": roles[i] if i < len(roles) else "",
               "minutes": minutes[i] if i < len(minutes) else 0} for i in range(len(speakers))]
        if key not in current:
            who = ", ".join(_short(s["name"]) for s in spk)
            op("add_question", f"Добавить вопрос: «{title}»" + (f" — {who}" if who else ""),
               title=title, position=pos, speakers=spk)
            continue
        ops.extend(_speaker_ops(current[key], spk, start=len(ops)))
    for key, existing in current.items():
        if key in wanted:
            continue
        mat_ids = [x.id for x in m.materials if existing.num and x.question_num == existing.num]
        order_ids = [x.id for x in m.orders if existing.num and x.agenda_num == existing.num]
        scenario_ids = [b.id for b in m.scenario if existing.num and b.agenda_num == existing.num]
        parts = []
        if mat_ids:
            parts.append(f"материалами: {len(mat_ids)}")
        if order_ids:
            parts.append(f"поручениями: {len(order_ids)}")
        if scenario_ids:
            parts.append(f"сценарием: {len(scenario_ids)}")
        extra = f" (вместе с {', '.join(parts)})" if parts else ""
        # запоминаем не только формулировку, но и id привязанных материалов/поручений/блоков
        # сценария на момент предложения — иначе коллега, положивший материал к этому вопросу,
        # пока человек разбирал список, узнал бы об этом только постфактум (ревьюер RC-2026-09-28-14, К1)
        op("remove_question", f"Убрать вопрос {existing.num or '·'}: «{existing.title}»{extra}",
           checked=False, item_id=existing.id, was=existing.title,
           was_materials=mat_ids, was_orders=order_ids, was_scenario=scenario_ids)
    return ops


def _role_of(item: AgendaItem, name: str) -> str:
    for i, s in enumerate(item.speakers):
        if s == name:
            return item.speaker_roles[i] if i < len(item.speaker_roles) else ""
    return ""


def _minutes_of(item: AgendaItem, name: str) -> int:
    for i, s in enumerate(item.speakers):
        if s == name:
            return item.speaker_minutes[i] if i < len(item.speaker_minutes) else 0
    return 0


def _speaker_ops(item: AgendaItem, wanted: list[dict], start: int) -> list[dict]:
    ops: list[dict] = []

    def op(kind: str, label: str, checked: bool = True, **fields) -> None:
        ops.append({"id": f"op{start + len(ops) + 1}", "kind": kind, "label": label, "checked": checked,
                    "item_id": item.id, **fields})

    want = {}
    for s in wanted:
        key = _norm(s.get("name", ""))
        if key:
            want[key] = s
    have_keys = {_norm(s) for s in item.speakers}
    gone = [s for s in item.speakers if _norm(s) not in want]
    new = [s for k, s in want.items() if k not in have_keys]
    label = f"Вопрос {item.num or '·'}"
    # один ушёл, один пришёл — это замена докладчика, а не два отдельных действия
    pairs = min(len(gone), len(new))
    for i in range(pairs):
        g_name, s_new = gone[i], new[i]
        op("replace_speaker", f"{label}: заменить {_short(g_name)} на {_short(s_new['name'])}",
           was={"name": g_name, "role": _role_of(item, g_name)},
           to={"name": s_new["name"], "role": s_new.get("role", ""), "minutes": int(s_new.get("minutes") or 0)})
    for s_new in new[pairs:]:
        op("add_speaker", f"{label}: добавить {_short(s_new['name'])}" +
           (f", {s_new['role']}" if s_new.get("role") else ""),
           speaker={"name": s_new["name"], "role": s_new.get("role", ""), "minutes": int(s_new.get("minutes") or 0)})
    for g_name in gone[pairs:]:
        op("remove_speaker", f"{label}: убрать {_short(g_name)}", checked=False,
           was={"name": g_name, "role": _role_of(item, g_name)})
    for name in item.speakers:
        key = _norm(name)
        w = want.get(key)
        if not w or _norm(name) not in have_keys:
            continue
        cur_role = _role_of(item, name)
        if w.get("role") and w["role"] != cur_role:
            op("speaker_role", f"{label}: должность {_short(name)} — «{w['role']}»" if not cur_role
               else f"{label}: должность {_short(name)} «{cur_role}» → «{w['role']}»",
               checked=not cur_role, name=name, was=cur_role, to=w["role"])
        cur_minutes = _minutes_of(item, name)
        if w.get("minutes") and int(w["minutes"]) != cur_minutes:
            op("speaker_minutes", f"{label}: регламент {_short(name)} — {int(w['minutes'])} мин",
               name=name, was=cur_minutes, to=int(w["minutes"]))
    return ops


# ── сравнение: участники ────────────────────────────────────────────────

def diff_participants(m: Meeting, data: dict) -> list[dict]:
    """Изменения, которые превратят список участников в предложенный. Ничего не меняет.

    Докладчиков ведёт повестка (`sync_agenda_participants` держит их в участниках сама) —
    здесь трогаются только участники, добавленные не докладчиками (`is_speaker=False`),
    как и в исходном `agenda/app/changes.py` (там это флаг `from_agenda`).
    """
    ops: list[dict] = []

    def op(kind: str, label: str, checked: bool = True, **fields) -> None:
        ops.append({"id": f"op{len(ops) + 1}", "kind": kind, "label": label, "checked": checked, **fields})

    mine = {_norm(p.name): p for p in m.participants}
    named = set()
    for person in data.get("participants") or []:
        name = person.get("name", "")
        key = _norm(name)
        if not key:
            continue
        named.add(key)
        role = person.get("role", "")
        org = person.get("organization", "")
        att = _attendance(person.get("attendance", ""))
        present = mine.get(key)
        if present is None:
            op("add_participant", f"Добавить участника: {_short(name)}" + (f", {role}" if role else ""),
               person={"name": name, "role": role, "organization": org, "attendance": att})
            continue
        if role and role != present.role:
            op("participant_role", (f"Участник {_short(present.name)}: должность «{role}»" if not present.role
                                    else f"Участник {_short(present.name)}: должность «{present.role}» → «{role}»"),
               checked=not present.role, pid=present.id, name=present.name, was=present.role, to=role)
        if org and org != present.organization:
            op("participant_org", (f"Участник {_short(present.name)}: ведомство «{org}»" if not present.organization
                                    else f"Участник {_short(present.name)}: ведомство «{present.organization}» → «{org}»"),
               checked=not present.organization, pid=present.id, name=present.name, was=present.organization, to=org)
        if att != "unknown" and att != present.attendance:
            op("participant_attendance", f"Участник {_short(present.name)}: форма участия — "
               + ("очно" if att == "in_person" else "по видеосвязи"),
               pid=present.id, name=present.name, was=present.attendance, to=att)
    for key, p in mine.items():
        if not p.is_speaker and key not in named:
            op("remove_participant", f"Убрать участника: {_short(p.name)}", checked=False,
               pid=p.id, name=p.name, was=p.role)
    return ops


# ── внесение отмеченного ─────────────────────────────────────────────────

def _item(m: Meeting, item_id: str) -> AgendaItem | None:
    return next((i for i in m.agenda if i.id == item_id), None)


def _participant(m: Meeting, pid: str) -> Participant | None:
    return next((p for p in m.participants if p.id == pid), None)


def _has_speaker(item: AgendaItem, name: str) -> bool:
    return any(s == name for s in item.speakers)


KINDS = {"add_question", "remove_question", "add_speaker", "replace_speaker", "remove_speaker",
         "speaker_role", "speaker_minutes", "add_participant", "remove_participant",
         "participant_role", "participant_org", "participant_attendance"}


def still_valid(m: Meeting, op: dict) -> bool:
    """То, что было на момент предложения, всё ещё так? Иначе изменение затёрло бы чужую правку.

    Изменение приходит из браузера, поэтому кривое (не тот вид, нет полей) — тоже «не годится».
    """
    try:
        return _still_valid(m, op)
    except (KeyError, TypeError, AttributeError, ValueError):
        return False


def _still_valid(m: Meeting, op: dict) -> bool:
    kind = op.get("kind")
    if kind not in KINDS:
        return False
    if kind == "add_question":
        return all(_norm(i.title) != _norm(op["title"]) for i in m.agenda)
    if kind == "remove_question":
        it = _item(m, op["item_id"])
        if it is None or it.title != op["was"]:
            return False
        # формулировка совпадает, но за это время к вопросу могли добавить материал, поручение
        # или блок сценария (например, «Бриф» прислал саммари доклада) — каскад удалил бы их
        # молча, поэтому это тоже устаревшая операция, а не только смена title/удаление вопроса
        if it.num:
            cur_materials = {x.id for x in m.materials if x.question_num == it.num}
            cur_orders = {x.id for x in m.orders if x.agenda_num == it.num}
            cur_scenario = {b.id for b in m.scenario if b.agenda_num == it.num}
        else:
            cur_materials = cur_orders = cur_scenario = set()
        return (cur_materials == set(op.get("was_materials") or [])
                and cur_orders == set(op.get("was_orders") or [])
                and cur_scenario == set(op.get("was_scenario") or []))
    if kind == "add_speaker":
        it = _item(m, op["item_id"])
        return it is not None and not _has_speaker(it, op["speaker"]["name"])
    if kind in ("replace_speaker", "remove_speaker"):
        it = _item(m, op["item_id"])
        return it is not None and _has_speaker(it, op["was"]["name"])
    if kind == "speaker_role":
        it = _item(m, op["item_id"])
        return it is not None and _has_speaker(it, op["name"]) and _role_of(it, op["name"]) == op["was"]
    if kind == "speaker_minutes":
        it = _item(m, op["item_id"])
        return it is not None and _has_speaker(it, op["name"]) and _minutes_of(it, op["name"]) == op["was"]
    if kind == "add_participant":
        return all(_norm(p.name) != _norm(op["person"]["name"]) for p in m.participants)
    if kind == "remove_participant":
        p = _participant(m, op["pid"])
        return p is not None and p.name == op["name"]
    if kind == "participant_role":
        p = _participant(m, op["pid"])
        return p is not None and p.role == op["was"]
    if kind == "participant_org":
        p = _participant(m, op["pid"])
        return p is not None and p.organization == op["was"]
    if kind == "participant_attendance":
        p = _participant(m, op["pid"])
        return p is not None and p.attendance == op["was"]
    return False


def apply(m: Meeting, op: dict) -> tuple[str, list[tuple[str, str, str]]]:
    """Внести одно изменение. Возвращает (описание для журнала, люди для окна реестра).

    Номера вопросов и порядок пересчитывает вызывающий (`renumber_agenda`) один раз после
    всех изменений — не после каждого, иначе следующие `item_id`/номера в этом же пакете
    могли бы устареть от нашей же собственной правки.
    """
    kind = op["kind"]
    people: list[tuple[str, str, str]] = []
    if kind == "add_question":
        item = AgendaItem(title=op["title"])
        for s in op.get("speakers") or []:
            item.speakers.append(s["name"])
            item.speaker_roles.append(s.get("role", ""))
            minutes = int(s.get("minutes") or 0)
            item.speaker_minutes.append(minutes if minutes > 0 else 5)
            people.append((s["name"], s.get("role", ""), ""))
        pos = int(op.get("position") or 0)
        if 1 <= pos <= len(m.agenda):
            m.agenda.insert(pos - 1, item)
        else:
            m.agenda.append(item)
        return f"добавлен вопрос «{item.title}»", people
    if kind == "remove_question":
        item = _item(m, op["item_id"])
        num = item.num
        # производные данные привязаны к номеру вопроса — без переноса на новый (его больше
        # нет) они молча остались бы висеть на чужом вопросе после пересчёта номеров
        before = (len(m.materials), len(m.orders), len(m.scenario))
        if num:
            m.materials = [x for x in m.materials if x.question_num != num]
            m.orders = [x for x in m.orders if x.agenda_num != num]
            m.scenario = [b for b in m.scenario if b.agenda_num != num]
        m.agenda.remove(item)
        removed = (before[0] - len(m.materials), before[1] - len(m.orders), before[2] - len(m.scenario))
        parts = []
        if removed[0]:
            parts.append(f"материалов: {removed[0]}")
        if removed[1]:
            parts.append(f"поручений: {removed[1]}")
        if removed[2]:
            parts.append(f"блоков сценария: {removed[2]}")
        extra = f" (и {', '.join(parts)})" if parts else ""
        return f"убран вопрос «{item.title}»{extra}", people
    if kind == "add_speaker":
        item = _item(m, op["item_id"])
        s = op["speaker"]
        item.speakers.append(s["name"])
        item.speaker_roles.append(s.get("role", ""))
        item.speaker_minutes.append(int(s.get("minutes") or 0))
        people.append((s["name"], s.get("role", ""), ""))
        return f"вопрос «{item.title}»: выступающий «{s['name']}»", people
    if kind == "replace_speaker":
        item = _item(m, op["item_id"])
        idx = next(i for i, nm in enumerate(item.speakers) if nm == op["was"]["name"])
        was = item.speakers[idx]
        item.speakers[idx] = op["to"]["name"]
        while len(item.speaker_roles) <= idx:
            item.speaker_roles.append("")
        while len(item.speaker_minutes) <= idx:
            item.speaker_minutes.append(0)
        if op["to"].get("role"):
            item.speaker_roles[idx] = op["to"]["role"]
        if op["to"].get("minutes"):
            item.speaker_minutes[idx] = int(op["to"]["minutes"])
        people.append((item.speakers[idx], item.speaker_roles[idx], ""))
        return f"вопрос «{item.title}»: «{was}» → «{item.speakers[idx]}»", people
    if kind == "remove_speaker":
        item = _item(m, op["item_id"])
        idx = next(i for i, nm in enumerate(item.speakers) if nm == op["was"]["name"])
        name = item.speakers.pop(idx)
        if idx < len(item.speaker_roles):
            item.speaker_roles.pop(idx)
        if idx < len(item.speaker_minutes):
            item.speaker_minutes.pop(idx)
        return f"вопрос «{item.title}»: убран «{name}»", people
    if kind == "speaker_role":
        item = _item(m, op["item_id"])
        idx = next(i for i, nm in enumerate(item.speakers) if nm == op["name"])
        while len(item.speaker_roles) <= idx:
            item.speaker_roles.append("")
        was, item.speaker_roles[idx] = item.speaker_roles[idx], op["to"]
        people.append((op["name"], op["to"], ""))
        return f"«{op['name']}»: должность «{was}» → «{op['to']}»", people
    if kind == "speaker_minutes":
        item = _item(m, op["item_id"])
        idx = next(i for i, nm in enumerate(item.speakers) if nm == op["name"])
        while len(item.speaker_minutes) <= idx:
            item.speaker_minutes.append(0)
        was, item.speaker_minutes[idx] = item.speaker_minutes[idx], int(op["to"])
        return f"«{op['name']}»: регламент {was} → {item.speaker_minutes[idx]}", people
    if kind == "add_participant":
        p = op["person"]
        role = p.get("role", "")
        m.participants.append(Participant(name=p["name"], role=role, organization=p.get("organization", ""),
                                          attendance=p.get("attendance") or "unknown",
                                          protocol_rank=protocol_rank(role)))
        people.append((p["name"], role, p.get("organization", "")))
        return f"добавлен участник «{p['name']}»", people
    if kind == "remove_participant":
        p = _participant(m, op["pid"])
        m.participants.remove(p)
        return f"убран участник «{p.name}»", people
    if kind == "participant_role":
        p = _participant(m, op["pid"])
        was, p.role = p.role, op["to"]
        p.protocol_rank = protocol_rank(p.role)
        people.append((p.name, p.role, p.organization))
        return f"участник «{p.name}»: должность «{was}» → «{p.role}»", people
    if kind == "participant_org":
        p = _participant(m, op["pid"])
        was, p.organization = p.organization, op["to"]
        people.append((p.name, p.role, p.organization))
        return f"участник «{p.name}»: ведомство «{was}» → «{p.organization}»", people
    if kind == "participant_attendance":
        p = _participant(m, op["pid"])
        was, p.attendance = p.attendance, op["to"]
        return f"участник «{p.name}»: форма участия {was} → {p.attendance}", people
    raise ValueError(f"неизвестное изменение «{kind}»")
