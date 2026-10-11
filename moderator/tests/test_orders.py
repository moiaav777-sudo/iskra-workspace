"""Вкладка «Поручения» (решения пользователя 02.10): конструктор (PATCH с base, ↑/↓), перечень из материалов
и из файла (предложения с галочками → bulk), порядок «из файла выше, чем из материалов», Word по эталону.
Искра подменяется: `llm_extract._call_iskra_raw`.
"""
from __future__ import annotations

import io
import json
import urllib.parse

import docx

from app import llm_extract, storage

from .test_api import audit, client, create, login, people  # noqa: F401  (фикстуры)
from .test_materials_v2 import _apply, _fake_iskra


def _orders(client, mid):
    return client.get(f"/api/meetings/{mid}").json()["meeting"]["orders"]


def _add(client, mid, text, **kw):
    r = client.post(f"/api/meetings/{mid}/orders", json={"text": text, **kw})
    assert r.status_code == 200, r.text
    return r.json()["meeting"]["orders"][-1]


# ── конструктор ─────────────────────────────────────────────────────────

def test_patch_order_with_base_and_conflict(client):
    login(client, "alice")
    mid = create(client)
    o = _add(client, mid, "Минпросвещения России представить доклад", assignee="Минпросвещения России",
             deadline="2026-06-25", agenda_num="1")
    assert o["deadline"] == "25.06.2026" and o["source"] == ""       # из календаря — храним ДД.ММ.ГГГГ
    base = {k: o[k] for k in ("text", "assignee", "deadline", "agenda_num")}
    r = client.patch(f"/api/meetings/{mid}/orders/{o['id']}", json={
        "text": "Минпросвещения России (С.С. Кравцову)  представить\nдоклад", "deadline": "2026-07-01", "base": base})
    assert r.status_code == 200, r.text
    after = r.json()["meeting"]["orders"][0]
    assert after["text"] == "Минпросвещения России (С.С. Кравцову) представить доклад"   # одним абзацем
    assert after["deadline"] == "01.07.2026"
    assert after["assignee"] == "Минпросвещения России" and after["agenda_num"] == "1"   # None — не тронуто
    # второе окно открыло поручение до этой правки — его основа устарела: 409, ничего не записано
    r = client.patch(f"/api/meetings/{mid}/orders/{o['id']}", json={"text": "Чужая правка", "base": base})
    assert r.status_code == 409 and "успел(а) изменить" in r.json()["detail"]
    assert _orders(client, mid)[0]["text"] == after["text"]
    # пустой текст — не правка, а «Убрать»
    r = client.patch(f"/api/meetings/{mid}/orders/{o['id']}", json={"text": "   "})
    assert r.status_code == 400
    # срок стёрли — пусто; поручения нет — 409
    r = client.patch(f"/api/meetings/{mid}/orders/{o['id']}", json={"deadline": ""})
    assert r.json()["meeting"]["orders"][0]["deadline"] == ""
    assert client.patch(f"/api/meetings/{mid}/orders/nope", json={"text": "x"}).status_code == 409


def test_patch_material_order_follows_into_material(client):
    login(client, "alice")
    mid = create(client)
    client.post(f"/api/meetings/{mid}/agenda", json={"num": "1", "title": "Об интернете"})
    m = _apply(client, mid, orders=[{"text": "Первое"}]).json()["meeting"]
    o = m["orders"][0]
    assert o["source"] == "material" and o["material_id"] == m["materials"][0]["id"]
    m = client.patch(f"/api/meetings/{mid}/orders/{o['id']}", json={"text": "Первое, поправленное"}).json()["meeting"]
    assert m["materials"][0]["orders"] == ["Первое, поправленное"]


