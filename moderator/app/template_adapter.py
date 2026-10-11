"""Адаптер шаблона: наполняет исходный 900-КБ HTML-шаблон данными совещания.

Механика: парсим шаблон lxml'ом (JS/CSS сохраняются побайтово – проверено),
меняем только контентные блоки (шапка, повестка, участники, поручения,
секции модератора, глоссарий, навигация, «все поручения»), сериализуем обратно.
Результат – тот же документ с рабочим редактором, но уже наполненный.
"""
from __future__ import annotations

import re
from html import escape as esc, unescape as unesc
from pathlib import Path

from lxml import html as lhtml

from .models import (
    Meeting,
    SECTION_CLOSING,
    SECTION_OPENING,
    SECTION_QUESTION,
    _find_role,
    _genitive_role,
    html_to_text,
)

TEMPLATE_PATH = Path(__file__).resolve().parent.parent / "templates" / "brief_template.html"

_ROMAN = {1: "I", 2: "II", 3: "III", 4: "IV", 5: "V", 6: "VI", 7: "VII",
          8: "VIII", 9: "IX", 10: "X", 11: "XI", 12: "XII", 13: "XIII",
          14: "XIV", 15: "XV", 16: "XVI", 17: "XVII", 18: "XVIII"}


def _roman(n: int) -> str:
    return _ROMAN.get(n, str(n))


def _parser():
    return lhtml.HTMLParser(recover=True)


def _set_inner(el, html_str: str) -> None:
    for child in list(el):
        el.remove(child)
    el.text = None
    if not html_str.strip():
        return
    frag = lhtml.fragment_fromstring(html_str, create_parent="div")
    for node in list(frag):
        el.append(node)
    if frag.text:
        el.text = frag.text


def _set_text_all(tree, xpath: str, value: str) -> None:
    for el in tree.xpath(xpath):
        el.text = value


# ── генераторы отдельных блоков ─────────────────────────────────────────

def _display_role(role: str) -> str:
    """Вид должности в выгрузке: «Министр/Министра» всегда с большой буквы."""
    return re.sub(
        r"(?i)\bминистра\b|\bминистр\b",
        lambda m: "Министра" if m.group(0).lower() == "министра" else "Министр",
        role or "",
    )


def _speaker_row(name: str, role: str = "", org: str = "") -> str:
    role = _display_role(role)
    return (
        '<div class="speaker-row participants-person-row" data-attendance="in-person" '
        f'data-organization="{esc(org)}" data-original-name="{esc(name)}" '
        'data-protocol-rank="9999" data-protocol-status="unknown">'
        '<div class="speaker-icon">👤</div><div>'
        f'<div class="speaker-name">{esc(name)}</div>'
        f'<div class="speaker-role">{esc(role)}</div></div></div>'
    )


def _participant_app_row(p, idx: int) -> str:
    """Строка участника в HTML-выгрузке — как в приложении, но без кнопок и без столбца ранга."""
    surname, rest = _split_name(p.name)
    role_org = _display_role(p.role or "")
    if p.organization:
        role_org = (role_org + " · " if role_org else "") + p.organization
    return (
        '<div class="part-row export-part-row">'
        f'<span class="p-num">{idx + 1}</span>'
        '<span class="spk-name-cell">'
        f'<b class="spk-name">{esc(surname)}</b>'
        + (f'<span class="spk-name-rest">{esc(rest)}</span>' if rest else "")
        + ('<span class="spk-who">докладчик</span>' if getattr(p, "is_speaker", False) else "")
        + '</span>'
        f'<span class="spk-role">{esc(role_org)}</span>'
        '</div>'
    )


_AGENDA_SPECIAL = {"вступительное слово", "подведение итогов", "заключительное слово"}


def _is_special_agenda(title: str) -> bool:
    """Спецпункты (без номера): вступительное слово / подведение итогов."""
    return (title or "").strip().lower() in _AGENDA_SPECIAL


