"""Маршруты материалов к вопросу: загрузка файлов, разбор Искрой, правка."""
from __future__ import annotations


from fastapi import APIRouter, File, HTTPException, UploadFile

from . import extract, llm_extract, readiness, storage
from .deps import at, check_base, check_text, meeting_or_404, pos, read_upload
from .models import SECTION_QUESTION, Material, Order, inject_material, new_id
from .schemas import MaterialAnalyzeIn, MaterialApplyIn, MaterialIn, MaterialUpdateIn

router = APIRouter()


@router.post("/api/meetings/{mid}/materials")
def add_material(mid: str, body: MaterialIn):
    m = meeting_or_404(mid)
    check_text(body.text)          # документ совещания переписывается целиком при каждом сохранении
    data = body.model_dump()
    data["id"] = new_id()
    # Ссылку принимаем только внутреннюю: чужой сайт из материала открываться не должен
    src = data.get("source") or {}
    url = str(src.get("url") or "")
    data["source"] = {"service": str(src.get("service") or "")[:40], "id": str(src.get("id") or "")[:100],
                      "kind": str(src.get("kind") or "")[:40],
                      "url": url[:300] if url.startswith("/") and not url.startswith("//") else ""} if src else {}
    if not data["proposal"] and (data["conclusion"] or data["proposals"]):
        data["proposal"] = llm_extract.compose_proposal(data["conclusion"], data["proposals"])
    m.materials.append(Material(**data))
    storage.save(m)
    # id нового материала — интерфейс ставит ему пометку «в пакете», а не «последнему в списке»
    return {"meeting": m.to_dict(), "readiness": readiness.readiness(m), "material_id": data["id"]}


@router.delete("/api/meetings/{mid}/materials/{key}")
def remove_material(mid: str, key: str):
    m = meeting_or_404(mid)
    idx = pos(m.materials, key, "Материал")
    mat_id = m.materials[idx].id
    # каскад: блоки сценария, вплетённые из этого материала, и поручения, отмеченные из его разбора
    m.scenario = [b for b in m.scenario if b.material_id != mat_id]
    m.orders = [o for o in m.orders if not mat_id or o.material_id != mat_id]
    m.materials.pop(idx)
    storage.save(m)
    return {"meeting": m.to_dict(), "readiness": readiness.readiness(m)}


def _linked_orders(m, mat) -> list:
    return [o for o in m.orders if mat.id and o.material_id == mat.id]


def _orders_snapshot(items) -> list[tuple]:
    """Поручения для сверки с основой: объекты или словари → (id, text, assignee, deadline) по id."""
    out = []
    for o in items or []:
        get = o.get if isinstance(o, dict) else (lambda k, _o=o: getattr(_o, k, ""))
        out.append(tuple(str(get(k) or "").strip() for k in ("id", "text", "assignee", "deadline")))
    return sorted(out)


def _sync_material_orders(m, mat, items) -> None:
    """Поручения материала → присланный список: совпавший id правим, без id заводим, остальные его — убираем.

    Поручения без `material_id` (заведённые руками, из документа «поручения») и поручения
    других материалов не трогаем. Строка с пустым текстом пропускается.
    """
    mine = {o.id: o for o in m.orders if mat.id and o.material_id == mat.id}
    keep: set[str] = set()
    fresh: list[Order] = []
    for it in items:
        text = (it.text or "").strip()
        if not text:
            continue
        o = mine.get(it.id) if it.id else None
        if o is not None:
            o.text, o.assignee, o.deadline = text, it.assignee.strip(), it.deadline.strip()
            keep.add(o.id)
        else:
            fresh.append(Order(text=text, assignee=it.assignee.strip(), deadline=it.deadline.strip(),
                               agenda_num=mat.question_num, material_id=mat.id, source="material"))
    m.orders = [o for o in m.orders if o.id not in mine or o.id in keep] + fresh
    mat.orders = [o.text for o in m.orders if o.material_id == mat.id]


def _move_material(m, mat, qnum: str) -> None:
    """Перепривязать материал к другому вопросу (решение пользователя 07.10): вслед за ним — его поручения
    (`agenda_num`) и блоки сценария. Блок «Подводка» старого вопроса остаётся на месте пустым (это каркас
    вопроса), «Предложение: …» из материала убирается; в новом вопросе блоки вплетаются заново
    (`inject_material`) — тем же путём, что при разборе."""
    if qnum and not any(a.num.strip() == qnum for a in m.agenda):
        raise HTTPException(status_code=400, detail=f"Вопроса {qnum} нет в повестке")
    mat.question_num = qnum
    if mat.id:
        for o in m.orders:
            if o.material_id == mat.id:
                o.agenda_num = qnum
        kept = []
        for b in m.scenario:
            if b.material_id == mat.id and b.section == SECTION_QUESTION:
                if b.kind == "reference":
                    b.text, b.material_id = "", ""
                    kept.append(b)
                continue                      # «Предложение: …» материала уходит вместе с ним
            kept.append(b)
        m.scenario = kept
        inject_material(m, mat)


