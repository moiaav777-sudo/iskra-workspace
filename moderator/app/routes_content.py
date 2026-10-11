"""Маршруты поручений, сценария и глоссария."""
from __future__ import annotations

import io
import os
from html import escape
from urllib.parse import quote

from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.responses import Response

from . import agenda_docx, extract, llm_extract, orders_docx, people_flow, portal, readiness, storage
from .deps import at, check_base, check_text, meeting_or_404, pos, read_upload
from .models import (GlossaryEntry, Order, ScenarioBlock, SECTION_CLOSING, SECTION_OPENING,
                     html_to_text, normalize_deadline)
from .schemas import (GlossaryIn, MoveIn, OpeningFromThesesIn, OrderIn, OrdersBulkIn, OrderUpdateIn,
                      ScenarioIn, ScenarioPatchIn)

router = APIRouter()


@router.post("/api/meetings/{mid}/orders")
def add_order(mid: str, body: OrderIn):
    m = meeting_or_404(mid)
    data = body.model_dump()
    data["deadline"] = normalize_deadline(data["deadline"])     # из календаря приходит ГГГГ-ММ-ДД — храним ДД.ММ.ГГГГ
    m.orders.append(Order(**data))
    storage.save(m)
    return {"meeting": m.to_dict(), "readiness": readiness.readiness(m)}


@router.delete("/api/meetings/{mid}/orders/{key}")
def remove_order(mid: str, key: str):
    m = meeting_or_404(mid)
    m.orders.pop(pos(m.orders, key, "Поручение"))
    storage.save(m)
    return {"meeting": m.to_dict(), "readiness": readiness.readiness(m)}


# ── вкладка «Поручения»: конструктор, перечень из материалов и из файла, Word ──
# Решения пользователя 02.10 (`../TRACK.md` → «Модератор» → «Поручения»): каждое поручение — блок с правкой,
# срок — из календаря, перечень предлагает Искра по материалам или по файлу (человек отмечает галочками),
# поручения из файла — выше поручений из материалов, Word — общий модуль `orders_docx` по эталону.

_ORDER_FIELDS = ("text", "assignee", "deadline", "agenda_num")


def _one_line(text: str) -> str:
    return " ".join((text or "").split())


@router.patch("/api/meetings/{mid}/orders/{key}")
def patch_order(mid: str, key: str, body: OrderUpdateIn):
    """Правка поручения в конструкторе. None — не трогаем; `base` разошёлся с записанным — 409."""
    m = meeting_or_404(mid)
    o = at(m.orders, key, "Поручение")
    base = {k: v for k, v in (body.base or {}).items() if k in _ORDER_FIELDS}
    check_base(o, base, mid, "Поручение")
    old_text = o.text
    # номер вопроса — только из повестки или пусто (как у bulk); прежний номер старой записи («I») оставить можно
    if body.agenda_num is not None:
        num = body.agenda_num.strip()
        if num and num != o.agenda_num and num not in {a.num for a in m.agenda if a.num.strip()}:
            raise HTTPException(status_code=400, detail=f"Вопроса {num} в повестке нет – выберите вопрос из списка")
    if body.text is not None:
        text = _one_line(body.text)
        if not text:
            raise HTTPException(status_code=400, detail="Текст поручения пустой – чтобы убрать поручение, "
                                                        "нажмите «Убрать»")
        o.text = text
    if body.assignee is not None:
        o.assignee = _one_line(body.assignee)
    if body.deadline is not None:
        o.deadline = normalize_deadline(body.deadline)
    if body.agenda_num is not None:
        o.agenda_num = body.agenda_num.strip()
    # поручение материала: его текст в списке материала идёт следом (окно материала показывает их)
    if o.material_id and o.text != old_text:
        old_key = llm_extract._order_key(old_text)
        for mat in m.materials:
            if mat.id == o.material_id:
                mat.orders = [o.text if llm_extract._order_key(t) == old_key else t for t in mat.orders]
    storage.save(m)
    return {"meeting": m.to_dict(), "readiness": readiness.readiness(m)}