def _split_name(name: str) -> tuple[str, str]:
    """ФИО → (ФАМИЛИЯ заглавными, остальное) — как _splitName в приложении."""
    parts = (name or "").strip().split()
    if not parts:
        return "", ""
    return parts[0].upper(), " ".join(parts[1:])


def _min_html(minutes) -> str:
    """Минуты отдельным блоком «N мин» (в приложении — поле ввода)."""
    if minutes in ("", None):
        return '<span class="min-wrap"></span>'
    return (f'<span class="min-wrap"><span class="min-val">{esc(str(minutes))}</span>'
            '<span class="min-lbl">мин</span></span>')


def _agenda_item_html(num: int, a) -> str:
    """Пункт повестки — тот же состав, что в приложении, но без кнопок и полей ввода."""
    special = _is_special_agenda(a.title)
    is_disc = bool(re.match(r"^обсуждение", a.title or "", re.I))
    speakers = list(a.speakers or [])
    roles = list(getattr(a, "speaker_roles", None) or [])
    mins = list(getattr(a, "speaker_minutes", None) or [])

    rows = []
    for si, s in enumerate(speakers):
        role = _display_role(roles[si] if si < len(roles) else "")
        mn = mins[si] if si < len(mins) else ""
        who = "" if special else ("докладчик" if si == 0 else "содокладчик")
        surname, rest = _split_name(s)
        rows.append(
            '<div class="speaker-line">'
            '<span class="spk-name-cell">'
            f'<b class="spk-name">{esc(surname)}</b>'
            + (f'<span class="spk-name-rest">{esc(rest)}</span>' if rest else "")
            + (f'<span class="spk-who">{who}</span>' if who else "")
            + "</span>"
            f'<span class="spk-role">{esc(role)}</span>'
            + _min_html(mn)
            + "</div>"
        )

    disc = ""
    if is_disc and a.minutes:
        disc = ('<div class="disc-line"><span class="chip">обсуждение</span>'
                + _min_html(a.minutes) + "</div>")

    num_html = "" if (special or not a.num) else f'<span class="q-num">{esc(str(a.num))}</span>'
    return (
        '<div class="section p-row agenda-row">'
        + num_html
        + '<div class="p-body">'
        + f'<div class="ag-title-row"><span class="ag-title">{esc(a.title)}</span></div>'
        + "".join(rows) + disc
        + "</div></div>"
    )


def _speaker_role_for_block(m: Meeting | None, b) -> str:
    """Должность докладчика для сценария: сначала повестка, затем список участников."""
    if not m or not getattr(b, "speaker", ""):
        return ""
    speaker = b.speaker
    for a in m.agenda:
        if str(a.num) != str(getattr(b, "agenda_num", "")):
            continue
        for i, name in enumerate(a.speakers or []):
            if name == speaker and i < len(a.speaker_roles or []) and a.speaker_roles[i]:
                return a.speaker_roles[i]
    return _find_role(m.participants, speaker)


def _append_role_if_missing(text: str, role: str) -> str:
    """Для старых сценариев без должности добавляем её в конец фразы."""
    plain_text = html_to_text(text)
    role = _display_role((role or "").strip())
    if not role:
        return _display_role(plain_text)
    # Если должность уже есть в тексте (например, сценарий был пересформирован), не дублируем.
    if role.lower() in plain_text.lower() or _genitive_role(role).lower() in plain_text.lower():
        return _display_role(plain_text)
    suffix = _genitive_role(role) or role
    stripped = plain_text.rstrip()
    punct = "." if stripped.endswith(".") else ""
    if punct:
        stripped = stripped[:-1].rstrip()
    # Если в старом сценарии уже была должность после запятой, но она в неверном
    # регистре или с ошибкой склонения («коммуникацего»), заменяем её целиком.
    low_stripped = stripped.lower()
    if "," in stripped and ("прошу" in low_stripped or low_stripped.startswith("доклад ")):
        head = stripped.split(",", 1)[0].rstrip()
        return f"{head}, {suffix}{punct}"
    return f"{stripped}, {suffix}{punct}"


