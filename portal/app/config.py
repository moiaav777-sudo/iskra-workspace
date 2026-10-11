"""Настройки портала: пути, секреты, перечень сервисов."""
from __future__ import annotations

import os
import secrets
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("PORTAL_DATA") or BASE_DIR / "data")
ENV_PATH = BASE_DIR / ".env"

# Сервисы рабочего пространства. id совпадает с префиксом адреса (/moderator/ …) и с проверкой в nginx.
SERVICES = [
    # tile_name — подпись на плитке главной: она говорит, что человек получит, а не как сервис зовётся.
    # Имя сервиса — «Совещание» (решение пользователя 21.09): он и есть совещание целиком.
    # Слово «Модератор» осталось только за вкладкой внутри — документом ведущего.
    # Служебный id и адрес /moderator/ не меняются: на них держатся ссылки, nginx и выкладка.
    {"id": "moderator", "name": "Совещание", "url": "/moderator/",
     "description": "Всё совещание в одном месте: повестка, участники, материалы, модератор, поручения",
     "new_url": "/moderator/#new", "new_label": "Новое совещание"},
    {"id": "brief", "name": "Бриф из справки", "tile_name": "Бриф", "url": "/brief/",
     "description": "Одностраничный бриф о системе образования региона по сводной справке",
     "new_url": "/brief/#new", "new_label": "Новый бриф"},
    {"id": "a3", "name": "Инфографика A3", "url": "/a3/",
     "description": "Справка, отчёт или план — на одном листе A3 для печати",
     "new_url": "/a3/#new", "new_label": "Новый лист"},
    # «Повестка и участники» снята с портала (решение пользователя 05.10: «повестку мы убили — удали её»):
    # повестка и участники ведутся в «Совещании». Плитки нет ни у кого, /auth/check/agenda отвечает отказом —
    # через nginx сервис больше не открыть. Запись в SERVICE_RUNTIME оставлена, пока сессия сервера не снимет
    # юниты work-agenda/meet-agenda (данные в data/agenda.db никто не трогает).
    {"id": "materials", "name": "Материалы", "tile_name": "Папка мероприятия", "url": "/materials/",
     "description": "Документы к мероприятию в одной папке: раскладываются по правилам, реестр собирается сам, видно чего не хватает",
     "new_url": "/materials/#new", "new_label": "Новое мероприятие"},
    {"id": "theses", "name": "Тезисы", "url": "/theses/",
     "description": "Тезисы выступления по теме или по приложенным документам, по правилам конкретного человека; в Word и во вступительное слово совещания",
     "new_url": "/theses/#new", "new_label": "Новые тезисы"},
    # Открыт всем с плашкой «новое, проверяется» (решение пользователя 05.10 вечером: «почему не вижу ни одного
    # нового раздела»; до этого был hidden — виден только администратору до живой проверки). Живой вызов Искры
    # ещё не проверен; снять "new", когда пользователь подтвердит работу короткой записью.
    {"id": "transcriber", "name": "Транскрибатор", "url": "/transcriber/",
     "description": "Запись совещания или разговора — в текст и краткое саммари",
     "new_url": "/transcriber/#new", "new_label": "Новая запись", "new": True},
    # Открыт всем с плашкой «новое, проверяется» (решение пользователя 05.10): объективка на человека — по имени
    # через открытые источники или по приложенной справке, Word по форме, объединение нескольких с содержанием
    # по фамилиям. Снять "new" после живой проверки пользователем.
    {"id": "profiles", "name": "Объективки", "url": "/profiles/",
     "description": "Объективка на человека: по имени из открытых источников или по приложенной справке, Word на одной странице",
     "new_url": "/profiles/#new", "new_label": "Новая объективка", "new": True},
]
# Как сервис устроен на ПК: порт для проверки живости, переменная с папкой, где лежат данные.
# Портал сам стоит рядом, поэтому берёт всё с той же машины (страница «Состояние» в админке).
SERVICE_RUNTIME = {
    "portal": {"port": 8700, "home_env": "", "data": "data/portal.db"},
    "moderator": {"port": 8765, "home_env": "MODERATOR_DIR", "data": "data/meetings.db"},
    "brief": {"port": 8931, "home_env": "BRIEF_HOME", "data": "Результаты"},
    "a3": {"port": 8940, "home_env": "A3_HOME", "data": "data/a3.db"},
    "agenda": {"port": 8950, "home_env": "AGENDA_HOME", "data": "data/agenda.db"},
    "theses": {"port": 8960, "home_env": "THESES_HOME", "data": "data/theses.db"},
    "materials": {"port": 8970, "home_env": "MATERIALS_HOME", "data": "Хранилище"},
    "transcriber": {"port": 8980, "home_env": "TRANSCRIBER_HOME", "data": "data/transcriber.db"},
    "profiles": {"port": 8990, "home_env": "PROFILES_HOME", "data": "data/profiles.db"},
}
SERVICE_IDS = [s["id"] for s in SERVICES]


