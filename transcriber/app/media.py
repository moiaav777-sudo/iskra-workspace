"""Загруженный файл записи: сохранить на время обработки, проверить вид и длительность.

Приватность (../CLAUDE.md → задача, п. 7): файл лежит в `config.UPLOAD_DIR` только на
время работы и удаляется сразу после — успешной ли, нет ли. Само содержимое записи
никогда не попадает в журнал (`app/audit.py`) и не хранится дольше необходимого.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import uuid
from pathlib import Path

from fastapi import UploadFile

from . import config

# Расширения, которые вообще имеет смысл принимать — точный разбор формата всё равно
# делает движок распознавания (ffmpeg внутри). Список закрывает случайные не-медиа файлы.
ALLOWED_EXT = {
    ".mp3", ".wav", ".m4a", ".aac", ".ogg", ".opus", ".flac", ".wma",
    ".mp4", ".m4v", ".mov", ".webm", ".mkv", ".avi",
}


class BadUpload(Exception):
    """Файл не подходит: неизвестный вид, слишком большой или слишком длинный."""


def _ext(filename: str) -> str:
    return Path(filename or "").suffix.lower()


def save_upload(file: UploadFile, max_bytes: int | None = None) -> Path:
    """Сохранить файл на диск потоково — большая запись не должна лечь в память целиком."""
    if max_bytes is None:
        max_bytes = config.MAX_FILE_BYTES
    ext = _ext(file.filename or "")
    if ext not in ALLOWED_EXT:
        raise BadUpload(
            f"Формат «{ext or 'без расширения'}» не похож на запись разговора. "
            f"Подходят аудио и видео: {', '.join(sorted(ALLOWED_EXT))}")
    config.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    dest = config.UPLOAD_DIR / f"{uuid.uuid4().hex}{ext}"
    written = 0
    with dest.open("wb") as out:
        while True:
            chunk = file.file.read(1024 * 1024)
            if not chunk:
                break
            written += len(chunk)
            if written > max_bytes:
                out.close()
                dest.unlink(missing_ok=True)
                raise BadUpload(
                    f"Файл больше {max_bytes // (1024 * 1024)} МБ — «Искра» принимает записи "
                    "до 500 МБ. Сожмите запись (например, только звук в MP3) или разбейте её на части.")
            out.write(chunk)
    if written == 0:
        dest.unlink(missing_ok=True)
        raise BadUpload("Файл пустой")
    return dest


def probe_duration(path: Path) -> float | None:
    """Длительность записи в секундах через ffprobe — если он есть на машине.

    Нет ffprobe — не считаем это поводом отказать: движок распознавания разберётся
    сам, а лимит длительности мы просто не проверим заранее (см. README.md).
    """
    if shutil.which("ffprobe") is None:
        return None
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", str(path)],
            capture_output=True, timeout=30, check=True,
        )
        data = json.loads(out.stdout or b"{}")
        duration = data.get("format", {}).get("duration")
        return float(duration) if duration is not None else None
    except (subprocess.SubprocessError, ValueError, json.JSONDecodeError, OSError):
        return None


def check_duration(path: Path, max_seconds: int | None = None) -> float | None:
    if max_seconds is None:
        max_seconds = config.MAX_DURATION_SECONDS
    duration = probe_duration(path)
    if duration is not None and duration > max_seconds:
        path.unlink(missing_ok=True)
        raise BadUpload(
            f"Запись длиннее {max_seconds // 60} минут ({int(duration // 60)} мин) — "
            "такую расшифровку «Искра» будет выдавать слишком долго. Разбейте запись на части.")
    return duration


def discard(path: Path) -> None:
    """Файл записи больше не нужен — удаляем сразу, не дожидаясь уборки мусора."""
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def clear_upload_dir() -> None:
    """При старте сервиса: файлы, оставшиеся от прошлого процесса, — не нужны никому.

    Задания живут только в памяти (`app/jobs.py`), поэтому после перезапуска ни один
    файл в `config.UPLOAD_DIR` не может относиться к работе, которая ещё идёт —
    прошлая работа потеряна вместе с процессом (см. `db.interrupt_stale`). Держать
    чужие записи со звуком на диске дальше необходимого — против приватности (п. 7).
    """
    if not config.UPLOAD_DIR.exists():
        return
    for item in config.UPLOAD_DIR.iterdir():
        if item.is_file():
            try:
                item.unlink()
            except OSError:
                pass