def _ensure_sentence_period(text: str) -> str:
    """Служебная фраза должна заканчиваться точкой."""
    text = (text or "").rstrip()
    if not text:
        return text
    return text if text.endswith((".", "!", "?")) else text + "."


def _points_html(plain: str, fallback: str) -> str:
    """Многострочный блок — по пункту списка на строку: одной строкой они слипаются в вёрстке."""
    points = [p.strip() for p in (plain or "").split("\n") if p.strip()]
    return "".join(f"<li>{esc(p)}</li>" for p in points) or f"<li>{fallback}</li>"


def _scenario_block_html(b, qnum: int = 0, m: Meeting | None = None) -> str:
    kind = b.kind
    role = _speaker_role_for_block(m, b)
    text_src = _append_role_if_missing(b.text, role) if kind in ("intro", "report") else html_to_text(b.text)
    if kind in ("intro", "report"):
        text_src = _ensure_sentence_period(text_src)
    text = esc(text_src)
    if kind == "opening" or kind == "intro" or kind == "replica":
        label = {"opening": "Подводка - логика вопроса", "intro": "Передача слова",
                 "replica": "Реплика после доклада"}[kind]
        # вступительное слово приезжает из «Тезисов» списком — сохраняем пункты, а не склеиваем в строку
        return (f'<div class="subsection moderator-logic-section"><div class="subsection-label">{label}</div>'
                f'<div class="note-block"><ul>{_points_html(text_src, text)}</ul></div></div>')
    if kind == "report":
        speaker = esc(b.speaker)
        role_html = esc(_display_role(role))
        return (
            '<div class="subsection builder-block moderator-report-subsection" draggable="false">'
            '<div class="subsection-label">Доклад</div>'
            f'<div class="speaker-row" data-attendance="in-person" data-organization="" '
            f'data-original-name="{speaker}" data-protocol-rank="9999" data-protocol-status="unknown">'
            '<div class="speaker-icon">👤</div><div>'
            f'<div class="speaker-name">{speaker}</div><div class="speaker-role">{role_html}</div></div></div>'
            f'<div class="report-title-line">{text}</div>'
            '<div class="moderator-info-row" data-no-reference="true">'
            '<span class="moderator-info-row-label">Справка</span>'
            f'<button class="question-info-btn info-chip" data-info-mode="report" data-question-num="{qnum}" type="button">Материалы доклада</button>'
            '</div></div>'
        )
    if kind == "comment":
        speaker = esc(b.speaker)
        return (
            '<div class="subsection moderator-comment-subsection"><div class="subsection-label">Комментарий участника</div>'
            '<div class="comment-box"><div class="comment-label">Реплика по ходу</div>'
            f'<div class="comment-person-line"><span class="speaker-icon">👤</span><span><strong>{speaker}</strong></span></div>'
            f'<div>{text}</div></div></div>'
        )
    if kind == "question":
        return ('<div class="subsection"><div class="subsection-label">Вопрос</div>'
                f'<div class="question"><div class="q-label">Вопрос</div><div class="question-text">{text}</div></div></div>')
    if kind == "order":
        return ('<div class="subsection order-subsection"><div class="subsection-label">Поручение</div>'
                f'<div class="poruchen"><div class="p-label">Поручение</div><ul><li>{text}</li></ul></div></div>')
    if kind == "problem":
        return ('<div class="subsection"><div class="subsection-label">Проблема</div>'
                f'<div class="warn-box"><div class="w-label">Проблема</div><div>{text}</div></div></div>')
    if kind == "decision":
        return ('<div class="subsection"><div class="subsection-label">Решение</div>'
                f'<div class="decision-box"><div class="solution-label">Решение</div><div>{text}</div></div></div>')
    if kind == "task":
        return ('<div class="subsection task-list-subsection"><div class="subsection-label">Задачи</div>'
                f'<div class="task-item"><div class="task-dot"></div><div>{text}</div></div></div>')
    if kind == "reference":
        return ('<div class="subsection"><div class="subsection-label">Справочная информация</div>'
                '<details class="ref-wrap"><summary class="ref-toggle"><span class="ref-toggle-icon ref-icon-down"></span>'
                '<span class="ref-toggle-label">Справочно</span><span class="ref-toggle-hint">раскрыть</span></summary>'
                f'<div class="ref-body">{text}</div></details></div>')
    if kind == "closing":
        # Искра отдаёт заключительное слово списком — сохраняем пункты, а не склеиваем в абзац
        return ('<div class="subsection"><div class="subsection-label">Заключительное слово</div>'
                f'<div class="note-block"><ul>{_points_html(html_to_text(b.text), text)}</ul></div></div>')
    # fallback: обычный блок-нота
    return f'<div class="subsection"><div class="subsection-label">{esc(kind)}</div><div class="note-block"><ul><li>{text}</li></ul></div></div>'