def test_move_order(client):
    login(client, "alice")
    mid = create(client)
    a, b, c = (_add(client, mid, t) for t in ("А", "Б", "В"))
    r = client.post(f"/api/meetings/{mid}/orders/{c['id']}/move", json={"direction": "up"})
    assert r.status_code == 200
    assert [o["text"] for o in r.json()["meeting"]["orders"]] == ["А", "В", "Б"]
    client.post(f"/api/meetings/{mid}/orders/{a['id']}/move", json={"direction": "up"})   # выше некуда
    assert [o["text"] for o in _orders(client, mid)] == ["А", "В", "Б"]
    client.post(f"/api/meetings/{mid}/orders/{a['id']}/move", json={"direction": "down"})
    assert [o["text"] for o in _orders(client, mid)] == ["В", "А", "Б"]


def test_bulk_without_duplicates_file_orders_first(client, audit):
    login(client, "alice")
    mid = create(client)
    client.post(f"/api/meetings/{mid}/agenda", json={"num": "1", "title": "Об интернете"})
    _add(client, mid, "Ручное поручение")
    mat = _apply(client, mid, orders=[{"text": "Из материала раньше"}]).json()["meeting"]["materials"][0]
    body = {"orders": [
        {"text": "Новое из материала", "assignee": "Минцифры России", "deadline": "2026-09-01",
         "question_num": "1", "material_id": mat["id"], "source": "material"},
        {"text": "Первое из файла", "source": "file", "question_num": "7"},       # вопроса 7 нет — без номера
        {"text": "ручное   поручение.", "source": "file"},                          # дубль записанного
        {"text": "Второе из файла", "source": "file", "material_id": mat["id"]},   # у файла материала нет
        {"text": "Второе из файла", "source": "file"},                             # дубль внутри пакета
    ]}
    r = client.post(f"/api/meetings/{mid}/orders/bulk", json=body)
    assert r.status_code == 200, r.text
    data = r.json()
    assert (data["added"], data["skipped"]) == (3, 2)
    orders = data["meeting"]["orders"]
    assert [o["text"] for o in orders] == ["Первое из файла", "Второе из файла", "Ручное поручение",
                                           "Из материала раньше", "Новое из материала"]
    assert [o["source"] for o in orders] == ["file", "file", "", "material", "material"]
    new = orders[-1]
    assert new["material_id"] == mat["id"] and new["deadline"] == "01.09.2026" and new["agenda_num"] == "1"
    assert orders[0]["agenda_num"] == "" and orders[1]["material_id"] == ""
    # повторное нажатие «Добавить отмеченные» ничего не задваивает
    r = client.post(f"/api/meetings/{mid}/orders/bulk", json=body)
    assert r.json()["added"] == 0 and len(r.json()["meeting"]["orders"]) == 5
    assert any(e["action"] == "Добавлены поручения в перечень" for e in audit)
    # чужой источник не принимается
    r = client.post(f"/api/meetings/{mid}/orders/bulk", json={"orders": [{"text": "x", "source": "evil"}]})
    assert r.status_code == 422


def test_old_orders_without_source_still_load(client):
    login(client, "alice")
    mid = create(client)
    legacy = {"id": mid, "title": "Старое",
              "orders": [{"text": "Поручение", "assignee": "", "deadline": "до 1 августа 2026 г.",
                          "agenda_num": "I", "id": "o1"}]}
    conn = storage._conn()
    conn.execute("UPDATE meetings SET data = ? WHERE id = ?", (json.dumps(legacy, ensure_ascii=False), mid))
    conn.commit()
    conn.close()
    o = _orders(client, mid)[0]
    assert o["source"] == "" and o["material_id"] == "" and o["deadline"] == "до 1 августа 2026 г."
    r = client.patch(f"/api/meetings/{mid}/orders/o1", json={"assignee": "Минфин России"})
    assert r.status_code == 200 and r.json()["meeting"]["orders"][0]["deadline"] == "до 1 августа 2026 г."


# ── перечень из материалов ──────────────────────────────────────────────

