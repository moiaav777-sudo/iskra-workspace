"""Каноническая модель данных совещания («модератор-бриф»).

Принцип: это модель СОВЕЩАНИЯ, а не модель HTML. Готовый HTML — одно из
представлений этой модели, которое собирает экспортёр/адаптер.
Хранится в локальной SQLite; тексты документов для разбора уходят в Искру.
"""
from __future__ import annotations

from dataclasses import dataclass, field, fields, is_dataclass
from typing import Any

import re
import uuid

from .names_common import accusative, genitive

# Типы сценарных блоков (соответствуют типовым блокам исходного шаблона).
SCENARIO_KINDS = (
    "opening",   # вводное слово
    "intro",     # подводка — логика вопроса
    "report",    # доклад / материалы доклада
    "replica",   # реплика модератора после доклада
    "comment",   # комментарий участника по ходу
    "question",  # вопрос к докладчику
    "order",     # поручение
    "problem",   # проблема / разногласие
    "decision",  # решение
    "task",      # задача
    "reference", # справочная информация
    "closing",   # заключительное слово
)

# Секции сценария: вводное слово / вопрос / заключительное слово.
SECTION_OPENING = "opening"
SECTION_QUESTION = "question"
SECTION_CLOSING = "closing"

# Типы материалов к вопросу (для выпадающего списка).
MATERIAL_TYPES = (
    "Доклад выступающего",
    "Презентация",
    "Справка",
    "Статистика / данные",
    "Аналитическая записка",
    "Инфографика",
    "Нормативный документ",
    "Письмо / обращение",
    "Тезисы выступления",
    "Саммари / одностраничник",
    "Материал к вопросу",
)

_MONTHS = {
    "января": 1, "февраля": 2, "марта": 3, "апреля": 4, "мая": 5, "июня": 6,
    "июля": 7, "августа": 8, "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12,
    "январь": 1, "февраль": 2, "март": 3, "апрель": 4, "май": 5, "июнь": 6,
    "июль": 7, "август": 8, "сентябрь": 9, "октябрь": 10, "ноябрь": 11, "декабрь": 12,
}

_ORDINALS = {1: "первому", 2: "второму", 3: "третьему", 4: "четвёртому", 5: "пятому",
             6: "шестому", 7: "седьмому", 8: "восьмому", 9: "девятому", 10: "десятому",
             11: "одиннадцатому", 12: "двенадцатому", 13: "тринадцатому", 14: "четырнадцатому",
             15: "пятнадцатому", 16: "шестнадцатому", 17: "семнадцатому", 18: "восемнадцатому",
             19: "девятнадцатому", 20: "двадцатому"}
_ROMAN = {"i": 1, "ii": 2, "iii": 3, "iv": 4, "v": 5, "vi": 6, "vii": 7, "viii": 8, "ix": 9, "x": 10}


def new_id() -> str:
    return uuid.uuid4().hex[:10]


# Полное наименование начинается с вида мероприятия, перед ним бывают одно-два определения:
# «Рабочее совещание …», «расширенного заседания …», «селекторного совещания …», «рабочей встречи с …».
# «в режиме ВКС», «(закрытое)», «2-е заседание» — подзаголовки, их правило не узнаёт (ревьюер RC-22-3, Н2).
_ADJ = r"[а-яё]+(?:ое|ого|ому|ом|ее|его|ему|ем|ая|ой|ую|ей|ые|ых|ым|ий|ый)"
_HEAD_WORD = r"(?:совещани[еяю]|заседани[еяю]|встреч[аиу])"
_FULL_TITLE_HEAD = re.compile(rf"^(?:{_ADJ}\s+){{0,2}}{_HEAD_WORD}\b", re.I)
_NOMINATIVE_HEAD = {"совещания": "совещание", "совещанию": "совещание", "совещание": "совещание",
                    "заседания": "заседание", "заседанию": "заседание", "заседание": "заседание",
                    "встречи": "встреча", "встречу": "встреча", "встреча": "встреча"}
# определение перед словом мероприятия — в именительный того же рода: «рабочего» → «рабочее», «рабочей» → «рабочая»
_ADJ_NOM = {"neuter": [("ого", "ое"), ("его", "ее"), ("ому", "ое"), ("ему", "ее"), ("ом", "ое"), ("ем", "ее"),
                       ("ый", "ое"), ("ий", "ее"), ("ое", "ое"), ("ее", "ее")],
            "fem": [("ой", "ая"), ("ей", "яя"), ("ую", "ая"), ("юю", "яя"), ("ая", "ая"), ("яя", "яя")]}


def is_full_title(subtitle: str) -> bool:
    """Похож ли `subtitle` на полное наименование совещания, а не на подзаголовок («в режиме ВКС»)."""
    return bool(_FULL_TITLE_HEAD.match(" ".join((subtitle or "").split())))


def _case(src: str, word: str) -> str:
    return word.upper() if src.isupper() else word


_HUSHING = set("жшчщгкх")   # после них «-ая», не «-яя»: «рабочей» → «рабочая», но «последней» → «последняя»


def _adj_nominative(word: str, gender: str) -> str:
    low = word.lower()
    for tail, nom in _ADJ_NOM[gender]:
        if low.endswith(tail):
            stem = low[: -len(tail)]
            if nom == "яя" and stem[-1:] in _HUSHING:
                nom = "ая"
            return _case(word, stem + nom)
    return word


