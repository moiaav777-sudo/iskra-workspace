"""Инфографика A3 в портале: конструктор листа (a3-kit.html).

Страница отдаётся самим порталом; доступ проверяется и в nginx, и здесь.
"""
from __future__ import annotations

from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from . import auth, config, db

router = APIRouter()


def kit_path() -> Path:
    return Path(config.read_env("A3_KIT") or config.BASE_DIR / "a3" / "a3-kit.html")


@router.get("/a3")
def a3_noslash():
    return RedirectResponse("/a3/", status_code=301)


@router.get("/a3/")
def a3_kit(request: Request):
    user = auth.verify_session(request.cookies.get(auth.SESSION_COOKIE))
    if not user:
        return RedirectResponse("/login?next=" + quote("/a3/"), status_code=302)
    if user["must_change_password"]:
        return RedirectResponse("/account?force=1", status_code=302)
    if "a3" not in db.allowed_services(user):
        return RedirectResponse("/?denied=a3", status_code=302)
    path = kit_path()
    if not path.exists():
        return HTMLResponse("<h1>Конструктор A3 не установлен</h1>", status_code=503)
    html = path.read_text(encoding="utf-8")
    i = html.lower().rfind("</body>")
    if i != -1:
        html = html[:i] + BACK_LINK + html[i:]
    return HTMLResponse(html, headers={"Cache-Control": "no-store"})


# Ссылка «← Все сервисы» поверх конструктора; при печати не видна.
BACK_LINK = (
    '<a href="/" id="portal-back" style="position:fixed;left:12px;bottom:12px;z-index:99999;'
    'background:#062a5f;color:#fff;font:600 13px/1 -apple-system,Segoe UI,Arial,sans-serif;'
    'padding:9px 12px;border-radius:8px;text-decoration:none;box-shadow:0 2px 8px rgba(0,0,0,.25)">← Все сервисы</a>'
    '<style>@media print{#portal-back{display:none!important}}</style>'
)
