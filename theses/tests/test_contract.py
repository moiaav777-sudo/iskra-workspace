"""Контракт сервиса с порталом (`../CLAUDE.md`) и основная работа с тезисами."""
from __future__ import annotations

from tests.conftest import headers, add_line, put_lines


# ── вход и защита ────────────────────────────────────────────────────────

def test_healthz_без_входа(client):
    r = client.get("/healthz")
    assert r.status_code == 200 and r.text == "ok"


def test_api_без_секрета_отклоняется(client):
    assert client.get("/api/theses").status_code == 401
    assert client.get("/api/theses", headers={"X-Portal-User": "emp"}).status_code == 401


def test_чужой_секрет_не_проходит(client):
    bad = headers()
    bad["X-Portal-Secret"] = "wrong-secret"
    assert client.get("/api/theses", headers=bad).status_code == 401


def test_страница_без_входа_ведёт_на_портал(client):
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login?next=/theses/"


def test_запрос_с_чужого_сайта_отклоняется(client, emp):
    head = dict(emp, Origin="https://zloy.example", Host="testserver")
    r = client.post("/api/theses", json={"topic": "проба"}, headers=head)
    assert r.status_code == 403


def test_me_возвращает_роль(client, chief):
    body = client.get("/api/me", headers=chief).json()
    assert body["login"] == "chief" and body["is_chief"] and not body["is_admin"]


# ── правила и профили ────────────────────────────────────────────────────

def test_правила_из_docs_видны(client, emp):
    body = client.get("/api/rules", headers=emp).json()
    assert any(f["name"].endswith(".md") for f in body["files"])
    assert all("образец" not in f["name"].lower() for f in body["files"])


def test_карточка_заводится_по_файлу_правил(client, emp):
    body = client.get("/api/profiles", headers=emp).json()
    assert body["profiles"], "профиль должен завестись сам по файлу из docs/"
    first = body["profiles"][0]
    assert first["has_rules"] and not first["missing_files"]


def test_карточку_удаляет_только_администратор(client, emp, admin):
    pid = client.post("/api/profiles", json={"name": "Проверочный"}, headers=emp).json()["id"]
    assert client.delete(f"/api/profiles/{pid}", headers=emp).status_code == 403
    assert client.delete(f"/api/profiles/{pid}", headers=admin).status_code == 200


def test_карточку_с_работой_не_удалить(client, emp, admin):
    pid = client.post("/api/profiles", json={"name": "Занятый"}, headers=emp).json()["id"]
    client.post("/api/theses", json={"topic": "тема", "profile": pid}, headers=emp)
    assert client.delete(f"/api/profiles/{pid}", headers=admin).status_code == 409


# ── тезисы ───────────────────────────────────────────────────────────────

def _new(client, emp, **kw):
    payload = {"topic": "О поддержке молодых педагогов", "minutes": 7}
    payload.update(kw)
    return client.post("/api/theses", json=payload, headers=emp).json()


def test_создание_и_чтение(client, emp):
    created = _new(client, emp)
    tid = created["thesis"]["id"]
    got = client.get(f"/api/theses/{tid}", headers=emp).json()
    assert got["thesis"]["topic"] == "О поддержке молодых педагогов"
    assert got["rev"] == 1
    # проверки чисел больше нет (решение пользователя 21.09) — и полей о ней тоже
    assert "unchecked" not in got["thesis"] and "indicators" not in got["thesis"]


def test_буллиты_и_хронометраж(client, emp):
    created = _new(client, emp)
    tid, rev = created["thesis"]["id"], created["rev"]
    text = " ".join(["слово"] * 120)
    r = add_line(client, emp, tid, **{"text": text, "rev": rev})
    assert r.status_code == 200
    body = r.json()
    assert body["thesis"]["words"] == 120
    assert body["thesis"]["spoken_minutes"] == 1.0