def full_title_of(title: str, subtitle: str) -> str:
    """Полное наименование к показу: `subtitle` в именительном с заглавной, иначе `title`.

    «Повестка» передаёт шапку так, как она стоит под словом «ПОВЕСТКА»: «рабочего совещания у
    Заместителя … по вопросам …» — родительный. В именительный переводятся вид мероприятия и
    определения перед ним, остальное в обеих формах одинаково.
    """
    text = " ".join((subtitle or "").split())
    if not is_full_title(text):
        return title
    words = text.split(" ")
    # слово вида мероприятия бывает с дефисом: «совещания-семинара», «встреча-брифинг»
    # (ревьюер RC-2026-09-27-2, К1: без значения по умолчанию здесь падал весь запрос — 500)
    head_at = next((i for i, w in enumerate(words) if w.lower().split("-", 1)[0] in _NOMINATIVE_HEAD), None)
    if head_at is None:
        return title
    head, _, tail = words[head_at].partition("-")
    gender = "fem" if head.lower().startswith("встреч") else "neuter"
    nominative = _NOMINATIVE_HEAD[head.lower()]
    if tail:
        # вторая часть склонялась вместе с первой: «-семинара» → «-семинар», «-совещания» → «-совещание»
        low = tail.lower()
        if low in _NOMINATIVE_HEAD:
            tail = _case(tail, _NOMINATIVE_HEAD[low])
        elif head.lower() != nominative and low.endswith("а") and len(low) > 3:
            tail = tail[:-1]
        nominative += "-" + tail
    out = [_adj_nominative(w, gender) for w in words[:head_at]]
    out.append(_case(head, nominative))
    out += words[head_at + 1:]
    result = " ".join(out)
    return result[:1].upper() + result[1:]


def normalize_date(s: str) -> str:
    """Привести дату к формату ДД.ММ.ГГГГ (напр. 07.09.2026)."""
    s = (s or "").strip()
    if not s:
        return ""
    m = re.match(r"^(\d{1,2})\.(\d{1,2})\.(\d{4})$", s)
    if m:
        d, mo, y = m.groups()
        return f"{int(d):02d}.{int(mo):02d}.{y}"
    m = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})$", s)
    if m:
        y, mo, d = m.groups()
        return f"{int(d):02d}.{int(mo):02d}.{y}"
    m = re.match(r"^(\d{1,2})[\s.]+([а-яё]+)[\s.,]*(\d{4})", s, re.IGNORECASE)
    if m:
        d, mon, y = m.groups()
        mi = _MONTHS.get(mon.lower())
        if mi:
            return f"{int(d):02d}.{mi:02d}.{y}"
    return s


DEADLINE_MAX = 300   # срок словами бывает длинным: «1 декабря 2026 г. с предоставлением промежуточного доклада …»
_DEADLINE_DATE = re.compile(
    r"(?:до\s+)?(?:(\d{1,2})\.(\d{1,2})\.(\d{4})|(\d{4})-(\d{1,2})-(\d{1,2})|(\d{1,2})\s+([а-яё]+)\s+(\d{4}))"
    r"(?:\s*(?:г\.?|года))?", re.IGNORECASE)


def normalize_deadline(s: str) -> str:
    """Срок поручения: строка, которая ЦЕЛИКОМ — дата («25.06.2026», «2026-06-25», «25 июня 2026 г.»,
    «до 25 июня 2026 г.»), → ДД.ММ.ГГГГ (в Word — «Срок – до 25 июня 2026 г.»); иное — текст как есть, пробелы
    схлопнуты («1 декабря 2026 г. с предоставлением промежуточного доклада …» не режется до первой даты:
    ревьюер RC-2026-10-02-20, К-1). Общую `normalize_date` не трогаем — на ней держится дата совещания."""
    text = " ".join((s or "").split())
    m = _DEADLINE_DATE.fullmatch(text)
    if not m:
        return text
    g = m.groups()
    if g[0]:
        d, mo, y = int(g[0]), int(g[1]), g[2]
    elif g[3]:
        y, mo, d = g[3], int(g[4]), int(g[5])
    else:
        mo = _MONTHS.get(g[7].lower())
        if not mo:
            return text
        d, y = int(g[6]), g[8]
    if not (1 <= d <= 31 and 1 <= mo <= 12):
        return text
    return f"{d:02d}.{mo:02d}.{y}"


def _ordinal(num: str) -> str:
    low = (num or "").strip().lower()
    idx = _ROMAN.get(low)
    if idx is None:
        try:
            idx = int(low)
        except (ValueError, TypeError):
            return "№" + num.strip()
    return _ORDINALS.get(idx, "№" + str(idx))


def _is_opening(title: str) -> bool:
    t = (title or "").strip().lower()
    return t in ("вступительное слово", "вступительное слово председателя", "вступительное")


def _is_closing(title: str) -> bool:
    t = (title or "").strip().lower()
    return t in ("подведение итогов", "заключительное слово", "заключительное", "подведение итога")


def _is_discussion(title: str) -> bool:
    """Блок «Обсуждение»: без докладчика, но со временем."""
    t = (title or "").strip().lower()
    return t == "обсуждение" or t.startswith("обсуждение ")


def _name_first(name: str) -> str:
    """«Фамилия Имя Отчество» -> «Имя Отчество Фамилия»."""
    parts = (name or "").split()
    if len(parts) >= 3:
        return " ".join(parts[1:] + parts[:1])
    return name