def _short_title(title: str, limit: int = 34) -> str:
    """Короткий заголовок для навигации: обрезаем по целому слову и добавляем многоточие."""
    t = " ".join((title or "").split())
    if len(t) <= limit:
        return t
    cut = t[:limit].rsplit(" ", 1)[0].rstrip(" ,.;:—-–")
    if not cut:
        cut = t[:limit]
    return cut + "…"


# ── заполнение разделов ─────────────────────────────────────────────────

def _time_range(m: Meeting) -> str:
    """Диапазон времени из таймлайна: первый старт – последний конец."""
    blocks = m.timeline()
    starts = [b["start"] for b in blocks if b.get("start")]
    ends = [b["end"] for b in blocks if b.get("end")]
    if starts and ends:
        return f"{starts[0]} – {ends[-1]}"
    if m.time_start or m.time_end:
        return f"{m.time_start} – {m.time_end}"
    return ""


def _fill_header(tree, m: Meeting) -> None:
    # заголовок — полное наименование в именительном; подзаголовок («в режиме ВКС») — отдельной строкой, как раньше
    _set_text_all(tree, "//*[@id='meeting-title-text']", m.full_title or "Без названия")
    _set_text_all(tree, "//*[contains(@class,'meeting-subtitle-text')]", m.subtitle_line)
    _set_text_all(tree, "//*[@id='meeting-date-text']", m.date or "")
    _set_text_all(tree, "//*[contains(@class,'place-primary')]", m.location_primary or "")
    _set_text_all(tree, "//*[contains(@class,'place-secondary')]", m.location_secondary or "")
    _set_text_all(tree, "//*[@id='meeting-time-text']", _time_range(m))


def _fill_agenda(tree, m: Meeting) -> None:
    sec = tree.xpath("//*[@id='s0']")
    if not sec:
        return
    body = sec[0].xpath(".//*[contains(@class,'section-body')]")
    if not body:
        return
    items = "".join(_agenda_item_html(i + 1, a) for i, a in enumerate(m.agenda))
    if not items:
        items = '<div class="empty">Повестка пуста</div>'
    _set_inner(body[0], items)


def _fill_participants(tree, m: Meeting) -> None:
    sec = tree.xpath("//*[@id='s-participants']")
    if not sec:
        return
    sec = sec[0]
    # В выгрузке раздел должен повторять вкладку приложения «Список участников»:
    # один сквозной список, номер + ФИО + должность/организация; без кнопок, форм и без столбца ранга.
    n_all = len(m.participants)
    summ = sec.xpath(".//*[@id='participants-design-summary']")
    if summ:
        summ[0].text = f"Участники – {n_all} чел."

    body = sec.xpath(".//*[contains(@class,'section-body')]")
    if not body:
        return
    rows = "".join(_participant_app_row(p, i) for i, p in enumerate(m.participants))
    if not rows:
        rows = '<div class="empty">Участников нет</div>'
    _set_inner(body[0], f'<div class="participants-export-list">{rows}</div>')


def _protocol_row(p) -> str:
    role_text = " · ".join(x for x in [p.role, p.organization] if x)
    return _speaker_row(p.name, role_text, p.organization)


