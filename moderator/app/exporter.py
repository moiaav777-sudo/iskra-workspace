"""Генерация финального HTML-файла модератор-брифа из модели совещания.

Милейстоун 1: чистая автономная вёрстка в фирменной палитре шаблона.
Милейстоун 2 (следующий): адаптер под оригинальный 900-КБ шаблон.
"""
from __future__ import annotations

from html import escape

from .models import html_to_text, Meeting, SECTION_CLOSING, SECTION_OPENING, SECTION_QUESTION

CSS = """
:root{--navy:#1F497D;--blue1:#5880C8;--blue2:#94AEDC;--bg:#F7F9FC;--text:#1a2b3c;
--border:#c4d4e8;--muted:#6b88a8;--gold:#96650e;}
*{box-sizing:border-box;margin:0;padding:0;}
body{font-family:Arial,'Helvetica Neue',sans-serif;background:var(--bg);color:var(--text);
font-size:14px;line-height:1.6;}
.page{max-width:900px;margin:0 auto;padding:32px 20px 80px;}
header{border-bottom:3px solid var(--blue1);padding-bottom:16px;margin-bottom:24px;}
.prefix{color:var(--muted);font-size:12px;text-transform:uppercase;letter-spacing:1px;}
h1{font-size:22px;margin:4px 0 2px;color:var(--navy);}
.subtitle{color:var(--muted);}
.meta{margin-top:12px;color:var(--text);font-size:13px;display:flex;flex-wrap:wrap;gap:8px 24px;}
.meta .badge{background:#fff;border:1px solid var(--border);border-radius:20px;padding:2px 12px;}
section.block{background:#fff;border:1px solid var(--border);border-radius:12px;
padding:20px 24px;margin-bottom:20px;box-shadow:0 2px 8px rgba(28,30,34,.05);}
h2{font-size:16px;color:var(--navy);margin-bottom:12px;display:flex;align-items:center;gap:8px;}
h2 .star{color:var(--gold);}
.q{margin-bottom:14px;padding-bottom:14px;border-bottom:1px dashed var(--border);}
.q:last-child{border-bottom:none;margin-bottom:0;padding-bottom:0;}
.q .q-num{font-weight:700;color:var(--navy);}
.q .q-time{color:var(--muted);font-size:12px;margin-left:8px;}
.q .q-speaker{font-size:13px;color:var(--text);margin-top:2px;}
.q .q-speaker b{color:var(--blue1);}
ol.participants,ul.orders{margin-left:20px;}
ol.participants li{margin-bottom:8px;}
.person .org{color:var(--muted);font-size:12px;}
.order{margin-bottom:10px;}
.order .deadline{color:var(--gold);font-size:12px;font-weight:600;}
.scene{padding:10px 0;border-bottom:1px dashed var(--border);}
.scene:last-child{border-bottom:none;}
.scene .kind{display:inline-block;font-size:11px;font-weight:700;text-transform:uppercase;
letter-spacing:.5px;color:#fff;background:var(--blue1);border-radius:4px;padding:1px 8px;margin-right:8px;}
.scene .kind.intro,.scene .kind.replica{background:var(--navy);}
.scene .kind.comment{background:#1F6F7E;}
.scene .kind.order{background:var(--gold);}
.scene .kind.problem{background:#b03030;}
.scene .kind.decision{background:#267a45;}
.scene .kind.opening,.scene .kind.closing{background:#333;}
.scene .speaker{font-weight:700;color:var(--blue1);}
table.glossary{width:100%;border-collapse:collapse;font-size:13px;}
table.glossary th,table.glossary td{border:1px solid var(--border);padding:6px 10px;text-align:left;}
table.glossary th{background:var(--bg);color:var(--navy);}
"""

_KIND_LABEL = {
    "opening": "Вводное слово", "intro": "Подводка", "report": "Доклад",
    "replica": "Реплика", "comment": "Комментарий", "question": "Вопрос",
    "order": "Поручение", "problem": "Проблема", "decision": "Решение",
    "task": "Задача", "reference": "Справка", "closing": "Заключительное слово",
}