ISKRA_ORDERS = {"orders": [
    {"text": "Минцифры России (М.И. Шадаеву) утвердить график подключения школ", "assignee": "Минцифры России",
     "deadline": "до 1 декабря 2026 г.", "question_num": "9", "material_id": "M1"},
    {"text": "Регионам представить графики", "assignee": "", "deadline": "", "question_num": "", "material_id": "[M1]"},
    {"text": "ручное поручение", "assignee": "", "deadline": "", "question_num": "", "material_id": "M7"},
]}


def test_propose_from_materials_ready_orders_and_one_iskra_run(client, monkeypatch, audit):
    calls = []
    _fake_iskra(monkeypatch, ISKRA_ORDERS, calls, tokens=(900, 120))
    login(client, "alice")
    mid = create(client)
    client.post(f"/api/meetings/{mid}/agenda", json={"num": "1", "title": "Об интернете"})
    client.post(f"/api/meetings/{mid}/agenda", json={"num": "2", "title": "О кадрах"})
    _add(client, mid, "Ручное поручение")
    # материал к вопросу 2 с готовыми поручениями (их предложила Искра при разборе)
    _apply(client, mid, question_num="2", title="Кадры", orders=[
        {"text": "Минпросвещения России подготовить кадры", "assignee": "Минпросвещения России",
         "deadline": "01.09.2027"}])
    # материал к вопросу 1 без поручений, но с выводом и предложениями — для Искры
    client.post(f"/api/meetings/{mid}/materials", json={
        "question_num": "1", "title": "Интернет в школах", "conclusion": "Темп подключения нужно удвоить.",
        "proposals": ["Утвердить график подключения."], "theses": ["Половина школ без скорости."]})
    # пустой материал — Искре нечего дать
    client.post(f"/api/meetings/{mid}/materials", json={"question_num": "1", "title": "Пустой"})
    before = _orders(client, mid)
    calls.clear()
    n_audit = len(audit)
    r = client.post(f"/api/meetings/{mid}/orders/propose-from-materials")
    assert r.status_code == 200, r.text
    data = r.json()
    assert len(calls) == 1 and data["asked_iskra"] is True              # один прогон на все материалы без поручений
    system, user = calls[0]
    assert system == llm_extract.ORDERS_PROMPT
    assert "КТО → ЧТО ДЕЛАЕТ → КАКОЙ РЕЗУЛЬТАТ → К КАКОМУ СРОКУ" in system and "дательном" in system
    assert "Срок НИКОГДА не придумывай" in system
    assert "[M1] Материал: Интернет в школах" in user and "Темп подключения нужно удвоить." in user
    assert "Минпросвещения России подготовить кадры" not in user          # готовые поручения Искре не шлём
    assert "Пустой" not in user
    mats = {x["title"]: x["id"] for x in client.get(f"/api/meetings/{mid}").json()["meeting"]["materials"]}
    props = data["proposals"]
    # по вопросам: без вопроса — первым, затем вопрос 1 (Искра), потом вопрос 2 (готовые)
    assert [p["text"] for p in props] == ["ручное поручение",
                                          "Минцифры России (М.И. Шадаеву) утвердить график подключения школ",
                                          "Регионам представить графики", "Минпросвещения России подготовить кадры"]
    dup, first, second, ready = props
    assert first["material_id"] == mats["Интернет в школах"] and first["question_num"] == "1"
    assert first["deadline"] == "01.12.2026" and first["source"] == "material"     # «до <дата>» — чистая дата
    assert second["material_id"] == mats["Интернет в школах"]                         # «[M1]» тоже узнаём
    assert dup["material_id"] == "" and dup["question_num"] == "" and dup["exists"] is True   # уже в перечне
    assert ready["material_id"] == mats["Кадры"]
    assert ready["question_num"] == "2" and ready["assignee"] == "Минпросвещения России"
    assert ready["deadline"] == "01.09.2027" and ready["exists"] is True             # окно разбора его уже завело
    assert not first["exists"] and [p["id"] for p in props] == ["p1", "p2", "p3", "p4"]
    assert _orders(client, mid) == before                                             # ничего не сохранено
    ev = [e for e in audit[n_audit:] if e["action"] == "Перечень поручений из материалов (Искра)"]
    assert len(ev) == 1 and (ev[0]["tokens_in"], ev[0]["tokens_out"]) == (900, 120)