@router.post("/api/meetings/{mid}/orders/{key}/move")
def move_order(mid: str, key: str, body: MoveIn):
    """↑/↓ в перечне: порядок списка — порядок в Word."""
    m = meeting_or_404(mid)
    idx = pos(m.orders, key, "Поручение")
    target = idx - 1 if body.direction == "up" else idx + 1
    if 0 <= target < len(m.orders):
        m.orders[idx], m.orders[target] = m.orders[target], m.orders[idx]
        storage.save(m)
    return {"meeting": m.to_dict(), "readiness": readiness.readiness(m)}


@router.post("/api/meetings/{mid}/orders/bulk")
def bulk_orders(mid: str, body: OrdersBulkIn):
    """Отмеченные человеком предложения → в перечень. Дубль по `_order_key` (с записанными и внутри
    пакета) не заводим — повторное нажатие ничего не задваивает. Поручения из файла — выше остальных."""
    m = meeting_or_404(mid)
    materials = {x.id: x for x in m.materials if x.id}
    nums = {a.num for a in m.agenda if a.num.strip()}
    seen = {llm_extract._order_key(o.text) for o in m.orders}
    added = skipped = 0
    for it in body.orders:
        text = _one_line(it.text)
        okey = llm_extract._order_key(text)
        if not okey or okey in seen:
            skipped += 1
            continue
        seen.add(okey)
        mat = materials.get(it.material_id) if it.source == "material" else None
        qnum = it.question_num.strip()
        if qnum not in nums:
            qnum = ""
        if mat is not None and not qnum:
            qnum = mat.question_num
        m.orders.append(Order(text=text, assignee=_one_line(it.assignee),
                              deadline=normalize_deadline(it.deadline), agenda_num=qnum,
                              material_id=mat.id if mat is not None else "", source=it.source))
        added += 1
    m.orders = llm_extract.file_orders_first(m.orders)
    storage.save(m)
    portal.note_change(f"в перечень поручений добавлено {added}" + (f", дублей пропущено {skipped}" if skipped else ""))
    return {"meeting": m.to_dict(), "readiness": readiness.readiness(m), "added": added, "skipped": skipped}


def _question_order(m) -> dict[str, int]:
    return {a.num: i for i, a in enumerate(x for x in m.agenda if x.num.strip())}


def _finish_proposals(mid: str, items: list[dict]) -> list[dict]:
    """Порядок по вопросам (общие — первыми), дубли внутри — прочь, `exists` — по свежему совещанию:
    Искра могла думать минуты, а коллега — добавить поручения."""
    m = meeting_or_404(mid)
    order = _question_order(m)

    def rank(item):
        q = str(item.get("question_num") or "").strip()
        return -1 if not q else order.get(q, len(order))

    existing = {llm_extract._order_key(o.text) for o in m.orders}
    seen: set[str] = set()
    out = []
    for item in sorted(items, key=rank):           # sorted — устойчивая: порядок внутри вопроса прежний
        okey = llm_extract._order_key(item["text"])
        if not okey or okey in seen:
            continue
        seen.add(okey)
        out.append(dict(item, id=f"p{len(out) + 1}", exists=okey in existing))
    return out


