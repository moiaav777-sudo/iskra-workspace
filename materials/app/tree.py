"""Дерево хранилища на диске.

    Хранилище/2026 год/10. Октябрь/14.10.2026 Совещание по детям-сиротам/

Год, месяц, дата с названием — и всё. Дата в имени папки есть всегда: у совещания
это день, когда собираются, у быстрого пакета — день сборки. Её можно поменять,
и тогда папка переезжает в свой месяц: иначе дерево врёт.

В браузере это дерево не показывается: там просто список мероприятий с поиском.
Единственный источник правды — диск; база рядом только кэширует текст документов.
"""
from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from . import naming

MONTHS = ["Январь", "Февраль", "Март", "Апрель", "Май", "Июнь",
          "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь"]
DATE_RE = re.compile(r"^(\d{2})\.(\d{2})\.(\d{4})[ _](.+)$")
YEAR_RE = re.compile(r"^(\d{4}) год$")


def root() -> Path:
    """Корень хранилища. На ПК — <папка на ПК>\materials\\Хранилище.

    Отдельная переменная, а не MATERIALS_HOME: ею портал называет папку самого
    сервиса (`SERVICE_RUNTIME` в `portal/app/config.py`), и путать их нельзя.
    """
    base = os.environ.get("MATERIALS_STORE") or (Path(__file__).resolve().parent.parent / "Хранилище")
    path = Path(base)
    path.mkdir(parents=True, exist_ok=True)
    return path


def inside(path: Path) -> bool:
    """Защита от «..» в названии: наружу из хранилища не выходим."""
    try:
        return path.resolve().is_relative_to(root().resolve())
    except (OSError, ValueError):
        return False


def folder_name(when: date, title: str) -> str:
    return f"{when.strftime('%d.%m.%Y')} {naming.safe_title(title) or 'Мероприятие'}"


def relative(when: date, title: str) -> Path:
    return (Path(f"{when.year} год") / f"{when.month:02d}. {MONTHS[when.month - 1]}"
            / folder_name(when, title))


def event_id(rel: Path) -> str:
    """Ключ мероприятия — из пути: папка переименована человеком значит и карточка новая.

    Прятать в папке служебный файл с номером не стали: папка должна оставаться
    обычной папкой с документами, которую не жалко открыть в проводнике.
    """
    return hashlib.sha1(str(rel).replace("\\", "/").encode("utf-8")).hexdigest()[:12]


@dataclass
class Event:
    id: str
    rel: Path
    date: str          # ДД.ММ.ГГГГ
    title: str

    @property
    def path(self) -> Path:
        return root() / self.rel

    @property
    def iso(self) -> str:
        d, m, y = self.date.split(".")
        return f"{y}-{m}-{d}"

    def to_dict(self) -> dict:
        return {"id": self.id, "date": self.date, "title": self.title,
                "folder": str(self.rel).replace("\\", "/")}


def from_rel(rel: Path) -> Event | None:
    """Мероприятие по относительному пути его папки: год / месяц / дата название."""
    parts = rel.parts
    if len(parts) < 3 or not YEAR_RE.match(parts[-3]):
        return None
    m = DATE_RE.match(parts[-1])
    if not m:
        return None
    day, month, year, title = m.groups()
    return Event(id=event_id(rel), rel=rel, date=f"{day}.{month}.{year}", title=title.strip())


def events() -> list[Event]:
    """Все мероприятия хранилища, свежие сверху. Обходим только нужную глубину."""
    found: list[Event] = []
    base = root()
    for year in base.iterdir():
        if not year.is_dir() or not YEAR_RE.match(year.name):
            continue
        for month in year.iterdir():
            if not month.is_dir():
                continue
            for folder in month.iterdir():
                if not folder.is_dir():
                    continue
                event = from_rel(folder.relative_to(base))
                if event:
                    found.append(event)
    found.sort(key=lambda e: (e.iso, e.title), reverse=True)
    return found


def years() -> list[str]:
    return sorted((m.group(1) for m in (YEAR_RE.match(p.name) for p in root().iterdir()
                                        if p.is_dir()) if m), reverse=True)


def find(eid: str) -> Event | None:
    for event in events():
        if event.id == eid:
            return event
    return None


def create(when: date, title: str) -> Event:
    rel = relative(when, title)
    (root() / rel).mkdir(parents=True, exist_ok=True)
    event = from_rel(rel)
    if event is None:  # название начиналось с чего-то вроде даты и путь не разобрался
        raise ValueError("Не удалось создать папку мероприятия")
    return event


DROPPED = "Убранное"


def dropped_dir() -> Path:
    """Куда уезжает убранное мероприятие. Лежит рядом с годами и в список не попадает:
    обход ищет только папки вида «2026 год»."""
    folder = root() / DROPPED
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def prune(path: Path) -> None:
    """Убрать опустевшие папки месяца и года после переезда мероприятия.

    Иначе в дереве остаются пустые «10. Октябрь» и «2026 год», и человек,
    открыв хранилище в проводнике, видит месяцы, в которых ничего не было.
    """
    base = root().resolve()
    while inside(path) and path.resolve() != base:
        try:
            next(path.iterdir())
            return                      # не пусто — дальше не идём
        except StopIteration:
            pass
        except OSError:
            return
        parent = path.parent
        try:
            path.rmdir()
        except OSError:
            return
        path = parent


def archive_dir(event: Event) -> Path:
    """«Архив» — первичка: документы ровно в том виде и под теми именами, в каких пришли."""
    path = event.path / "Архив"
    path.mkdir(exist_ok=True)
    return path


# Предел длины пути. Windows без особой настройки не пишет путь длиннее 260 знаков,
# а сервис на боевом живёт именно там. Название совещания попадает в путь дважды —
# в имя папки и в имя файла («Саммари_<название совещания>.docx»), — и при названии
# в 87 знаков путь первички выходил в 261: запись падала, «Бриф» получал 500.
# Запас в 20 знаков — на папку «~перекладка~/» при перестановке.
MAX_PATH = 240


def fit(path: Path) -> Path:
    """Укоротить имя файла так, чтобы весь путь уложился в предел Windows,
    а само имя — в предел Linux (255 байт, кириллица — два байта на букву).

    Режем имя, а не расширение: без расширения документ не откроется. Первичка
    от этого теряет хвост имени — но только когда иначе её не записать вовсе.
    """
    stem, ext = naming.split_ext(path.name)
    tail = f".{ext}" if ext else ""
    over = len(str(path)) - MAX_PATH
    if over > 0:
        stem = stem[:max(12, len(stem) - over)].rstrip(" .")
    while len(f"{stem}{tail}".encode("utf-8")) > naming.MAX_NAME_BYTES and len(stem) > 12:
        stem = stem[:-1].rstrip(" .")
    name = stem + tail
    return path if name == path.name else path.with_name(name)


def unique(path: Path) -> Path:
    """Свободное имя рядом: «Справка (2).docx», если «Справка.docx» уже лежит."""
    path = fit(path)
    if not path.exists():
        return path
    stem, ext = naming.split_ext(path.name)
    tail = f".{ext}" if ext else ""
    for n in range(2, 1000):
        candidate = path.with_name(f"{stem} ({n})){tail}".replace("))", ")"))
        if not candidate.exists():
            return candidate
    # столько одинаковых имён в одной папке не бывает, но падать наружу нельзя
    return path.with_name(f"{stem} ({os.urandom(4).hex()}){tail}")