def _initials(name: str) -> str:
    """«Фамилия Имя Отчество» -> «И.О. Фамилия»."""
    parts = (name or "").split()
    if len(parts) >= 3:
        surname = parts[0]
        inits = "".join(p[0].upper() + "." for p in parts[1:3])
        return inits + " " + surname
    return name


_FEMALE_NAMES = {
    "ольга", "татьяна", "ирина", "елена", "наталья", "наталия", "анна", "мария", "светлана",
    "людмила", "галина", "валентина", "вера", "надежда", "любовь", "оксана", "юлия", "дарья",
    "евгения", "александра", "виктория", "екатерина", "лариса", "нина", "маргарита", "полина",
    "алиса", "валерия", "ксения", "анастасия", "анжела", "дина", "зарина", "айгуль", "гюльнара",
    "алина", "инна", "кристина", "диана", "эльмира", "лера",
}


def _is_female(name: str) -> bool:
    """Определить пол по отчеству, с фолбэком по имени (тюркские/кавказские отчества)."""
    parts = (name or "").split()
    if len(parts) >= 3:
        patr = parts[2].lower()
        if patr.endswith("вна") or patr.endswith("кызы"):
            return True
        if patr.endswith("вич") or patr.endswith("оглы") or patr.endswith("улы"):
            return False
    if len(parts) >= 2:
        return parts[1].lower() in _FEMALE_NAMES
    return False


def _initials_genitive(name: str) -> str:
    """«Фамилия Имя Отчество» -> «И.О. Фамилии» (родительный) для строки «Доклад …».

    Склоняет общий модуль `names_common` — своего склонения у сервиса нет (../CLAUDE.md п. 5а).
    """
    parts = (name or "").split()
    if len(parts) >= 3:
        surname = genitive(name).split()[0]
        inits = "".join(p[0].upper() + "." for p in parts[1:3])
        return inits + " " + surname
    return genitive(name) if parts else name


def normalize_agenda(items: list) -> list:
    """Вступительное/подведение итогов — без номера; вопросы нумеруются 1..N."""
    qnum = 0
    for a in items:
        if _is_opening(a.title) or _is_closing(a.title):
            a.num = ""
        else:
            qnum += 1
            a.num = str(qnum)
    return items


def renumber_agenda(meeting) -> None:
    """Перенумеровать повестку и перевести на новые номера всё, что на них ссылается.

    Поручения, блоки сценария и материалы привязаны к номеру вопроса. Если нумерация
    съехала (вставили вопрос в середину, поменяли местами, удалили), без переноса
    ссылок поручение молча уезжает к чужому вопросу.
    """
    before = [a.num for a in meeting.agenda]
    normalize_agenda(meeting.agenda)
    moved = {}
    for old, a in zip(before, meeting.agenda):
        if old and a.num and old != a.num and old not in moved:
            moved[old] = a.num
    if not moved:
        return
    for o in meeting.orders:
        o.agenda_num = moved.get(o.agenda_num, o.agenda_num)
    for b in meeting.scenario:
        b.agenda_num = moved.get(b.agenda_num, b.agenda_num)
    for mat in meeting.materials:
        mat.question_num = moved.get(mat.question_num, mat.question_num)


def _norm(s: str) -> str:
    """Нормализация для сравнения: нижний регистр + схлопывание пробелов."""
    return " ".join((s or "").strip().lower().split())


def _find_role(participants: list["Participant"], name: str) -> str:
    """Должность по ФИО: сначала точное имя, затем фамилия+имя, затем только фамилия."""
    n = _norm(name)
    if not n:
        return ""
    np = n.split()
    # 1) точное полное имя
    for p in participants:
        if _norm(p.name) == n:
            return p.role
    # 2) фамилия + имя (первые два слова)
    if len(np) >= 2:
        for p in participants:
            pp = _norm(p.name).split()
            if len(pp) >= 2 and pp[0] == np[0] and pp[1] == np[1]:
                return p.role
    # 3) только фамилия (фолбэк)
    sn = np[0]
    for p in participants:
        pp = _norm(p.name).split()
        if pp and pp[0] == sn:
            return p.role
    return ""


_ROLE_GEN = {
    "заместитель": "заместителя", "министр": "министра", "директор": "директора",
    "руководитель": "руководителя", "начальник": "начальника", "статс-секретарь": "статс-секретаря",
    "председатель": "председателя", "помощник": "помощника", "специалист": "специалиста",
    "и.о.": "и.о.", "и.о": "и.о.",
}


def _match_case(src: str, value: str) -> str:
    """Сохраняем заглавную букву должности: Министр -> Министра."""
    if src and src[0].isupper() and value:
        return value[0].upper() + value[1:]
    return value


def _genitive_role(role: str) -> str:
    """Склонение должности в родительный падеж: склоняем заголовочные слова
    (по словарю и типовым окончаниям); тире/дефис отдельным словом не считаем."""
    words = (role or "").split()
    if not words:
        return ""
    out = []
    title_word_count = 0
    for w in words:
        low = w.lower()
        if low in ("–", "—", "-"):
            out.append(w)
            continue
        title_word_count += 1
        # Склоняем только первые два слова собственно должности.
        # Название ведомства дальше не трогаем: иначе «коммуникаций» превращается в «коммуникацего».
        should_decline = title_word_count <= 2
        if low in _ROLE_GEN and should_decline:
            value = _ROLE_GEN[low]
            # Требование: «Министр/Министра» всегда с большой буквы.
            if low == "министр":
                value = value.capitalize()
            out.append(_match_case(w, value))
        elif low == "министр":
            out.append("Министр")
        elif low == "министра":
            out.append("Министра")
        elif should_decline and low.endswith(("тель", "тор", "ник", "арь")):
            out.append(_match_case(w, w + "а"))
        elif should_decline and low.endswith("ый"):
            out.append(_match_case(w, w[:-2] + "ого"))      # Первый -> Первого
        elif should_decline and low.endswith("ий"):
            out.append(_match_case(w, w[:-2] + "его"))      # Старший -> Старшего
        else:
            out.append(w)
    return " ".join(out)


