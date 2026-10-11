"""Приёмная: куда падает документ, который ещё не знает своего мероприятия.

98 % документов приходит в мессенджере, часто с телефона. Там человек не выбирает
папку — он просто делится файлом. Поэтому документ сначала ложится сюда, в общую
папку вне мероприятий, и сразу читается: текст в поиск, заголовок — в предложенное
название. Дальше в «Приёмной» человек одним нажатием отправляет его в мероприятие.

Лежит всё так же на диске, рядом с годами:

    Хранилище/Приёмная/Инф_справка_ТН_2026 РФ.docx

Имя не трогаем: это первичка, её ценность в том, что она пришла именно такой.
"""
from __future__ import annotations

import os
from pathlib import Path

import json
import time

from . import doc_text, folder, meeting, naming, routing, similar, store, tree

DIR = "Приёмная"
DROPPED = "Убранное"     # ненужное присланное: из ленты ушло, с диска нет
MEANT = "#приёмная"      # раздел в настройках: куда направляли присланное
NOTE = "#команда"        # слова, присланные вместе с документом: «напиши бриф»
SENDER = "#отправитель"  # кто прислал — отправляет в соседний сервис его браузер
SENT = "#ушло"           # куда документ уже отправлен: {to, state, url, by, when}

# Пометки живут рядом с кэшем, а не в папке: служебных файлов там нет.
MARKS = (MEANT, NOTE, SENDER, SENT)
STALE = 15 * 60          # заявка «отправляется» старше этого — брошена, её можно перезаявить


class Busy(Exception):
    """Документ уже отправляется или отправлен — второе нажатие ничего не делает."""


def path() -> Path:
    folder = tree.root() / DIR
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def save(origin: str, data: bytes) -> str:
    """Положить присланный документ в приёмную и сразу прочитать его."""
    name = naming.safe_file(origin)
    if not naming.split_ext(name)[1]:
        # имя доехало без расширения — узнаём формат по содержимому, иначе
        # документ останется нечитаемым: ни названия, ни текста в поиск
        found = doc_text.guess_ext(data)
        if found:
            name = f"{name}.{found}"
    target = tree.unique(path() / name)
    target.write_bytes(data)
    store.remember(target, data, origin=target.name)
    return target.name


def meant_for(name: str, event_id: str = "") -> str:
    """Мероприятие, в которое документ направляли с телефона.

    Помним, потому что угадывать тут нечего: человек уже сказал, куда шлёт,
    и в ленте подсказка должна показывать именно это мероприятие, а не похожее.
    """
    if event_id:
        store.set_setting(MEANT, name, event_id)
    return store.setting(MEANT, name)


def forget_meant(name: str) -> None:
    store.set_setting(MEANT, name, "")


def note_for(name: str, text: str | None = None) -> str:
    """Слова, присланные вместе с документом. Они главнее любых догадок о виде."""
    if text is not None:
        store.set_setting(NOTE, name, text.strip()[:300])
    return store.setting(NOTE, name)


def sender_of(name: str, login: str = "") -> str:
    if login:
        store.set_setting(SENDER, name, login)
    return store.setting(SENDER, name)


def sent(name: str) -> dict | None:
    """Куда документ ушёл (или идёт прямо сейчас). Нет записи — никуда."""
    raw = store.setting(SENT, name)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return None


def claim(name: str, to: str, by: str) -> dict:
    """Заявить отправку. Второе нажатие, вторая вкладка, второй человек — `Busy`:
    документ в соседний сервис уходит один раз."""
    if to not in routing.TARGETS:
        raise ValueError("неизвестно, куда")
    c = store.conn()
    # проверка и запись — одной транзакцией: две вкладки в один миг встают в очередь,
    # и вторая уже видит заявку первой (ревьюер Н1)
    c.execute("BEGIN IMMEDIATE")
    try:
        was = sent(name)
        if was and was.get("state") == "done":
            raise Busy(was)
        if was and was.get("state") == "sending":
            # вкладку закрыли посреди отправки — заявка не должна держать документ вечно
            if time.time() - float(was.get("when") or 0) < STALE:
                raise Busy(was)
        now = {"to": to, "state": "done" if to == "keep" else "sending",
               "url": "", "by": by, "when": time.time()}
        c.execute("INSERT OR REPLACE INTO settings (login, key, value) VALUES (?,?,?)",
                  (SENT, name, json.dumps(now, ensure_ascii=False)))
        c.commit()
    except BaseException:
        c.rollback()
        raise
    return now