@router.post("/api/meetings/{mid}/orders/propose-from-materials")
def propose_from_materials(mid: str):
    """Проект перечня из материалов: готовые поручения материалов (их предложила Искра при разборе) + один
    прогон Искры по материалам, у которых поручений нет, но есть вывод/предложения/тезисы. Ничего не сохраняет:
    человек отмечает галочками и шлёт `POST …/orders/bulk`."""
    m = meeting_or_404(mid)
    if not m.materials:
        raise HTTPException(status_code=400, detail="Сначала добавьте и разберите материалы к вопросам – "
                                                    "перечень собирается из них")
    nums = {a.num for a in m.agenda if a.num.strip()}
    items: list[dict] = []
    ask = []
    for mat in m.materials:
        texts = [t for t in (mat.orders or []) if str(t).strip()]
        if texts:
            linked = {llm_extract._order_key(o.text): o for o in m.orders if mat.id and o.material_id == mat.id}
            for t in texts:
                o = linked.get(llm_extract._order_key(t))
                items.append({"text": _one_line(t), "assignee": o.assignee if o else "",
                              "deadline": o.deadline if o else "", "question_num": mat.question_num,
                              "material_id": mat.id, "source": "material"})
        elif mat.conclusion or mat.proposals or mat.theses or mat.proposal:
            ask.append(mat)
    warnings: list[str] = []
    asked = 0
    if ask:
        labels = [(f"M{i}", mat) for i, mat in enumerate(ask, 1)]
        by_label = dict(labels)
        try:
            raw = llm_extract.propose_orders(llm_extract.orders_input_from_materials(m, labels), warnings)
        except Exception as e:
            if not items:
                raise HTTPException(status_code=502, detail=f"Искра не ответила: {e}")
            warnings.append(f"Искра не ответила ({e}) – показаны только готовые поручения материалов")
            raw = []
        for r in raw:
            mat = by_label.get(r["material_id"].strip("[] "))
            qnum = mat.question_num if mat is not None else (r["question_num"] if r["question_num"] in nums else "")
            items.append({"text": _one_line(r["text"]), "assignee": r["assignee"], "deadline": r["deadline"],
                          "question_num": qnum, "material_id": mat.id if mat is not None else "",
                          "source": "material"})
            asked += 1
    if not items and not warnings:
        warnings.append("В материалах нет ни поручений, ни выводов и предложений – Искре не из чего собрать перечень")
    out = _finish_proposals(mid, items)
    portal.note_change(f"предложено поручений: {len(out)} (Искрой – {asked}, материалов без поручений – "
                       f"{len(ask)}), уже в перечне – {sum(1 for x in out if x['exists'])}")
    return {"proposals": out, "warnings": warnings, "asked_iskra": bool(ask)}


@router.post("/api/meetings/{mid}/orders/propose-from-file")
def propose_from_file(mid: str, file: UploadFile = File(...)):
    """Файл с поручениями (Word, Excel, PowerPoint, PDF, RTF, ODT, текст — по содержимому) → текст →
    Искра вычленяет поручения → предложения с галочками. Файл не хранится; ничего не сохраняет."""
    m = meeting_or_404(mid)
    name = os.path.basename(file.filename or "")
    data = read_upload(file)
    if extract.problem(name, data) and not extract.listed(name):
        raise HTTPException(status_code=400, detail=f"Файл «{name or 'без имени'}» не принимается: нужны "
                                                    f"{extract.WHAT}")
    try:
        text = extract.extract_bytes(data, name)
    except Exception as e:      # битый PDF с верной сигнатурой бросает не ValueError — как у соседей, 400, не 500
        raise HTTPException(status_code=400, detail=f"Не удалось извлечь текст из «{name}»: {e}")
    text = (text or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail=f"В файле «{name}» нет текста – возможно, это скан")
    check_text(text)
    qs = [f"{a.num}. {a.title}" for a in m.agenda if a.num.strip()]
    user = ("Вопросы повестки:\n" + "\n".join(qs) + "\n\n" if qs else "") + \
        f"Документ с поручениями («{name}»):\n" + text
    warnings: list[str] = []
    try:
        raw = llm_extract.propose_orders(user, warnings)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Искра не ответила: {e}")
    nums = {a.num for a in m.agenda if a.num.strip()}
    items = [{"text": _one_line(r["text"]), "assignee": r["assignee"], "deadline": r["deadline"],
              "question_num": r["question_num"] if r["question_num"] in nums else "", "material_id": "",
              "source": "file"} for r in raw]
    if not items:
        warnings.append("Искра не нашла в файле поручений")
    out = _finish_proposals(mid, items)
    portal.note_change(f"файл с поручениями разобран: предложено {len(out)}, уже в перечне – "
                       f"{sum(1 for x in out if x['exists'])}")
    return {"proposals": out, "warnings": warnings}