def service_port(service_id: str) -> int | None:
    """Порт сервиса. На боевом ПК он закреплён, на локальном стенде свой — его задаёт dev_stack.sh."""
    run = SERVICE_RUNTIME.get(service_id) or {}
    if service_id == "portal":
        url = read_env("PORTAL_URL")
        tail = url.rsplit(":", 1)[-1].strip("/") if ":" in url.rsplit("/", 1)[-1] else ""
        if tail.isdigit():
            return int(tail)
    dev = read_env(f"DEV_{service_id.upper()}_PORT")
    if dev.isdigit():
        return int(dev)
    return run.get("port")
VISIBLE_SERVICES = [s for s in SERVICES if not s.get("hidden")]
# Роли. Их две: администратор настраивает пространство и смотрит всё, сотрудник ведёт документы.
# Отдельная роль «только смотреть» была заведена 19.09 и в тот же день убрана решением пользователя:
# наблюдать он будет из-под учётной записи администратора, которая документы всё равно не правит.
ROLES = {"admin": "Администратор", "employee": "Сотрудник"}
READ_ONLY_ROLES: set[str] = set()

# Глубина подчинения: не больше трёх уровней (например, начальник управления → начальник отдела → специалист).
MAX_CHAIN = 3

# Администратор администрирует, а не ведёт документы (решение пользователя от 19.09):
# под его учётной записью нельзя создавать и править документы — работа идёт под личными
# учётками, чтобы в журнале стоял конкретный человек. Исключение — необратимые действия
# и настройки сервисов: их по контракту (п. 2) делает только администратор.
# Если сервис заводит новое действие «только для администратора» — путь добавляется сюда.
ADMIN_WRITE_PATHS = {
    "moderator": [("DELETE", r"/api/meetings/[^/]+/permanent")],
    "a3": [("DELETE", r"/api/sheets/\d+")],
    "brief": [("POST", r"/api/purge"), ("POST", r"/api/settings"), ("POST", r"/api/test")],
    # Маршрут «permanent» исчез при переделке 20.09; нынешние необратимые действия — эти три.
    # Пока список не совпадал с сервисом, стереть не мог никто: сотрудника не пускал сервис,
    # администратора — портал.
    "materials": [("DELETE", r"/api/events/[^/]+"),
                  ("DELETE", r"/api/events/[^/]+/archive/[^/]+"),
                  ("DELETE", r"/api/waiting/[^/]+")],
    "transcriber": [("DELETE", r"/api/records/[^/]+")],
    "profiles": [("DELETE", r"/api/profiles/[^/]+/permanent")],   # «Стереть навсегда»; обычное «Удалить» — в архив
}


def read_env(name: str, default: str = "") -> str:
    val = os.environ.get(name, "")
    if val:
        return val
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if line.startswith(name + "="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return default


def ensure_env(name: str) -> str:
    """Секрет из окружения или .env; если его нет — сгенерировать и сохранить в .env.

    Запись атомарная: иначе одновременная замена ключа Искры может потерять SECRET_KEY
    и разлогинить всех.
    """
    val = read_env(name)
    if val:
        return val
    val = secrets.token_hex(32)
    write_env(name, val)
    return val


def write_env(name: str, value: str) -> None:
    """Записать строку в .env портала атомарно; пустое значение убирает строку."""
    lines = ENV_PATH.read_text(encoding="utf-8-sig").splitlines() if ENV_PATH.exists() else []
    lines = [l for l in lines if not l.strip().startswith(name + "=")]
    if value:
        lines.append(f"{name}={value}")
    tmp = ENV_PATH.with_suffix(ENV_PATH.suffix + ".tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.replace(tmp, ENV_PATH)


class SecretMissing(RuntimeError):
    """PORTAL_SECRET не найден — сервисы не смогут доверять заголовкам."""


def portal_secret() -> str:
    """Общий секрет nginx ↔ сервисы: подтверждает, что заголовки X-Portal-* поставил портал.

    Его нельзя молча сгенерировать: новый секрет не совпадёт с тем, что стоит в nginx и
    в сервисах, и всё рабочее пространство тихо развалится. Лучше явная ошибка.
    """
    val = read_env("PORTAL_SECRET")
    if not val:
        raise SecretMissing("PORTAL_SECRET не задан: проверьте .env портала и настройку nginx")
    return val


def secure_cookies() -> bool:
    return read_env("PORTAL_INSECURE_COOKIES") != "1"