def test_propose_from_materials_without_iskra_when_all_have_orders(client, monkeypatch):
    calls = []
    _fake_iskra(monkeypatch, ISKRA_ORDERS, calls)
    login(client, "alice")
    mid = create(client)
    client.post(f"/api/meetings/{mid}/agenda", json={"num": "1", "title": "Об интернете"})
    _apply(client, mid, orders=[{"text": "Готовое поручение"}])
    calls.clear()
    data = client.post(f"/api/meetings/{mid}/orders/propose-from-materials").json()
    assert calls == [] and data["asked_iskra"] is False
    assert [p["text"] for p in data["proposals"]] == ["Готовое поручение"]
    # материалов нет — 400, Искру не зовём
    mid2 = create(client)
    r = client.post(f"/api/meetings/{mid2}/orders/propose-from-materials")
    assert r.status_code == 400 and calls == []


def test_propose_retries_strictly_after_prose(client, monkeypatch):
    answers = ["Вот поручения: …", json.dumps(ISKRA_ORDERS, ensure_ascii=False)]
    seen = []

    def fake(system, user, *a, **kw):
        seen.append(user)
        return answers.pop(0)
    monkeypatch.setattr(llm_extract, "_call_iskra_raw", fake)
    login(client, "alice")
    mid = create(client)
    client.post(f"/api/meetings/{mid}/materials", json={"title": "Общий", "conclusion": "Нужно ускорить."})
    data = client.post(f"/api/meetings/{mid}/orders/propose-from-materials").json()
    assert len(seen) == 2 and seen[1].startswith(llm_extract.MATERIAL_STRICT_REMINDER)
    assert len(data["proposals"]) == 3


# ── перечень из файла ───────────────────────────────────────────────────

FILE_TEXT = ("1. Минпросвещения России (С.С. Кравцову) обеспечить подключение школ.\nСрок – до 25 июня 2026 г.\n"
             "2. Рособрнадзору (А.А. Музаеву) провести эксперимент.\n")


def test_propose_from_file_txt(client, monkeypatch, audit):
    calls = []
    _fake_iskra(monkeypatch, {"orders": [
        {"text": "Минпросвещения России (С.С. Кравцову) обеспечить подключение школ", "deadline": "25.06.2026",
         "question_num": "1", "material_id": "M3"},
        {"text": "Рособрнадзору (А.А. Музаеву) провести эксперимент", "deadline": "", "question_num": "5"}]},
        calls, tokens=(400, 60))
    login(client, "alice")
    mid = create(client)
    client.post(f"/api/meetings/{mid}/agenda", json={"num": "1", "title": "Об интернете"})
    _add(client, mid, "Рособрнадзору (А.А. Музаеву) провести эксперимент.")
    files = {"file": ("поручения.txt", FILE_TEXT.encode("utf-8"), "text/plain")}
    r = client.post(f"/api/meetings/{mid}/orders/propose-from-file", files=files)
    assert r.status_code == 200, r.text
    system, user = calls[0]
    assert system == llm_extract.ORDERS_PROMPT and "Срок – до 25 июня 2026 г." in user and "1. Об интернете" in user
    props = r.json()["proposals"]
    assert [p["source"] for p in props] == ["file", "file"] and all(p["material_id"] == "" for p in props)
    # внутри группы — по номерам вопросов (без номера — первыми), по порядку появления (TRACK, решение 02.10)
    assert props[0]["question_num"] == "" and props[0]["exists"] is True
    assert props[1]["question_num"] == "1" and props[1]["deadline"] == "25.06.2026" and not props[1]["exists"]
    assert len(_orders(client, mid)) == 1                                  # ничего не сохранено, файл не хранится
    ev = [e for e in audit if e["action"] == "Разобран файл с поручениями (Искра)"]
    assert len(ev) == 1 and (ev[0]["tokens_in"], ev[0]["tokens_out"]) == (400, 60)
    # не тот формат — 400 без Искры
    calls.clear()
    r = client.post(f"/api/meetings/{mid}/orders/propose-from-file",
                    files={"file": ("virus.exe", b"MZ\x00\x00", "application/octet-stream")})
    assert r.status_code == 400 and calls == []
    r = client.post(f"/api/meetings/{mid}/orders/propose-from-file",
                    files={"file": ("пусто.txt", b"   \n", "text/plain")})
    assert r.status_code == 400 and calls == []


