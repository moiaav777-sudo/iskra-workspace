"""Настройки сервиса «Объективки».

Папка сервиса задаётся переменной PROFILES_HOME (её же портал знает по своему .env и пишет туда
ключ «Искры»): в ней `.env`, база `data/profiles.db` и файлы (`data/photos`, `data/docx`,
`data/merged`). Без PROFILES_HOME — папка самого сервиса (`profiles/`), как у соседей.

Значения читаются при каждом обращении: портал переписывает `.env` (ключ «Искры») без
перезапуска сервиса, а тесты подменяют PROFILES_HOME на временную папку.
"""
from __future__ import annotations

import os
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
SERVICE_DIR = APP_DIR.parent              # profiles/
STATIC_DIR = SERVICE_DIR / "static"

PORT_DEFAULT = 8990                       # выдан общим треком (решение пользователя 05.10)
ISKRA_URL_DEFAULT = "https://cloud.iskrabot.ru"


def home() -> Path:
    value = os.environ.get("PROFILES_HOME", "").strip()
    return Path(value) if value else SERVICE_DIR


def _from_file(path: Path, name: str) -> str:
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError:
        return ""
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        if key.strip() == name:
            return val.strip().strip('"').strip("'")
    return ""


def env(name: str, default: str = "") -> str:
    """Переменная окружения, иначе `<PROFILES_HOME>/.env`, иначе `.env` рядом с сервисом."""
    value = os.environ.get(name, "")
    if value:
        return value
    for folder in dict.fromkeys([home(), SERVICE_DIR]):
        found = _from_file(folder / ".env", name)
        if found:
            return found
    return default


def data_dir() -> Path:
    return Path(env("PROFILES_DATA", str(home() / "data")))


def db_path() -> Path:
    return data_dir() / "profiles.db"


def photos_dir() -> Path:
    return data_dir() / "photos"


def docx_dir() -> Path:
    return data_dir() / "docx"


def merged_dir() -> Path:
    return data_dir() / "merged"


def port() -> int:
    try:
        return int(env("PROFILES_PORT", str(PORT_DEFAULT)))
    except ValueError:
        return PORT_DEFAULT


def portal_url() -> str:
    return env("PORTAL_URL", "http://127.0.0.1:8700").rstrip("/")


def dev() -> bool:
    """Стенд без портала: вход подставляется. Только если PORTAL_SECRET не задан."""
    return env("PROFILES_DEV") == "1" and not env("PORTAL_SECRET")


DEV_USER = {"login": "dev", "role": "employee", "name": "Разработчик", "team": ["*"]}
