"""Папка мероприятия: что в ней лежит и в каком порядке.

Карточка всегда собирается заново по диску. Человек мог положить файл мимо портала —
через сетевой диск или прямо на ПК, — и сервис обязан это увидеть.

Номер документа — это его место в пакете, а не метка. Порядок такой:

    00  реестр — собирает сервис
    01  повестка
    02  список участников
    03  модератор
    04  бриф
    05… общие материалы — те, что не привязаны к вопросу
    …   материалы по вопросам, по возрастанию номера вопроса

Номера 01–04 закреплены за своими документами (решение пользователя 07.10: «Модератор
всегда должен вставать п. 3»): нет повестки — модератор всё равно 03, а 01 пустует.
Материалы идут с 05 подряд, без пропусков. Любое изменение в папке пересчитывает
нумерацию целиком; решение пользователя от 20.09 — делать это молча, не спрашивая.
"""
from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass, field
from pathlib import Path

from . import naming, store, tree

log = logging.getLogger("materials.folder")


# Папка, где файлы лежат между двумя проходами переименования, — под своими же именами.
# Раньше это была приставка к имени («~перекладка~05. …»), и длинное имя с ней выходило
# за 255 байт: первый проход падал, и пересчёт целиком откатывался — молча, раз за разом,
# а новый документ оставался без номера (07.10, «Модератор_И61.docx»). Имя файла
# с приставкой — остаток старого способа: документом он по-прежнему не считается.
STAGING = "~перекладка~"

# Пересчёт папки — один за раз. Два одновременных (загрузка и правка названия) переставляли
# одни и те же файлы: второй не находил файл, который уже унёс первый, и откатывал всё.
_locks: dict[str, threading.RLock] = {}
_locks_guard = threading.Lock()


def _lock(event: tree.Event) -> threading.RLock:
    with _locks_guard:
        return _locks.setdefault(str(event.path), threading.RLock())


@dataclass
class Item:
    name: str
    parsed: naming.Parsed
    size: int
    mtime: float

    def to_dict(self) -> dict:
        p = self.parsed
        return {"name": self.name, "number": p.number, "num": p.num, "sub": p.sub,
                "title": p.title, "question": p.question, "ext": p.ext, "size": self.size,
                "mtime": self.mtime}


def items(event: tree.Event) -> list[Item]:
    """Файлы в папке мероприятия. Подпапки («Архив» с первичкой) не трогаем."""
    found: list[Item] = []
    folder = event.path
    if not folder.is_dir():
        return found
    for entry in os.scandir(folder):
        if not entry.is_file() or entry.name.startswith((".", "~$", STAGING)):
            continue
        stat = entry.stat()
        found.append(Item(name=entry.name, parsed=naming.parse(entry.name),
                          size=stat.st_size, mtime=stat.st_mtime))
    found.sort(key=lambda i: i.parsed.sort_key)
    return found


def documents(all_items: list[Item]) -> list[Item]:
    """Всё, кроме реестра: реестр — отчёт о папке, а не документ пакета."""
    return [i for i in all_items if i.name != naming.REGISTRY]


def state(event: tree.Event, all_items: list[Item]) -> dict:
    """Что в папке есть. Чего в ней нет — не наше дело: пакеты бывают разные."""
    present = {role(event, i) for i in documents(all_items)}
    return {"count": len(documents(all_items)),
            "have": [title for num, title, _ in naming.ROLES if num in present]}


def questions(all_items: list[Item]) -> list[int]:
    return sorted({i.parsed.question for i in all_items if i.parsed.question})


# ── порядок и нумерация ──────────────────────────────────────────────────

