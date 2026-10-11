"""Работа за порталом workspace.example.ru: пользователь из заголовков nginx и журнал действий.

Режим портала включается, когда задан PORTAL_SECRET (переменная окружения или файл .env рядом
с папкой «Результаты»). Без него программа работает по-старому — только для этого компьютера.
"""
import hmac
import json
import os
import queue
import threading
import urllib.request
from pathlib import Path
from urllib.parse import unquote

_ENV_CACHE = {}


def _env(root, name, default=""):
    val = os.environ.get(name, "")
    if val:
        return val
    path = Path(root) / ".env"
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return default
    cached = _ENV_CACHE.get(path)
    if not cached or cached[0] != mtime:
        values = {}
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                values[k.strip()] = v.strip().strip('"').strip("'")
        _ENV_CACHE[path] = cached = (mtime, values)
    return cached[1].get(name, default)


def enabled(root):
    return bool(_env(root, "PORTAL_SECRET"))


def user_from_headers(root, headers):
    secret = _env(root, "PORTAL_SECRET")
    if not secret or not hmac.compare_digest(headers.get("X-Portal-Secret") or "", secret):
        return None
    login = (headers.get("X-Portal-User") or "").strip()
    if not login:
        return None
    role = headers.get("X-Portal-Role") or "employee"
    # круг показа (ACCESS-SPEC.md): логины тех, кто под человеком по цепочке; «*» — всё пространство
    # (администратор и роль chief). Это фильтр показа, а не запрет
    team_raw = (headers.get("X-Portal-Team") or "").strip()
    team = ["*"] if team_raw == "*" else [t for t in (x.strip() for x in team_raw.split(",")) if t]
    return {"login": login, "is_admin": role == "admin", "role": role, "team": team,
            "name": unquote(headers.get("X-Portal-Name") or "") or login}


_PEOPLE = {"at": 0.0, "list": []}


def people(root):
    """Сотрудники пространства для окна «Доступ» и подписи «чьё это»: GET /internal/users,
    держим минуту. Портал молчит — последний полученный список (или пустой)."""
    import time
    if time.time() - _PEOPLE["at"] < 60 and _PEOPLE["list"]:
        return _PEOPLE["list"]
    secret = _env(root, "PORTAL_SECRET")
    if not secret:
        return []
    url = _env(root, "PORTAL_URL", "http://127.0.0.1:8700") + "/internal/users"
    try:
        req = urllib.request.Request(url, headers={"X-Portal-Secret": secret})
        with urllib.request.urlopen(req, timeout=5) as r:
            data = json.loads(r.read() or b"{}")
        rows = [{"login": str(u.get("login") or ""), "name": str(u.get("name") or u.get("login") or "")}
                for u in data.get("users", []) if isinstance(u, dict) and u.get("login") and u.get("active", True)]
    except Exception:
        return _PEOPLE["list"]
    _PEOPLE.update(at=time.time(), list=rows)
    return rows


_QUEUE = queue.Queue(maxsize=10000)
_WORKER = {"thread": None}
_WORKER_LOCK = threading.Lock()


def _deliver():
    while True:
        root, path, payload = _QUEUE.get()
        secret = _env(root, "PORTAL_SECRET")
        url = _env(root, "PORTAL_URL", "http://127.0.0.1:8700") + path
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        for _ in range(3):
            try:
                req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json",
                                                                      "X-Portal-Secret": secret})
                urllib.request.urlopen(req, timeout=5).close()
                break
            except Exception:
                continue


def _send(root, path, payload):
    """Одна очередь на всё, что уходит в портал: по порядку, в фоне и без поломок при сбое."""
    if not _env(root, "PORTAL_SECRET"):
        return
    with _WORKER_LOCK:
        t = _WORKER["thread"]
        if t is None or not t.is_alive():
            _WORKER["thread"] = threading.Thread(target=_deliver, name="portal-send", daemon=True)
            _WORKER["thread"].start()
    try:
        _QUEUE.put_nowait((root, path, payload))
    except queue.Full:
        pass


def send_audit(root, **event):
    """Событие в журнал портала: по одному и по порядку, не задерживая ответ и не ломая работу при сбое."""
    _send(root, "/internal/audit", {"service": "brief", **event})


def ask_person(root, name, role="", organization=""):
    """Что сказал бы реестр людей о находке разбора (контракт п. 5а, решение пользователя 21.09).

    Пишет в реестр только человек — окном «Добавить в реестр?» и запросом из браузера. Отсюда
    уходит лишь вопрос: POST /internal/people с force: false ничего не меняет и отвечает
    suggest — new (такого нет), differs (записан иначе), same (совпадает). None — портала
    нет или он не ответил: спросить нельзя, и окно не показываем.
    """
    secret = _env(root, "PORTAL_SECRET")
    if not secret:
        return None
    url = _env(root, "PORTAL_URL", "http://127.0.0.1:8700") + "/internal/people"
    body = json.dumps({"name": name, "role": role, "organization": organization, "force": False,
                       "login": "", "source": "brief"}, ensure_ascii=False).encode("utf-8")
    try:
        req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json",
                                                              "X-Portal-Secret": secret})
        with urllib.request.urlopen(req, timeout=8) as r:
            answer = str(json.loads(r.read() or b"{}").get("suggest") or "")
        return answer if answer in ("new", "differs", "same") else None
    except Exception:
        return None


def check_people(root, names):
    """Сверка ФИО с реестром людей перед выводом в документ (контракт п. 5а).

    Ответ нужен до сборки, поэтому зовём сразу, а не очередью. Возвращает словарь «ФИО → ответ
    портала» ({status: exact | close | unknown, ...}); None — портала нет (программа работает
    сама по себе); {} — портал есть, но не ответил: сверка не прошла, и об этом надо сказать.
    """
    secret = _env(root, "PORTAL_SECRET")
    if not secret:
        return None
    url = _env(root, "PORTAL_URL", "http://127.0.0.1:8700") + "/internal/people/check"
    body = json.dumps({"names": list(names)}, ensure_ascii=False).encode("utf-8")
    try:
        req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json",
                                                              "X-Portal-Secret": secret})
        with urllib.request.urlopen(req, timeout=8) as r:
            items = json.loads(r.read() or b"{}").get("items") or []
        return {str(x.get("name") or ""): x for x in items if isinstance(x, dict)}
    except Exception:
        return {}
