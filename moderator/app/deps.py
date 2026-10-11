"""Общее для маршрутов: текущий пользователь, поиск совещания и элементов, проверки правок."""
from __future__ import annotations

import contextvars

from fastapi import HTTPException, UploadFile

from . import storage
from .models import AgendaItem, Meeting

MAX_UPLOAD_BYTES = 30 * 1024 * 1024   # один загружаемый файл
MAX_TEXT_CHARS = 1_000_000            # текст для разбора / материала
MAX_RESULT_CHARS = 1_000_000          # html и text документа «Результатов» — каждое (≈ 2 МБ кириллицы)

# пользователь запроса: ставится в middleware из заголовков портала
current_user = contextvars.ContextVar("current_user", default=None)


def meeting_or_404(mid: str) -> Meeting:
    # Все сотрудники видят и правят все совещания; каждое изменение уходит в журнал портала.
    if not current_user.get():
        raise HTTPException(status_code=401, detail="Требуется вход")
    m = storage.get(mid)
    if m is None:
        raise HTTPException(status_code=404, detail="Совещание не найдено")
    return m


def admin_meeting_or_404(mid: str, action: str) -> Meeting:
    """Необратимые действия — только администратор."""
    m = meeting_or_404(mid)
    if not current_user.get()["is_admin"]:
        raise HTTPException(status_code=403, detail=f"Только администратор может {action}")
    return m


STALE = "уже изменили в другом окне — данные обновлены"


def pos(items: list, key: str, what: str) -> int:
    """Позиция элемента по его стабильному id.

    Позиция меняется, как только кто-то добавил или переставил пункт в другой
    вкладке, поэтому запросы адресуют элемент по id: 409 лучше, чем правка
    чужого пункта.
    """
    for i, item in enumerate(items):
        if item.id == key:
            return i
    raise HTTPException(status_code=409, detail=f"{what} {STALE}")


def at(items: list, key: str, what: str):
    """Элемент списка по стабильному id."""
    return items[pos(items, key, what)]


def check_base(obj, base: dict | None, mid: str, what: str) -> None:
    """Длинную правку принимаем, только если её основа не изменилась.

    Иначе сохранение молча затрёт чужой текст: два человека, открывшие один
    блок, видели бы «Сохранено» оба, а в бриф попал бы текст того, кто нажал позже.
    """
    if not base:
        return
    if all(getattr(obj, k, None) == v for k, v in base.items()):
        return
    user = current_user.get() or {}
    who = storage.last_editor_name(mid, user.get("login", ""))
    raise HTTPException(
        status_code=409,
        detail=f"{what} успел(а) изменить " + (who or "коллега") +
               ", пока шла правка. Ваш текст не сохранён — откройте обновлённый и перенесите правки.",
    )


def speaker_pos(item: AgendaItem, sidx: int, name: str | None, what: str = "Выступающий") -> int:
    """Выступающие лежат параллельными списками, поэтому позиция + сверка ФИО."""
    if sidx < 0 or sidx >= len(item.speakers):
        raise HTTPException(status_code=409, detail=f"{what} {STALE}")
    if name is not None and item.speakers[sidx] != name:
        raise HTTPException(status_code=409, detail=f"{what} {STALE}")
    return sidx


def read_upload(f: UploadFile) -> bytes:
    data = f.file.read(MAX_UPLOAD_BYTES + 1)
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail=f"Файл «{f.filename}» больше {MAX_UPLOAD_BYTES // (1024 * 1024)} МБ")
    return data


def check_text(text: str) -> None:
    if len(text) > MAX_TEXT_CHARS:
        raise HTTPException(status_code=413, detail="Текст слишком большой для разбора")
