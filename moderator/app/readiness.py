"""Расчёт готовности совещания («модератор готов на N%»).

Готовность — это полнота данных: именованный чек-лист обязательных элементов,
каждый со своим весом. Процент = сумма весов выполненных пунктов.
API отдаёт не только число, но и список «чего именно не хватает».
"""
from __future__ import annotations

from .models import (
    Meeting,
    SECTION_CLOSING,
    SECTION_OPENING,
    SECTION_QUESTION,
    _is_discussion,
)


_SECTION = {
    "header_title": "agenda", "header_date": "agenda", "header_chair": "agenda",
    "agenda_any": "agenda", "agenda_time": "agenda", "agenda_speaker": "agenda",
    "participants_any": "participants", "participants_full": "participants",
    "orders_any": "orders",
    "scenario_opening": "scenario", "scenario_intro": "scenario", "scenario_closing": "scenario",
    "glossary_any": "glossary",
    "materials_any": "materials",
}


def readiness(meeting: Meeting) -> dict:
    """Вернуть {percent, done_count, total_count, items: [{id,label,weight,done,section}]}."""
    items = _checks(meeting)
    for i in items:
        i["section"] = i.get("section") or _SECTION.get(i["id"], "")
    total = sum(i["weight"] for i in items)
    done_weight = sum(i["weight"] for i in items if i["done"])
    percent = round(done_weight * 100 / total) if total else 0
    return {
        "percent": percent,
        "done_count": sum(1 for i in items if i["done"]),
        "total_count": len(items),
        "items": items,
    }


def _checks(m: Meeting) -> list[dict]:
    has_agenda = len(m.agenda) > 0
    # только пронумерованные вопросы (без вступительного/подведения итогов)
    questions = [a for a in m.agenda if a.num.strip()]
    # вопросы, где нужен докладчик (без «Обсуждения»)
    report_questions = [a for a in questions if not _is_discussion(a.title)]
    agenda_all_time = bool(questions) and all(a.time.strip() for a in questions)
    agenda_all_speakers = bool(report_questions) and all(a.speakers for a in report_questions)
    has_participants = len(m.participants) > 0
    participants_full = has_participants and all(
        p.role.strip() and p.organization.strip() for p in m.participants
    )
    # подводка нужна вопросам со 2-го (кроме «Обсуждения»): блок kind="reference"
    first_num = questions[0].num if questions else None
    intro_needed = {a.num for a in report_questions if a.num != first_num}
    ref_nums = {
        b.agenda_num for b in m.scenario
        if b.section == SECTION_QUESTION and b.kind == "reference" and b.agenda_num
    }
    intro_done = bool(questions) and intro_needed.issubset(ref_nums)

    items = [
        _item("header_title", "Указано название совещания", 5, bool(m.title.strip())),
        _item("header_date", "Указана дата", 5, bool(m.date.strip())),
        _item("header_chair", "Указан председатель", 5, bool(m.chair.strip())),
        _item("agenda_any", "В повестке есть хотя бы один вопрос", 10, has_agenda),
        _item("agenda_time", "У каждого вопроса указано время", 5, agenda_all_time),
        _item("agenda_speaker", "У каждого вопроса указан докладчик", 10, agenda_all_speakers),
        _item("participants_any", "Есть хотя бы один участник", 10, has_participants),
        _item("participants_full", "У всех участников есть должность и организация", 5, participants_full),
        _item("orders_any", "Есть хотя бы одно поручение", 10, len(m.orders) > 0),
        _item("scenario_opening", "Есть вводное слово", 10, _has_kind(m, SECTION_OPENING)),
        _item("scenario_intro", "Есть подводка к каждому вопросу", 10, intro_done),
        _item("scenario_closing", "Есть заключительное слово", 5, _has_kind(m, SECTION_CLOSING)),
        _item("glossary_any", "Есть хотя бы одно сокращение в глоссарии", 10, len(m.glossary) > 0),
    ]

    # материалы: у каждого вопроса (кроме «Обсуждения») есть хотя бы один материал
    mat_qnums = {str(mat.question_num).strip() for mat in m.materials if mat.question_num}
    for q in report_questions:
        items.append({
            "id": f"material_q{q.num}",
            "label": f"Материал к вопросу {q.num}",
            "weight": 5,
            "done": q.num in mat_qnums,
            "section": "materials",
        })
    if not report_questions:
        items.append(_item("materials_any", "Есть хотя бы один материал", 5, len(m.materials) > 0))

    # «Результаты» (решение пользователя 02.10): документы из «Транскрибатора» появляются после
    # совещания, поэтому вес 0 — полоска вкладки «Результаты» (пункты по section) их показывает,
    # а общая готовность модератора к совещанию от них не проседает
    kinds = {r.kind for r in m.results}
    for kind, label in (("transcript", "Результаты: есть транскрипция"),
                        ("summary", "Результаты: есть саммари"),
                        ("decisions", "Результаты: есть проект протокольных решений")):
        items.append({"id": f"results_{kind}", "label": label, "weight": 0,
                      "done": kind in kinds, "section": "results"})
    return items


def _has_kind(m: Meeting, section: str) -> bool:
    return any(b.section == section and b.text.strip() for b in m.scenario)


def _item(id_: str, label: str, weight: int, done: bool) -> dict:
    return {"id": id_, "label": label, "weight": weight, "done": done}