def _replace_primary_body(section, label: str, rows_html: str, is_org_panel: bool = False) -> None:
    """В primary-wrap с данным label заменить список людей на rows_html.

    Для «Докладчиков»: вставляем после add-speaker-btn-wrap.
    Для «Участников»: вставляем в панель организаций после вкладок/форм.
    """
    for wrap in section.xpath(".//details[contains(@class,'primary-wrap')]"):
        summary = wrap.xpath("./summary//*[contains(@class,'primary-toggle-label')]")
        if not summary or summary[0].text != label:
            continue
        body = wrap.xpath("./*[contains(@class,'primary-body')]")
        if not body:
            continue
        b = body[0]
        if not is_org_panel:
            # удалить существующие speaker-row, сохранить add-speaker-btn-wrap
            for row in b.xpath("./*[contains(@class,'speaker-row')]"):
                row.getparent().remove(row)
            # вставить после add-speaker-btn-wrap
            wrap_el = b.xpath("./*[@id='add-speaker-btn-wrap']")
            if wrap_el:
                frag = lhtml.fragment_fromstring(rows_html, create_parent="div")
                anchor = wrap_el[0]
                idx = anchor.getparent().index(anchor)
                for node in reversed(list(frag)):
                    anchor.getparent().insert(idx + 1, node)
        else:
            # вставить в панель организаций
            panel = b.xpath(".//*[contains(@class,'participants-view-panel')][@data-participants-panel='organizations']")
            if panel:
                _set_inner(panel[0], rows_html)


def _fill_orders(tree, m: Meeting) -> None:
    sec = tree.xpath("//*[@id='s-orders']")
    if not sec:
        return
    summ = sec[0].xpath(".//*[@id='orders-design-summary']")
    if summ:
        summ[0].text = f"{len(m.orders)} поручений · {len(m.agenda)} вопросов"
    body = sec[0].xpath(".//*[@id='orders-page-body']")
    if not body:
        return
    # сгруппировать поручения по вопросу
    groups: dict[str, list] = {}
    for o in m.orders:
        groups.setdefault(o.agenda_num or "", []).append(o)
    html_str = ""
    for qnum, orders in groups.items():
        qlabel = f"Вопрос {qnum}" if qnum else "Общие поручения"
        cards = ""
        for i, o in enumerate(orders, 1):
            dl = f'<div class="orders-design-meta-row"><span class="orders-design-meta-label">Срок</span><span class="orders-design-meta-value" data-order-meta="deadline">{esc(o.deadline)}</span></div>' if o.deadline else ""
            cards += (
                f'<article class="orders-design-card" data-order-id="order-custom-{i}">'
                f'<div class="orders-design-text">{esc(o.text)}</div>'
                f'<div class="orders-design-meta">{dl}</div></article>'
            )
        html_str += (
            f'<details class="orders-design-group" data-question-id="m{esc(qnum)}" open="">'
            f'<summary class="orders-design-group-title"><span class="orders-design-group-label">{esc(qlabel)}</span>'
            f'<span class="orders-design-group-hint">скрыть</span></summary>'
            f'<div class="orders-design-list">{cards}</div></details>'
        )
    _set_inner(body[0], html_str)