# ── Word ────────────────────────────────────────────────────────────────

def _header(client, mid, **kw):
    fields = {"title": "Тест", "subtitle": "", "chair": "", "chair_role": "", "date": "", "location_primary": "",
              "location_secondary": "", "time_start": "", "time_end": ""}
    fields.update(kw)
    r = client.put(f"/api/meetings/{mid}/header", json=fields)
    assert r.status_code == 200, r.text


def _paras(content):
    return [p.text for p in docx.Document(io.BytesIO(content)).paragraphs]


def test_export_orders_docx_in_list_order(client, audit):
    login(client, "alice")
    mid = create(client)
    _header(client, mid, chair="Чернышенко Дмитрий Николаевич",
            chair_role="Заместитель Председателя Правительства Российской Федерации", date="14.10.2026")
    assert client.get(f"/api/meetings/{mid}/orders/export.docx").status_code == 400   # пустой перечень
    a = _add(client, mid, "Минпросвещения России (С.С. Кравцову) обеспечить подключение школ.", deadline="2026-06-25")
    _add(client, mid, "Рособрнадзору (А.А. Музаеву) провести эксперимент.")
    client.post(f"/api/meetings/{mid}/orders/bulk", json={"orders": [
        {"text": "Минфину России предусмотреть финансирование.", "deadline": "начиная с 1 сентября 2026 г.",
         "source": "file"}]})
    client.post(f"/api/meetings/{mid}/orders/{a['id']}/move", json={"direction": "down"})
    r = client.get(f"/api/meetings/{mid}/orders/export.docx")
    assert r.status_code == 200, r.text
    assert urllib.parse.unquote(r.headers["x-filename"]) == "Перечень поручений 14.10.2026.docx"
    assert "filename*=UTF-8''" + urllib.parse.quote("Перечень поручений 14.10.2026.docx") in r.headers["content-disposition"]
    assert r.headers["x-names-check"] == "done"
    text = [t for t in _paras(r.content) if t.strip()]
    assert text[0].startswith("ЗАМЕСТИТЕЛЬ ПРЕДСЕДАТЕЛЯ ПРАВИТЕЛЬСТВА")
    body = text[1:]
    # порядок — порядок перечня: файл выше, затем после ↓ — «Рособрнадзору», затем «Минпросвещения» со сроком
    assert body == ["1. Минфину России предусмотреть финансирование.", "Срок – начиная с 1 сентября 2026 г.",
                    "2. Рособрнадзору (А.А. Музаеву) провести эксперимент.",
                    "3. Минпросвещения России (С.С. Кравцову) обеспечить подключение школ.",
                    "Срок – до 25 июня 2026 г.", "Д.Н. Чернышенко"]
    assert sum(1 for t in body if t.startswith("Срок")) == 2                         # без даты строки «Срок» нет
    assert any(e["action"] == "Выгружен перечень поручений в Word" for e in audit)


