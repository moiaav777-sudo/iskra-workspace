"""Правила имени файла в папке мероприятия.

Имя файла — это и есть данные: номер, название, к какому вопросу материал. Никаких
скрытых пометок в папке нет. Поэтому папку можно открыть в проводнике и всё понять
без сервиса, а сервис в любой момент собирает карточку заново по тому, что лежит на диске.

    05. Информация по докладу Минстроя России (к вопросу 1).docx
    13.1. Предложения Минстроя России (к вопросу 3).docx

Порядок номеров взят из папок, которые человек вёл руками: 00 — реестр, 01 — повестка,
02 — список участников, 03 — модератор, 04 — бриф, с 05 — материалы по вопросам.
Номера 01–04 закреплены за своими документами (решение пользователя 07.10: «Модератор
всегда должен вставать п. 3») — нет повестки, модератор всё равно 03.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import unquote

PERCENT = re.compile(r"%[0-9A-Fa-f]{2}")

# Реестр собирает сам сервис, он всегда нулевой и в старшинстве не участвует.
REGISTRY = "00. Реестр материалов.docx"

# Порядок старшинства в пакете, и у каждой ступени свой закреплённый номер (решение
# пользователя 07.10: «Модератор всегда должен вставать п. 3»). Номер не зависит от того,
# что ещё лежит в папке: есть только модератор — он 03, а 01, 02 и 04 ждут своих документов.
# Пришла повестка позже — встала на 01, и ни один номер материалов не сдвинулся.
ROLES = [
    (1, "Повестка", ("повестка",)),
    (2, "Список участников", ("список участников",)),
    (3, "Модератор", ("модератор",)),
    (4, "Бриф", ("бриф",)),
]
ROLE_TITLES = {num: title for num, title, _ in ROLES}
ROLE_COMMON = 5      # общий материал: к конкретному вопросу не привязан
ROLE_QUESTION = 6    # материал к вопросу повестки
# С этого номера идут материалы — общие и к вопросам — подряд, без пропусков.
# Туда же уходит второй документ одной ступени (второй модератор): закреплённый номер один.
FIRST_MATERIAL = len(ROLES) + 1

NUMBERED = re.compile(r"^(\d{1,3})(?:\.(\d{1,3}))?\.\s*(.+)$")
QUESTION = re.compile(r"\s*\(к вопросу (\d{1,2})\)\s*$")
# Windows не разрешает эти знаки в имени файла, а ещё имя не может кончаться точкой или пробелом.
FORBIDDEN = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
# Предел длины названия — не способ сокращать, а страховка: в Windows путь целиком
# не длиннее 260 знаков, а до имени файла уже израсходована папка мероприятия.
# Сокращает название по смыслу `wording.py`, здесь только последняя защита.
MAX_TITLE = 120
# Имя файла на сервере (Linux) не длиннее 255 байт, а кириллица — два байта на букву:
# 120 знаков названия с номером и «(к вопросу 12)» выходят за предел, и переименование
# падает («File name too long»). Запас до 255 — на « (2)» от `tree.unique`.
MAX_NAME_BYTES = 245
# Служебные слова: на них название обрываться не должно — «…на 550 мест по адресу»
# оставляет оборот без конца, а «…на 550 мест» читается как законченная мысль.
STOP_WORDS = {"по", "при", "для", "над", "под", "из", "от", "до", "на", "в", "во", "с", "со",
              "к", "ко", "о", "об", "обо", "за", "у", "и", "а", "но", "или", "что", "как", "же"}


@dataclass
class Parsed:
    """Разобранное имя файла."""
    num: int | None = None       # None — файл ещё не разложен
    sub: int | None = None       # 13.1 — приложение к материалу 13
    title: str = ""
    question: int | None = None
    ext: str = ""

    @property
    def number(self) -> str:
        if self.num is None:
            return ""
        return f"{self.num:02d}" + (f".{self.sub}" if self.sub else "")

    @property
    def sort_key(self) -> tuple:
        return (self.num if self.num is not None else 99, self.sub or 0, self.title.lower())


def split_ext(filename: str) -> tuple[str, str]:
    stem, dot, ext = filename.rpartition(".")
    if not dot or len(ext) > 5 or "/" in ext:
        return filename, ""
    return stem, ext.lower()


def parse(filename: str) -> Parsed:
    stem, ext = split_ext(filename)
    m = NUMBERED.match(stem)
    num = sub = None
    rest = stem
    if m:
        num = int(m.group(1))
        sub = int(m.group(2)) if m.group(2) else None
        rest = m.group(3)
    question = None
    q = QUESTION.search(rest)
    if q:
        question = int(q.group(1))
        rest = rest[:q.start()]
    return Parsed(num=num, sub=sub, title=rest.strip(), question=question, ext=ext)


def shorten(title: str, limit: int = MAX_TITLE) -> str:
    """Укоротить название, не разорвав слово.

    Обрезка по счётчику знаков даёт «…по адресу г. Ульяновск, бульва» — читается
    как опечатка. Поэтому режем по концу оборота: по запятой, тире или скобке,
    а если их нет — по последнему пробелу, сняв повисший предлог.

    Многоточия в конце не ставим: фраза и так выходит законченной, а точки в имени
    файла читаются как ошибка, а не как знак того, что название длиннее.
    """
    title = (title or "").strip()
    if len(title) <= limit:
        return title
    # Лучший разрез — там, где кончается оборот: по запятой, тире или скобке.
    # «Справка о развитии Ямало-Ненецкого автономного округа, подготовленная…»
    # превращается в «Справка о развитии Ямало-Ненецкого автономного округа».
    head = title[:limit]
    clause = max(head.rfind(", "), head.rfind("; "), head.rfind(" — "),
                 head.rfind(" – "), head.rfind(" ("))
    if clause >= limit * 0.3:
        return head[:clause].rstrip(" ,;:-–—(")
    cut = title[:limit - 1]
    space = cut.rfind(" ")
    if space >= limit // 2:
        cut = cut[:space]
    cut = cut.rstrip(" ,.;:-–—(«\"")
    words = cut.split(" ")
    # Хвост вроде «… по адресу г» — оборот, оборванный на полуслове. Сначала
    # снимаем огрызки («г», «ул», «по»), затем — предлог вместе с его словом.
    # Дальше в название не лезем: «и» между двумя словами названия резать нельзя.
    while len(words) > 2 and len(words[-1].strip(".,")) <= 2:
        words = words[:-1]
    if len(words) > 2 and words[-2].lower().strip(".,") in STOP_WORDS:
        words = words[:-2]
    return " ".join(words).rstrip(" ,.;:-–—")


def quotes(text: str) -> str:
    """Прямые кавычки — в ёлочки: в имени файла Windows прямые запрещены,
    а выбрасывать их жалко — «Макет экрана "Стоимость обучения"» без них разваливается."""
    out, opening = [], True
    for ch in text or "":
        if ch == '"':
            out.append("«" if opening else "»")
            opening = not opening
        else:
            out.append(ch)
    return "".join(out)


def looks_human(stem: str) -> bool:
    """Имя файла уже похоже на название? Тогда лучше него ничего не придумаешь.

    Человек, который сам назвал файл «Проект постановления Правительства РФ
    о проведении эксперимента», объяснил документ лучше, чем его первая строка.
    А «Инф_справка_ТН_2026 РФ» — это не название, а пометка для себя: подчёркивания
    вместо пробелов, сокращения, номер года.
    """
    if "_" in stem:
        return False
    words = stem.split()
    if len(words) < 3:
        return False
    return sum(1 for w in words if len(w) >= 4 and w.isalpha()) >= 3


def safe_title(title: str) -> str:
    """Название, пригодное для имени файла в Windows."""
    title = FORBIDDEN.sub(" ", quotes(title or "").replace("\n", " "))
    title = re.sub(r"\s+", " ", title).strip(" .")
    return shorten(title).strip(" .")


def safe_file(filename: str) -> str:
    """Имя файла-первички: как прислали, только без знаков, запрещённых в Windows.

    Первичку не сокращаем и не причёсываем: её ценность ровно в том, что она
    лежит нетронутой — по ней сверяют, что именно пришло и от кого.
    """
    name = FORBIDDEN.sub(" ", (filename or "").replace("\n", " "))
    name = re.sub(r"\s+", " ", name).strip(" .")
    return name[:150].strip(" .") or "документ"


def compose(num: int | None, sub: int | None, title: str, question: int | None, ext: str) -> str:
    """Имя файла по правилам. Суффикс «(к вопросу N)» ставится, только если вопрос указан.

    Имя целиком укладывается в `MAX_NAME_BYTES`: длинное название укорачивается по смыслу
    (`shorten`), а номер, вопрос и расширение остаются целыми.
    """
    title = safe_title(title) or "Без названия"
    tail = f" (к вопросу {question})" if question else ""
    head = ""
    if num is not None:
        head = f"{num:02d}" + (f".{sub}" if sub else "") + ". "
    dot = f".{ext}" if ext else ""
    room = MAX_NAME_BYTES - len(f"{head}{tail}{dot}".encode("utf-8"))
    limit = len(title)
    while len(title.encode("utf-8")) > room and limit > 8:
        limit -= 2
        title = shorten(title, limit).strip(" .") or "Без названия"
    return f"{head}{title}{tail}{dot}"


def from_filename(filename: str) -> str:
    """Запасное название, когда в документе не за что зацепиться.

    Имена приезжают из почты как есть: «Лекция_МФМ_блок_2.1_v7.docx»,
    «Инф_справка_ТН_2026 РФ.docx». Убираем подчёркивания и технические хвосты.
    """
    stem, _ = split_ext(filename)
    stem = parse(filename).title or stem
    if PERCENT.search(stem):            # имя приехало из интернета: «%22» вместо кавычки
        stem = unquote(stem)
    stem = quotes(stem)
    stem = stem.replace("_", " ").replace("  ", " ")
    stem = re.sub(r"\s*\(\d+\)\s*$", "", stem)                 # «(2)» — второй скачанный файл
    stem = re.sub(r"[ _-]*v\d+(\.\d+)?\s*$", "", stem, flags=re.I)   # «v7», «v2.1»
    stem = re.sub(r"\s*[-–—]?\s*копия\s*$", "", stem, flags=re.I)
    stem = re.sub(r"\s+", " ", stem).strip(" .-_")
    if stem and stem[0].islower():
        stem = stem[0].upper() + stem[1:]
    return stem or "Без названия"


NUMBER_HEAD = re.compile(r"^\d{1,3}(?:\.\d{1,3})?\.\s*")


def opens_with(text: str, genres) -> object | None:
    """Первый жанр, с которого начинается хоть одна строка подсказки.

    Именно «начинается», а не «встречается»: справка, у которой в тексте попалось
    слово «бриф», не должна лезть в начало пакета, а «Информация об участниках
    долевого строительства» — превращаться в «Список участников».
    """
    for line in (text or "").splitlines():
        low = NUMBER_HEAD.sub("", line.strip()).lower().lstrip("«\"'(")
        for key, hints in genres:
            for hint in hints:
                if low.startswith(hint) and (len(low) == len(hint)
                                             or not low[len(hint)].isalpha()):
                    return key
    return None


def role_of(title: str) -> int | None:
    """Ступень старшинства по названию документа, если она узнаётся.

    Повестку, список участников, модератора и бриф человек кладёт в начало пакета
    всегда — по названию их видно, откуда бы они ни пришли.
    """
    return opens_with(title, [(num, hints) for num, _, hints in ROLES])
