"""Вкладка «Результаты»: документы после совещания из «Транскрибатора» (решение пользователя 02.10).

«Транскрибатор» кладёт сюда полную транскрипцию, саммари и проект протокольных решений —
запросом из браузера человека за порталом (same-origin, как A3 кладёт материалы): пользователь,
права и проверка `Origin` — общие, из `main.auth_middleware`. Правки одного совещания идут по
очереди (замок совещания там же), поэтому повтор с тем же источником дублем не ляжет.
"""
from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, HTTPException

from . import portal, readiness, richtext, storage
from .deps import MAX_RESULT_CHARS, meeting_or_404, pos
from .models import RESULT_TITLES, Result
from .schemas import ResultIn

router = APIRouter()

# Кто может прислать документ и под каким адресом у него лежат ссылки (белый список, памятка §8).
SOURCES = {"transcriber": {"name": "«Транскрибатора»", "prefix": "/transcriber/"}}
KIND_WORDS = {"transcript": "транскрипция", "summary": "саммари", "decisions": "проект протокольных решений"}
_GOT = {"transcript": ("получена", "обновлена"), "summary": ("получено", "обновлено"),
        "decisions": ("получен", "обновлён")}


def _inner_url(url: str, prefix: str, what: str) -> str:
    """Ссылка соседа — только относительный путь под его префиксом: чужой сайт из совещания не открываем."""
    url = (url or "").strip()
    if not url:
        return ""
    low = url.lower()
    bad = (not url.startswith(prefix) or len(url) > 500 or "//" in url or "\\" in url
           or any(ord(c) < 33 or ord(c) == 127 for c in url)
           or "/../" in url.split("#", 1)[0].split("?", 1)[0] + "/"
           or any(x in low for x in ("%2e", "%2f", "%5c")))
    if bad:
        raise HTTPException(status_code=400,
                            detail=f"{what}: принимается только адрес сервиса вида {prefix}…, без адреса сайта")
    return url


def _size_ok(value: str, what: str) -> None:
    if len(value) > MAX_RESULT_CHARS:
        def num(n: int) -> str:
            return f"{n:,}".replace(",", " ")
        raise HTTPException(
            status_code=413,
            detail=f"{what} слишком большой: {num(len(value))} знаков, предел — {num(MAX_RESULT_CHARS)}. "
                   "Скачайте его в Word в «Транскрибаторе»")


def _now() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


@router.post("/api/meetings/{mid}/results")
def put_result(mid: str, body: ResultIn):
    """Положить документ; повтор с тем же `source.service + source.id` и `kind` заменяет прежний."""
    m = meeting_or_404(mid)
    service = (body.source.service or "").strip()
    src = SOURCES.get(service)
    if src is None:
        raise HTTPException(status_code=400, detail="Документы во вкладку «Результаты» принимаются только "
                                                    "из «Транскрибатора»")
    source_id = (body.source.id or "").strip()
    if not source_id or len(source_id) > 100:
        raise HTTPException(status_code=400, detail="Не указана запись «Транскрибатора» (source.id)")
    source_url = _inner_url(body.source.url, src["prefix"], "Ссылка на запись")
    docx_url = _inner_url(body.docx_url, src["prefix"], "Ссылка «Скачать Word»")
    _size_ok(body.html, "Документ")
    _size_ok(body.text, "Текст документа")

    html = richtext.clean_html(body.html)
    text = (body.text or "").strip() or richtext.html_text(html)
    kind_word = KIND_WORDS[body.kind]
    # пустая разметка («<p> </p>») — тоже пустой документ (ревьюер RC-2026-10-02-18, Н-4)
    if not richtext.html_text(html).strip() and not text:
        # «Транскрибатор» шлёт все три вида, даже если саммари или решений ещё нет: пустой документ
        # не ошибка (иначе остальные не дошли бы), но и не запись — прежний, если был, не стираем
        portal.note_change(f"из {src['name']}: пустой документ ({kind_word}) — не сохранён")
        return {"meeting": m.to_dict(), "readiness": readiness.readiness(m), "stored": False}
    title = " ".join((body.title or "").split())[:300] or RESULT_TITLES[body.kind]
    now = _now()
    same = next((r for r in m.results if r.kind == body.kind and r.source.get("service") == service
                 and r.source.get("id") == source_id), None)
    if same is None:
        m.results.append(Result(kind=body.kind, title=title, html=html, text=text, docx_url=docx_url,
                                source={"service": service, "id": source_id, "url": source_url},
                                created=now, updated=now))
        verb = _GOT[body.kind][0]
    else:
        same.title, same.html, same.text, same.docx_url = title, html, text, docx_url
        same.source = {"service": service, "id": source_id, "url": source_url}
        same.updated = now
        verb = _GOT[body.kind][1]
    portal.note_change(f"из {src['name']} {verb} {kind_word}: «{title[:120]}»")
    storage.save(m)
    return {"meeting": m.to_dict(), "readiness": readiness.readiness(m), "stored": True}


@router.delete("/api/meetings/{mid}/results/{rid}")
def remove_result(mid: str, rid: str):
    m = meeting_or_404(mid)
    gone = m.results.pop(pos(m.results, rid, "Документ"))
    portal.note_change(f"убран документ «{gone.title[:120]}» ({KIND_WORDS.get(gone.kind, gone.kind)})")
    storage.save(m)
    return {"meeting": m.to_dict(), "readiness": readiness.readiness(m)}
