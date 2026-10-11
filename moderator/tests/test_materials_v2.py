"""Окно разбора материала и вступительное слово (решения пользователя 02.10).

Диаграммы (`app/charts.py`), схема разбора материала с тезисами/выводом/предложениями/поручениями,
поручения с галочками и `material_id`, каскад при удалении, правка без затирания подводки,
`POST /api/meetings/{id}/opening`. Искра подменяется: `llm_extract._call_iskra_raw`.
"""
from __future__ import annotations

import json
import xml.etree.ElementTree as ET

import pytest

from app import charts, llm_extract, storage, template_adapter
from app.models import Material

from .test_api import audit, client, create, login, people  # noqa: F401  (фикстуры)

SVG_NS = "{http://www.w3.org/2000/svg}"

THESIS = ("Доля школ с интернетом сто мегабит выросла до сорока пяти процентов, "
          "это заметный сдвиг, но половина школ всё ещё без нужной скорости подключения.")   # 23 слова

ANSWER = {
    "type": "Справка", "question_num": "1", "speaker": "", "title": "Интернет в школах",
    "intro": "Прошу доложить о подключении школ к интернету.",
    "summary": "Подробно.\n\n- 2024 – 39%\n- 2025 – 45%",
    "figures": [
        {"title": "Доля школ с интернетом 100 Мбит/с", "unit": "%", "type": "line",
         "items": [["2024", 39.0], ["2025", 45.0]], "source": "таблица 1", "svg": "<svg><script>alert(1)</script></svg>"},
        {"title": "Кривой тип", "unit": "%", "type": "pie", "items": [["а", 1], ["б", 2]]},
        {"title": "Одна точка", "unit": "%", "type": "bar_v", "items": [["2025", 45]]},
        {"title": "Не число", "unit": "%", "type": "bar_h", "items": [["а", "много"], ["б", 2]]},
    ],
    "theses": [THESIS, THESIS, THESIS, "Короткий тезис.", "Пятый тезис лишний."],
    "conclusion": "Темп подключения нужно удвоить.",
    "proposals": ["Предлагаю утвердить график подключения.", "Прошу поддержать дополнительное финансирование."],
    "orders": [{"text": "Минцифры России обеспечить подключение школ к 01.09.2027", "assignee": "Минцифры России",
                "deadline": "01.09.2027"}, "Регионам представить графики подключения"],
}


def _fake_iskra(monkeypatch, answer, calls=None, tokens=(0, 0)):
    def fake(system, user, *a, **kw):
        if calls is not None:
            calls.append((system, user))
        if tokens != (0, 0):
            llm_extract._note_usage({"status": "completed",
                                     "usage": {"prompt_tokens": tokens[0], "completion_tokens": tokens[1]}})
        return answer if isinstance(answer, str) else json.dumps(answer, ensure_ascii=False)
    monkeypatch.setattr(llm_extract, "_call_iskra_raw", fake)


# ── диаграммы ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("kind", charts.TYPES)
def test_chart_every_type_is_valid_svg(kind):
    fig = {"title": "Доля <школ> & «связь»", "unit": "%", "type": kind,
           "items": [["2023", 30], ["2024 <прогноз>", 39.5], ["2025", 1234567.25]]}
    out = charts.svg(fig)
    root = ET.fromstring(out)                          # валидный XML
    assert root.tag == SVG_NS + "svg" and root.get("viewBox", "").startswith("0 0 560 ")
    assert "<script" not in out and "href" not in out and "foreignObject" not in out
    # интерфейс чистит svg DOMPurify: оформление только атрибутами, без стилей, ссылок и вставок
    for bad in ("<style", "style=", "class=", "<use", "<image", "<defs", "href=", "xlink"):
        assert bad not in out, bad
    texts = " ".join(t.text or "" for t in root.iter(SVG_NS + "text"))
    assert "Доля <школ> & «связь»" in texts          # подпись экранирована и читается как текст
    assert "&lt;школ&gt;" in out and "<школ>" not in out
    if kind != "donut":
        assert "1 234 567,25%" in texts     # по-русски: пробел тысяч, запятая
        assert "39,5%" in texts


