"""Люди в совещании и общий реестр портала (../CLAUDE.md п. 5а, решения пользователя 21.09).

Два правила:

- **В реестр пишет только человек.** Разбор и импорт реестр не меняют: сервис спрашивает
  портал (`force: false` → new | differs | same) и ставит новых и записанных иначе в очередь
  карточки `registry_pending`. Интерфейс показывает окно «Добавить в реестр?», а «Добавить»
  уходит из браузера под вошедшим человеком (`POST /api/people` портала).
- **Сверка ФИО перед выводом.** Всё, что уходит в документ ведущего, сверяется с реестром:
  `close` (опечатка — «Кадошников» при записанном «Кадочникове») документ не выпускает,
  `unknown` — предупреждение и предложение добавить.
"""
from __future__ import annotations

import threading
import time

from . import portal
from .models import Meeting, _is_closing, _is_opening

DOC_KINDS = {"участники": "Список участников", "повестка": "Повестка", "поручения": "Поручения",
             "модератор": "Модератор", "материал": "Материал"}


def document_label(m: Meeting, kind: str) -> str:
    """«Список участников к совещанию «Обеспечение жильем детей-сирот» от 14.10.2026» — из какого документа человек."""
    label = DOC_KINDS.get((kind or "").lower(), kind or "Документ")
    topic = m.short_title or m.title
    return (f"{label} к совещанию «{topic}»" if topic else label) + (f" от {m.date}" if m.date else "")


SUGGEST_BUDGET = 3.0     # с. на все вопросы к реестру за один разбор или импорт


def _ask(person: dict, document: str, login: str) -> dict | None:
    """Один человек: что о нём думает реестр. Работает в потоке — контекста запроса тут нет."""
    suggest = portal.people_suggest({**person, "document": document}, login=login)
    if suggest not in ("new", "differs"):
        return None
    card = portal.people_get(person["name"]) if suggest == "differs" else None
    return {**person, "suggest": suggest, "document": document,
            "registry_role": (card or {}).get("role", ""),
            "registry_org": (card or {}).get("organization", "")}


def queue_noted(m: Meeting, document: str) -> int:
    """Людей, которых отметил разбор или импорт в этом запросе, — в очередь окна, если портал
    говорит «новый» или «записан иначе». Возвращает, сколько добавилось.

    Вопросы к реестру идут параллельно и с общим бюджетом ~3 с (SRE 21.09): список на 20 человек
    не должен ждать 20 ответов по очереди. Не успели — человек в очередь не попадёт сейчас,
    спросим при следующем разборе; работе это не мешает.
    """
    people = portal.noted_people()
    if not people:
        return 0
    login = portal.current_login()
    by_name = {person["name"]: person for person in people}
    answers = portal.people_parallel(lambda name: _ask(by_name[name], document, login), list(by_name),
                                     budget=SUGGEST_BUDGET, what="«Добавить в реестр?»")
    added = 0
    waiting = {p.get("name", "").lower(): p for p in m.registry_pending}
    for name in by_name:
        item = answers.get(name)
        if item and waiting.get(item["name"].lower()) != item:
            waiting[item["name"].lower()] = item
            added += 1
    if added:
        m.registry_pending = list(waiting.values())
        portal.note_change(f"ждут решения о реестре людей: {len(m.registry_pending)}")
    return added


def drop_known_from_pending(m: Meeting) -> int:
    """Из очереди «Добавить в реестр?» убрать тех, кого реестр знает в полной записи (`close`): разбор
    пишет докладчика коротко («Кравцов»), портал на короткую запись отвечает «новый», а сверка — «похож
    на Кравцова С.С.». Таких людей не добавляют, а заменяют (окно «Докладчики и реестр людей»), иначе
    два окна противоречат друг другу, и из первого в реестр уезжает короткая запись (ревьюер
    RC-2026-10-02-1, Н1). Сверяем ровно тот набор, что и `check_agenda` (`agenda_names`): интерфейс
    следом спросит то же, и ответ возьмётся из кэша `check_names` (30 с), а не пойдёт в портал второй раз
    (ревьюер RC-2026-10-02-2, Н-5). Реестр не ответил — очередь не трогаем. Возвращает, сколько убрано."""
    pending = {p.get("name", "") for p in m.registry_pending}
    names = agenda_names(m)
    if not pending or not any(n in pending for n in names):
        return 0
    res = check_names(m.id, names)
    if res.get("unavailable"):
        return 0
    known = {x["name"] for x in res["items"] if x.get("status") == "close"}
    before = len(m.registry_pending)
    m.registry_pending = [p for p in m.registry_pending if p.get("name", "") not in known]
    return before - len(m.registry_pending)


