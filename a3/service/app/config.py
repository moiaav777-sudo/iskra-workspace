"""Настройки сервиса библиотеки листов A3.

Всё берётся из `service/.env` (в репозиторий не попадает) с разумными значениями
по умолчанию для локального стенда.
"""
from __future__ import annotations

import os
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
SERVICE_DIR = APP_DIR.parent          # a3/service
A3_DIR = SERVICE_DIR.parent           # a3


# Ключ «Искры» в папке сервиса не хранится (см. a3/CLAUDE.md → «Нельзя»):
# на ПК он лежит рядом с данными стенда, на сервере его пишет туда портал.
SECRETS_FILE = Path(os.environ.get("A3_SECRETS", str(Path.home() / ".work-portal-dev" / "a3.env")))


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
    """Переменная окружения, иначе service/.env, иначе файл с ключами, иначе default."""
    value = os.environ.get(name)
    if value:
        return value
    # на ПК портал кладёт ключ «Искры» в корень папки сервиса, поэтому смотрим оба .env
    for path in (SERVICE_DIR / ".env", A3_DIR / ".env", SECRETS_FILE):
        found = _from_file(path, name)
        if found:
            return found
    return default


# Порт выдаёт общий трек (см. ../TRACK.md). До выдачи работаем на 8940 локально.
PORT = int(read_env("A3_PORT", "8940"))

# Общий секрет с порталом: без него заголовкам X-Portal-* верить нельзя.
PORTAL_SECRET = read_env("PORTAL_SECRET", "")

# Журнал портала.
AUDIT_URL = read_env("AUDIT_URL", "http://127.0.0.1:8700/internal/audit")

# База: вне репозитория, рядом с сервисом.
DB_PATH = Path(read_env("A3_DB", str(SERVICE_DIR / "data" / "a3.db")))

# Конструктор листа. Тот же файл, что открывается сам по себе без сервиса.
KIT_PATH = Path(read_env("A3_KIT", str(A3_DIR / "a3-kit.html")))

# Стенд без портала: вход подставляется, секрет не проверяется.
# Отладочный режим (подставляет пользователя без проверки секрета) допустим только на стенде:
# если секрет портала задан, значит сервис работает за порталом и вход отключать нельзя.
DEV = read_env("A3_DEV", "") == "1" and not read_env("PORTAL_SECRET", "")
DEV_USER = {"login": "dev", "role": "admin", "name": "Разработчик"}