def test_маркер_требуется_фактура_попадает_в_список(client, emp):
    created = _new(client, emp)
    tid, rev = created["thesis"]["id"], created["rev"]
    body = add_line(client, emp, tid, **{
        "text": "Охват составил [ТРЕБУЕТСЯ ФАКТУРА: доля молодых педагогов] процентов.",
        "rev": rev}).json()
    gaps = body["thesis"]["gaps"]
    assert len(gaps) == 1
    # прежняя метка «ТРЕБУЕТСЯ ФАКТУРА» читается как новая «нужна цифра» (22.09, п. 5)
    assert gaps[0]["marker"] == "нужна цифра"
    assert gaps[0]["suggestion"] == "доля молодых педагогов"
    assert "[нужна цифра: доля молодых педагогов]" in body["thesis"]["bullets"][0]["text"]
    assert body["thesis"]["open_gaps"] == 1


def test_маркер_убрали_руками_место_закрыто(client, emp):
    created = _new(client, emp)
    tid, rev = created["thesis"]["id"], created["rev"]
    added = add_line(client, emp, tid, text="Охват — [ТРЕБУЕТСЯ ФАКТУРА: доля] процентов.",
                     rev=rev).json()
    line = added["thesis"]["bullets"][0]
    body = put_lines(client, emp, tid, [{"id": line["id"], "text": "Охват — 42 процента."}],
                     added["rev"]).json()
    assert body["thesis"]["open_gaps"] == 0


# ── один редактор на весь текст (задача 21.09) ───────────────────────────

def test_редактор_сохраняет_весь_текст_разом(client, emp):
    """Строки — тезисы; пустые не хранятся; тезис узнаётся по id и сохраняет часть."""
    created = _new(client, emp)
    tid, rev = created["thesis"]["id"], created["rev"]
    body = put_lines(client, emp, tid, ["первый", "  ", {"text": "второй", "emphasis": "strong"}],
                     rev).json()
    first, second = body["thesis"]["bullets"]
    assert [first["text"], second["text"]] == ["первый", "второй"]
    assert second["emphasis"] == "strong"

    from app import storage
    stored = storage.get(tid)
    stored.thesis.bullets[0].part = "контекст"
    rev = storage.save(stored.thesis, expected_rev=body["rev"]).rev

    # порядок поменяли, первый поправили, добавили третий
    moved = put_lines(client, emp, tid, [
        {"id": second["id"], "text": "второй", "emphasis": "aside"},
        {"id": first["id"], "text": "первый, поправленный"},
        "третий"], rev).json()["thesis"]["bullets"]
    assert [b["text"] for b in moved] == ["второй", "первый, поправленный", "третий"]
    assert moved[0]["id"] == second["id"] and moved[0]["emphasis"] == "aside"
    assert moved[1]["id"] == first["id"] and moved[1]["part"] == "контекст"
    assert moved[2]["id"] not in (first["id"], second["id"])


def test_строку_разрезали_enter_ом_повтор_id_это_новый_тезис(client, emp):
    """Браузер при Enter копирует разметку строки вместе с id — второй такой id не
    должен перетереть первый тезис."""
    created = _new(client, emp)
    tid, rev = created["thesis"]["id"], created["rev"]
    line = put_lines(client, emp, tid, ["Раз, два."], rev).json()
    bid = line["thesis"]["bullets"][0]["id"]
    body = put_lines(client, emp, tid, [{"id": bid, "text": "Раз,"}, {"id": bid, "text": "два."}],
                     line["rev"]).json()
    ids = [b["id"] for b in body["thesis"]["bullets"]]
    assert ids[0] == bid and ids[1] != bid and len(set(ids)) == 2


def test_редактор_без_версии_не_сохраняет(client, emp):
    """Редактор шлёт весь текст: без версии он затёр бы чужую правку."""
    tid = _new(client, emp)["thesis"]["id"]
    r = client.put(f"/api/theses/{tid}/text", headers=emp, json={"bullets": [{"text": "раз"}]})
    assert r.status_code == 400