def _parse_time(t: str) -> int:
    m = re.match(r"^(\d{1,2}):(\d{2})$", (t or "").strip())
    if m:
        return int(m.group(1)) * 60 + int(m.group(2))
    return 0


def _fmt_time(minutes: int) -> str:
    minutes = minutes % (24 * 60)
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


@dataclass
class Participant:
    name: str = ""
    role: str = ""
    organization: str = ""
    is_speaker: bool = False
    attendance: str = "in_person"  # in_person | remote | unknown
    protocol_rank: int = 0
    id: str = field(default_factory=new_id)  # стабильный ключ: правка и удаление идут по нему, а не по позиции


@dataclass
class AgendaItem:
    num: str = ""                 # "I", "II", …
    title: str = ""
    time: str = ""                # вычисляемое время (заполняется таймлайном)
    speakers: list[str] = field(default_factory=list)          # ФИО докладчиков (по порядку)
    speaker_roles: list[str] = field(default_factory=list)     # должности выступающих (из повестки, по порядку)
    speaker_minutes: list[int] = field(default_factory=list)   # длительность выступления каждого
    minutes: int = 0                                           # длительность пункта без докладчиков (напр. «Обсуждение»)
    id: str = field(default_factory=new_id)


@dataclass
class Order:
    text: str = ""
    assignee: str = ""
    deadline: str = ""
    agenda_num: str = ""          # к какому вопросу относится (номер)
    id: str = field(default_factory=new_id)
    # из какого материала предложено (окно разбора материала, галочки — решение пользователя 02.10):
    # удаление материала убирает и его поручения; правка материала правит их списком
    material_id: str = ""
    # откуда поручение (вкладка «Поручения», решение пользователя 02.10): "material" — из материала
    # (окно разбора или «Сформировать перечень из материалов»), "file" — из разобранного файла с поручениями,
    # "transcriber" — из «Транскрибатора», "" — вручную или из старых записей. Порядок перечня — порядок
    # списка `Meeting.orders`: он же порядок в Word
    source: str = ""


ORDER_SOURCES = ("material", "file", "transcriber")


@dataclass
class ScenarioBlock:
    section: str = SECTION_OPENING  # opening | question | closing
    agenda_num: str = ""            # для секций-вопросов
    kind: str = "intro"
    text: str = ""
    speaker: str = ""               # для report / comment
    minutes: int = 0                # длительность блока
    material_id: str = ""           # связь с материалом (для каскадного удаления)
    id: str = field(default_factory=new_id)


@dataclass
class GlossaryEntry:
    term: str = ""
    definition: str = ""
    id: str = field(default_factory=new_id)


@dataclass
class Material:
    id: str = field(default_factory=new_id)  # стабильный ключ (правка, удаление, каскад блоков сценария)
    question_num: str = ""       # к какому вопросу (номер)
    type: str = "Материал к вопросу"
    speaker: str = ""            # чей доклад (опционально)
    title: str = ""              # краткое название
    text: str = ""               # содержимое (извлечённый текст)
    filename: str = ""           # имя исходного файла
    summary: str = ""            # саммари (все ключевые цифры/значения)
    intro: str = ""             # подводка к вопросу (1-2 предложения, о чём просим доложить)
    theses: list[str] = field(default_factory=list)  # тезисы
    proposal: str = ""           # предложение (старое поле: вывод + «- » + предложения одной строкой — для сценария и старого кода)
    orders: list[str] = field(default_factory=list)  # поручения (тексты); сами поручения — в Meeting.orders с material_id
    # окно разбора материала (решение пользователя 02.10): диаграммы из цифр текста, вывод, предложения к решению.
    # figures: [{id, title, unit, type: bar_h|bar_v|line|donut, items: [[подпись, число]], source, svg}] —
    # svg рисует сервер (app/charts.py) из items, присланному не верим
    figures: list[dict] = field(default_factory=list)
    conclusion: str = ""         # 1–2 предложения: к чему всё ведёт
    proposals: list[str] = field(default_factory=list)  # предложения к решению (из них — заключительное слово)
    # откуда приехал материал: {"service": "brief", "id": "…", "url": "/brief/#brief=…", "kind": "бриф"}
    # нужен, чтобы из совещания можно было открыть исходный документ, а не искать его руками
    source: dict = field(default_factory=dict)
    # под каким именем исходный файл лёг в пакет совещания (папку «Материалов»): интерфейс кладёт его
    # туда сам после сохранения материала и запоминает имя PATCH-ем — карточка показывает «в пакете: …»
    package: str = ""


# Вкладка «Результаты» (решение пользователя 02.10): документы после совещания из «Транскрибатора».
# Порядок — порядок карточек на вкладке.
RESULT_KINDS = ("transcript", "summary", "decisions")
RESULT_TITLES = {"transcript": "Транскрипция совещания", "summary": "Саммари совещания",
                 "decisions": "Проект протокольных решений"}


