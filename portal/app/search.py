"""Поиск по всем сервисам сразу: портал спрашивает каждый и складывает ответы.

Зачем: работа идёт вокруг темы или региона («Удмуртия», «дети-сироты»), а материалы
лежат в четырёх сервисах. Раньше искать приходилось в каждом отдельно.

Как: у каждого сервиса есть `GET /api/search?q=&limit=`, он отвечает своим списком.
Портал ходит к сервисам по 127.0.0.1 с общим секретом и заголовками того же человека,
что и спрашивает, — прав это не расширяет: сервис отдаёт ровно то, что показал бы ему сам.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from . import config, db

TIMEOUT = 5
PER_SERVICE = 5
MIN_QUERY = 2


def _ask_service(service_id: str, user: dict, query: str) -> dict:
    """Один сервис. Ошибка сервиса не должна ронять весь поиск — она просто видна в ответе."""
    name = next((s["name"] for s in config.SERVICES if s["id"] == service_id), service_id)
    url = (f"http://127.0.0.1:{config.service_port(service_id)}/api/search"
           f"?q={urllib.parse.quote(query)}&limit={PER_SERVICE}")
    group = {"service": service_id, "name": name, "items": [], "error": ""}
    request = urllib.request.Request(url, headers={
        "X-Portal-Secret": config.portal_secret(),
        "X-Portal-User": user["login"],
        "X-Portal-Role": user["role"],
        "X-Portal-Name": urllib.parse.quote(user.get("full_name") or user["login"]),
        "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            payload = json.loads(response.read().decode("utf-8", "replace") or "{}")
    except urllib.error.HTTPError as e:
        group["error"] = "не умеет искать" if e.code == 404 else f"ответил {e.code}"
        return group
    except (urllib.error.URLError, OSError, ValueError):
        group["error"] = "не отвечает"
        return group

    items = payload.get("items")
    if not isinstance(items, list):
        group["error"] = "ответил непонятным"
        return group
    for item in items[:PER_SERVICE]:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()[:200]
        url_item = str(item.get("url") or "").strip()[:300]
        # адрес принимаем только внутренний: сервис не должен уводить человека на чужой сайт
        if not title or not url_item.startswith("/"):
            continue
        same = item.get("same_as") if isinstance(item.get("same_as"), dict) else None
        group["items"].append({
            "title": title,
            "url": url_item,
            "when": str(item.get("when") or "")[:40],
            "who": str(item.get("who") or "")[:100],
            "kind": str(item.get("kind") or "")[:40],
            "same_as": {"service": str(same.get("service") or "")[:40],
                        "id": str(same.get("id") or "")[:100]} if same else None,
        })
    return group


def _merge_pairs(groups: list[dict]) -> None:
    """Одно совещание не должно находиться дважды.

    «Повестка» помечает записи, уже уехавшие в «Модератор». Если обе строки попали в ответ,
    оставляем ту, где идёт работа (в «Модераторе»), и вешаем на неё вторую ссылку.
    Если пара не нашлась — просто показываем, что документ есть и в другом сервисе.
    """
    by_service = {g["service"]: g for g in groups}
    for group in groups:
        for item in list(group["items"]):
            same = item.get("same_as")
            if not same or not same.get("id"):
                continue
            twin_group = by_service.get(same["service"])
            twin = None
            if twin_group:
                twin = next((t for t in twin_group["items"] if t["url"].endswith("=" + same["id"])), None)
            name = next((s["name"] for s in config.SERVICES if s["id"] == same["service"]), same["service"])
            if twin is not None:
                twin["also"] = {"name": group["name"], "url": item["url"]}
                group["items"].remove(item)
            else:
                item["also"] = {"name": name, "url": f"/{same['service']}/#meeting={same['id']}"}


def search(user: dict, query: str) -> dict:
    query = query.strip()
    if len(query) < MIN_QUERY:
        return {"query": query, "groups": [], "short": True}
    services = [s for s in db.allowed_services(user) if s in config.SERVICE_RUNTIME]
    if not services:
        return {"query": query, "groups": []}
    with ThreadPoolExecutor(max_workers=len(services)) as pool:
        groups = list(pool.map(lambda s: _ask_service(s, user, query), services))
    _merge_pairs(groups)
    order = {s["id"]: i for i, s in enumerate(config.SERVICES)}
    groups.sort(key=lambda g: (not g["items"], order.get(g["service"], 99)))
    return {"query": query, "groups": groups,
            "found": sum(len(g["items"]) for g in groups)}