def resolve_pending(m: Meeting, name: str) -> bool:
    """Человек решил — «Добавить» или «Не добавлять»: из очереди убираем."""
    key = " ".join((name or "").split()).lower()
    before = len(m.registry_pending)
    m.registry_pending = [p for p in m.registry_pending if p.get("name", "").lower() != key]
    return len(m.registry_pending) != before


def _dedup(names: list[str]) -> list[str]:
    seen, out = set(), []
    for n in names:
        n = " ".join((n or "").split())
        if n and n.lower() not in seen:
            seen.add(n.lower())
            out.append(n)
    return out


def document_names(m: Meeting) -> list[str]:
    """ФИО, которые уходят в документ ведущего, в именительном — как они хранятся."""
    names = [b.speaker for b in m.scenario if b.kind in ("intro", "report")]
    names += [sp for a in m.agenda if a.num.strip() for sp in a.speakers]
    return _dedup(names)


def agenda_names(m: Meeting) -> list[str]:
    """ФИО, которые уходят в документ «Повестка» — ровно те, что печатает `agenda_docx.build_agenda`.

    Раньше сверялись только докладчики вопросов с номером (`a.num.strip()`) — Word-экспорт
    при этом печатает ещё двоих: председателя строкой «Вступительного слова» и докладчиков
    вопроса, добавленного вручную и ещё не перенумерованного (сохраняется с `num=""`).
    Несверенное ФИО в таком месте уходило в подписанный как сверенный документ (ревьюер
    RC-2026-09-28-14, К2) — здесь собираем то же самое множество, что и `build_agenda`:
    председателя (когда печатается вступительное слово) и докладчиков всех вопросов, кроме
    вступительного слова и подведения итогов, независимо от номера.
    """
    opening = next((a for a in m.agenda if _is_opening(a.title)), None)
    names = [m.chair] if opening is not None and (m.chair or m.chair_role) else []
    names += [sp for a in m.agenda if not _is_opening(a.title) and not _is_closing(a.title)
              for sp in a.speakers]
    return _dedup(names)


def participant_names(m: Meeting) -> list[str]:
    """ФИО, которые уходят в документ «Список участников»."""
    return _dedup([p.name for p in m.participants])


CHECK_TTL = 30.0         # с. — интерфейс сверил, через секунду export.docx не сверяет второй раз
_checked: dict[tuple, tuple[float, dict]] = {}
_checked_lock = threading.Lock()


def forget_checks(mid: str) -> None:
    """Сбросить запомненную сверку совещания: заменили ФИО или человек добавил его в реестр."""
    with _checked_lock:
        for k in [k for k in _checked if k[0] == mid]:
            _checked.pop(k, None)


def check_names(mid: str, names: list[str]) -> dict:
    """Сверка произвольного набора ФИО с реестром. `unavailable` — портал не ответил.

    Результат держим 30 с по тому же набору ФИО (и по документу — ключ включает `mid`):
    сверку сначала просит интерфейс, а через секунду — выгрузка. Поменяли ФИО — набор
    другой, сверка новая.
    """
    key = (mid, tuple(n.lower() for n in names))
    now = time.monotonic()
    with _checked_lock:
        hit = _checked.get(key)
        if hit and now - hit[0] < CHECK_TTL:
            return hit[1]
    items = portal.people_check(names)
    if items is None:
        return {"items": [], "unavailable": True}      # молчание портала не запоминаем
    result = {"items": items, "unavailable": False}
    with _checked_lock:
        for k in [k for k, v in _checked.items() if now - v[0] >= CHECK_TTL]:
            _checked.pop(k, None)
        _checked[key] = (now, result)
    return result


def check(m: Meeting) -> dict:
    """Сверка ФИО документа ведущего с реестром — обёртка над `check_names`."""
    return check_names(m.id, document_names(m))