def test_ничего_не_поменялось_в_журнал_не_пишется(client, emp, monkeypatch):
    from app import portal
    sent = []
    monkeypatch.setattr(portal, "send_audit", lambda **kw: sent.append(kw))
    created = _new(client, emp)
    tid = created["thesis"]["id"]
    body = put_lines(client, emp, tid, ["Коллеги."], created["rev"]).json()
    sent.clear()
    same = put_lines(client, emp, tid, [{"id": body["thesis"]["bullets"][0]["id"],
                                         "text": "Коллеги."}], body["rev"]).json()
    assert same["rev"] == body["rev"] and sent == []


def test_журнал_говорит_что_поменялось_в_тексте(client, emp, monkeypatch):
    from app import portal
    sent = []
    monkeypatch.setattr(portal, "send_audit", lambda **kw: sent.append(kw))
    created = _new(client, emp)
    tid = created["thesis"]["id"]
    body = put_lines(client, emp, tid, ["Раз.", "Два."], created["rev"]).json()
    one, two = body["thesis"]["bullets"]
    sent.clear()
    put_lines(client, emp, tid, [{"id": one["id"], "text": "Раз, поправлено."},
                                 {"id": two["id"], "text": "Два.", "emphasis": "strong"}],
              body["rev"])
    assert sent and sent[-1]["action"] == "Изменён текст тезисов"
    details = sent[-1]["details"]
    assert "«Раз.» → «Раз, поправлено.»" in details
    assert "обычный → акцент" in details


def test_одновременная_правка_даёт_409(client, emp):
    created = _new(client, emp)
    tid, rev = created["thesis"]["id"], created["rev"]
    add_line(client, emp, tid, **{"text": "раз", "rev": rev})
    r = add_line(client, emp, tid, **{"text": "два", "rev": rev})
    assert r.status_code == 409
    assert "current" in r.json()["detail"]


def test_архив_восстановление_и_удаление(client, emp, admin):
    tid = _new(client, emp)["thesis"]["id"]
    assert client.delete(f"/api/theses/{tid}", headers=emp).status_code == 200
    assert [t["id"] for t in client.get("/api/theses", headers=emp).json()["theses"]] == []
    assert client.get("/api/theses?archived=true", headers=emp).json()["theses"][0]["id"] == tid
    client.post(f"/api/theses/{tid}/restore", headers=emp)
    assert client.delete(f"/api/theses/{tid}/permanent", headers=emp).status_code == 403
    assert client.delete(f"/api/theses/{tid}/permanent", headers=admin).status_code == 200


def test_поиск_для_портала(client, emp):
    _new(client, emp)
    body = client.get("/api/search?q=педагог", headers=emp).json()
    assert body["items"], "портал должен найти тезисы по теме"
    item = body["items"][0]
    assert set(item) == {"title", "url", "when", "who", "kind"}
    assert item["url"].startswith("/theses/#")


def test_поиск_молчит_на_коротком_запросе(client, emp):
    assert client.get("/api/search?q=п", headers=emp).json()["items"] == []


# ── передача в «Модератор» ───────────────────────────────────────────────

def test_адреса_общей_оси_совещания(client, emp):
    """Совещание своё не заводим: просим «Модератор» найти или завести,
    документ кладём в папку «Материалов» (контракт `../CLAUDE.md`, п. 9)."""
    created = _new(client, emp)
    tid, rev = created["thesis"]["id"], created["rev"]
    add_line(client, emp, tid, **{"text": "Коллеги, добрый день.", "rev": rev})
    body = client.get(f"/api/theses/{tid}/moderator-payload", headers=emp).json()
    assert body["find_or_create_url"] == "/moderator/api/meetings/find-or-create"
    assert body["opening_url_template"].endswith("/scenario/opening-from-theses")
    assert body["materials_files_url_template"] == "/materials/api/events/<id>/files"
    # папки у совещания может ещё не быть — заводим её сами (п. 9, три шага)
    assert body["materials_link_url"] == "/materials/api/events/link"
    assert body["moderator_folder_url_template"] == "/moderator/api/meetings/<id>/folder"
    assert body["source"] == {"service": "theses", "id": tid}
    assert "create_url" not in body, "своё совещание сервис не заводит"
    assert body["payload"]["text"] == "Коллеги, добрый день."
    assert body["payload"]["minutes"] == 7
    assert body["payload"]["source"] == {"service": "theses", "id": tid}