def test_export_orders_docx_without_chair_and_registry_down(client, people):
    login(client, "alice")
    mid = create(client)
    _add(client, mid, "Поручение без срока.")
    r = client.get(f"/api/meetings/{mid}/orders/export.docx")
    assert r.status_code == 200
    assert urllib.parse.unquote(r.headers["x-filename"]) == "Перечень поручений.docx"   # даты совещания нет
    assert [t for t in _paras(r.content) if t.strip()] == ["1. Поручение без срока."]   # ни шапки, ни подписи
    # подпись председателя сверяется с реестром: реестр молчит — только с согласия и с пометкой в документе
    _header(client, mid, chair="Чернышенко Дмитрий Николаевич", date="14.10.2026")
    people.down = True
    r = client.get(f"/api/meetings/{mid}/orders/export.docx")
    assert r.status_code == 409 and r.headers["x-names-check"] == "unavailable"
    r = client.get(f"/api/meetings/{mid}/orders/export.docx?unchecked=1")
    assert r.status_code == 200 and r.headers["x-names-check"] == "skipped"
    assert _paras(r.content)[0].startswith("ФИО не сверены с реестром людей")
    assert "(ФИО не сверены)" in urllib.parse.unquote(r.headers["x-filename"])


# ── сроки: составной срок не режется (ревьюер RC-2026-10-02-20, К-1) ─────

TERM4 = ("1 декабря 2026 г. с предоставлением промежуточного доклада в Правительство Российской Федерации "
         "в срок до 1 сентября 2026 г.")          # пункт 4 эталона, 124 знака


def test_normalize_deadline_only_whole_dates():
    from app.models import normalize_date, normalize_deadline
    for raw in ("25.06.2026", "25.6.2026", "2026-06-25", "25 июня 2026 г.", "25 июня 2026 года", "до 25 июня 2026 г.",
                "До 25.06.2026", "  25  июня   2026  "):
        assert normalize_deadline(raw) == "25.06.2026", raw
    for raw in (TERM4, "15 мая 2026 г. и далее ежеквартально", "начиная с 1 сентября 2026 г.", "ежеквартально",
                "IV квартал 2026 г.", "32.13.2026", "25 смарта 2026 г."):
        assert normalize_deadline(raw) == raw, raw
    assert normalize_deadline("  1 декабря 2026 г.   с  докладом ") == "1 декабря 2026 г. с докладом"
    assert normalize_deadline("") == ""
    # дата совещания — по-прежнему общей normalize_date
    assert normalize_date("07.9.2026") == "07.09.2026" and normalize_date("2026-09-07") == "07.09.2026"
    assert normalize_date("7 сентября 2026 г.") == "07.09.2026"


def test_long_deadline_kept_from_file_to_word(client, monkeypatch):
    _fake_iskra(monkeypatch, {"orders": [
        {"text": "Координационному центру Правительства Российской Федерации (Д.И. Годунову) организовать работу",
         "deadline": TERM4},
        {"text": "Минпросвещения России (С.С. Кравцову) обеспечить подключение школ", "deadline": "до 25 июня 2026 г."}]})
    login(client, "alice")
    mid = create(client)
    _header(client, mid, date="25.05.2026")
    files = {"file": ("поручения.txt", ("4. …\nСрок - " + TERM4).encode("utf-8"), "text/plain")}
    props = client.post(f"/api/meetings/{mid}/orders/propose-from-file", files=files).json()["proposals"]
    assert [p["deadline"] for p in props] == [TERM4, "25.06.2026"]               # составной — целиком, дата — дата
    r = client.post(f"/api/meetings/{mid}/orders/bulk", json={"orders": [
        {k: p[k] for k in ("text", "assignee", "deadline", "question_num", "material_id", "source")} for p in props]})
    assert r.status_code == 200, r.text
    orders = r.json()["meeting"]["orders"]
    assert [o["deadline"] for o in orders] == [TERM4, "25.06.2026"]
    # PATCH, как его шлёт окно: все поля, срок словами из текстового поля
    o = orders[0]
    base = {k: o[k] for k in ("text", "assignee", "deadline", "agenda_num")}
    r = client.patch(f"/api/meetings/{mid}/orders/{o['id']}",
                     json=dict(base, text=o["text"] + ".", base=base))
    assert r.status_code == 200, r.text
    assert r.json()["meeting"]["orders"][0]["deadline"] == TERM4
    body = [t for t in _paras(client.get(f"/api/meetings/{mid}/orders/export.docx").content) if t.strip()]
    assert body[1] == "Срок – " + TERM4                                         # без «до», как в источнике
    assert body[3] == "Срок – до 25 июня 2026 г."                               # чистая дата — «до …»