@dataclass
class Result:
    id: str = field(default_factory=new_id)
    kind: str = "transcript"     # transcript | summary | decisions
    title: str = ""
    html: str = ""               # очищенный сервером HTML (app/richtext.py)
    text: str = ""               # плоский текст
    docx_url: str = ""           # «Скачать Word»: относительный адрес соседа за порталом (/transcriber/…)
    # откуда: {"service": "transcriber", "id": "<запись>", "url": "/transcriber/#rec=<id>"};
    # повтор с тем же service+id и kind заменяет документ, а не кладёт второй
    source: dict = field(default_factory=dict)
    created: str = ""            # ISO, когда документ пришёл впервые
    updated: str = ""            # ISO, последняя замена


@dataclass
class Meeting:
    id: str = ""
    owner: str = ""  # логин пользователя-владельца ("" — общие/наследие)
    source: dict = field(default_factory=dict)  # откуда приехало: {"service": "agenda", "meeting_id": "…"}
    folder: str = ""     # id папки совещания в сервисе «Материалы» (файлы ведут они, см. ../CLAUDE.md п. 9)
    title: str = ""
    # короткое название, поправленное человеком; пусто — считаем правилом `meeting_title.short_title`
    short_title_manual: str = ""
    subtitle: str = ""
    chair: str = ""
    # должность председателя — только для документов «Повестка»/«Список участников» в Word
    # (решение пользователя 28.09, перенос из сервиса «Повестка», app/agenda_docx.py):
    # «Вступительное слово» печатает председателя строкой ФИО – должность, как любого докладчика
    chair_role: str = ""
    date: str = ""
    location_primary: str = ""
    location_secondary: str = ""
    time_start: str = ""
    time_end: str = ""
    gap_minutes: int = 3
    shared_with: list[str] = field(default_factory=list)  # логины, кому доступно
    agenda: list[AgendaItem] = field(default_factory=list)
    participants: list[Participant] = field(default_factory=list)
    orders: list[Order] = field(default_factory=list)
    scenario: list[ScenarioBlock] = field(default_factory=list)
    glossary: list[GlossaryEntry] = field(default_factory=list)
    materials: list[Material] = field(default_factory=list)
    # «Результаты»: транскрипция, саммари, проект протокольных решений из «Транскрибатора»
    results: list[Result] = field(default_factory=list)
    # порядок участников задан вручную (стрелками ↑/↓) — тогда сервер не
    # пересортировывает список по протокольному старшинству при чтении
    participants_manual: bool = False
    # люди, которых разбор или импорт нашёл, а в реестре их нет или записаны иначе:
    # [{name, role, organization, suggest: new|differs, registry_role, registry_org, document}].
    # В реестр пишет только человек (../CLAUDE.md п. 5а) — окно «Добавить в реестр?» по этой очереди.
    registry_pending: list[dict] = field(default_factory=list)

    @property
    def full_title(self) -> str:
        """Полное наименование для человека и документов.

        `title` — ключ `find-or-create` и источник `short_title`, поэтому полное наименование
        из «Повестки» и из разбора повестки ложится в `subtitle`, а `title` не переписывается
        (ревьюер RC-2026-09-22-1, К2). Но `subtitle` бывает и обычным подзаголовком («в режиме ВКС»):
        тогда полное — по-прежнему `title`. «Повестка» шлёт шапку в родительном и со строчной
        («совещания у …») — приводим к именительному при чтении, данные не переписываем
        (ревьюер RC-2026-09-22-2, К1).
        """
        return full_title_of(self.title, self.subtitle)

    @property
    def subtitle_line(self) -> str:
        """Строка подзаголовка в документах: `subtitle`, если это не полное наименование."""
        line = (self.subtitle or "").strip()
        # подзаголовок, равный названию, — не печатаем дважды (ревьюер RC-22-3, Н1)
        if is_full_title(line) or line.lower() == (self.title or "").strip().lower():
            return ""
        return line

    @property
    def short_title(self) -> str:
        """Тема совещания для списков — одно правило на пространство (../CLAUDE.md п. 9)."""
        from .meeting_title import short_title
        return self.short_title_manual.strip() or short_title(self.title)

    def to_dict(self) -> dict[str, Any]:
        d = _serialize(self)
        d["short_title"] = self.short_title   # соседи и интерфейс показывают его, полное — в подсказке
        d["full_title"] = self.full_title
        d["subtitle_line"] = self.subtitle_line
        from .meeting_title import short_title
        d["short_title_auto"] = short_title(self.title)   # что посчитало правило — подсказка в поле правки
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Meeting":
        m = cls(
            id=d.get("id", ""),
            owner=d.get("owner", ""),
            title=d.get("title", ""),
            subtitle=d.get("subtitle", ""),
            chair=d.get("chair", ""),
            chair_role=d.get("chair_role", ""),
            date=d.get("date", ""),
            location_primary=d.get("location_primary", ""),
            location_secondary=d.get("location_secondary", ""),
            time_start=d.get("time_start", ""),
            time_end=d.get("time_end", ""),
            gap_minutes=int(d.get("gap_minutes", 3) or 3),
        )
        m.source = dict(d.get("source") or {})
        m.folder = str(d.get("folder", "") or "")
        m.short_title_manual = str(d.get("short_title_manual", "") or "")
        m.shared_with = [str(x) for x in d.get("shared_with", []) or []]
        m.agenda = [AgendaItem(**x) for x in d.get("agenda", [])]
        m.participants = [Participant(**x) for x in d.get("participants", [])]
        m.orders = [_known(Order, x) for x in d.get("orders", [])]
        m.scenario = [_known(ScenarioBlock, x) for x in d.get("scenario", [])]
        m.glossary = [_known(GlossaryEntry, x) for x in d.get("glossary", [])]
        m.materials = [_known(Material, x) for x in d.get("materials", [])]
        # совещание до 02.10 «Результатов» не знает — пустой список
        m.results = [_known(Result, x) for x in d.get("results", []) or [] if isinstance(x, dict)]
        m.participants_manual = bool(d.get("participants_manual", False))
        m.registry_pending = [dict(x) for x in d.get("registry_pending", []) or [] if isinstance(x, dict)]
        m.ids_assigned = m._assign_missing_ids(d)
        return m

    def _assign_missing_ids(self, d: dict[str, Any]) -> bool:
        """Записи, сохранённые до появления id, получили его при чтении; дубли — новый.

        True, если что-то поменялось: хранилище сразу запишет это, иначе id
        будут разными при каждом чтении.
        """
        changed = False
        for key in ("agenda", "participants", "orders", "scenario", "glossary", "materials"):
            seen = set()
            for raw, item in zip(d.get(key, []), getattr(self, key)):
                if not raw.get("id") or item.id in seen:
                    if not item.id or item.id in seen:   # у старых материалов в базе лежит id=""
                        item.id = new_id()
                    changed = True
                seen.add(item.id)
        raw_results = [x for x in d.get("results", []) or [] if isinstance(x, dict)]
        seen = set()
        for raw, res in zip(raw_results, self.results):
            if not raw.get("id") or res.id in seen:
                res.id = new_id()
                changed = True
            seen.add(res.id)
        # старые материалы без id: блоки сценария связывались с ними по тексту — привязать по id
        for raw, mat in zip(d.get("materials", []), self.materials):
            if raw.get("id"):
                continue
            for b in self.scenario:
                if b.material_id or b.agenda_num != mat.question_num:
                    continue
                if (b.kind == "reference" and mat.intro and b.text == mat.intro) or \
                        (b.kind == "replica" and mat.proposal and b.text == "Предложение: " + mat.proposal):
                    b.material_id = mat.id
        return changed

    def refresh_speaker_phrases(self) -> int:
        """Пересклонить ФИО в собранных сценарием фразах «прошу доложить …» и «Доклад …».

        Их текст склонялся при сборке сценария, прежним своим правилом («Павел» → «Павела»),
        а после замены опечатки в ФИО устаревает. Фразы, собранные по шаблону, получают ФИО
        заново из поля `speaker` общим склонением. Текст, переписанный руками иначе, не трогаем.
        """
        changed = 0
        for b in self.scenario:
            if not b.speaker or b.kind not in ("intro", "report"):
                continue
            wrapped = re.fullmatch(r"\s*<p>(.*)</p>\s*", b.text or "", re.S)
            body = wrapped.group(1) if wrapped else (b.text or "")
            if "<" in body:
                continue          # размеченный текст — правили руками, не переписываем
            if b.kind == "intro":
                m = re.fullmatch(r"(?s)(.*?прошу (?:также )?доложить )([^,]+?)((?:,.*)?\.?)\s*", body)
                fresh = accusative(b.speaker) if m else ""
            else:
                m = re.fullmatch(r"(?s)(Доклад )([^,]+?)((?:,.*)?\.?)\s*", body)
                fresh = _initials_genitive(b.speaker) if m else ""
            if not m or not fresh or m.group(2).strip().rstrip(".") == fresh.rstrip("."):
                continue
            body = m.group(1) + fresh + m.group(3)
            b.text = f"<p>{body}</p>" if wrapped else body
            changed += 1
        return changed

    def build_scenario(self) -> None:
        """Сформировать структуру сценария по повестке (скелет)."""
        # сохраняем существующие вводное/заключительное слово (текст и минуты),
        # чтобы при перегенерации не терять написанное; по умолчанию 5 мин
        saved = {}
        for b in self.scenario:
            if b.section in (SECTION_OPENING, SECTION_CLOSING):
                saved[b.section] = (b.text, b.minutes)
        opening_text, opening_min = saved.get(SECTION_OPENING, ("", 0))
        closing_text, closing_min = saved.get(SECTION_CLOSING, ("", 0))
        if opening_min <= 0:
            opening_min = 5
        if closing_min <= 0:
            closing_min = 5
        self.scenario.clear()
        self.scenario.append(ScenarioBlock(section=SECTION_OPENING, kind="opening", text=opening_text, minutes=opening_min))
        # первый вопрос: пустой блок «Подводка» не заводим — его кладёт материал (inject_material);
        # у совещаний до 02.10 подводка первого вопроса лежит в тексте вступительного слова
        first_qnum = None
        for a in self.agenda:
            if a.num.strip():
                first_qnum = a.num
                break
        for item in self.agenda:
            if not item.num.strip():
                continue
            if _is_opening(item.title) or _is_closing(item.title):
                continue
            if _is_discussion(item.title):
                continue  # обсуждение: без подводки/докладов/предложения — только время в таймлайне
            ordinal = _ordinal(item.num)
            # блок «Подводка» для второго и последующих вопросов (перед докладами)
            if first_qnum and item.num != first_qnum and not _is_discussion(item.title):
                self.scenario.append(ScenarioBlock(section=SECTION_QUESTION, agenda_num=item.num, kind="reference", text="", minutes=2))
            # подводка к каждому докладчику + его доклад
            for si, sp in enumerate(item.speakers):
                if not sp:
                    continue
                role = (item.speaker_roles[si] if si < len(item.speaker_roles) and item.speaker_roles[si] else "") or _find_role(self.participants, sp)
                acc = accusative(sp)          # склонение — общий модуль, см. ../CLAUDE.md п. 5а
                role_gen = _genitive_role(role)
                if si == 0:
                    intro = f"по {ordinal} вопросу прошу доложить {acc}" + (f", {role_gen}" if role_gen else "") + "."
                else:
                    intro = f"по данному вопросу прошу также доложить {acc}" + (f", {role_gen}" if role_gen else "") + "."
                self.scenario.append(ScenarioBlock(section=SECTION_QUESTION, agenda_num=item.num, kind="intro", text=intro, speaker=sp))
                report = f"Доклад {_initials_genitive(sp)}" + (f", {role_gen}" if role_gen else "") + "."
                self.scenario.append(ScenarioBlock(section=SECTION_QUESTION, agenda_num=item.num, kind="report", text=report, speaker=sp))
            # блок «Предложение» по итогам каждого вопроса (заполняется из материалов)
            self.scenario.append(ScenarioBlock(section=SECTION_QUESTION, agenda_num=item.num, kind="replica", text="Предложение: ", minutes=1))
        self.scenario.append(ScenarioBlock(section=SECTION_CLOSING, kind="closing", text=closing_text, minutes=closing_min))
        # вплести материалы (справки/тезисы и предложения) в сценарий
        for mat in self.materials:
            inject_material(self, mat)

    def timeline(self) -> list[dict]:
        """Полный таймлайн: [{label, kind, speaker, minutes, start, end, group}]."""
        if not self.time_start:
            return []
        cur = _parse_time(self.time_start)
        blocks: list[dict] = []

        def push(label: str, kind: str, speaker: str, minutes: int, group: str = "") -> None:
            nonlocal cur
            if minutes <= 0:
                return
            end = cur + minutes
            blocks.append({"label": label, "kind": kind, "speaker": speaker, "minutes": minutes,
                           "start": _fmt_time(cur), "end": _fmt_time(end), "group": group})
            cur = end

        for b in self.scenario:
            if b.section == SECTION_OPENING:
                push("Вступительное слово", "opening", "", b.minutes, "opening")
        for item in self.agenda:
            if not item.num.strip():
                continue
            grp = f"question-{item.num}"
            if _is_discussion(item.title):
                push(f"Обсуждение: {item.title}", "comment", "", item.minutes, grp)
                continue
            # подводка к вопросу; у первого — только если её положил материал (сценарий пустую не заводит)
            for b in self.scenario:
                if b.section == SECTION_QUESTION and b.agenda_num == item.num and b.kind == "reference":
                    push(f"Подводка к вопросу {item.num}", "reference", b.speaker, b.minutes, grp)
            # доклады (передача слова — без времени, не показываем)
            for i, sp in enumerate(item.speakers):
                minutes = item.speaker_minutes[i] if i < len(item.speaker_minutes) else 0
                if minutes > 0:
                    push(f"Доклад: {sp}", "report", sp, minutes, grp)
                else:
                    blocks.append({"label": f"Доклад: {sp}", "kind": "report", "speaker": sp,
                                   "minutes": 0, "start": "", "end": "", "group": grp})
            # итоговое слово (предложение не показываем — оставляем только комментарии)
            for b in self.scenario:
                if b.section == SECTION_QUESTION and b.agenda_num == item.num and b.kind == "replica":
                    if b.text.startswith("Предложение:"):
                        continue
                    push(f"Итоговое слово (вопрос {item.num})", "replica", b.speaker, b.minutes, grp)
            push("Комментарии", "comment", "", self.gap_minutes, grp)
        for b in self.scenario:
            if b.section == SECTION_CLOSING:
                push("Заключительное слово", "closing", "", b.minutes, "closing")

        return blocks

    def fill_agenda_times(self) -> None:
        """Заполнить поле time вопросов из таймлайна (HH:MM–HH:MM)."""
        if not self.time_start:
            return
        blocks = self.timeline()
        spans: dict[str, list[str]] = {}
        for b in blocks:
            g = b.get("group", "")
            if not g.startswith("question-") or not b.get("start"):
                continue
            spans.setdefault(g, [b["start"], b["end"]])
            spans[g][0] = min(spans[g][0], b["start"])
            spans[g][1] = max(spans[g][1], b["end"])
        for a in self.agenda:
            if a.num.strip():
                sp = spans.get(f"question-{a.num}")
                if sp:
                    a.time = f"{sp[0]}–{sp[1]}"

    def block_times(self) -> dict:
        """{индекс блока сценария: 'start-end'} — время блоков с ненулевой длительностью."""
        if not self.time_start:
            return {}
        cur = _parse_time(self.time_start)
        result = {}

        def fmt(start, minutes):
            return f"{_fmt_time(start)}-{_fmt_time(start + minutes)}"

        for i, b in enumerate(self.scenario):
            if b.section == SECTION_OPENING and b.minutes > 0:
                result[i] = fmt(cur, b.minutes)
                cur += b.minutes
        report_indices = [i for i, b in enumerate(self.scenario) if b.section == SECTION_QUESTION and b.kind == "report"]
        ri = 0
        for item in self.agenda:
            if not item.num.strip():
                continue
            for i, b in enumerate(self.scenario):
                if b.section == SECTION_QUESTION and b.agenda_num == item.num and b.kind == "intro" and b.minutes > 0:
                    result[i] = fmt(cur, b.minutes)
                    cur += b.minutes
            for si, sp in enumerate(item.speakers):
                minutes = item.speaker_minutes[si] if si < len(item.speaker_minutes) else 0
                if minutes > 0:
                    if ri < len(report_indices):
                        result[report_indices[ri]] = fmt(cur, minutes)
                    cur += minutes
                ri += 1
            for i, b in enumerate(self.scenario):
                if b.section == SECTION_QUESTION and b.agenda_num == item.num and b.kind not in ("intro", "report") and b.minutes > 0:
                    result[i] = fmt(cur, b.minutes)
                    cur += b.minutes
            # комментарии после вопроса
            if self.gap_minutes > 0:
                cur += self.gap_minutes
        for i, b in enumerate(self.scenario):
            if b.section == SECTION_CLOSING and b.minutes > 0:
                result[i] = fmt(cur, b.minutes)
                cur += b.minutes
        return result