@router.get("/api/meetings/{mid}/orders/export.docx")
def export_orders_docx(mid: str, unchecked: bool = False):
    """«Перечень поручений» в Word — общий модуль `orders_docx` по эталону (`docs/примеры/…`), порядок —
    порядок перечня. Подпись председателя сверяется с реестром, как ФИО любого документа совещания."""
    from .routes_meetings import DOCX_MEDIA, _require_checked
    m = meeting_or_404(mid)
    if not any(o.text.strip() for o in m.orders):
        raise HTTPException(status_code=400, detail="В перечне нет поручений – добавьте их на вкладке «Поручения»")
    checked = _require_checked(people_flow.check_orders(m), unchecked)
    when = orders_docx.parse_deadline(m.date)
    data = orders_docx.build([{"text": o.text, "assignee": o.assignee, "deadline": o.deadline} for o in m.orders],
                             chair=m.chair, chair_role=m.chair_role, when=when)
    name = orders_docx.file_name(when)
    if checked["unavailable"]:
        from docx import Document
        doc = Document(io.BytesIO(data))
        agenda_docx._mark_unchecked(doc)            # та же пометка, что у «Повестки» и «Списка участников»
        data = agenda_docx.to_bytes(doc)
        name = name[:-len(".docx")] + " (ФИО не сверены).docx"
    return Response(
        data, media_type=DOCX_MEDIA,
        headers={"Content-Disposition": f"attachment; filename=\"orders.docx\"; filename*=UTF-8''{quote(name)}",
                 "X-Filename": quote(name),
                 "X-Names-Check": "skipped" if checked["unavailable"] else "done"},
    )


# ── сценарий ────────────────────────────────────────────────────────────


@router.post("/api/meetings/{mid}/scenario")
def add_scenario(mid: str, body: ScenarioIn):
    m = meeting_or_404(mid)
    m.scenario.append(ScenarioBlock(**body.model_dump()))
    storage.save(m)
    return {"meeting": m.to_dict(), "readiness": readiness.readiness(m)}


@router.delete("/api/meetings/{mid}/scenario/{key}")
def remove_scenario(mid: str, key: str):
    m = meeting_or_404(mid)
    m.scenario.pop(pos(m.scenario, key, "Блок сценария"))
    storage.save(m)
    return {"meeting": m.to_dict(), "readiness": readiness.readiness(m)}


@router.patch("/api/meetings/{mid}/scenario/{key}")
def patch_scenario(mid: str, key: str, body: ScenarioPatchIn):
    m = meeting_or_404(mid)
    b = at(m.scenario, key, "Блок сценария")
    check_base(b, body.base, mid, "Блок сценария")
    b.text = body.text
    b.speaker = body.speaker
    b.minutes = body.minutes
    storage.save(m)
    return {"meeting": m.to_dict(), "readiness": readiness.readiness(m)}


def _bullets_html(lines: list[str]) -> str:
    """Строки тезисов → разметка сценария: он хранится rich-текстом (Quill), а не простым."""
    if len(lines) == 1:
        return f"<p>{escape(lines[0])}</p>"
    return "<ul>" + "".join(f"<li>{escape(line)}</li>" for line in lines) + "</ul>"


@router.post("/api/meetings/{mid}/scenario/opening-from-theses")
def opening_from_theses(mid: str, body: OpeningFromThesesIn):
    """Приём тезисов из сервиса «Тезисы»: они целиком ложатся во вступительное слово.

    Запрос приходит из браузера пользователя тем же доменом, поэтому пользователь и права
    обычные, из заголовков портала; общих секретов между сервисами не появляется — так же
    сделан приём повестки. Текст заменяется целиком: тезисы ведут в своём сервисе, здесь
    их правят уже после передачи. Что было до замены, видно в журнале портала.
    """
    m = meeting_or_404(mid)
    check_text(body.text or "")
    lines = [ln.strip() for ln in (body.text or "").replace("\r\n", "\n").split("\n") if ln.strip()]
    if not lines:
        raise HTTPException(status_code=400, detail="Тезисы пустые — вступительное слово не изменилось")

    block = next((b for b in m.scenario if b.section == SECTION_OPENING), None)
    if block is None:
        # сценарий ещё не формировали: заводим только вступительное слово, остальное соберётся как обычно
        block = ScenarioBlock(section=SECTION_OPENING, kind="opening", minutes=5)
        m.scenario.insert(0, block)
    was = len(html_to_text(block.text).strip())
    block.text = _bullets_html(lines)
    if body.minutes > 0:
        block.minutes = min(int(body.minutes), 600)
    storage.save(m)

    portal.note_object(m.id)
    src = str((body.source or {}).get("id") or "")
    portal.note_change(
        "вступительное слово из «Тезисов»" + (f" ({src})" if src else "") +
        f": было {was} знаков → стало {sum(len(x) for x in lines)}, пунктов {len(lines)}"
    )
    return {"ok": True, "url": f"/moderator/#meeting={quote(m.id)}", "replaced": was > 0}