def test_цифры_больше_не_держат_выгрузку(client, emp):
    """Решение пользователя 21.09: «всё будет на моё решение». Число, которого нет
    ни в каких материалах, выгрузке и передаче не мешает."""
    created = _new(client, emp)
    tid, rev = created["thesis"]["id"], created["rev"]
    add_line(client, emp, tid, text="Обеспечено 260 тысяч мест.", rev=rev)
    assert client.get(f"/api/theses/{tid}/moderator-payload", headers=emp).status_code == 200
    assert client.get(f"/api/theses/{tid}/export/theses.docx", headers=emp).status_code == 200
    for route in ("indicators", "indicators/x/confirm"):
        assert client.post(f"/api/theses/{tid}/{route}", headers=emp,
                           json={"rev": rev}).status_code in (404, 405)


def test_вставить_текст_убрано(client, emp):
    """«Вставить текст» заменило окно «Что нужно» (задача 21.09, п. 5): маршрута нет."""
    created = _new(client, emp)
    tid = created["thesis"]["id"]
    r = client.post(f"/api/theses/{tid}/sources", headers=emp,
                    json={"name": "справка", "text": "текст " * 30, "rev": created["rev"]})
    assert r.status_code in (404, 405)


def test_каждый_тезис_едет_отдельной_строкой(client, emp):
    """«Модератор» режет text по строкам и делает из них пункты списка
    (`moderator/app/routes_content.py` → opening_from_theses). Значит одна мысль —
    одна строка, пустых строк быть не должно."""
    created = _new(client, emp)
    tid, rev = created["thesis"]["id"], created["rev"]
    for text in ("Коллеги, добрый день.", "   ", "Клубы созданы в 80 процентах школ.",
                 "Охват ниже [ТРЕБУЕТСЯ ФАКТУРА: охват на селе]."):
        rev = add_line(client, emp, tid, **{"text": text, "rev": rev}).json()["rev"]
    payload = client.get(f"/api/theses/{tid}/moderator-payload", headers=emp).json()["payload"]
    lines = payload["text"].split("\n")
    assert lines == ["Коллеги, добрый день.",
                     "Клубы созданы в 80 процентах школ.",
                     "Охват ниже [нужна цифра: охват на селе]."]
    assert all(line.strip() for line in lines), "пустой пункт в сценарии не нужен"


def test_служебный_блок_доуточнения_не_уезжает(client, emp):
    """Что доуточнить — забота составителя, вслух это не читают."""
    created = _new(client, emp)
    tid, rev = created["thesis"]["id"], created["rev"]
    add_line(client, emp, tid, **{"text": "Охват ниже [ТРЕБУЕТСЯ ФАКТУРА: охват на селе].", "rev": rev})
    text = client.get(f"/api/theses/{tid}/moderator-payload", headers=emp).json()["payload"]["text"]
    assert "ЧТО НЕОБХОДИМО ДОУТОЧНИТЬ" not in text
    assert "[нужна цифра: охват на селе]" in text, (
        "сама метка остаётся: человек должен видеть, чего не хватает")