def finish(name: str, url: str) -> dict | None:
    was = sent(name)
    if not was:
        return None
    was.update(state="done", url=(url or "")[:500], when=time.time())
    store.set_setting(SENT, name, json.dumps(was, ensure_ascii=False))
    return was


def release(name: str, by: str = "", admin: bool = False) -> bool:
    """Отправка не удалась — снять заявку, чтобы можно было попробовать снова.
    Готовую («done») не трогаем: документ уже у соседа. Чужую заявку снимает
    только её автор или администратор (ревьюер Н3) — иначе False."""
    was = sent(name)
    if not was or was.get("state") != "sending":
        return True
    if by and was.get("by") != by and not admin:
        return False
    store.set_setting(SENT, name, "")
    return True


def carry(old: str, new: str) -> None:
    """Документ переименовали — пометки едут за ним."""
    if old == new:
        return
    for section in MARKS:
        value = store.setting(section, old)
        if value:
            store.set_setting(section, new, value)
        store.set_setting(section, old, "")


def forget_all(name: str) -> None:
    for section in MARKS:
        store.set_setting(section, name, "")


def same_file(data: bytes) -> str | None:
    """Такой же файл уже лежит в приёмной? Тогда второй копии там не место.

    Документ часто присылают двое сразу — каждый со своего телефона. Совпал
    отпечаток, значит, это тот же самый файл, и человеку незачем разбирать
    одно и то же дважды.
    """
    mark = similar.digest(data)
    here = path()
    for entry in os.scandir(here):
        if entry.is_file() and store.about(here / entry.name)["hash"] == mark:
            return entry.name
    return None


def dropped_dir() -> Path:
    """Куда уходит ненужное. Удалять за человека нельзя: он мог ошибиться кнопкой,
    а документ бывает единственным экземпляром (контракт с порталом, п. 2)."""
    folder = path() / DROPPED
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def repair(name: str) -> str:
    """Дописать расширение файлу, который приехал без него.

    Такие документы сервис не может ни прочитать, ни назвать по-человечески.
    Чиним при первом же действии человека — не заставлять же его перекладывать
    всё заново из-за того, что телефон не донёс имя.
    """
    target = file(name)
    if target is None or naming.split_ext(name)[1]:
        return name
    try:
        found = doc_text.guess_ext(target.read_bytes())
    except OSError:
        return name
    if not found:
        return name
    fresh = tree.unique(target.with_name(f"{name}.{found}"))
    target.rename(fresh)
    store.rename(target, fresh)
    was = store.setting(MEANT, name)
    if was:
        store.set_setting(MEANT, fresh.name, was)
        store.set_setting(MEANT, name, "")
    return fresh.name


def file(name: str) -> Path | None:
    target = path() / name
    if not tree.inside(target) or not target.is_file():
        return None
    return target


# ── подсказки ────────────────────────────────────────────────────────────
#
# Документ прилетел один и сам о себе ничего не говорит. Сервис подсказывает
# два вопроса из трёх: к какому мероприятию это похоже и не лежит ли там уже
# прежняя версия. Третий вопрос — что с этим делать — остаётся человеку.

NEAREST = 10          # дальше десятой по близости папки не заглядываем
EVENT_HINT = 0.25     # ниже этого сходство — уже гадание, лучше промолчать


def _event_words(event: tree.Event) -> set[str]:
    bag = similar.words(event.title)
    for item in folder.documents(folder.items(event)):
        bag |= similar.words(item.parsed.title)
    return bag


