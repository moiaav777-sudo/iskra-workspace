"""Связь папки с совещанием «Модератора».

Совещание одно на всё пространство, и у него одна папка (контракт `../CLAUDE.md`, п. 9).
Карточку ведёт «Модератор», папку — мы; связь необязательная: папка-склад без совещания
остаётся как была.

Где живёт связь. В самой папке её нет: служебных файлов там не заводим, иначе папку
нельзя будет открыть в проводнике и всё понять. Поэтому связь лежит рядом с кэшем
прочитанного текста — как и он, потеряется без беды: папка станет обычным складом,
а «Модератор» при следующем обращении привяжет её заново.
"""
from __future__ import annotations

from . import store, tree

BY_MEETING = "#совещание"      # id совещания → путь папки
BY_FOLDER = "#папка"           # путь папки → id совещания


def _rel(event: tree.Event) -> str:
    return str(event.rel).replace("\\", "/")


def bind(meeting: str, event: tree.Event) -> None:
    """Привязать папку к совещанию. Прежняя привязка этой папки снимается."""
    was = store.setting(BY_FOLDER, _rel(event))
    if was and was != meeting:
        store.set_setting(BY_MEETING, was, "")
    store.set_setting(BY_MEETING, meeting, _rel(event))
    store.set_setting(BY_FOLDER, _rel(event), meeting)


def folder_of(meeting: str) -> tree.Event | None:
    """Папка совещания. Папку убрали или переименовали мимо нас — связи нет."""
    rel = store.setting(BY_MEETING, meeting)
    if not rel:
        return None
    return next((e for e in tree.events() if _rel(e) == rel), None)


def meeting_of(event: tree.Event) -> str:
    return store.setting(BY_FOLDER, _rel(event))


def moved(old_rel: str, event: tree.Event) -> None:
    """Папка переехала — связь едет следом."""
    meeting = store.setting(BY_FOLDER, old_rel)
    if not meeting:
        return
    store.set_setting(BY_FOLDER, old_rel, "")
    store.set_setting(BY_MEETING, meeting, _rel(event))
    store.set_setting(BY_FOLDER, _rel(event), meeting)


def unbind(event: tree.Event) -> None:
    """Папку убрали — совещание остаётся без папки, и это не беда."""
    meeting = store.setting(BY_FOLDER, _rel(event))
    if meeting:
        store.set_setting(BY_MEETING, meeting, "")
    store.set_setting(BY_FOLDER, _rel(event), "")