def _fill_moderator(tree, m: Meeting) -> None:
    main = tree.xpath("//*[contains(@class,'main-content')]")
    if not main:
        return
    mc = main[0]
    # собрать существующие секции модератора
    existing = mc.xpath("./details[contains(@class,'moderator-section')]")
    # построить новый HTML для всех секций модератора
    opening = [b for b in m.scenario if b.section == SECTION_OPENING]
    closing = [b for b in m.scenario if b.section == SECTION_CLOSING]

    opening_html = "".join(_scenario_block_html(b, m=m) for b in opening) or \
        '<div class="note-block"><ul><li></li></ul></div>'
    transition = '<div class="subsection transition-subsection"><div class="quick-link-row transition-link-row"><a class="quick-link-btn transition-next-btn" href="#m1">Переходим к первому вопросу</a></div></div>' if m.agenda else ""
    m0 = (
        '<details class="section moderator-section moderator-current-section" data-short-title="Вводное слово" id="m0" open="">'
        '<summary class="section-head"><div class="section-num">0</div><div class="section-title">Вводное слово</div>'
        f'<div class="section-time">{esc(m.time_start)} – {esc(m.time_start)}</div></summary>'
        f'<div class="section-body">{opening_html}{transition}</div></details>'
    )

    question_html = ""
    for i, a in enumerate(m.agenda, 1):
        blocks = [b for b in m.scenario if b.section == SECTION_QUESTION and b.agenda_num == a.num]
        inner = "".join(_scenario_block_html(b, i, m) for b in blocks) or \
            '<div class="subsection moderator-logic-section"><div class="subsection-label">Подводка - логика вопроса</div><div class="note-block"><ul><li></li></ul></div></div>'
        question_html += (
            f'<details class="section moderator-section question-manageable" data-question-num="{i}" '
            f'data-short-title="{esc(_short_title(a.title))}" id="m{i}">'
            '<summary class="section-head">'
            f'<div class="section-num">{esc(_roman(i))}</div><div class="section-title">{esc(a.title)}</div>'
            f'<div class="section-time">{esc(a.time)}</div></summary>'
            f'<div class="section-body">{inner}</div></details>'
        )

    closing_html = "".join(_scenario_block_html(b, m=m) for b in closing) or \
        '<div class="subsection moderator-logic-section"><div class="subsection-label">Подводка - логика вопроса</div><div class="note-block"><ul><li></li></ul></div></div>'
    m_closing = (
        '<details class="section moderator-section" id="m-closing">'
        '<summary class="section-head"><div class="section-num">★</div><div class="section-title">Заключительное слово</div>'
        f'<div class="section-time">{esc(m.time_end)} – {esc(m.time_end)}</div></summary>'
        f'<div class="section-body">{closing_html}</div></details>'
    )

    new_sections_html = m0 + question_html + m_closing

    # удалить существующие секции модератора из main-content
    for el in existing:
        el.getparent().remove(el)
    # вставить новые перед закрытием main-content (после s-orders)
    frag = lhtml.fragment_fromstring(new_sections_html, create_parent="div")
    for node in list(frag):
        mc.append(node)


def _fill_sidebar(tree, m: Meeting) -> None:
    """Подпункты «Модератора» в левом столбце (разделы — статично в шаблоне)."""
    nav = tree.xpath("//*[@id='sidebar-nav']")
    if not nav:
        return
    links = '<a class="sub" data-sec="m0" data-view-link="moderator" href="#m0">Вводное слово</a>'
    for i, a in enumerate(m.agenda, 1):
        links += (
            f'<a class="sub" data-sec="m{i}" data-view-link="moderator" href="#m{i}">'
            f'<span class="sn">{i}</span>{esc(_short_title(a.title))}</a>'
        )
    links += '<a class="sub" data-sec="m-closing" data-view-link="moderator" href="#m-closing">Заключительное слово</a>'

    frag = lhtml.fragment_fromstring(links, create_parent="div")
    group = nav[0].xpath(".//a[contains(concat(' ', normalize-space(@class), ' '), ' nav-group ')]")
    if group:
        idx = list(nav[0]).index(group[0])
        for off, node in enumerate(list(frag), 1):
            nav[0].insert(idx + off, node)
    else:
        for node in list(frag):
            nav[0].append(node)


def _fill_glossary(tree, m: Meeting) -> None:
    tbodies = tree.xpath("//*[contains(@class,'glossary-table')]/tbody")
    if not tbodies:
        return
    rows = "".join(
        f'<tr><td><span style="font-weight:700;">{esc(g.term)}</span></td><td>{esc(g.definition)}</td></tr>'
        for g in m.glossary
    )
    _set_inner(tbodies[0], rows)


def _plain(text: str) -> str:
    """Читаемый текст: снимает HTML, в том числе дважды экранированный (Искра)."""
    t = text or ""
    for _ in range(3):
        t = unesc(t)
        t = re.sub(r"<br\s*/?>", "\n", t, flags=re.I)
        t = re.sub(r"</(?:p|div|li|h[1-6]|tr)\s*>", "\n", t, flags=re.I)
        t = re.sub(r"<[^>]*>", "", t)
    t = t.replace("\r", "")
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()


