"""Передача готовых тезисов на общую ось совещания (`../CLAUDE.md`, п. 9).

Совещание одно на всё пространство, и у него один хозяин: карточку ведёт «Модератор»,
папку с файлами — «Материалы». Поэтому сервис «Тезисы» своего совещания не заводит
и своей раскладки файлов не держит. Нажатие делает три вещи:

1. просит «Модератор» найти или завести совещание —
   `POST /moderator/api/meetings/find-or-create` с `{date, title, source}`
   → `{id, url, created, folder}`. Вызов идемпотентен по паре «дата + название»,
   так что повторное нажатие не плодит двойников;
2. кладёт тезисы во вступительное слово сценария —
   `POST /moderator/api/meetings/<id>/scenario/opening-from-theses`;
3. кладёт готовый документ Word в папку совещания —
   `POST /materials/api/events/<folder>/files` вместе с `source: {service, id}`.
   Номер не передаём и не угадываем: его ставят «Материалы» по старшинству,
   броней в пакете нет. По `source` они отличают новую редакцию тех же тезисов
   от других тезисов к другому вопросу — имя файла тут ни при чём, а тезисов
   к одному совещанию бывает несколько (п. 9, дополнен 20.09).

Все запросы уходят из браузера пользователя тем же доменом: права обычные,
из заголовков портала, общих секретов между сервисами не появляется.

**Пустой `folder` — обычное дело**, и разбирается он сам (п. 9): папку заводит
интерфейс «Модератора», поэтому у совещания, только что созданного отсюда, её ещё
нет. Тогда мы доводим дело сами — дата и название у нас на руках:
`POST /materials/api/events/link` → берём `event.id` →
`PUT /moderator/api/meetings/<id>/folder`. Оба вызова идемпотентны, лишний заход
ничего не ломает. Сосед промолчал — работаем как раньше и объясняем человеку.
"""
from __future__ import annotations

import datetime as dt

from .models import Thesis

LIST_URL = "/moderator/api/meetings"
FIND_OR_CREATE_URL = "/moderator/api/meetings/find-or-create"
OPENING_URL = "/moderator/api/meetings/<id>/scenario/opening-from-theses"
# папки у совещания может ещё не быть — заводим её и запоминаем у «Модератора»
MATERIALS_LINK_URL = "/materials/api/events/link"
MODERATOR_FOLDER_URL = "/moderator/api/meetings/<id>/folder"
# сюда кладётся готовый документ; <id> — папка совещания
MATERIALS_FILES_URL = "/materials/api/events/<id>/files"


def text_of(thesis: Thesis) -> str:
    """Текст выступления: буллиты по порядку, по одному на строку.

    «Модератор» режет его по строкам и складывает пунктами списка во вступительное
    слово, поэтому одна мысль — одна строка, пустых строк быть не должно.
    Служебный блок «Что необходимо доуточнить» не уезжает — он для составителя.
    Сами маркеры `[ТРЕБУЕТСЯ …]` остаются: человек должен видеть, чего не хватает,
    прежде чем читать текст вслух.
    """
    return "\n".join(bullet.text.strip() for bullet in thesis.bullets if bullet.text.strip())


def payload(thesis: Thesis) -> dict:
    return {
        "text": text_of(thesis),
        "minutes": thesis.minutes,
        "source": {"service": "theses", "id": thesis.id},
    }


def suggest_meeting(thesis: Thesis) -> dict:
    """Чем заполнить «дату и название», если подходящего совещания ещё нет.

    Название берём из того, где выступление («совещание с регионами»), а если это
    не сказано — из темы: с пустым названием «Модератор» совещание не заведёт.
    Дату не выдумываем: её ставит человек, по ней же идёт поиск двойников.
    """
    return {"title": thesis.event.strip() or thesis.topic.strip(), "date": ""}


def envelope(thesis: Thesis) -> dict:
    """Всё, что нужно браузеру: где спросить совещание, куда положить текст и документ."""
    return {
        "list_url": LIST_URL,
        "find_or_create_url": FIND_OR_CREATE_URL,
        "opening_url_template": OPENING_URL,
        "materials_link_url": MATERIALS_LINK_URL,
        "moderator_folder_url_template": MODERATOR_FOLDER_URL,
        "materials_files_url_template": MATERIALS_FILES_URL,
        "source": {"service": "theses", "id": thesis.id},
        "suggest": suggest_meeting(thesis),
        "payload": payload(thesis),
        "today": dt.date.today().strftime("%d.%m.%Y"),
    }