def render(meeting: Meeting) -> str:
    e = escape
    parts: list[str] = []

    # Шапка
    parts.append('<div class="page">')
    parts.append('<header>')
    parts.append('<div class="prefix">Модератор</div>')
    parts.append(f'<h1>{e(meeting.full_title or "Без названия")}</h1>')   # полное наименование в именительном
    if meeting.subtitle_line:
        parts.append(f'<div class="subtitle">{e(meeting.subtitle_line)}</div>')
    parts.append('<div class="meta">')
    if meeting.chair:
        parts.append(f'<span>Председатель: <b>{e(meeting.chair)}</b></span>')
    if meeting.date:
        parts.append(f'<span>{e(meeting.date)}</span>')
    if meeting.location_primary:
        loc = meeting.location_primary + (
            f" · {meeting.location_secondary}" if meeting.location_secondary else ""
        )
        parts.append(f'<span>{e(loc)}</span>')
    if meeting.time_start or meeting.time_end:
        parts.append(f'<span class="badge">{e(meeting.time_start)} – {e(meeting.time_end)}</span>')
    parts.append('</div>')
    parts.append('</header>')

    # Повестка
    parts.append('<section class="block"><h2><span class="star">★</span>Повестка</h2>')
    for a in meeting.agenda:
        parts.append('<div class="q">')
        parts.append(f'<div><span class="q-num">{e(a.num + ". ") if a.num else ""}</span>'
                     f'<span class="q-title">{e(a.title)}</span>'
                     f'{f"<span class=q-time>{e(a.time)}</span>" if a.time else ""}</div>')
        if a.speakers:
            parts.append(f'<div class="q-speaker">докл. <b>{e(", ".join(a.speakers))}</b></div>')
        parts.append('</div>')
    if not meeting.agenda:
        parts.append('<div class="q"><span class="q-time">Повестка пуста</span></div>')
    parts.append('</section>')

    # Участники
    parts.append('<section class="block"><h2>Участники</h2><ol class="participants">')
    for p in meeting.participants:
        org = f'<span class="org"> – {e(p.organization)}</span>' if p.organization else ""
        role = f'<span>, {e(p.role)}</span>' if p.role else ""
        parts.append(f'<li><div class="person"><b>{e(p.name or "Без имени")}</b>{role}{org}</div></li>')
    if not meeting.participants:
        parts.append('<div class="q"><span class="q-time">Участники не добавлены</span></div>')
    parts.append('</section>')

    # Поручения
    parts.append('<section class="block"><h2>Поручения</h2><ul class="orders">')
    for o in meeting.orders:
        dl = f' <span class="deadline">Срок – {e(o.deadline)}</span>' if o.deadline else ""
        who = f' <span class="q-speaker">({e(o.assignee)})</span>' if o.assignee else ""
        parts.append(f'<li class="order">{e(o.text)}{who}{dl}</li>')
    if not meeting.orders:
        parts.append('<div class="q"><span class="q-time">Поручений нет</span></div>')
    parts.append('</section>')

    # Ход совещания (сценарий модератора)
    parts.append('<section class="block"><h2>Ход совещания</h2>')
    for b in meeting.scenario:
        cls = b.kind if b.kind in _KIND_LABEL else "other"
        label = _KIND_LABEL.get(b.kind, b.kind)
        speaker = f' <span class="speaker">{e(b.speaker)}</span>' if b.speaker else ""
        if b.kind == "opening":
            parts.append(f'<div class="scene"><span class="kind opening">{e(label)}</span>'
                         f'<div>{e(html_to_text(b.text))}</div></div>')
        elif b.kind == "closing":
            parts.append(f'<div class="scene"><span class="kind closing">{e(label)}</span>'
                         f'<div>{e(html_to_text(b.text))}</div></div>')
        else:
            head = f'<span class="kind {cls}">{e(label)}</span>'
            if b.agenda_num:
                head += f' <span class="q-num">(вопрос {e(b.agenda_num)})</span>'
            parts.append(f'<div class="scene">{head}{speaker}<div>{e(html_to_text(b.text))}</div></div>')
    if not meeting.scenario:
        parts.append('<div class="q"><span class="q-time">Сценарий пуст</span></div>')
    parts.append('</section>')

    # Глоссарий
    if meeting.glossary:
        parts.append('<section class="block"><h2>Глоссарий</h2><table class="glossary">'
                     '<thead><tr><th>Сокращение</th><th>Расшифровка</th></tr></thead><tbody>')
        for g in meeting.glossary:
            parts.append(f'<tr><td><b>{e(g.term)}</b></td><td>{e(g.definition)}</td></tr>')
        parts.append('</tbody></table></section>')

    parts.append('</div>')

    title = e(meeting.title or "Модератор-бриф")
    return (
        "<!DOCTYPE html>\n<html lang=\"ru\"><head><meta charset=\"utf-8\">"
        f"<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        f"<title>{title}</title><style>{CSS}</style></head><body>"
        + "".join(parts)
        + "</body></html>"
    )