def _inline(s: str) -> str:
    """Инлайновая разметка: экранирование + **жирный** → <b>."""
    return re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", esc(s))


def _as_text(item) -> str:
    if isinstance(item, str):
        return item
    if isinstance(item, dict):
        return str(item.get("text") or item.get("title") or item.get("value") or item)
    return str(item)


def _fmt_block(text: str, limit: int = 4000) -> str:
    """Текст → абзацы и маркированные списки (как в приложении)."""
    p = _plain(text)[:limit]
    if not p:
        return ""
    out: list[str] = []
    in_list = False
    for line in p.split("\n"):
        line = line.strip()
        if not line:
            if in_list:
                out.append("</ul>")
                in_list = False
            continue
        m = re.match(r"^[-•*·▪]\s+(.*)$", line) or re.match(r"^\d{1,2}[.)]\s+(.*)$", line)
        if m:
            if not in_list:
                out.append('<ul class="mat-list">')
                in_list = True
            out.append(f"<li>{_inline(m.group(1))}</li>")
        else:
            if in_list:
                out.append("</ul>")
                in_list = False
            out.append(f"<p>{_inline(line)}</p>")
    if in_list:
        out.append("</ul>")
    return "".join(out)


def _figures_html(figures: list) -> str:
    """Диаграммы материала: SVG рисуем заново из items (app/charts.py) — в базе мог лежать чужой svg."""
    from . import charts
    out = []
    for f in figures:
        try:
            pic = charts.svg(f)
        except (ValueError, TypeError, AttributeError):
            continue
        src = str(f.get("source") or "").strip()
        cap = f'<div class="mat-figure-source" style="font-size:0.85em;color:#5b6270">Источник: {esc(src[:300])}</div>' if src else ""
        out.append(f'<figure class="mat-figure" style="margin:0.5rem 0 1rem;max-width:560px">{pic}{cap}</figure>')
    return "".join(out)


def _material_card(mat) -> str:
    parts = ['<div class="mat-card">']
    if mat.type:
        parts.append(f'<span class="mat-type">{esc(mat.type)}</span>')
    spk = f' <span class="mat-speaker">– {esc(mat.speaker)}</span>' if mat.speaker else ""
    parts.append(f'<div class="mat-title">{esc(mat.title or mat.filename or "(без названия)")}{spk}</div>')
    if mat.summary:
        parts.append(f'<div class="mat-label">Саммари</div><div class="mat-text">{_fmt_block(mat.summary)}</div>')
    elif mat.text:
        parts.append(f'<div class="mat-label">Содержание</div><div class="mat-text">{_fmt_block(mat.text)}</div>')
    figs = _figures_html(getattr(mat, "figures", None) or [])
    if figs:
        parts.append(f'<div class="mat-label">Диаграммы</div>{figs}')
    if mat.theses:
        items = "".join(f"<li>{_inline(_plain(_as_text(t)))}</li>" for t in mat.theses)
        parts.append(f'<div class="mat-label">Тезисы</div><ul class="mat-list">{items}</ul>')
    conclusion = getattr(mat, "conclusion", "") or ""
    proposals = [p for p in (getattr(mat, "proposals", None) or []) if p]
    if conclusion or proposals:
        if conclusion:
            parts.append(f'<div class="mat-proposal"><b>Вывод:</b> {_inline(_plain(conclusion))}</div>')
        if proposals:
            items = "".join(f"<li>{_inline(_plain(p))}</li>" for p in proposals)
            parts.append(f'<div class="mat-label">Предложения</div><ul class="mat-list">{items}</ul>')
    elif mat.proposal:
        parts.append(f'<div class="mat-proposal"><b>Предложение:</b> {_inline(_plain(mat.proposal))}</div>')
    if mat.orders:
        items = "".join(f"<li>{_inline(_plain(_as_text(t)))}</li>" for t in mat.orders)
        parts.append(f'<div class="mat-label">Поручения</div><ul class="mat-list">{items}</ul>')
    parts.append("</div>")
    return "".join(parts)