def test_chart_rejects_bad_items():
    ok = {"type": "bar_h", "title": "", "unit": ""}
    for items in ([], [["a", 1]], [["a", 1], ["b", "много"]], [["a", 1], ["b", float("inf")]],
                  [["a", 1], ["b", None]], "строка", [["a", 1, 2], ["b", 2]], [["a", 1]] * 13):
        with pytest.raises(ValueError):
            charts.svg(dict(ok, items=items))
    with pytest.raises(ValueError):
        charts.svg({"type": "pie", "items": [["a", 1], ["b", 2]]})
    with pytest.raises(ValueError):
        charts.svg({"type": "donut", "items": [["a", -1], ["b", 2]]})
    with pytest.raises(ValueError):
        charts.svg({"type": "donut", "items": [["a", 0], ["b", 0]]})


def test_chart_size_is_reasonable():
    items = [["Очень длинное название субъекта Российской Федерации №" + str(i), i * 1000.5] for i in range(12)]
    for kind in charts.TYPES:
        out = charts.svg({"type": kind, "title": "Т" * 300, "unit": "тыс. человек", "items": items})
        assert len(out.encode("utf-8")) < 20_000
        ET.fromstring(out)


# ── схема разбора ───────────────────────────────────────────────────────

def test_material_schema_figures_theses_orders():
    warnings: list[str] = []
    res = llm_extract.check_material_schema(ANSWER, warnings)
    assert [f["id"] for f in res["figures"]] == ["f1"]
    fig = res["figures"][0]
    assert fig["type"] == "line" and fig["items"] == [["2024", 39.0], ["2025", 45.0]] and fig["source"] == "таблица 1"
    assert "<script" not in fig["svg"] and fig["svg"].startswith("<svg")   # svg нарисован сервером, не взят из ответа
    text = "\n".join(warnings)
    assert "«Кривой тип» отброшена" in text and "«Одна точка» отброшена" in text and "«Не число» отброшена" in text
    assert len(res["theses"]) == 4 and "оставлены первые 4" in text
    assert "тезис 4: 2 слов" in text and "тезис 1:" not in text          # длину считаем, тезис не режем
    assert res["theses"][3] == "Короткий тезис."
    assert res["orders"] == [
        {"id": "o1", "text": "Минцифры России обеспечить подключение школ к 01.09.2027",
         "assignee": "Минцифры России", "deadline": "01.09.2027"},
        {"id": "o2", "text": "Регионам представить графики подключения", "assignee": "", "deadline": ""}]
    assert res["conclusion"] == "Темп подключения нужно удвоить." and len(res["proposals"]) == 2
    assert res["proposal"] == ("Темп подключения нужно удвоить. - Предлагаю утвердить график подключения. "
                               "- Прошу поддержать дополнительное финансирование.")
    # подводка — «о чём вопрос», без обращения: слово передают реплики сценария (RC-2026-10-02-15, Н-2)
    assert res["intro"] == "О подключении школ к интернету."


def test_material_schema_old_answer_still_passes():
    old = {"question_num": "2", "title": "Справка", "summary": "Цифры", "intro": "Подводка",
           "theses": ["Раз", "Два"], "proposal": "Согласовать", "orders": ["Поручение строкой"]}
    res = llm_extract.check_material_schema(old)
    assert res["proposal"] == "Согласовать" and res["conclusion"] == "" and res["proposals"] == []
    assert res["figures"] == [] and res["orders"][0]["text"] == "Поручение строкой"
    assert res["orders"][0]["assignee"] == "" and res["orders"][0]["deadline"] == ""