def _page(prefix: str, title: str, meeting: Meeting, body: str) -> str:
    """Общая обёртка отдельного документа («Повестка», «Список участников»): та же вёрстка,
    что у брифа целиком, но с одним разделом — так «Материалы» узнают вид по имени файла,
    а не по содержимому."""
    e = escape
    head = [f'<div class="prefix">{e(prefix)}</div>', f'<h1>{e(meeting.full_title or meeting.title or "Без названия")}</h1>']
    if meeting.subtitle_line:
        head.append(f'<div class="subtitle">{e(meeting.subtitle_line)}</div>')
    meta = []
    if meeting.chair:
        meta.append(f'<span>Председатель: <b>{e(meeting.chair)}</b></span>')
    if meeting.date:
        meta.append(f'<span>{e(meeting.date)}</span>')
    if meta:
        head.append('<div class="meta">' + "".join(meta) + '</div>')
    return (
        "<!DOCTYPE html>\n<html lang=\"ru\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        f"<title>{e(title)}</title><style>{CSS}</style></head><body>"
        f'<div class="page"><header>{"".join(head)}</header>{body}</div>'
        "</body></html>"
    )


def render_agenda(meeting: Meeting) -> str:
    """Отдельный документ «Повестка»: пункты по порядку, докладчики (../CLAUDE.md п. 9,
    номер и место в пакете расставляют «Материалы» по имени файла)."""
    e = escape
    items = [a for a in meeting.agenda if a.num.strip()]
    parts = ['<section class="block"><h2>Повестка дня</h2>']
    for a in items:
        parts.append('<div class="q">')
        parts.append(f'<div><span class="q-num">{e(a.num)}. </span><span class="q-title">{e(a.title)}</span>'
                     f'{f"<span class=q-time>{e(a.time)}</span>" if a.time else ""}</div>')
        if a.speakers:
            people = []
            for i, sp in enumerate(a.speakers):
                role = a.speaker_roles[i] if i < len(a.speaker_roles) else ""
                people.append(e(sp) + (f", {e(role)}" if role else ""))
            parts.append(f'<div class="q-speaker">докл. <b>{"; ".join(people)}</b></div>')
        parts.append('</div>')
    if not items:
        parts.append('<div class="q"><span class="q-time">Повестка пуста</span></div>')
    parts.append('</section>')
    return _page("Повестка", "Повестка", meeting, "".join(parts))


def render_participants(meeting: Meeting) -> str:
    """Отдельный документ «Список участников»: ФИО, должность, ведомство по порядку (протокольное
    старшинство сервис уже поддерживает в `meeting.participants`)."""
    e = escape
    parts = ['<section class="block"><h2>Участники</h2><ol class="participants">']
    for p in meeting.participants:
        org = f'<span class="org"> – {e(p.organization)}</span>' if p.organization else ""
        role = f'<span>, {e(p.role)}</span>' if p.role else ""
        parts.append(f'<li><div class="person"><b>{e(p.name or "Без имени")}</b>{role}{org}</div></li>')
    parts.append('</ol>')
    if not meeting.participants:
        parts.append('<div class="q"><span class="q-time">Участники не добавлены</span></div>')
    parts.append('</section>')
    return _page("Список участников", "Список участников", meeting, "".join(parts))


def agenda_filename() -> str:
    """Ровно «Повестка» — по имени «Материалы» узнают вид документа и не добавляют
    название совещания (`../materials/app/wording.py` → `WHOLE`)."""
    return "Повестка.html"


def participants_filename() -> str:
    """Ровно «Список участников» — та же причина, что у `agenda_filename`."""
    return "Список участников.html"