def test_подсказка_для_нового_совещания(client, emp):
    """Подходящего совещания может не быть — тогда «Модератор» заведёт его сам.
    Название предлагаем мы: с пустым названием он совещание не заведёт."""
    created = client.post("/api/theses", headers=emp, json={
        "topic": "О школьных клубах", "event": "совещание с регионами", "minutes": 7}).json()
    tid, rev = created["thesis"]["id"], created["rev"]
    add_line(client, emp, tid, **{"text": "Коллеги.", "rev": rev})
    suggest = client.get(f"/api/theses/{tid}/moderator-payload", headers=emp).json()["suggest"]
    assert suggest["title"] == "совещание с регионами"

    # не сказано, где выступление — подставляем тему
    other = client.post("/api/theses", headers=emp,
                        json={"topic": "О сиротах", "minutes": 5}).json()
    oid, orev = other["thesis"]["id"], other["rev"]
    add_line(client, emp, oid, **{"text": "Коллеги.", "rev": orev})
    assert client.get(f"/api/theses/{oid}/moderator-payload",
                      headers=emp).json()["suggest"]["title"] == "О сиротах"


def test_пустой_текст_не_считается_текстом(client, emp):
    """Стёрли всё в редакторе — выгружать и передавать нечего."""
    created = _new(client, emp)
    tid, rev = created["thesis"]["id"], created["rev"]
    body = put_lines(client, emp, tid, ["", "   "], rev).json()
    assert body["thesis"]["has_text"] is False and body["thesis"]["bullets"] == []
    assert client.get(f"/api/theses/{tid}/export/theses.docx", headers=emp).status_code == 400
    assert client.get(f"/api/theses/{tid}/moderator-payload", headers=emp).status_code == 400
    filled = put_lines(client, emp, tid, ["Коллеги, добрый день."], body["rev"]).json()
    assert filled["thesis"]["has_text"] is True
    assert client.get(f"/api/theses/{tid}/export/theses.docx", headers=emp).status_code == 200


def test_дата_предлагается_сегодняшняя_а_название_не_пустое(client, emp):
    """`find-or-create` требует дату и название и ищет двойников по этой паре."""
    import datetime as dt
    created = client.post("/api/theses", headers=emp, json={
        "topic": "О сиротах", "event": "совещание с регионами", "minutes": 7}).json()
    tid, rev = created["thesis"]["id"], created["rev"]
    add_line(client, emp, tid, **{"text": "Коллеги.", "rev": rev})
    body = client.get(f"/api/theses/{tid}/moderator-payload", headers=emp).json()
    assert body["today"] == dt.date.today().strftime("%d.%m.%Y")
    assert body["suggest"]["title"] == "совещание с регионами"
    assert body["suggest"]["date"] == "", "дату ставит человек, выдумывать её нечестно"


def test_source_уходит_с_документом(client, emp):
    """По `source` «Материалы» отличают новую редакцию тех же тезисов от других
    тезисов к другому вопросу (п. 9, дополнен 20.09). Тезисов к одному совещанию
    бывает несколько, поэтому передавать его обязательно."""
    created = _new(client, emp)
    tid, rev = created["thesis"]["id"], created["rev"]
    add_line(client, emp, tid, **{"text": "Коллеги.", "rev": rev})
    body = client.get(f"/api/theses/{tid}/moderator-payload", headers=emp).json()
    assert body["source"] == {"service": "theses", "id": tid}
    # тот же ключ уходит и в «Модератор», и в «Материалы» — документ один и тот же
    assert body["payload"]["source"] == body["source"]


# ── один запрос на действие (находка SRE 21.09: парные POST /api/theses) ──