def test_analyze_returns_warnings_and_ready_svg(client, monkeypatch):
    _fake_iskra(monkeypatch, dict(ANSWER, speaker="Небылицын Пётр Петрович"))
    login(client, "alice")
    mid = create(client)
    r = client.post(f"/api/meetings/{mid}/materials/analyze", json={"text": "Справка про интернет в школах"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == {"result", "warnings"}
    assert any("«Кривой тип»" in w for w in body["warnings"])
    assert any("Небылицын" in w for w in body["warnings"])               # докладчика нет в тексте
    res = body["result"]
    assert res["figures"][0]["svg"].startswith("<svg") and res["figures"][0]["id"] == "f1"
    assert res["text"] == "Справка про интернет в школах"                 # текст материала не теряется
    assert "problems" not in res
    # ничего не сохранено: человек ещё не отметил галочки
    assert client.get(f"/api/meetings/{mid}").json()["meeting"]["materials"] == []


def test_analyze_retries_strictly_after_prose(client, monkeypatch):
    calls = []
    answers = iter(["Конечно! Вот разбор: всё хорошо.", json.dumps(ANSWER, ensure_ascii=False)])

    def fake(system, user, *a, **kw):
        calls.append(user)
        return next(answers)
    monkeypatch.setattr(llm_extract, "_call_iskra_raw", fake)
    login(client, "alice")
    mid = create(client)
    r = client.post(f"/api/meetings/{mid}/materials/analyze", json={"text": "текст"})
    assert r.status_code == 200 and len(calls) == 2
    assert calls[1].startswith(llm_extract.MATERIAL_STRICT_REMINDER) and not calls[0].startswith("Предыдущий")


# ── применение, удаление, правка ────────────────────────────────────────

def _apply(client, mid, **over):
    result = llm_extract.check_material_schema(dict(ANSWER, **over))
    result["text"] = "Полный текст справки"
    return client.post(f"/api/meetings/{mid}/materials/apply", json={"result": result, "filename": "справка.docx"})


def test_apply_keeps_everything_and_orders_without_duplicates(client):
    login(client, "alice")
    mid = create(client)
    client.post(f"/api/meetings/{mid}/agenda", json={"num": "1", "title": "Об интернете"})
    # такое поручение уже есть (руками, другой регистр и точка) — второй раз не заводим
    client.post(f"/api/meetings/{mid}/orders", json={"text": "регионам  представить графики подключения."})
    figs = [dict(ANSWER["figures"][0], svg="<svg onload='alert(1)'></svg>")]
    orders = [ANSWER["orders"][0], {"text": "Минцифры России обеспечить подключение школ к 01.09.2027"},
              {"text": "Регионам представить графики подключения"}]
    r = _apply(client, mid, figures=figs, orders=orders)
    assert r.status_code == 200, r.text
    m = r.json()["meeting"]
    mat = m["materials"][0]
    assert r.json()["material_id"] == mat["id"]       # какой материал новый — говорит сервер (RC-28, Н-5)
    assert mat["intro"] == "О подключении школ к интернету." and mat["text"] == "Полный текст справки"
    assert mat["filename"] == "справка.docx"
    assert mat["conclusion"] == ANSWER["conclusion"] and mat["proposals"] == ANSWER["proposals"]
    assert mat["proposal"].startswith("Темп подключения нужно удвоить. - Предлагаю")
    assert len(mat["figures"]) == 1 and "onload" not in mat["figures"][0]["svg"]
    ET.fromstring(mat["figures"][0]["svg"])                             # svg пересчитан сервером из items
    mine = [o for o in m["orders"] if o["material_id"] == mat["id"]]
    assert len(m["orders"]) == 2 and len(mine) == 1                       # дубли не заведены
    assert mine[0] == dict(mine[0], text=ANSWER["orders"][0]["text"], assignee="Минцифры России",
                           deadline="01.09.2027", agenda_num="1")
    assert mat["orders"] == [ANSWER["orders"][0]["text"]] + [o["text"] for o in orders[1:]]


def test_delete_material_removes_its_orders_only(client):
    login(client, "alice")
    mid = create(client)
    client.post(f"/api/meetings/{mid}/agenda", json={"num": "1", "title": "Об интернете"})
    client.post(f"/api/meetings/{mid}/orders", json={"text": "Ручное поручение"})
    _apply(client, mid, orders=[{"text": "Поручение первого материала"}])
    m = _apply(client, mid, orders=[{"text": "Поручение второго материала"}]).json()["meeting"]
    first, second = m["materials"]
    assert client.delete(f"/api/meetings/{mid}/materials/{first['id']}").status_code == 200
    left = client.get(f"/api/meetings/{mid}").json()["meeting"]
    assert sorted(o["text"] for o in left["orders"]) == ["Поручение второго материала", "Ручное поручение"]
    assert [x["id"] for x in left["materials"]] == [second["id"]]


def test_patch_without_intro_keeps_intro(client):
    login(client, "alice")
    mid = create(client)
    client.post(f"/api/meetings/{mid}/agenda", json={"num": "1", "title": "Об интернете"})
    mat = _apply(client, mid).json()["meeting"]["materials"][0]
    r = client.patch(f"/api/meetings/{mid}/materials/{mat['id']}", json={"summary": "Новое саммари"})
    assert r.status_code == 200, r.text
    after = r.json()["meeting"]["materials"][0]
    assert after["summary"] == "Новое саммари"
    for k in ("intro", "title", "theses", "conclusion", "proposals", "proposal", "type"):
        assert after[k] == mat[k], k
    # вывод поправили — старое поле proposal пересобрано из вывода и предложений
    r = client.patch(f"/api/meetings/{mid}/materials/{mat['id']}", json={"conclusion": "Новый вывод."})
    after = r.json()["meeting"]["materials"][0]
    assert after["conclusion"] == "Новый вывод." and after["proposal"].startswith("Новый вывод. - Предлагаю")
    assert after["intro"] == mat["intro"]


def test_patch_orders_of_material(client):
    login(client, "alice")
    mid = create(client)
    client.post(f"/api/meetings/{mid}/agenda", json={"num": "1", "title": "Об интернете"})
    client.post(f"/api/meetings/{mid}/orders", json={"text": "Ручное поручение"})
    _apply(client, mid, orders=[{"text": "Чужое поручение другого материала"}])
    m = _apply(client, mid, orders=[{"text": "Первое"}, {"text": "Второе"}]).json()["meeting"]
    mat = m["materials"][1]
    mine = {o["text"]: o for o in m["orders"] if o["material_id"] == mat["id"]}
    assert set(mine) == {"Первое", "Второе"}

    # без orders — поручения не трогаем
    r = client.patch(f"/api/meetings/{mid}/materials/{mat['id']}", json={"title": "Другое название"})
    assert sorted(o["text"] for o in r.json()["meeting"]["orders"]) == sorted(
        ["Ручное поручение", "Чужое поручение другого материала", "Первое", "Второе"])

    first_id = mine["Первое"]["id"]
    r = client.patch(f"/api/meetings/{mid}/materials/{mat['id']}", json={"orders": [
        {"id": first_id, "text": "Первое, поправленное", "assignee": "Минпросвещения России",
         "deadline": "01.12.2026"},
        {"text": "Новое поручение", "assignee": "", "deadline": ""},
        {"text": "   "},                                              # пустая строка пропускается
    ]})
    assert r.status_code == 200, r.text
    m = r.json()["meeting"]
    mine = [o for o in m["orders"] if o["material_id"] == mat["id"]]
    assert len(mine) == 2
    upd = next(o for o in mine if o["id"] == first_id)
    assert upd["text"] == "Первое, поправленное" and upd["assignee"] == "Минпросвещения России"
    assert upd["deadline"] == "01.12.2026"
    new = next(o for o in mine if o["text"] == "Новое поручение")
    assert new["agenda_num"] == "1" and new["id"] != upd["id"]
    assert "Второе" not in [o["text"] for o in m["orders"]]               # нет в списке — убрано
    others = sorted(o["text"] for o in m["orders"] if o["material_id"] != mat["id"])
    assert others == ["Ручное поручение", "Чужое поручение другого материала"]
    assert m["materials"][1]["orders"] == ["Первое, поправленное", "Новое поручение"]


def test_old_meeting_without_new_fields_loads(client):
    login(client, "alice")
    mid = create(client)
    legacy = {
        "id": mid, "title": "Старое",
        "agenda": [{"num": "1", "title": "Вопрос", "id": "a1"}],
        "orders": [{"text": "Поручение", "assignee": "", "deadline": "", "agenda_num": "1", "id": "o1"}],
        "materials": [{"id": "m1", "question_num": "1", "type": "Справка", "speaker": "", "title": "Справка",
                       "text": "", "filename": "", "summary": "Саммари", "intro": "Подводка",
                       "theses": ["Тезис"], "proposal": "Согласовать", "orders": ["Поручение"], "source": {},
                       "from_the_future": 1}],      # поле версии новее — пропускаем, а не падаем
    }
    conn = storage._conn()
    conn.execute("UPDATE meetings SET data = ? WHERE id = ?", (json.dumps(legacy, ensure_ascii=False), mid))
    conn.commit()
    conn.close()
    r = client.get(f"/api/meetings/{mid}")
    assert r.status_code == 200, r.text
    m = r.json()["meeting"]
    mat = m["materials"][0]
    assert mat["figures"] == [] and mat["conclusion"] == "" and mat["proposals"] == []
    assert mat["proposal"] == "Согласовать" and m["orders"][0]["material_id"] == ""
    assert "from_the_future" not in mat
    html = client.get(f"/api/meetings/{mid}/export").text
    assert "<b>Предложение:</b> Согласовать" in html                     # старый материал — старый вид


# ── документ «Модератор» ────────────────────────────────────────────────

def test_material_card_has_svg_conclusion_and_proposals():
    res = llm_extract.check_material_schema(ANSWER)
    mat = Material(title="Интернет", summary="Саммари", figures=res["figures"], theses=res["theses"],
                   conclusion=res["conclusion"], proposals=res["proposals"], proposal=res["proposal"],
                   orders=["Поручение"])
    card = template_adapter._material_card(mat)
    assert "<svg" in card and "Источник: таблица 1" in card
    assert "<b>Вывод:</b> Темп подключения нужно удвоить." in card
    assert "<li>Предлагаю утвердить график подключения.</li>" in card
    assert "<b>Предложение:</b>" not in card                                # не дублируем старым полем
    assert "<li>Поручение</li>" in card


def test_export_html_keeps_svg_intact(client):
    login(client, "alice")
    mid = create(client)
    client.post(f"/api/meetings/{mid}/agenda", json={"num": "1", "title": "Об интернете"})
    _apply(client, mid)
    html = client.get(f"/api/meetings/{mid}/export").text
    start = html.index("<svg")
    piece = html[start:html.index("</svg>", start) + 6]
    assert 'viewBox="0 0 560 ' in piece and "<polyline" in piece
    assert "Предлагаю утвердить график подключения." in html


# ── вступительное слово из материалов ───────────────────────────────────

OPENING_ANSWER = ("- Коллеги, сегодня обсуждаем цифровую среду школ.\n"
                  "- Наша задача – обеспечить скоростной интернет каждой школе. **Это приоритет.**\n"
                  "- По первому вопросу доложит Корнеев Андрей Алексеевич, Статс-секретарь.")


def test_opening_from_materials(client, monkeypatch, audit):
    calls = []
    _fake_iskra(monkeypatch, OPENING_ANSWER, calls, tokens=(700, 90))
    login(client, "alice")
    mid = create(client)
    client.post(f"/api/meetings/{mid}/agenda", json={
        "num": "1", "title": "О подключении школ", "speakers": ["Корнеев Андрей Алексеевич"],
        "speaker_roles": ["Статс-секретарь – заместитель Министра просвещения"], "speaker_minutes": [10]})
    client.post(f"/api/meetings/{mid}/agenda", json={
        "num": "2", "title": "О кадрах", "speakers": ["Кравцов Сергей Сергеевич"],
        "speaker_roles": ["Министр просвещения"], "speaker_minutes": [10]})
    client.post(f"/api/meetings/{mid}/scenario/generate")
    _apply(client, mid, question_num="1", theses=["Тезис вопроса про школы."], orders=[],
           proposals=["Предложение, которого во вступлении быть не должно."])
    _apply(client, mid, question_num="", title="Паспорт нацпроекта", theses=["Общий тезис о масштабе."],
           conclusion="Общий вывод.", orders=[])
    r = client.post(f"/api/meetings/{mid}/opening")
    assert r.status_code == 200, r.text
    assert len(calls) == 1
    system, user = calls[0]
    assert system == llm_extract.OPENING_PROMPT
    assert user.index("Общие материалы") < user.index("Вопрос 1. О подключении школ")
    assert "Общий тезис о масштабе." in user and "Тезис вопроса про школы." in user
    # вступительное слово звучит до первого вопроса: слово не передаёт ни по одному, только называет докладчиков
    first = user[user.index("Вопрос 1."):user.index("Вопрос 2.")]
    second = user[user.index("Вопрос 2."):]
    assert "Докладчики: Корнеев Андрей Алексеевич, Статс-секретарь – заместитель Министра просвещения" in first
    assert "Докладчики: Кравцов Сергей Сергеевич, Министр просвещения" in second   # именительный
    assert "Прошу доложить:" not in user and "Кравцова" not in user
    assert "слово никому не передавай – только назови, кто доложит по вопросу" in system.lower()
    assert "передача слова идёт репликами сценария по каждому вопросу" in system
    assert "Предложение, которого во вступлении быть не должно." not in user
    assert "около 600 слов" in user                                    # 5 минут × 120 слов
    opening = [b for b in r.json()["meeting"]["scenario"] if b["section"] == "opening"]
    assert len(opening) == 1
    assert opening[0]["text"] == (
        "<ul><li>Коллеги, сегодня обсуждаем цифровую среду школ.</li>"
        "<li>Наша задача – обеспечить скоростной интернет каждой школе.</li><li>Это приоритет.</li>"
        "<li>По первому вопросу доложит Корнеев Андрей Алексеевич, Статс-секретарь.</li></ul>")
    ev = [e for e in audit if e["action"].startswith("Сформировано вступительное слово")]
    assert len(ev) == 1 and (ev[0]["tokens_in"], ev[0]["tokens_out"]) == (700, 90)


def test_opening_needs_materials(client, monkeypatch):
    calls = []
    _fake_iskra(monkeypatch, OPENING_ANSWER, calls)
    login(client, "alice")
    mid = create(client)
    client.post(f"/api/meetings/{mid}/agenda", json={"num": "1", "title": "Вопрос"})
    r = client.post(f"/api/meetings/{mid}/opening")
    assert r.status_code == 400 and "разберите материалы" in r.json()["detail"]
    assert calls == []                                                 # Искру зря не зовём


def test_failed_opening_still_reports_tokens(client, monkeypatch, audit):
    def fake(system, user, *a, **kw):
        llm_extract._note_usage({"status": "failed", "usage": {"prompt_tokens": 500, "completion_tokens": 3}})
        raise RuntimeError("Искра: неожиданный статус «failed»")
    login(client, "alice")
    mid = create(client)
    client.post(f"/api/meetings/{mid}/materials", json={"title": "Общий", "theses": ["Тезис."]})
    monkeypatch.setattr(llm_extract, "_call_iskra_raw", fake)
    before = len(audit)
    r = client.post(f"/api/meetings/{mid}/opening")
    assert r.status_code == 502
    ev = audit[before:]
    assert len(ev) == 1 and (ev[0]["tokens_in"], ev[0]["tokens_out"]) == (500, 3)


def test_split_bullets_keeps_initials_and_lines():
    assert llm_extract.split_bullets("Доклад А.А. Корнеева. Далее\n1. Второй пункт") == [
        "Доклад А.А. Корнеева.", "Далее.", "Второй пункт."]


# ── ревью RC-2026-10-02-15 ──────────────────────────────────────────────

def test_patch_rebuilds_proposal_even_if_window_sends_old_one(client):
    """Н-1: окно присылает старую склейку proposal — сервер её не берёт, пересобирает и правит сценарий."""
    login(client, "alice")
    mid = create(client)
    client.post(f"/api/meetings/{mid}/agenda", json={"num": "1", "title": "Об интернете",
                                                     "speakers": ["Корнеев Андрей Алексеевич"]})
    client.post(f"/api/meetings/{mid}/scenario/generate")
    mat = _apply(client, mid).json()["meeting"]["materials"][0]
    assert mat["proposal"]                                       # сервер отдал непустую склейку
    r = client.patch(f"/api/meetings/{mid}/materials/{mat['id']}", json={
        "proposal": mat["proposal"], "conclusion": "Новый вывод.", "proposals": ["Новое предложение."],
        "base": {"proposal": mat["proposal"], "conclusion": mat["conclusion"]}})
    assert r.status_code == 200, r.text
    m = r.json()["meeting"]
    assert m["materials"][0]["proposal"] == "Новый вывод. - Новое предложение."
    replicas = [b["text"] for b in m["scenario"] if b["kind"] == "replica" and b["material_id"] == mat["id"]]
    assert replicas == ["Предложение: Новый вывод. - Новое предложение."]
    # старый материал без вывода и предложений: proposal правится как раньше, пустые поля его не стирают
    client.post(f"/api/meetings/{mid}/materials", json={"title": "Старый", "proposal": "Согласовать"})
    old = client.get(f"/api/meetings/{mid}").json()["meeting"]["materials"][1]
    r = client.patch(f"/api/meetings/{mid}/materials/{old['id']}",
                     json={"proposal": "Согласовать иначе", "conclusion": "", "proposals": []})
    assert r.json()["meeting"]["materials"][1]["proposal"] == "Согласовать иначе"


def test_first_question_intro_goes_to_its_own_block_not_opening(client, monkeypatch):
    """Н-2: подводка первого вопроса — в его блок «Подводка», а не в собранное вступительное слово."""
    _fake_iskra(monkeypatch, OPENING_ANSWER)
    login(client, "alice")
    mid = create(client)
    client.post(f"/api/meetings/{mid}/agenda", json={"num": "1", "title": "Об интернете",
                                                     "speakers": ["Корнеев Андрей Алексеевич"]})
    client.post(f"/api/meetings/{mid}/scenario/generate")
    client.post(f"/api/meetings/{mid}/materials", json={"title": "Общий", "theses": ["Тезис."]})
    opening_html = [b for b in client.post(f"/api/meetings/{mid}/opening").json()["meeting"]["scenario"]
                    if b["section"] == "opening"][0]["text"]
    m = _apply(client, mid, question_num="1").json()["meeting"]
    opening = [b for b in m["scenario"] if b["section"] == "opening"][0]
    assert opening["text"] == opening_html                       # собранный список не тронут
    ref = [b for b in m["scenario"] if b["kind"] == "reference" and b["agenda_num"] == "1"]
    assert len(ref) == 1 and ref[0]["text"] == "О подключении школ к интернету."
    assert ref[0]["material_id"] == m["materials"][-1]["id"]
    # подводка идёт перед передачей слова, и таймлайн её видит
    kinds = [b["kind"] for b in m["scenario"] if b["agenda_num"] == "1"]
    assert kinds.index("reference") < kinds.index("intro")


def test_intro_address_is_removed():
    c = llm_extract.clean_intro
    assert c("Прошу доложить о подключении школ.") == "О подключении школ."
    assert c("прошу также доложить об итогах года") == "Об итогах года"
    assert c("О готовности школ") == "О готовности школ"
    assert c("Прошу доложить о") == "Прошу доложить о"              # нечего оставить — не трогаем


def test_opening_input_has_no_address_from_old_intro(client):
    """Н-2 (б): у старого материала подводка «Прошу доложить о …» во вход вступительного слова идёт без обращения."""
    login(client, "alice")
    mid = create(client)
    client.post(f"/api/meetings/{mid}/agenda", json={"num": "1", "title": "Об интернете"})
    m = storage.get(mid)
    m.materials.append(Material(question_num="1", title="Старый", intro="Прошу доложить о сети в школах."))
    storage.save(m)
    text = llm_extract.build_opening_input(storage.get(mid))
    assert "О чём вопрос: О сети в школах." in text and "оложить" not in text


def test_patch_orders_conflict_when_colleague_changed_them(client):
    """Н-5: поручения в основе правки; коллега их поменял — 409, его правка цела."""
    login(client, "alice")
    mid = create(client)
    client.post(f"/api/meetings/{mid}/agenda", json={"num": "1", "title": "Об интернете"})
    m = _apply(client, mid, orders=[{"text": "Первое"}]).json()["meeting"]
    mat = m["materials"][0]
    seen = [{k: o[k] for k in ("id", "text", "assignee", "deadline")} for o in m["orders"]]
    # тот же снимок — правка проходит
    r = client.patch(f"/api/meetings/{mid}/materials/{mat['id']}", json={
        "orders": [dict(seen[0], text="Первое, правка")], "base": {"orders": seen}})
    assert r.status_code == 200, r.text
    # коллега тем временем удалил поручение — правка окна по старому снимку не возвращает его
    oid = r.json()["meeting"]["orders"][0]["id"]
    seen2 = [{k: o[k] for k in ("id", "text", "assignee", "deadline")} for o in r.json()["meeting"]["orders"]]
    assert client.delete(f"/api/meetings/{mid}/orders/{oid}").status_code == 200
    r = client.patch(f"/api/meetings/{mid}/materials/{mat['id']}", json={
        "orders": [dict(seen2[0], text="Первое, вторая правка")], "base": {"orders": seen2}})
    assert r.status_code == 409 and "успел" in r.json()["detail"]
    assert client.get(f"/api/meetings/{mid}").json()["meeting"]["orders"] == []
    # без orders в теле снимок не мешает правке полей
    r = client.patch(f"/api/meetings/{mid}/materials/{mat['id']}", json={"title": "Т", "base": {"orders": seen2}})
    assert r.status_code == 200


def test_orders_limits():
    warnings: list[str] = []
    res = llm_extract.check_material_schema({"orders": [f"Поручение {i}" for i in range(40)]}, warnings)
    assert len(res["orders"]) == llm_extract.ORDERS_MAX and any("оставлены первые 30" in w for w in warnings)


def test_patch_orders_limit(client):
    login(client, "alice")
    mid = create(client)
    client.post(f"/api/meetings/{mid}/materials", json={"title": "М"})
    key = client.get(f"/api/meetings/{mid}").json()["meeting"]["materials"][0]["id"]
    r = client.patch(f"/api/meetings/{mid}/materials/{key}", json={"orders": [{"text": "x"}] * 31})
    assert r.status_code == 422


def test_chart_rejects_huge_numbers():
    for v in (1e308, -1e308, 1e15):
        with pytest.raises(ValueError):
            charts.svg({"type": "line", "items": [["a", 1], ["b", v]]})
    assert "nan" not in charts.svg({"type": "line", "items": [["a", 1], ["b", 9.9e14]]})


def test_split_bullets_keeps_city_abbreviation():
    assert llm_extract.split_bullets("Совещание в г. Москве прошло. Итог") == ["Совещание в г. Москве прошло.", "Итог."]


def test_failed_iskra_call_is_marked_as_error_in_audit(client, monkeypatch, audit):
    def fake(system, user, *a, **kw):
        llm_extract._note_usage({"status": "failed", "usage": {"prompt_tokens": 5, "completion_tokens": 1}})
        raise RuntimeError("сбой")
    login(client, "alice")
    mid = create(client)
    client.post(f"/api/meetings/{mid}/materials", json={"title": "Общий", "theses": ["Тезис."]})
    monkeypatch.setattr(llm_extract, "_call_iskra_raw", fake)
    before = len(audit)
    assert client.post(f"/api/meetings/{mid}/opening").status_code == 502
    assert audit[before:][0]["details"].startswith("ошибка, код 502")


# ── ревью RC-2026-10-02-18 ──────────────────────────────────────────────

def test_old_meeting_intro_in_opening_is_not_doubled(client):
    """Н-1: у совещания до 02.10 подводка первого вопроса лежит в opening — пересборка не кладёт её блоком."""
    login(client, "alice")
    mid = create(client)
    client.post(f"/api/meetings/{mid}/agenda", json={"num": "1", "title": "Об интернете",
                                                     "speakers": ["Корнеев Андрей Алексеевич"]})
    client.post(f"/api/meetings/{mid}/scenario/generate")
    m = storage.get(mid)
    m.materials.append(Material(id="old1", question_num="1", title="Старая справка",
                                intro="Прошу доложить о подключении школ."))
    opening = next(b for b in m.scenario if b.section == "opening")
    opening.text = "Добрый день, коллеги.\n\nПрошу доложить о подключении школ."    # так клал код до 02.10
    storage.save(m)
    r = client.post(f"/api/meetings/{mid}/scenario/generate")
    assert r.status_code == 200
    sc = r.json()["meeting"]["scenario"]
    assert [b for b in sc if b["kind"] == "reference"] == []
    assert sum("Прошу доложить о подключении" in b["text"] for b in sc) == 1      # только в opening, как было
    assert [x for x in r.json()["meeting"]["materials"] if x["id"] == "old1"]
    # подводки в opening нет — блок «Подводка» первому вопросу кладётся, как у остальных
    m = storage.get(mid)
    next(b for b in m.scenario if b.section == "opening").text = "Добрый день, коллеги."
    storage.save(m)
    sc = client.post(f"/api/meetings/{mid}/scenario/generate").json()["meeting"]["scenario"]
    assert [b["text"] for b in sc if b["kind"] == "reference"] == ["Прошу доложить о подключении школ."]


def test_old_material_proposal_kept_when_conclusion_added(client):
    """Н-2: старому материалу дописали вывод — прежнее «Предложение» встаёт первым пунктом предложений."""
    login(client, "alice")
    mid = create(client)
    client.post(f"/api/meetings/{mid}/materials", json={"title": "Старый", "proposal": "Согласовать график"})
    key = client.get(f"/api/meetings/{mid}").json()["meeting"]["materials"][0]["id"]
    r = client.patch(f"/api/meetings/{mid}/materials/{key}",
                     json={"proposal": "Согласовать график", "conclusion": "Вывод.", "proposals": []})
    mat = r.json()["meeting"]["materials"][0]
    assert mat["proposals"] == ["Согласовать график"]
    assert mat["proposal"] == "Вывод. - Согласовать график"
    # повторная правка нового вида старое не задваивает
    r = client.patch(f"/api/meetings/{mid}/materials/{key}", json={"conclusion": "Вывод 2."})
    assert r.json()["meeting"]["materials"][0]["proposals"] == ["Согласовать график"]