# ── глоссарий ───────────────────────────────────────────────────────────


@router.post("/api/meetings/{mid}/glossary")
def add_glossary(mid: str, body: GlossaryIn):
    m = meeting_or_404(mid)
    m.glossary.append(GlossaryEntry(**body.model_dump()))
    storage.save(m)
    return {"meeting": m.to_dict(), "readiness": readiness.readiness(m)}


@router.delete("/api/meetings/{mid}/glossary/{key}")
def remove_glossary(mid: str, key: str):
    m = meeting_or_404(mid)
    m.glossary.pop(pos(m.glossary, key, "Сокращение"))
    storage.save(m)
    return {"meeting": m.to_dict(), "readiness": readiness.readiness(m)}


@router.post("/api/meetings/{mid}/scenario/generate")
def generate_scenario(mid: str):
    m = meeting_or_404(mid)
    m.build_scenario()
    storage.save(m)
    return {"meeting": m.to_dict(), "readiness": readiness.readiness(m)}


@router.post("/api/meetings/{mid}/opening")
def generate_opening(mid: str):
    """Искра собирает вступительное слово из материалов (решение пользователя 02.10).

    Сначала общие материалы (масштаб, приоритеты), потом вопросы по порядку с передачей слова
    докладчикам; доклады не пересказывает. Объём — по хронометражу блока opening
    (минуты × `WORDS_PER_MINUTE`). Текст блока заменяется целиком, прежний виден в журнале.
    """
    m = meeting_or_404(mid)
    input_text = llm_extract.build_opening_input(m)
    if not input_text:
        raise HTTPException(status_code=400, detail="Сначала разберите материалы к вопросам – "
                                                    "вступительное слово собирается из них")
    try:
        lines = llm_extract.generate_opening(input_text)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Искра не ответила: {e}")
    if not lines:
        raise HTTPException(status_code=502, detail="Искра вернула пустой ответ – попробуйте ещё раз")
    # Искра могла отвечать минуты: перечитываем совещание, чтобы не затереть правки коллег
    m = meeting_or_404(mid)
    block = next((b for b in m.scenario if b.section == SECTION_OPENING), None)
    if block is None:
        block = ScenarioBlock(section=SECTION_OPENING, kind="opening", minutes=5)
        m.scenario.insert(0, block)
    was = len(html_to_text(block.text).strip())
    block.text = _bullets_html(lines)
    storage.save(m)
    portal.note_change(f"вступительное слово из материалов: было {was} знаков → стало "
                       f"{sum(len(x) for x in lines)}, пунктов {len(lines)}")
    return {"meeting": m.to_dict(), "readiness": readiness.readiness(m)}


@router.post("/api/meetings/{mid}/closing")
def generate_closing(mid: str):
    """Искра собирает заключительное слово из тезисов и предложений всех материалов."""
    m = meeting_or_404(mid)
    input_text = llm_extract.build_closing_input(m)
    if not input_text:
        raise HTTPException(status_code=400, detail="Нет материалов с тезисами или предложениями")
    try:
        closing = llm_extract.generate_closing(input_text)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Искра не ответила: {e}")
    # Искра могла отвечать минуты: перечитываем совещание, чтобы не затереть правки коллег
    m = meeting_or_404(mid)
    block = None
    for b in m.scenario:
        if b.section == SECTION_CLOSING:
            block = b
            break
    if block is None:
        block = ScenarioBlock(section=SECTION_CLOSING, kind="closing", text="", minutes=5)
        m.scenario.append(block)
    block.text = closing
    storage.save(m)
    return {"meeting": m.to_dict(), "readiness": readiness.readiness(m)}
