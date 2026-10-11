"""Настройки сервиса «Транскрибатор».

Всё берётся из `service/.env` (в репозиторий не попадает, см. .gitignore) с разумными
значениями по умолчанию для локального стенда. Устройство файла — как у соседей
(`a3/service/app/config.py`).
"""
from __future__ import annotations

import os
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
SERVICE_DIR = APP_DIR.parent          # transcriber/


def _from_file(path: Path, name: str) -> str:
    if not path.exists():
        return ""
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        if key.strip() == name:
            return val.strip().strip('"').strip("'")
    return ""


def read_env(name: str, default: str = "") -> str:
    """Переменная окружения, иначе `.env` рядом с сервисом, иначе значение по умолчанию."""
    value = os.environ.get(name)
    if value:
        return value
    found = _from_file(SERVICE_DIR / ".env", name)
    return found or default


# Порт выдаёт общий трек (см. ../CLAUDE.md → «Продуктив» → п. 7). Уже выдан — 8980.
PORT = int(read_env("TRANSCRIBER_PORT", "8980"))

# Общий секрет с порталом: без него заголовкам X-Portal-* верить нельзя.
PORTAL_SECRET = read_env("PORTAL_SECRET", "")

# Журнал портала.
AUDIT_URL = read_env("AUDIT_URL", "http://127.0.0.1:8700/internal/audit")

# База: вне репозитория, рядом с сервисом.
DB_PATH = Path(read_env("TRANSCRIBER_DB", str(SERVICE_DIR / "data" / "transcriber.db")))

# Куда на время обработки кладётся загруженная запись. Файл удаляется сразу после
# разбора (успешного или нет) — политика хранения описана в README.md.
UPLOAD_DIR = Path(read_env("TRANSCRIBER_UPLOADS", str(SERVICE_DIR / "data" / "uploads")))

# Предел на файл и на длительность записи. Решение пользователя 02.10: распознаёт «Искра»,
# а она принимает в чат аудио и видео до 500 МБ (iskrabot.ru) — больше не принимаем и мы,
# иначе человек ждал бы загрузки, чтобы получить отказ «Искры» (413).
ISKRA_MAX_BYTES = 500 * 1024 * 1024
MAX_FILE_BYTES = min(int(read_env("TRANSCRIBER_MAX_BYTES", str(ISKRA_MAX_BYTES))), ISKRA_MAX_BYTES)
MAX_DURATION_SECONDS = int(read_env("TRANSCRIBER_MAX_SECONDS", str(4 * 60 * 60)))  # 4 ч

# Движок распознавания речи (решение пользователя 02.10, меняет решение 27.09):
#   iskra   — по умолчанию: файл записи уходит в «Искру» (`/api/v1/chat/files` → фоновый прогон
#             `/api/v1/runs`), расшифровка приходит TXT-файлом результата (app/engine/iskra_engine.py);
#   fake    — подмена для тестов и показа интерфейса;
#   whisper — локальная модель faster-whisper, опциональный резерв (если поставлена).
ENGINE = (read_env("TRANSCRIBER_ENGINE", "iskra").strip().lower() or "iskra")

# «Искра»: адрес и сроки. Ключ (`ISKRA_API_KEY`) читается при каждом вызове — портал
# переписывает его в .env без перезапуска сервиса.
ISKRA_URL_DEFAULT = "https://cloud.iskrabot.ru"
# Правила API «Искры» (../CLAUDE.md, п. 5в): синхронный ход ограничен 90 с, фоновый прогон
# `/api/v1/runs` — 15 минутами; расшифровка идёт только фоновым прогоном с опросом.
# Срок загрузки файла записи (секунды на сетевую операцию, до 500 МБ потоком).
ISKRA_UPLOAD_TIMEOUT = float(read_env("TRANSCRIBER_ISKRA_UPLOAD_TIMEOUT", "900"))
# Сколько ждём один прогон, прежде чем снять его и попросить готовую расшифровку из черновиков
# повторным прогоном в том же разговоре: 15 минут «Искры» и минута запаса.
ISKRA_RUN_LIMIT = float(read_env("TRANSCRIBER_ISKRA_RUN_LIMIT", str(16 * 60)))
# Общий срок ожидания «Искры» после загрузки записи: два прогона по 15 минут и запас.
ISKRA_DEADLINE = float(read_env("TRANSCRIBER_ISKRA_DEADLINE", str(40 * 60)))
# Скачивание TXT-файла расшифровки из результата прогона: срок и предел размера.
ISKRA_DOWNLOAD_TIMEOUT = float(read_env("TRANSCRIBER_ISKRA_DOWNLOAD_TIMEOUT", "300"))
ISKRA_RESULT_MAX_BYTES = int(read_env("TRANSCRIBER_ISKRA_RESULT_MAX_BYTES", str(50 * 1024 * 1024)))
# Запасной путь: файла в результате нет, а «Искра» отдаёт текст частями с меткой [ПРОДОЛЖЕНИЕ] —
# столько раз самое большее просим «дальше» (каждая часть — свой фоновый прогон).
ISKRA_MAX_PARTS = int(read_env("TRANSCRIBER_ISKRA_MAX_PARTS", "40"))

# Размер модели faster-whisper (резервный движок) — не хардкодится, чтобы можно было поднять точность
# (medium/large-v3) или, наоборот, ускорить (tiny/base) без правки кода.
# По умолчанию small — компромисс скорости и качества на CPU без GPU.
WHISPER_MODEL = read_env("TRANSCRIBER_WHISPER_MODEL", "small").strip() or "small"

# Стенд без портала: вход подставляется, секрет не проверяется.
# Как у соседей (a3, agenda): допустимо только когда PORTAL_SECRET не задан — если он
# есть, значит сервис работает за порталом, и вход отключать нельзя.
DEV = read_env("TRANSCRIBER_DEV", "") == "1" and not read_env("PORTAL_SECRET", "")
DEV_USER = {"login": "dev", "role": "admin", "name": "Разработчик", "team": ["*"]}
