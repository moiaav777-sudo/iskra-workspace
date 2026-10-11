"""Вход и защита по контракту портала (см. ../../CLAUDE.md).

Своего входа у сервиса нет: пользователя сообщает nginx заголовками X-Portal-*.
Верить им можно только при совпадении X-Portal-Secret с PORTAL_SECRET.
"""
from __future__ import annotations

import hmac
from urllib.parse import unquote

from fastapi import HTTPException, Request

from . import config

UNSAFE = {"POST", "PUT", "PATCH", "DELETE"}


def current_user(request: Request) -> dict[str, str]:
    if config.DEV:
        return dict(config.DEV_USER)
    secret = request.headers.get("X-Portal-Secret", "")
    if not config.PORTAL_SECRET or not hmac.compare_digest(
        secret.encode("utf-8"), config.PORTAL_SECRET.encode("utf-8")
    ):
        raise HTTPException(status_code=403, detail="Запрос не от портала")
    login = request.headers.get("X-Portal-User", "").strip()
    if not login:
        raise HTTPException(status_code=403, detail="Портал не сообщил пользователя")
    role = request.headers.get("X-Portal-Role", "employee").strip() or "employee"
    name = unquote(request.headers.get("X-Portal-Name", "")).strip()
    # Круг показа от портала: логины подчинённых через запятую, «*» — видно всё.
    team_raw = (request.headers.get("X-Portal-Team") or "").strip()
    team = ["*"] if team_raw == "*" else [t for t in (x.strip() for x in team_raw.split(",")) if t]
    return {"login": login, "role": role, "name": name or login, "team": team}


def require_admin(user: dict[str, str]) -> None:
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Действие доступно только администратору")


def check_origin(request: Request) -> None:
    """Изменяющий запрос с чужого сайта отклоняем."""
    if request.method not in UNSAFE:
        return
    origin = request.headers.get("Origin")
    if not origin:
        return
    host = request.headers.get("Host", "")
    if origin.split("://", 1)[-1] != host:
        raise HTTPException(status_code=403, detail="Запрос с чужого сайта")


def client_ip(request: Request) -> str:
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else ""