def test_deadline_limit_300(client):
    login(client, "alice")
    mid = create(client)
    o = _add(client, mid, "Поручение")
    term = ("с ежеквартальным докладом " * 20)[:299].strip() + "я"
    term = term[:299]
    assert len(term) == 299
    r = client.patch(f"/api/meetings/{mid}/orders/{o['id']}", json={"deadline": term})
    assert r.status_code == 200, r.text
    assert r.json()["meeting"]["orders"][0]["deadline"] == " ".join(term.split())
    r = client.post(f"/api/meetings/{mid}/orders/bulk", json={"orders": [{"text": "Другое", "deadline": term, "source": "file"}]})
    assert r.status_code == 200 and r.json()["meeting"]["orders"][0]["deadline"] == " ".join(term.split())
    assert client.patch(f"/api/meetings/{mid}/orders/{o['id']}", json={"deadline": "я" * 301}).status_code == 422
    # окно материала — тот же предел
    client.post(f"/api/meetings/{mid}/agenda", json={"num": "1", "title": "Вопрос"})
    mat = _apply(client, mid, orders=[{"text": "Из материала"}]).json()["meeting"]["materials"][0]
    r = client.patch(f"/api/meetings/{mid}/materials/{mat['id']}",
                     json={"orders": [{"text": "Из материала", "deadline": TERM4}]})
    assert r.status_code == 200, r.text


def test_smart_import_orders_keep_long_deadline(client):
    login(client, "alice")
    mid = create(client)
    result = {"doc_type": "поручения", "confidence": "высокая", "summary": "",
              "data": {"orders": [{"text": "Поручение из документа", "deadline": TERM4},
                                  {"text": "Второе", "deadline": "2026-06-25"}]}}
    r = client.post(f"/api/meetings/{mid}/apply", json={"result": result})
    assert r.status_code == 200, r.text
    orders = r.json()["meeting"]["orders"]
    assert [o["deadline"] for o in orders] == [TERM4, "25.06.2026"] and orders[0]["source"] == "file"
    # и сохраняется из конструктора (раньше — 422 на любую правку)
    o = orders[0]
    base = {k: o[k] for k in ("text", "assignee", "deadline", "agenda_num")}
    assert client.patch(f"/api/meetings/{mid}/orders/{o['id']}", json=dict(base, assignee="ФОИВ", base=base)).status_code == 200


def test_patch_unknown_question_and_broken_pdf(client, monkeypatch):
    calls = []
    _fake_iskra(monkeypatch, {"orders": []}, calls)
    login(client, "alice")
    mid = create(client)
    client.post(f"/api/meetings/{mid}/agenda", json={"num": "1", "title": "Вопрос"})
    o = _add(client, mid, "Поручение", agenda_num="I")                      # старый номер
    r = client.patch(f"/api/meetings/{mid}/orders/{o['id']}", json={"agenda_num": "99"})
    assert r.status_code == 400 and "99" in r.json()["detail"]
    assert client.patch(f"/api/meetings/{mid}/orders/{o['id']}", json={"agenda_num": "I", "text": "Поправили"}).status_code == 200
    assert client.patch(f"/api/meetings/{mid}/orders/{o['id']}", json={"agenda_num": "1"}).status_code == 200
    assert client.patch(f"/api/meetings/{mid}/orders/{o['id']}", json={"agenda_num": ""}).status_code == 200
    for data in (b"%PDF-1.4 garbage", b"%PDF"):
        r = client.post(f"/api/meetings/{mid}/orders/propose-from-file", files={"file": ("битый.pdf", data, "application/pdf")})
        assert r.status_code == 400 and "Не удалось извлечь текст" in r.json()["detail"]
    assert calls == []
