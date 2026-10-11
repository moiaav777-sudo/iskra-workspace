"""Состояние рабочего пространства: живы ли сервисы, что с копиями и местом на диске.

Раньше это можно было узнать только с самого ПК через удалённый стол. Данные сотрудников
здесь не читаются: только живость, версия, размер файлов и итоги ночных копий.
"""
from __future__ import annotations

import json
import shutil
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import config

CACHE_SECONDS = 10          # страницу обновляют часто, а лишний обход диска ни к чему
_cache: dict = {"time": 0.0, "data": None}


def _service_home(service_id: str) -> Path | None:
    run = config.SERVICE_RUNTIME.get(service_id) or {}
    if not run.get("home_env"):
        return config.BASE_DIR
    home = config.read_env(run["home_env"])
    return Path(home) if home else None


def _ping(port: int) -> float:
    """Один запрос к /healthz; время в мс. perf_counter, а не monotonic: на Windows у monotonic шаг
    15,6 мс, и все замеры выходили кратными ему (703 мс = 45 тиков — находка SRE 21.09)."""
    started = time.perf_counter()
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=4) as r:
        if r.status != 200:
            raise ValueError(f"код {r.status}")
        r.read(200)
    return (time.perf_counter() - started) * 1000


def _healthz(port: int, tries: int = 3) -> dict:
    """Живость: сервис обязан отвечать 200 на /healthz без входа (контракт с порталом).
    Один прогревочный запрос не считается, дальше `tries` замеров: медиана и максимум."""
    try:
        # Общий срок на сервис — 2 с вместе с прогревом: быстрый сервис успевает все замеры, медленный
        # даёт прогрев и один замер (хуже всего — 2 таймаута по 4 с). Ревьюер RC-4 и RC-5.
        budget = time.perf_counter() + 2.0
        _ping(port)
        times = []
        while len(times) < tries and (not times or time.perf_counter() < budget):
            times.append(_ping(port))
        times.sort()
    except (urllib.error.URLError, OSError, ValueError) as e:
        return {"ok": False, "ms": None, "ms_max": None, "error": str(getattr(e, "reason", e))[:120]}
    return {"ok": True, "ms": round(times[len(times) // 2]), "ms_max": round(times[-1]), "error": ""}


def _size(path: Path) -> dict:
    """Размер данных сервиса: один файл базы или папка с результатами."""
    try:
        if path.is_file():
            st = path.stat()
            return {"mb": round(st.st_size / 1048576, 1), "files": 1, "changed": int(st.st_mtime)}
        if path.is_dir():
            total = files = 0
            changed = 0
            for f in path.rglob("*"):
                if f.is_file():
                    st = f.stat()
                    total += st.st_size
                    files += 1
                    changed = max(changed, int(st.st_mtime))
                    if files > 50000:      # огромную папку не обходим целиком, хватит оценки
                        break
            return {"mb": round(total / 1048576, 1), "files": files, "changed": changed}
    except OSError:
        pass
    return {"mb": None, "files": 0, "changed": 0}


def _version(home: Path | None) -> str:
    if not home:
        return ""
    try:
        return (home / "VERSION").read_text(encoding="utf-8").strip()[:40]
    except OSError:
        return ""


def _state_file(name: str) -> dict | None:
    """Итог работы ops-скриптов (backup.ps1, backup_check.ps1) — они кладут его в portal/logs."""
    try:
        return json.loads((config.BASE_DIR / "logs" / name).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None


def _one(service: dict) -> dict:
    run = dict(config.SERVICE_RUNTIME.get(service["id"]) or {})
    run["port"] = config.service_port(service["id"])
    home = _service_home(service["id"])
    item = {"id": service["id"], "name": service["name"], "port": run.get("port"),
            "version": _version(home), "home": str(home) if home else "",
            "data": {"mb": None, "files": 0, "changed": 0}}
    item.update(_healthz(run["port"]) if run.get("port") else {"ok": None, "ms": None, "error": "порт не задан"})
    if home and run.get("data"):
        item["data"] = _size(home / run["data"])
    return item


def collect(force: bool = False) -> dict:
    if not force and _cache["data"] and time.time() - _cache["time"] < CACHE_SECONDS:
        return _cache["data"]

    services = [{"id": "portal", "name": "Портал"}] + [dict(s) for s in config.SERVICES]
    with ThreadPoolExecutor(max_workers=len(services)) as pool:
        items = list(pool.map(_one, services))

    try:
        usage = shutil.disk_usage(config.BASE_DIR)
        disk = {"free_gb": round(usage.free / 1073741824, 1), "total_gb": round(usage.total / 1073741824, 1)}
    except OSError:
        disk = {"free_gb": None, "total_gb": None}

    data = {"services": items, "disk": disk, "iskra": _state_file("iskra.state"),
            "backup": _state_file("backup.state"), "backup_check": _state_file("backup-check.state"),
            # уведомления идут через VPS ключом portal-keys\id_notify: есть ключ — настроено
            "notifications": (config.BASE_DIR.parent / "portal-keys" / "id_notify").exists(),
            "checked": int(time.time())}
    _cache.update(time=time.time(), data=data)
    return data