def check_agenda(m: Meeting) -> dict:
    """Сверка ФИО докладчиков документа «Повестка» с реестром."""
    return check_names(m.id, agenda_names(m))


def check_participants(m: Meeting) -> dict:
    """Сверка ФИО документа «Список участников» с реестром."""
    return check_names(m.id, participant_names(m))


def orders_names(m: Meeting) -> list[str]:
    """ФИО, которые уходят в «Перечень поручений» отдельной строкой, — подпись председателя.
    Исполнители стоят внутри текста поручений в дательном («(С.С. Кравцову)»): до склонения их не сверить,
    текст поручения человек правит сам."""
    return _dedup([m.chair])


def check_orders(m: Meeting) -> dict:
    """Сверка ФИО «Перечня поручений» (подпись председателя) с реестром."""
    names = orders_names(m)
    if not names:
        return {"items": [], "unavailable": False}     # сверять нечего — реестр не зовём
    return check_names(m.id, names)


def replace_name(m: Meeting, old: str, new: str, role: str = "") -> int:
    """Заменить ФИО во всём совещании одним нажатием: повестка, участники, сценарий, материалы.

    Фразы сценария, собранные по шаблону («прошу доложить …», «Доклад …»), склоняются
    заново; фамилия в остальном тексте заменяется по основе («Кадошников…» → «Кадочников…»).
    `role` — должность из реестра (просьба пользователя 02.10: разбор пишет докладчика коротко,
    «Кравцов», а в реестре он есть полностью и с должностью): встаёт докладчику вопроса и участнику
    там, где должность ещё пуста; заполненную руками не трогаем. `old == new` с `role` — только это.
    """
    old, new = " ".join((old or "").split()), " ".join((new or "").split())
    role = " ".join((role or "").split())
    if not old or not new or (old == new and not role):
        return 0
    low = old.lower()
    same = old == new
    count = 0
    for a in m.agenda:
        for i, sp in enumerate(a.speakers):
            if sp.strip().lower() == low:
                if not same:
                    a.speakers[i] = new
                    count += 1
                if role:
                    # дополняем до числа докладчиков, не до индекса: перестановка ↑/↓ двигает должности
                    # только при полной длине списка (routes_agenda), иначе должность уедет к соседу
                    while len(a.speaker_roles) < len(a.speakers):
                        a.speaker_roles.append("")
                    if not a.speaker_roles[i]:
                        a.speaker_roles[i] = role
                        count += 1
    for p in m.participants:
        if p.name.strip().lower() == low:
            if not same:
                p.name = new
                count += 1
            if role and not p.role:
                p.role = role
                count += 1
    # замена из реестра закрывает и вопрос очереди «Добавить в реестр?» по старому имени: иначе окно
    # с коротким «Кравцов — нет в реестре» остаётся и может записать в реестр короткую запись (ревьюер
    # RC-2026-10-02-1, Н1)
    if not same:
        resolve_pending(m, old)
    if same:
        return count
    for b in m.scenario:
        if b.speaker.strip().lower() == low:
            b.speaker = new
            count += 1
    for mat in m.materials:
        if mat.speaker.strip().lower() == low:
            mat.speaker = new
            count += 1
    if m.chair.strip().lower() == low:
        m.chair = new
        count += 1
    # фамилия в свободном тексте: по основе, тогда покрыты все падежи («Кадошникова», «Кадошниковым»)
    old_stem, new_stem = _stem(old.split()[0]), _stem(new.split()[0])
    if old_stem and new_stem and old_stem != new_stem:
        for b in m.scenario:
            if old_stem in (b.text or ""):
                b.text = b.text.replace(old_stem, new_stem)
                count += 1
    count += m.refresh_speaker_phrases()
    return count


def _stem(surname: str) -> str:
    """Основа фамилии без конечной гласной: «Петрова» → «Петров» (тогда найдётся и «Петровой»).

    Короче четырёх букв не берём: «Ким» внутри чужих слов встречается слишком часто.
    """
    s = (surname or "").strip()
    if s[-1:].lower() in ("а", "я", "й", "ь"):
        s = s[:-1]
    return s if len(s) >= 4 else ""
