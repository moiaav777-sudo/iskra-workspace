"""Пакет к совещанию: один архив вместо пяти скачиваний.

Человек готовит совещание в разных сервисах, а руководителю нужно отдать всё сразу:
бриф совещания, повестку и список участников, приложенные материалы. Портал собирает это
по тем же адресам, что открыты человеку, и складывает в zip.
"""
from __future__ import annotations

import io
import json
import re
import urllib.error
import urllib.parse
import urllib.request
import zipfile

from . import config

TIMEOUT = 30
BAD_NAME = re.compile(r"[^\w .,()\-–—]+", re.UNICODE)


def _safe(name: str, default: str) -> str:
    name = BAD_NAME.sub(" ", (name or "").strip()).strip() or default
    return name[:80]


def _ask(service_id: str, path: str, user: dict, timeout: int = TIMEOUT) -> tuple[int, bytes, str]:
    """Запрос к сервису от имени человека: прав это не расширяет."""
    port = config.service_port(service_id)
    url = f"http://127.0.0.1:{port}{path}"
    request = urllib.request.Request(url, headers={
        "X-Portal-Secret": config.portal_secret(),
        "X-Portal-User": user["login"],
        "X-Portal-Role": user["role"],
        "X-Portal-Name": urllib.parse.quote(user.get("full_name") or user["login"]),
    })
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read(), response.headers.get("Content-Type", "")
    except urllib.error.HTTPError as e:
        return e.code, b"", ""
    except (urllib.error.URLError, OSError, ValueError):
        return 0, b"", ""


def ask_json(service_id: str, path: str, user: dict, timeout: int = TIMEOUT):
    """Спросить сервис от имени человека и разобрать ответ.

    Нужно не только пакету: так же портал узнаёт, сколько документов ждёт
    в «Приёмной» «Материалов» для значка на плитке, и показывает ближайшие
    совещания и поручения на главной. Эти лёгкие вызовы (СРЕ 28.09, П1: сосед
    под нагрузкой держал поток портала до 30 с, а через него ждёт и auth_request
    на весь портал) зовут с коротким `timeout`, а не общим 30-секундным: сосед
    не ответил вовремя — молча пусто/понятная ошибка, а не повисший поток.
    """
    status, body, _ = _ask(service_id, path, user, timeout)
    if status != 200 or not body:
        return None
    try:
        return json.loads(body.decode("utf-8", "replace"))
    except ValueError:
        return None


def build(meeting_id: str, user: dict) -> tuple[str, bytes, list[str]]:
    """Возвращает (имя архива, содержимое, список того, что не попало)."""
    meeting = ask_json("moderator", f"/api/meetings/{urllib.parse.quote(meeting_id)}", user)
    if not meeting:
        raise LookupError("Совещание не найдено")
    data = meeting.get("meeting") or meeting
    title = _safe(data.get("title") or "Совещание", "Совещание")
    date = (data.get("date") or "").replace(".", "-")
    missing: list[str] = []
    buffer = io.BytesIO()

    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        status, body, _ = _ask("moderator", f"/api/meetings/{urllib.parse.quote(meeting_id)}/export", user)
        if status == 200 and body:
            zf.writestr("1. Бриф совещания.html", body)
        else:
            missing.append("бриф совещания")

        # повестка и список участников лежат в «Повестке», если совещание пришло оттуда
        source = data.get("source") or {}
        agenda_id = source.get("meeting_id") if source.get("service") == "agenda" else ""
        if agenda_id:
            for name, path in (("2. Повестка.docx", "agenda.docx"), ("3. Список участников.docx", "participants.docx")):
                status, body, _ = _ask("agenda", f"/api/meetings/{urllib.parse.quote(agenda_id)}/export/{path}", user)
                if status == 200 and body:
                    zf.writestr(name, body)
                else:
                    missing.append(name.split(". ", 1)[-1].replace(".docx", ""))
        else:
            missing.append("повестка и список участников (совещание не связано с «Повесткой»)")

        # материалы: что приложено к вопросам, включая брифы и листы A3
        materials = data.get("materials") or []
        lines = []
        for number, material in enumerate(materials, 1):
            name = material.get("title") or material.get("filename") or f"Материал {number}"
            src = material.get("source") or {}
            where = f" — {src.get('kind')}, открыть: {src.get('url')}" if src.get("url") else ""
            lines.append(f"{number}. {name}{where}")
        if lines:
            zf.writestr("4. Материалы — список.txt", "\n".join(lines).encode("utf-8"))
        else:
            missing.append("материалы (к совещанию ничего не приложено)")

        if missing:
            zf.writestr("Чего не хватает.txt", ("В пакет не вошло:\n- " + "\n- ".join(missing) +
                                                "\n\nЭто не ошибка: соберите недостающее в сервисах и скачайте пакет снова.").encode("utf-8"))

    name = f"Пакет к совещанию — {title}" + (f" — {date}" if date else "") + ".zip"
    return name, buffer.getvalue(), missing