def _fill_materials(tree, m: Meeting) -> None:
    """Раздел «Материалы»: группировка по вопросам повестки + общие материалы."""
    box = tree.xpath("//*[@id='materials-body']")
    if not box:
        return
    if not m.materials:
        _set_inner(box[0], '<div class="mat-empty">Материалы не приложены</div>')
        return
    html_str = ""
    used: set[int] = set()
    for a in m.agenda:
        qnum = (a.num or "").strip()
        if not qnum:
            continue
        idxs = [i for i, x in enumerate(m.materials) if (x.question_num or "").strip() == qnum]
        if not idxs:
            continue
        used.update(idxs)
        html_str += f'<div class="mat-group"><div class="mat-group-title">Вопрос {esc(qnum)}. {esc(a.title)}</div>'
        html_str += "".join(_material_card(m.materials[i]) for i in idxs)
        html_str += "</div>"
    rest = [x for i, x in enumerate(m.materials) if i not in used]
    if rest:
        html_str += '<div class="mat-group"><div class="mat-group-title">Общие материалы</div>'
        html_str += "".join(_material_card(x) for x in rest)
        html_str += "</div>"
    _set_inner(box[0], html_str)


def _fill_all_orders(tree, m: Meeting) -> None:
    body = tree.xpath("//*[@id='all-orders-body']")
    if not body:
        return
    groups: dict[str, list] = {}
    for o in m.orders:
        groups.setdefault(o.agenda_num or "", []).append(o)
    html_str = ""
    for qnum, orders in groups.items():
        # заголовок вопроса
        a = next((x for x in m.agenda if x.num == qnum), None)
        title = f"{a.num}. {a.title}" if a else ("Общие поручения" if not qnum else f"Вопрос {qnum}")
        items = ""
        for i, o in enumerate(orders, 1):
            items += (
                f'<div class="all-orders-item-wrap"><li class="order-full-source" data-order-base-new="true" '
                f'data-order-deadline="" data-order-id="s{esc(qnum)}-order-{i}" value="{i}">{esc(o.text)}</li>'
                f'<button aria-label="Удалить поручение" class="all-orders-delete-btn" data-delete-order-id="s{esc(qnum)}-order-{i}" title="Удалить поручение из базы" type="button">×</button></div>'
            )
        html_str += (
            f'<section class="all-orders-group" data-question-id="m{esc(qnum)}"><h3>{esc(title)}</h3>'
            f'<ol class="all-orders-list">{items}</ol></section>'
        )
    _set_inner(body[0], html_str)


# ── главная функция ─────────────────────────────────────────────────────

def render(meeting: Meeting) -> str:
    if not TEMPLATE_PATH.exists():
        raise FileNotFoundError("Шаблон не найден: templates/brief_template.html")
    raw = TEMPLATE_PATH.read_text(encoding="utf-8")
    tree = lhtml.fromstring(raw, parser=_parser())

    _fill_header(tree, meeting)
    _fill_agenda(tree, meeting)
    _fill_participants(tree, meeting)
    _fill_orders(tree, meeting)
    _fill_moderator(tree, meeting)
    _fill_sidebar(tree, meeting)
    _fill_materials(tree, meeting)
    _fill_glossary(tree, meeting)
    _fill_all_orders(tree, meeting)

    # заголовок документа (вкладка браузера)
    titles = tree.xpath("//title")
    if titles:
        titles[0].text = meeting.full_title or "Модератор-бриф"

    out = lhtml.tostring(tree, encoding="unicode", method="html")
    # парсер HTML пишет атрибуты строчными: браузер сам вернёт viewBox внутри <svg>, но файл,
    # открытый как XML или в старом просмотрщике, без него не масштабируется — возвращаем регистр
    out = re.sub(r"(<svg\b[^>]*?\s)viewbox=", r"\1viewBox=", out)
    return "<!DOCTYPE html>\n" + out