def _known(cls, raw: dict):
    """Запись из базы → объект: поля, которых этот код не знает (запись новой версии после отката),
    пропускаем, а не падаем; отсутствующие берут значения по умолчанию (старые записи)."""
    names = {f.name for f in fields(cls)}
    return cls(**{k: v for k, v in (raw or {}).items() if k in names})


def _serialize(x: Any) -> Any:
    if is_dataclass(x):
        return {f.name: _serialize(getattr(x, f.name)) for f in fields(x)}
    if isinstance(x, list):
        return [_serialize(i) for i in x]
    return x



def _first_qnum(m: "Meeting") -> str | None:
    return next((a.num for a in m.agenda if a.num.strip()), None)


def _flat(text: str) -> str:
    """Текст для сравнения: без разметки, регистра, лишних пробелов и точки в конце."""
    return " ".join(html_to_text(text or "").lower().replace("ё", "е").split()).rstrip(" .")


def _intro_in_opening(m: "Meeting", intro: str) -> bool:
    needle = _flat(intro)
    return bool(needle) and any(b.section == SECTION_OPENING and needle in _flat(b.text) for b in m.scenario)


def inject_material(m: "Meeting", material: "Material") -> None:
    """Вплести материал в сценарий: подводка/тезисы — в вопрос, предложение — в итог.

    Подводка у всех вопросов, и у первого, — в блок «Подводка» (`reference`) этого вопроса.
    """
    qnum = material.question_num
    if not qnum:
        return
    q_indices = [i for i, b in enumerate(m.scenario)
                 if b.section == SECTION_QUESTION and b.agenda_num == qnum]
    if not q_indices:
        return  # вопроса ещё нет в сценарии — появится при генерации
    # предложение — в конец вопроса (заполнить существующий блок «Предложение»)
    if material.proposal:
        filled = False
        for b in m.scenario:
            if b.section == SECTION_QUESTION and b.agenda_num == qnum and b.kind == "replica" and b.text.strip() == "Предложение:":
                b.text = "Предложение: " + material.proposal
                b.material_id = material.id
                filled = True
                break
        if not filled:
            m.scenario.insert(q_indices[-1] + 1, ScenarioBlock(
                section=SECTION_QUESTION, agenda_num=qnum, kind="replica",
                text="Предложение: " + material.proposal, minutes=1,
                material_id=material.id,
            ))
    # подводка к вопросу (короткая, 1-2 предложения) — у всех вопросов одинаково, в свой блок «Подводка».
    # Раньше подводка первого вопроса дописывалась в блок opening; с 02.10 вступительное слово собирает
    # /opening из материалов, и дописанная подводка ломала его список (ревьюер RC-2026-10-02-15, Н-2)
    # У совещаний до 02.10 подводка первого вопроса уже лежит в тексте вступительного слова (его сохраняет
    # build_scenario) — второй раз блоком не кладём: задвоение и +2 минуты (ревьюер RC-2026-10-02-18, Н-1)
    if material.intro and qnum == _first_qnum(m) and _intro_in_opening(m, material.intro):
        return
    if material.intro:
        ref_idx = None
        for i, b in enumerate(m.scenario):
            if b.section == SECTION_QUESTION and b.agenda_num == qnum and b.kind == "reference" and not b.text:
                ref_idx = i
                break
        if ref_idx is not None:
            m.scenario[ref_idx].text = material.intro
            m.scenario[ref_idx].material_id = material.id
        else:
            m.scenario.insert(q_indices[0], ScenarioBlock(
                section=SECTION_QUESTION, agenda_num=qnum, kind="reference",
                text=material.intro, minutes=2,
                material_id=material.id,
            ))



_HTML_TAG_RE = __import__("re").compile(r"<[^>]+>")


def html_to_text(s: str) -> str:
    """Rich-текст сценария (HTML) → простой текст для экспорта."""
    if not s:
        return ""
    t = s.replace("<br>", "\n").replace("<br/>", "\n").replace("<br />", "\n")
    t = __import__("re").sub(r"</(p|div|li|h[1-6])>", "\n", t)
    t = _HTML_TAG_RE.sub("", t)
    return t