def role(event: tree.Event, item: Item) -> int:
    """Ступень старшинства документа: 1–4 — повестка, участники, модератор, бриф.

    Смотрим по очереди на три вещи, и этого хватает почти всегда:

    1. название — «01. Повестка.docx», тут всё очевидно;
    2. имя файла, под которым документ прислали, — «Бриф_ЯНАО.docx»: название могло
       взяться из содержимого и слова «бриф» в себе не нести;
    3. первые строки самого документа — там обычно и написано «ПОВЕСТКА».

    Без второго и третьего повестка, начинающаяся с первого вопроса, и бриф, у которого
    на первой странице шапка письма, остаются обычными материалами в хвосте пакета.
    """
    known = naming.role_of(item.parsed.title)
    if known:
        return known
    try:
        about = store.about(event.path / item.name)
    except Exception:                   # noqa: BLE001 — кэш занят или файл не читается
        # из-за одного непрочитанного файла пересчёт всей папки падать не должен
        log.exception("не прочитали «%s» для старшинства", item.name)
        about = {"text": "", "origin": ""}
    head = "\n".join((about["text"] or "").splitlines()[:3])
    known = naming.role_of(about["origin"]) or naming.role_of(head)
    if known:
        return known
    return naming.ROLE_QUESTION if item.parsed.question else naming.ROLE_COMMON


@dataclass
class Chain:
    """Документ вместе со своими приложениями: 05 и 05.1, 05.2."""
    head: Item
    subs: list[Item] = field(default_factory=list)


def chains(all_items: list[Item]) -> list[Chain]:
    out: list[Chain] = []
    current: Chain | None = None
    for item in documents(all_items):
        if (item.parsed.sub and current is not None
                and item.parsed.num is not None
                and item.parsed.num == current.head.parsed.num):
            current.subs.append(item)
            continue
        current = Chain(head=item)
        out.append(current)
    return out


@dataclass
class Move:
    src: str
    dst: str

    def to_dict(self) -> dict:
        return {"src": self.src, "dst": self.dst}


def _title_of(event: tree.Event, item: Item) -> str:
    """Название документа. У файла без номера его ещё нет — берём из самого документа."""
    if item.parsed.num is not None:
        return item.parsed.title
    try:
        cached = store.cached(event.path / item.name)["title"]
    except Exception:                   # noqa: BLE001 — без названия из текста обойдёмся
        log.exception("не прочитали «%s» для названия", item.name)
        cached = ""
    return naming.safe_title(cached) or naming.from_filename(item.name)


def plan_numbering(event: tree.Event, order: list[str] | None = None) -> list[Move]:
    """План нумерации всей папки: «было → стало» целиком, до единой правки на диске.

    Повестка, список участников, модератор и бриф получают свои закреплённые номера
    01–04, сколько бы их ни было в папке; материалы — с 05 подряд. Если документов одной
    ступени два (так быть не должно: второй становится новой редакцией первого), первый
    занимает закреплённый номер, а второй уходит к материалам.

    `order` — порядок, который человек задал руками, перетаскиванием или стрелками.
    Он решает только внутри одной ступени: материал к вопросу 3 не встанет впереди
    материала к вопросу 1, как его ни тащи, — нумерация идёт по повестке.
    """
    where = {name: i for i, name in enumerate(order or [])}
    links = chains(items(event))
    steps = {chain.head.name: role(event, chain.head) for chain in links}
    fixed = {num for num, _, _ in naming.ROLES}

    def key(chain: Chain) -> tuple:
        p = chain.head.parsed
        placed = where.get(chain.head.name)
        if placed is not None:
            where_now = (0, float(placed))          # человек переставил руками
        elif p.num is not None:
            where_now = (1, float(p.num))           # уже в пакете — держим место
        else:
            where_now = (2, chain.head.mtime)       # только положили — в конец, по времени
        return (steps[chain.head.name], p.question or 0, where_now, p.title.lower())

    links.sort(key=key)

    moves: list[Move] = []
    taken: set[int] = set()
    following = naming.FIRST_MATERIAL
    for chain in links:
        head, question = chain.head, chain.head.parsed.question
        step = steps[head.name]
        if step in fixed and step not in taken:
            number = step                           # закреплённый номер ступени
            taken.add(step)
        else:
            number = following
            following += 1
        dst = naming.compose(number, None, _title_of(event, head), question, head.parsed.ext)
        if dst != head.name:
            moves.append(Move(src=head.name, dst=dst))
        # приложение всегда при своём документе и наследует его вопрос
        for order_no, sub in enumerate(chain.subs, start=1):
            dst = naming.compose(number, order_no, _title_of(event, sub), question, sub.parsed.ext)
            if dst != sub.name:
                moves.append(Move(src=sub.name, dst=dst))
    return moves