def test_повтор_создания_с_тем_же_ключом_не_заводит_вторые_тезисы(client, emp, monkeypatch):
    """Двойной щелчок по «Создать» дошёл до сервера дважды — тезисы одни, запись в журнале одна."""
    import threading
    from app import portal
    seen = []
    monkeypatch.setattr(portal, "send_audit", lambda **e: seen.append(e))
    body = {"topic": "Двойной щелчок", "minutes": 5, "key": "nabc12345xyz"}
    got, barrier = [], threading.Barrier(4)

    def post():
        barrier.wait()
        got.append(client.post("/api/theses", json=body, headers=emp).json()["thesis"]["id"])

    threads = [threading.Thread(target=post) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert len(got) == 4 and len(set(got)) == 1, f"тезисов завелось {len(set(got))}"
    listed = [t for t in client.get("/api/theses", headers=emp).json()["theses"]
              if t["topic"] == "Двойной щелчок"]
    assert len(listed) == 1
    created = [e for e in seen if e["action"] == "Созданы тезисы"]
    assert len(created) == 1, "повтор ничего не менял — в журнал не пишем"


def test_разные_ключи_и_без_ключа_создают_как_раньше(client, emp):
    ids = {client.post("/api/theses", json={"topic": f"Тема {n}", "key": key}, headers=emp)
           .json()["thesis"]["id"]
           for n, key in enumerate(("nkey000001", "nkey000002", "", "плохой ключ"))}
    assert len(ids) == 4


def test_свежая_база_выдерживает_одновременные_подключения(tmp_path, monkeypatch):
    """На свежей базе одновременные запросы не должны получать «database is locked»:
    WAL и таблицы готовятся один раз, а не каждым подключением наперегонки."""
    import importlib
    import threading
    from app import storage
    from app.models import Thesis
    errors = []
    for round_ in range(15):
        monkeypatch.setenv("THESES_DATA", str(tmp_path / f"db{round_}"))
        importlib.reload(storage)
        barrier = threading.Barrier(8)

        def work():
            barrier.wait()
            try:
                storage.create(Thesis(topic="проба"))
            except Exception as error:  # noqa: BLE001
                errors.append(str(error))

        threads = [threading.Thread(target=work) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(20)
    assert not errors, errors[:3]


def test_подробности_в_журнале_не_длиннее_300_знаков(client, emp, monkeypatch):
    """Памятка, п. 8: тема, место и «что нужно» разом дают длинную запись — обрезаем."""
    from app import portal
    sent = []
    monkeypatch.setattr(portal, "_queue", type("Q", (), {"put_nowait": lambda self, e: sent.append(e)})())
    monkeypatch.setattr(portal, "_worker", type("W", (), {"is_alive": lambda self: True})())
    created = _new(client, emp)
    tid = created["thesis"]["id"]
    long = "очень длинное пожелание " * 20
    client.put(f"/api/theses/{tid}", headers=emp, json={
        "topic": "Тема " * 30, "event": "Совещание " * 20, "ask": long, "minutes": 9,
        "rev": created["rev"]})
    details = [e["details"] for e in sent if e["action"] == "Изменена карточка тезисов"]
    assert details and len(details[-1]) <= 300
    sent.clear()
    portal.send_audit(login="emp", action="проба", object="x", details="слово " * 200, ip="")
    assert len(sent[0]["details"]) == 300 and sent[0]["details"].endswith("…")


# ── ревьюер RC-2026-09-21-16, Н3: ключ создания не застревает ────────────

def test_н3_ключ_после_удаления_тезисов_создаёт_заново(client, emp, admin):
    body = {"topic": "О ключах", "key": "stuckkey01"}
    first = client.post("/api/theses", json=body, headers=emp).json()["thesis"]["id"]
    assert client.delete(f"/api/theses/{first}/permanent", headers=admin).status_code == 200
    again = client.post("/api/theses", json=body, headers=emp)
    assert again.status_code == 200 and again.json()["thesis"]["id"] != first


def test_н3_брошенный_ключ_перезанимается(client, emp, monkeypatch):
    """Процесс упал между занятием ключа и созданием — повтор не ждёт суток."""
    from app import storage
    assert storage.claim_create_key("emp:lostkey01") == (True, "")
    monkeypatch.setattr(storage, "CREATE_KEY_STALE", 0)
    r = client.post("/api/theses", json={"topic": "О брошенном", "key": "lostkey01"}, headers=emp)
    assert r.status_code == 200 and r.json()["thesis"]["topic"] == "О брошенном"


def test_н3_ключ_свой_у_каждого(client, emp):
    """Тот же ключ у другого человека — это другое окно, а не повтор."""
    from tests.conftest import headers
    other = headers("emp2", "employee", "Другой")
    one = client.post("/api/theses", json={"topic": "Моё", "key": "samekey001"}, headers=emp).json()
    two = client.post("/api/theses", json={"topic": "Его", "key": "samekey001"}, headers=other).json()
    assert one["thesis"]["id"] != two["thesis"]["id"] and two["thesis"]["topic"] == "Его"


# ── п. 10: сервис «Модератор» называется «Совещание» (решение пользователя 21.09) ──

def test_п10_в_видимых_текстах_сервис_называется_совещание():
    """Адреса `/moderator/...` не меняются, а человек «Модератора» как сервиса на странице не видит."""
    import re
    from pathlib import Path
    from app import portal
    src = (Path(__file__).resolve().parent.parent / "static" / "app.js").read_text()
    code = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    code = "\n".join(line.split("//")[0] if "'" not in line.split("//")[0][-1:] else line
                     for line in code.splitlines() if not line.strip().startswith("//"))
    strings = re.findall(r"'([^'\n]*)'", code)
    assert not [s for s in strings if "Модератор" in s], "в строках страницы остался «Модератор»"
    # а строка действия журнала — служебная, по ней аналитик ведёт ряд: прежняя (RC-18)
    assert portal.describe("GET", "/api/theses/x/moderator-payload") == "Тезисы переданы в «Модератор»"


def test_н4_файл_базы_убрали_на_ходу_сервис_оживает(client, emp):
    """Ревьюер RC-16, Н4: без перезапуска все запросы получали 500."""
    from app import storage
    assert client.post("/api/theses", json={"topic": "До"}, headers=emp).status_code == 200
    for tail in ("", "-wal", "-shm"):
        path = storage.DB_PATH.with_name(storage.DB_PATH.name + tail)
        if path.exists():
            path.unlink()
    assert client.get("/api/theses", headers=emp).status_code == 200
    assert client.post("/api/theses", json={"topic": "После"}, headers=emp).status_code == 200



def test_rc18_н4_задания_после_удаления_базы(client, emp):
    """Н4 для таблицы заданий: после удаления файла базы «no such table: jobs»."""
    from app import storage
    tid = client.post("/api/theses", json={"topic": "До"}, headers=emp).json()["thesis"]["id"]
    assert client.get(f"/api/theses/{tid}/job", headers=emp).status_code == 200
    for tail in ("", "-wal", "-shm"):
        path = storage.DB_PATH.with_name(storage.DB_PATH.name + tail)
        if path.exists():
            path.unlink()
    tid = client.post("/api/theses", json={"topic": "После"}, headers=emp).json()["thesis"]["id"]
    assert client.get(f"/api/theses/{tid}/job", headers=emp).status_code == 200


def test_rc18_пометка_у_микрофона():
    """Решение пользователя 22.09: голос оставляем, но говорим, куда уходит звук."""
    from pathlib import Path
    src = (Path(__file__).resolve().parent.parent / "static" / "app.js").read_text()
    assert ("Звук распознаёт сервис браузера (Google, Microsoft или Яндекс) — ' +\n"
            "    'служебное лучше вводить текстом.") in src



# ── аудит Security 21.09 (high): чтение файлов ПК через имя файла правил ──

def _outside(tmp_path):
    secret = tmp_path / "секрет.txt"
    secret.write_text("СЕКРЕТНО: содержимое файла вне docs", encoding="utf-8")
    return secret


TRAVERSALS = ("../{name}", "..\\{name}", "{abs}", "..%2F{name}", "..%5C{name}",
              "%2e%2e/{name}", "docs/../../{name}")


def test_sec_путь_наружу_в_адресе_правил_не_открывается(client, emp, tmp_path, monkeypatch):
    from app import rules
    secret = _outside(tmp_path)
    monkeypatch.setattr(rules, "DOCS_DIR", tmp_path / "docs")
    rules.DOCS_DIR.mkdir()
    (rules.DOCS_DIR / "правила.md").write_text("# Правила\nтекст", encoding="utf-8")
    assert client.get("/api/rules/правила.md", headers=emp).status_code == 200
    for pattern in TRAVERSALS:
        name = pattern.format(name=secret.name, abs=str(secret))
        r = client.get(f"/api/rules/{name}", headers=emp)
        assert r.status_code == 404, name
        assert "СЕКРЕТНО" not in r.text, name
    assert client.get("/api/rules/..", headers=emp).status_code in (404, 405)


def test_sec_карточка_не_принимает_путь_наружу(client, emp, tmp_path, monkeypatch):
    from app import rules
    secret = _outside(tmp_path)
    monkeypatch.setattr(rules, "DOCS_DIR", tmp_path / "docs")
    rules.DOCS_DIR.mkdir()
    (rules.DOCS_DIR / "правила.md").write_text("# Правила\nтекст", encoding="utf-8")
    for field in ("own_file", "base_file"):
        for bad in (str(secret), "../" + secret.name, "..\\" + secret.name):
            r = client.post("/api/profiles", json={"name": "Проба", field: bad}, headers=emp)
            assert r.status_code == 400, (field, bad)
    pid = client.post("/api/profiles", json={"name": "Проба", "own_file": "правила.md"},
                      headers=emp).json()["id"]
    r = client.put(f"/api/profiles/{pid}", headers=emp,
                   json={"name": "Проба", "own_file": "../" + secret.name})
    assert r.status_code == 400
    # существование файла вне docs ничего не выдаёт
    assert client.get("/api/profiles", headers=emp).json()["profiles"]


def test_sec_путь_в_записанной_карточке_не_читается(client, emp, tmp_path, monkeypatch):
    """Карточка с путём наружу уже в базе (записана старым кодом): текст не уходит
    ни в ответ, ни в «Искру»."""
    from app import rules, storage
    secret = _outside(tmp_path)
    monkeypatch.setattr(rules, "DOCS_DIR", tmp_path / "docs")
    rules.DOCS_DIR.mkdir()
    prof = storage.Profile(name="Проба", own_file=str(secret), base_file="../" + secret.name)
    storage.save_profile(prof)
    created = client.post("/api/theses", json={"topic": "Тема", "profile": prof.id}, headers=emp).json()
    tid = created["thesis"]["id"]
    r = client.get(f"/api/theses/{tid}/rules", headers=emp)
    assert r.status_code == 200 and "СЕКРЕТНО" not in r.text and r.json()["text"] == ""
    assert "СЕКРЕТНО" not in prof.rules_text()
    card = next(p for p in client.get("/api/profiles", headers=emp).json()["profiles"] if p["id"] == prof.id)
    assert card["has_rules"] is False


def test_sec_имя_файла_правил_только_внутри_docs(tmp_path, monkeypatch):
    """Сам разбор имени: на ПК разделитель «\\» и адрес «..%5C» доходят до него, у нас на
    стенде маршрут их не пропускает — поэтому проверяем функции напрямую."""
    from app import rules
    secret = _outside(tmp_path)
    monkeypatch.setattr(rules, "DOCS_DIR", tmp_path / "docs")
    rules.DOCS_DIR.mkdir()
    (rules.DOCS_DIR / "правила.md").write_text("# Правила\nтекст", encoding="utf-8")
    assert rules.exists("правила.md") and "текст" in rules.read("правила.md")
    for bad in ("../" + secret.name, "..\\" + secret.name, str(secret), "..", ".",
                "docs/../" + secret.name, "правила.md/../../" + secret.name):
        assert not rules.exists(bad), bad
        try:
            text = rules.read(bad)
        except FileNotFoundError:
            continue
        assert "СЕКРЕТНО" not in text, bad