def _near(event: tree.Event) -> int:
    from datetime import date
    year, month, day = (int(x) for x in event.iso.split("-"))
    return abs((date(year, month, day) - date.today()).days)


def suggest(known: dict, name: str) -> tuple[tree.Event | None, str]:
    """Мероприятие, к которому документ скорее всего относится, и почему так решили."""
    mine = similar.words(known.get("title") or name) | similar.words((known.get("text") or "")[:600])
    seen = similar.dates(known.get("text") or "")
    best, why, score = None, "", 0.0
    for event in sorted(tree.events(), key=_near)[:NEAREST]:
        if event.date in seen:
            points, reason = 1.0, f"в тексте есть дата {event.date}"
        else:
            points = similar.overlap(mine, _event_words(event))
            reason = "созвучно тому, что уже в папке"
        if points > score and points >= EVENT_HINT:
            best, why, score = event, reason, points
    return best, why


def same_as(event: tree.Event, known: dict) -> dict | None:
    """Документ в папке, на который похож присланный. Точная копия — отдельный случай."""
    mine = similar.words(known.get("title") or "")
    text = similar.words((known.get("text") or "")[:2000])
    for item in folder.documents(folder.items(event)):
        info = store.about(event.path / item.name)
        row = {"name": item.name, "number": item.parsed.number, "title": item.parsed.title}
        if known.get("hash") and info["hash"] == known["hash"]:
            return {**row, "why": "это уже есть — файл тот же самый", "copy": True}
        if info["origin"] and info["origin"] == known.get("origin"):
            return {**row, "why": "тот же файл присылали раньше", "copy": False}
        if similar.overlap(mine, similar.words(info["title"] or item.parsed.title)) >= 0.6:
            return {**row, "why": "похоже по названию", "copy": False}
        if similar.overlap(text, similar.words((info["text"] or "")[:2000])) >= 0.5:
            return {**row, "why": "текст сильно пересекается", "copy": False}
    return None


def items() -> list[dict]:
    """Что лежит в приёмной: имя, размер, название, узнанный вид и подсказки."""
    out: list[dict] = []
    here = path()
    for entry in os.scandir(here):
        if not entry.is_file() or entry.name.startswith((".", "~$")):
            continue                    # подпапка «Убранное» сюда не попадает
        stat = entry.stat()
        entry_name = repair(entry.name)
        known = store.about(here / entry_name)
        # Показываем имя, под которым прислали: заголовок из документа («Наименование
        # вопроса № 4-К/23») человеку ничего не говорит. Он годится только для подсказок.
        shown = naming.split_ext(entry_name)[0]
        title = known["title"] or shown
        role = naming.role_of(shown) or naming.role_of(title)
        meant = tree.find(meant_for(entry_name)) if meant_for(entry_name) else None
        if meant is not None:
            event, why = meant, "прислано в это мероприятие"
        else:
            event, why = suggest({**known, "title": title}, entry_name)
        twin = same_as(event, {**known, "title": title}) if event else None
        # куда документу дальше: команда человека главнее вида, вид узнаётся теми же
        # приметами, что и номер в папке; приметы спорят — только выбор, не догадка
        note = note_for(entry_name)
        head = "\n".join((known["text"] or "").splitlines()[:3])
        kind, sure = routing.kind_of(entry_name, known["title"] or "", head)
        route = routing.decide(note, kind, sure, len((known["text"] or "").strip()))
        out.append({"name": entry_name, "size": stat.st_size, "when": stat.st_mtime,
                    "title": shown,
                    "looks_like": naming.ROLE_TITLES[role].lower() if role else "",
                    "suggest": {"id": event.id, "title": event.title, "date": event.date,
                                "why": why, "meeting": meeting.meeting_of(event)} if event else None,
                    "same_as": twin,
                    "note": note, "sender": sender_of(entry_name), "sent": sent(entry_name),
                    "route": route})
    out.sort(key=lambda i: i["when"], reverse=True)
    return out