def _recover(event: tree.Event) -> None:
    """Вернуть в папку то, что осталось в «~перекладка~/» после оборванного пересчёта.

    Там лежат настоящие документы под своими прежними именами (процесс остановили между
    двумя проходами), и прятать их нельзя: следующий пересчёт даст им номера.
    """
    stage = event.path / STAGING
    if not stage.is_dir():
        return
    for left in sorted(stage.iterdir()):
        if not left.is_file():
            continue
        back = tree.unique(event.path / left.name)
        try:
            left.rename(back)
        except OSError:
            log.exception("не вернули «%s» из перекладки", left.name)
            continue
        if back.name != left.name:
            try:
                store.rename(event.path / left.name, back)
            except Exception:           # noqa: BLE001 — кэш перечитается
                log.exception("кэш не переехал за «%s»", back.name)
        log.warning("«%s» вернулся из оборванной перекладки как «%s»", left.name, back.name)
    try:
        stage.rmdir()
    except OSError:
        pass


def apply(event: tree.Event, moves: list[Move]) -> list[Move]:
    """Переименование в два прохода: сначала в сторону, в папку «~перекладка~».

    Иначе обмен номерами между двумя файлами затрёт один из них. В сторону файл уезжает
    под своим же именем — оно уже лежит на диске, значит, и там уложится в предел длины.
    """
    folder = event.path
    stage = folder / STAGING
    staged: list[tuple[Path, Path, Path]] = []      # временное, куда ляжет, откуда взяли
    try:
        for move in moves:
            src = folder / move.src
            if not src.exists() or not tree.inside(src):
                continue
            stage.mkdir(exist_ok=True)
            tmp = stage / move.src
            try:
                src.rename(tmp)
            except FileNotFoundError:
                continue            # файл только что унесли или переименовали — без него
            staged.append((tmp, folder / move.dst, src))
    except OSError:
        # файл держит Word — возвращаем всё как было: половина переложенной папки
        # хуже, чем непереложенная
        log.warning("перекладка «%s» не удалась, папка оставлена как была", folder.name,
                    exc_info=True)
        for tmp, _, src in staged:
            try:
                tmp.rename(src)
            except OSError:
                pass
        _tidy(stage)
        return []

    done: list[Move] = []
    for number, (tmp, dst, src) in enumerate(staged):
        try:
            final = tree.unique(dst)
            tmp.rename(final)
        except OSError:
            log.warning("«%s» → «%s» не удалось, остальное вернули на место", src.name,
                        dst.name, exc_info=True)
            for rest_tmp, _, rest_src in staged[number:]:
                try:
                    rest_tmp.rename(tree.unique(rest_src))
                except OSError:
                    pass
            break
        done.append(Move(src=src.name, dst=final.name))
    _tidy(stage)
    return done


def _tidy(stage: Path) -> None:
    try:
        stage.rmdir()               # не пустая — значит, что-то не вернулось: разберёт _recover
    except OSError:
        pass


def renumber(event: tree.Event, order: list[str] | None = None) -> list[Move]:
    """Пересчитать номера и переименовать файлы. Кэш текста едет следом за именами.

    Пересчёт папки идёт один за раз, а план считается под тем же замком — по тому,
    что лежит на диске сейчас, а не до чужой перекладки.
    """
    with _lock(event):
        _recover(event)
        done = apply(event, plan_numbering(event, order))
        for move in done:
            try:
                store.rename(event.path / move.src, event.path / move.dst)
            except Exception:           # noqa: BLE001 — файлы уже переложены, кэш догонит
                log.exception("кэш не переехал за «%s»", move.dst)
        return done