@router.patch("/api/meetings/{mid}/materials/{key}")
def update_material(mid: str, key: str, body: MaterialUpdateIn):
    m = meeting_or_404(mid)
    mat = at(m.materials, key, "Материал")
    base = dict(body.base or {})
    base_orders = base.pop("orders", None)
    check_base(mat, base, mid, "Материал")
    # поручения материала в основе: окно видело их при открытии; коллега поправил или убрал — 409,
    # иначе его правка молча затёрлась бы, а убранное вернулось бы новым (ревьюер RC-2026-10-02-15, Н-5)
    if base_orders is not None and body.orders is not None and \
            _orders_snapshot(base_orders) != _orders_snapshot(_linked_orders(m, mat)):
        # заведомо несовпадающая основа: тот же 409 и тот же текст «успел(а) изменить …», что у полей
        check_base(mat, {"orders": None}, mid, "Поручения материала")
    old_proposal = mat.proposal
    old_new_fields = bool(mat.conclusion or mat.proposals)
    # не присланное поле (None) не трогаем: правка саммари не должна стирать подводку
    for k in ("type", "speaker", "title", "summary", "intro", "conclusion"):
        v = getattr(body, k)
        if v is not None:
            setattr(mat, k, v.strip() if k != "summary" else v)
    for k in ("theses", "proposals"):
        v = getattr(body, k)
        if v is not None:
            setattr(mat, k, [t.strip() for t in v if t and t.strip()])
    # proposal — производное от вывода и предложений: есть они — всегда пересобираем, присланное
    # proposal не берём (окно слало старую склейку, и «Предложение: …» в сценарии не обновлялось, Н-1).
    # У материала прежнего разбора (ни вывода, ни предложений) proposal правится как раньше.
    # Старому материалу дописали вывод или предложения — прежнее «Предложение» не теряем: оно встаёт
    # первым пунктом предложений (ревьюер RC-2026-10-02-18, Н-2)
    legacy = (body.proposal if body.proposal is not None else old_proposal or "").strip()
    if not old_new_fields and (mat.conclusion or mat.proposals) and legacy and legacy not in mat.proposals:
        mat.proposals = [legacy] + mat.proposals
    if mat.conclusion or mat.proposals:
        mat.proposal = llm_extract.compose_proposal(mat.conclusion, mat.proposals)
    elif body.proposal is not None:
        mat.proposal = body.proposal
    elif old_new_fields:
        mat.proposal = ""                     # вывод и предложения стёрли — склейка тоже пустая
    if body.orders is not None:
        _sync_material_orders(m, mat, body.orders)
    if body.question_num is not None and body.question_num.strip() != (mat.question_num or ""):
        _move_material(m, mat, body.question_num.strip())
    if body.package is not None:
        mat.package = " ".join(body.package.split())   # имя файла одной строкой, без переводов строк
    # блоки сценария из этого материала — вслед за правкой: подводка и «Предложение: …»
    if mat.id:
        for b in m.scenario:
            if b.material_id != mat.id:
                continue
            if b.kind == "reference" and body.intro:
                b.text = mat.intro
            elif b.kind == "replica" and mat.proposal != old_proposal and \
                    b.text.strip() == ("Предложение: " + old_proposal).strip():
                b.text = "Предложение: " + mat.proposal
    storage.save(m)
    return {"meeting": m.to_dict(), "readiness": readiness.readiness(m)}


@router.post("/api/meetings/{mid}/materials/upload")
def upload_materials(mid: str, files: list[UploadFile] = File(...)):
    """Загрузка файлов материалов (Word, Excel, PowerPoint, PDF, RTF, ODT, TXT/MD/CSV, HTML) -> текст или HTML."""
    meeting_or_404(mid)
    out = []
    for f in files:
        name = f.filename or "файл"
        ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
        content = read_upload(f)
        if ext in ("html", "htm"):
            out.append({"name": name, "text": content.decode("utf-8", errors="replace"),
                        "is_html": True, "size": len(content)})
            continue
        if extract.problem(name, content) and not extract.listed(name):
            out.append({"name": name, "error": f"Неподдерживаемый тип: {ext or 'без расширения'} "
                                               f"(подходят {extract.WHAT})"})
            continue
        try:
            text = extract.extract_bytes(content, name)
        except Exception as e:
            out.append({"name": name, "error": f"Не удалось извлечь текст: {e}"})
            continue
        out.append({"name": name, "text": text, "is_html": False, "size": len(content)})
    return {"files": out}


@router.post("/api/meetings/{mid}/materials/analyze")
def analyze_material(mid: str, body: MaterialAnalyzeIn):
    """Разобрать текст материала через Искру: саммари, диаграммы, тезисы, вывод, предложения, поручения.

    Ответ: `{"result": {...}, "warnings": ["…"]}` — замечания строками (отброшенные диаграммы,
    длина тезисов, докладчик не из текста). Ничего не сохраняет: человек отмечает в окне, что взять.
    """
    m = meeting_or_404(mid)
    check_text(body.text)
    try:
        result = llm_extract.analyze_material(body.text, body.filename, m)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Искра не ответила: {e}")
    warnings = result.pop("problems", [])
    return {"result": result, "warnings": warnings}


@router.post("/api/meetings/{mid}/materials/apply")
def apply_material(mid: str, body: MaterialApplyIn):
    """Применить разобранный материал: сохранить + отмеченные поручения + подводка/предложение в сценарий."""
    m = meeting_or_404(mid)
    check_text(str((body.result or {}).get("text") or ""))
    try:
        result = llm_extract.check_material_schema(body.result)
    except Exception as e:
        raise HTTPException(status_code=422, detail=f"Данные материала не подходят по структуре: {e}")
    mat = llm_extract.apply_material(m, result, body.filename)
    storage.save(m)
    return {"meeting": m.to_dict(), "readiness": readiness.readiness(m), "material_id": mat.id}
