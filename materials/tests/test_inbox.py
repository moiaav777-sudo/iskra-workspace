"""Приёмная: документ с телефона приходит по ключу и ждёт в ленте «Пришло»."""
import pytest


@pytest.fixture()
def key(client, monkeypatch):
    """Ключ приёмной проверяет портал — в тестах подменяем его ответ."""
    from app import portal
    monkeypatch.setattr(portal, "user_by_inbox_key", lambda k: {
        "login": "emp", "role": "employee", "is_admin": False, "is_chief": False,
        "name": "Сотрудник", "team": []} if k == "good-key" else None)
    return {"X-Inbox-Key": "good-key"}


def _send(client, headers, text="Справка о развитии округа", name="Справка.txt", **data):
    return client.post("/api/inbox", headers=headers, data=data,
                       files=[("files", (name, text.encode(), "text/plain"))])


def test_без_ключа_приёмка_закрыта(client):
    assert _send(client, {}).status_code == 401


def test_чужой_ключ_не_проходит(client, key):
    assert _send(client, {"X-Inbox-Key": "no-such-key"}).status_code == 401


def test_документ_ложится_в_приёмную_и_читается(client, key, emp):
    r = _send(client, key, name="Инф_справка_ТН.txt")
    answer = r.json()
    assert answer["ok"] and answer["saved"] == ["Инф_справка_ТН.txt"]
    assert answer["where"] == "Приёмная"
    # телефон показывает эту строку уведомлением — она должна читаться человеком
    assert answer["message"] == "Принято: Инф_справка_ТН → Приёмная — разберите в портале"

    waiting = client.get("/api/waiting", headers=emp).json()
    assert waiting["active"] is None
    assert waiting["items"][0]["name"] == "Инф_справка_ТН.txt"
    assert waiting["items"][0]["title"] == "Инф_справка_ТН"      # как прислали
    # текст прочитан сразу: документ находится поиском ещё до раскладки
    assert waiting["items"][0]["looks_like"] == ""


def test_вид_документа_узнаётся(client, key, emp):
    _send(client, key, text="Повестка совещания", name="п.txt")
    assert client.get("/api/waiting", headers=emp).json()["items"][0]["looks_like"] == "повестка"


def test_телефон_знает_мероприятие_и_документ_идёт_сразу_туда(client, key, emp, event):
    r = _send(client, key, event=event["id"])
    assert r.json()["where"] == "02.09.2026 Встреча с Музаевым"
    card = client.get(f"/api/events/{event['id']}", headers=emp).json()
    assert "05. Справка.txt" in [f["name"] for f in card["files"]]   # имя как прислали
    assert client.get("/api/waiting", headers=emp).json()["items"] == []


def test_активное_мероприятие_принимает_без_вопросов(client, key, emp, event):
    client.post("/api/active", headers=emp, json={"event": event["id"]})
    r = _send(client, key)
    assert r.json()["where"] == "02.09.2026 Встреча с Музаевым"

    client.post("/api/active", headers=emp, json={"event": ""})
    assert _send(client, key, name="вторая.txt").json()["where"] == "Приёмная"


def test_список_мероприятий_для_телефона(client, key, emp, event):
    items = client.get("/api/inbox/events", headers=key).json()["items"]
    assert items and items[0]["id"] == event["id"]
    assert items[0]["date"] == "02.09.2026"


def test_из_приёмной_в_мероприятие(client, key, emp, event):
    _send(client, key, name="Инф_справка_ТН.txt")
    r = client.post("/api/waiting/Инф_справка_ТН.txt/move", headers=emp,
                    json={"event": event["id"]})
    assert r.json()["name"] == "05. Инф_справка_ТН.txt"      # с телефона — имя как прислали
    assert client.get("/api/waiting", headers=emp).json()["items"] == []

    card = client.get(f"/api/events/{event['id']}", headers=emp).json()
    # первичка поехала вместе с документом: имя, под которым прислали, видно
    assert [a["name"] for a in card["archive"]] == ["Инф_справка_ТН.txt"]


def test_стереть_из_приёмной_может_только_админ(client, key, emp, admin):
    _send(client, key, name="мусор.txt")
    assert client.delete("/api/waiting/мусор.txt", headers=emp).status_code == 403
    assert client.delete("/api/waiting/мусор.txt", headers=admin).status_code == 200
    assert client.get("/api/waiting", headers=emp).json()["items"] == []


def test_убрать_из_приёмной_может_любой_и_файл_цел(client, key, emp):
    """Мусор приходит тому, кому его прислали, — ждать администратора незачем.
    Но и стирать за человека нельзя: документ бывает единственным экземпляром."""
    _send(client, key, name="лишнее.txt")
    assert client.post("/api/waiting/лишнее.txt/drop", headers=emp).status_code == 200
    assert client.get("/api/waiting", headers=emp).json()["items"] == []

    from app import inbox
    assert (inbox.path() / "Убранное" / "лишнее.txt").is_file()


def test_присланное_можно_скачать_не_раскладывая(client, key, emp):
    _send(client, key, name="Документ.txt", text="Справка о развитии округа")
    r = client.get("/api/waiting/Документ.txt/download", headers=emp)
    assert r.status_code == 200 and r.text == "Справка о развитии округа"


def test_лента_не_открывается_по_ключу(client, key):
    """Ключ только кладёт документы — читать им нельзя.

    И это же держит адреса на местах: на боевом nginx пускает мимо проверки входа
    всё, что под «/materials/api/inbox». Поэтому лента, перенос и «присылать сюда»
    живут рядом с этим префиксом, а не под ним, — им проверка входа нужна.
    """
    assert client.get("/api/waiting", headers=key).status_code == 401
    assert client.post("/api/active", headers=key, json={"event": ""}).status_code == 401


def test_активное_мероприятие_живёт_сутки(client, key, emp, event):
    """Иначе документ через неделю улетит в позапрошлое совещание, и никто не заметит."""
    import time
    from app import store
    client.post("/api/active", headers=emp, json={"event": event["id"]})
    assert client.get("/api/waiting", headers=emp).json()["active"]["hours"] == 24

    store.set_setting("emp", "active_event", f"{event['id']}|{time.time() - 25 * 3600}")
    assert _send(client, key).json()["where"] == "Приёмная"
    assert client.get("/api/waiting", headers=emp).json()["active"] is None


def test_точная_копия_не_плодится(client, key, emp, event):
    client.post("/api/active", headers=emp, json={"event": event["id"]})
    _send(client, key)
    again = _send(client, key).json()
    assert again["saved"] == [] and again["skipped"] == ["05. Справка.txt"]
    assert again["message"] == "Это уже есть: Справка"

    card = client.get(f"/api/events/{event['id']}", headers=emp).json()
    assert len([f for f in card["files"] if f["number"] != "00"]) == 1


def test_подсказка_куда_похоже_и_на_что_похоже(client, key, emp, event):
    eid = event["id"]
    client.post(f"/api/events/{eid}/files", headers=emp,
                files=[("files", ("п.txt", "Позиция Минстроя".encode(), "text/plain"))])
    _send(client, key, text="Позиция Минстроя России", name="письмо.txt")

    waiting = client.get("/api/waiting", headers=emp).json()["items"][0]
    assert waiting["suggest"]["id"] == eid
    assert waiting["same_as"]["name"] == "05. Позиция Минстроя.txt"
    assert waiting["same_as"]["why"] == "похоже по названию"
    assert waiting["same_as"]["copy"] is False


def test_заменить_сохраняет_номер_и_название(client, key, emp, event):
    eid = event["id"]
    client.post(f"/api/events/{eid}/files", headers=emp,
                files=[("files", ("п.txt", "Позиция Минстроя".encode(), "text/plain"))])
    _send(client, key, text="Позиция Минстроя, уточнённая", name="письмо.txt")

    r = client.post("/api/waiting/письмо.txt/move", headers=emp,
                    json={"event": eid, "how": "replace", "target": "05. Позиция Минстроя.txt"})
    assert r.json()["name"] == "05. Позиция Минстроя.txt"

    card = client.get(f"/api/events/{eid}", headers=emp).json()
    assert [f["name"] for f in card["files"]] == [
        "00. Реестр материалов.docx", "05. Позиция Минстроя.txt"]
    # прежняя версия цела и подписана датой
    from datetime import date
    stamp = date.today().strftime("%d.%m.%Y")
    assert f"05. Позиция Минстроя (версия от {stamp}).txt" in [a["name"] for a in card["archive"]]

    got = client.get(f"/api/events/{eid}/files/05. Позиция Минстроя.txt/download", headers=emp)
    assert got.text == "Позиция Минстроя, уточнённая"


def test_дополнить_даёт_номер_приложения(client, key, emp, event):
    eid = event["id"]
    client.post(f"/api/events/{eid}/files", headers=emp,
                files=[("files", ("п.txt", "Позиция Минстроя".encode(), "text/plain"))])
    _send(client, key, text="Расчёт потребности", name="расчёт.txt")

    r = client.post("/api/waiting/расчёт.txt/move", headers=emp,
                    json={"event": eid, "how": "append", "target": "05. Позиция Минстроя.txt"})
    assert r.json()["name"] == "05.1. расчёт.txt"
    card = client.get(f"/api/events/{eid}", headers=emp).json()
    assert [f["name"] for f in card["files"]] == [
        "00. Реестр материалов.docx", "05. Позиция Минстроя.txt", "05.1. расчёт.txt"]


def test_копия_не_проходит_и_через_выбор_мероприятия(client, key, emp, event):
    eid = event["id"]
    _send(client, key, name="справка.txt")
    client.post("/api/waiting/справка.txt/move", headers=emp, json={"event": eid})
    _send(client, key, name="справка.txt")
    r = client.post("/api/waiting/справка.txt/move", headers=emp, json={"event": eid}).json()
    assert r["how"] == "same" and r["message"] == "Это уже есть: справка"

    card = client.get(f"/api/events/{eid}", headers=emp).json()
    assert len([f for f in card["files"] if f["number"] != "00"]) == 1
    assert client.get("/api/waiting", headers=emp).json()["items"] == []


def test_двое_прислали_одно_и_то_же(client, key, emp):
    """Документ часто шлют сразу двое — в приёмной он должен лежать один раз."""
    first = _send(client, key, name="Справка.txt").json()
    assert first["saved"] == ["Справка.txt"]

    again = _send(client, key, name="Справка_от_коллеги.txt").json()
    assert again["saved"] == [] and again["skipped"] == ["Справка.txt"]
    assert again["message"] == "Это уже есть: Справка о развитии округа"
    assert len(client.get("/api/waiting", headers=emp).json()["items"]) == 1


def test_обновлённая_версия_с_телефона_ждёт_решения(client, key, emp, event):
    """Молча класть нельзя: это или новая редакция, или дополнение, или другой документ."""
    eid = event["id"]
    client.post(f"/api/events/{eid}/files", headers=emp,
                files=[("files", ("п.txt", "Позиция Минстроя".encode(), "text/plain"))])
    client.post("/api/active", headers=emp, json={"event": eid})

    r = _send(client, key, text="Позиция Минстроя, уточнённая", name="позиция_v2.txt").json()
    assert r["saved"] == [] and r["waiting"] == ["позиция_v2.txt"]
    assert r["where"] == "Приёмная"
    assert r["message"] == ("Принято: позиция_v2 — "
                            "похоже на «05. Позиция Минстроя». Решите в портале")

    # в папку ничего не легло, документ ждёт в ленте с подсказкой на то же мероприятие
    card = client.get(f"/api/events/{eid}", headers=emp).json()
    assert len([f for f in card["files"] if f["number"] != "00"]) == 1
    item = client.get("/api/waiting", headers=emp).json()["items"][0]
    assert item["suggest"]["id"] == eid and item["suggest"]["why"] == "прислано в это мероприятие"
    assert item["same_as"]["name"] == "05. Позиция Минстроя.txt"

    # человек решает в портале — и документ встаёт новой редакцией
    r = client.post("/api/waiting/позиция_v2.txt/move", headers=emp, json={
        "event": eid, "how": "replace", "target": "05. Позиция Минстроя.txt"}).json()
    assert r["name"] == "05. Позиция Минстроя.txt"
    assert client.get("/api/waiting", headers=emp).json()["items"] == []


def test_непохожий_документ_с_телефона_ложится_сразу(client, key, emp, event):
    """Осторожность не должна мешать обычной работе."""
    eid = event["id"]
    client.post(f"/api/events/{eid}/files", headers=emp,
                files=[("files", ("п.txt", "Позиция Минстроя".encode(), "text/plain"))])
    client.post("/api/active", headers=emp, json={"event": eid})
    r = _send(client, key, text="Смета на капитальный ремонт кровли", name="смета.txt").json()
    assert r["saved"] == ["06. смета.txt"]
    assert r["where"] == "02.09.2026 Встреча с Музаевым"


def test_негодный_ключ_объясняется_на_телефоне(client, key):
    """Телефон показывает уведомлением поле message — без него окно будет пустым."""
    r = _send(client, {"X-Inbox-Key": "no-such-key"})
    assert r.status_code == 401
    assert r.json()["message"].startswith("Ключ приёмной не подошёл")


def test_пустой_запрос_объясняется_на_телефоне(client, key):
    """Без message уведомление на телефоне выходит пустым — и человек в тупике."""
    # поле files приехало текстом — ровно то, что бывает при неверном типе поля
    r = client.post("/api/inbox", headers=key, data={"files": "Справка.docx"})
    assert r.status_code == 400
    assert "текстом, а не файлом" in r.json()["message"]

    # поля files нет вовсе, приехало что-то другое
    r = client.post("/api/inbox", headers=key, data={"mode": "add"},
                    files=[("прочее", ("x.txt", b"x", "text/plain"))])
    assert r.status_code == 400 and "Поля files нет" in r.json()["message"]


def test_документ_принимается_телом_запроса(client, key, emp):
    """«Быстрые команды» на iPhone не у всех дают собрать файловое поле формы:
    телефон требует у файла текстовое свойство и шлёт строку вместо документа.
    Поэтому принимаем документ и целиком телом запроса — имя приходит заголовком."""
    r = client.post("/api/inbox",
                    headers={**key, "X-Inbox-Name": "%D0%A1%D0%BF%D1%80%D0%B0%D0%B2%D0%BA%D0%B0_%D0%AF%D0%9D%D0%90%D0%9E.txt",
                             "Content-Type": "application/octet-stream"},
                    content="Справка о развитии округа".encode())
    assert r.status_code == 200
    assert r.json()["saved"] == ["Справка_ЯНАО.txt"]
    assert r.json()["message"].startswith("Принято: Справка_ЯНАО")

    item = client.get("/api/waiting", headers=emp).json()["items"][0]
    assert item["name"] == "Справка_ЯНАО.txt" and item["title"] == "Справка_ЯНАО"


def test_имя_берётся_из_content_disposition(client, key, emp):
    r = client.post("/api/inbox",
                    headers={**key, "Content-Type": "application/octet-stream",
                             "Content-Disposition":
                                 'attachment; filename="%D0%9F%D0%BE%D0%B2%D0%B5%D1%81%D1%82%D0%BA%D0%B0.txt"'},
                    content="Повестка совещания".encode())
    assert r.status_code == 200 and r.json()["saved"] == ["Повестка.txt"]


def test_расширение_узнаётся_по_содержимому(client, key, emp):
    """Имя доехало без расширения — иначе документ остался бы нечитаемым."""
    import io
    from docx import Document
    doc = Document()
    doc.add_paragraph("Справка о развитии округа")
    buffer = io.BytesIO()
    doc.save(buffer)

    r = client.post("/api/inbox", headers={**key, "Content-Type": "application/octet-stream"},
                    content=buffer.getvalue())
    assert r.json()["saved"] == ["Документ.docx"]
    item = client.get("/api/waiting", headers=emp).json()["items"][0]
    assert item["title"] == "Документ"     # как доехало; назвать можно прямо в ленте


def test_название_правится_прямо_в_ленте(client, key, emp):
    _send(client, key, name="Документ")
    r = client.post("/api/waiting/Документ.txt/title", headers=emp,
                    json={"title": "Справка о развитии Ямало-Ненецкого округа"})
    assert r.json()["name"] == "Справка о развитии Ямало-Ненецкого округа.txt"
    items = client.get("/api/waiting", headers=emp).json()["items"]
    assert items[0]["title"] == "Справка о развитии Ямало-Ненецкого округа"
    assert items[0]["when"] > 0


# ── маршрут из приёмной: команда, вид, заявка на отправку (задача 27.09) ────

LONG = "Справка о состоянии дел в отрасли жилищного строительства региона. " * 6


def _item(client, emp, name):
    items = client.get("/api/waiting", headers=emp).json()["items"]
    return next(i for i in items if i["name"] == name)


def test_команда_рядом_с_документом_приходит_полем_формы(client, key, emp):
    r = _send(client, key, text=LONG, name="справка.txt", note="напиши бриф").json()
    assert r["where"] == "Приёмная"
    assert r["message"].endswith("команда: «напиши бриф»")
    item = _item(client, emp, "справка.txt")
    assert item["note"] == "напиши бриф" and item["sender"] == "emp"
    assert item["route"] == {"to": "brief", "ask": [], "why": "по команде"}


def test_команда_приходит_и_заголовком(client, key, emp):
    from urllib.parse import quote
    r = client.post("/api/inbox", content=LONG.encode(),
                    headers={**key, "Content-Type": "text/plain",
                             "X-Inbox-Name": quote("справка.txt"),
                             "X-Inbox-Note": quote("сделай одностраничник")})
    assert r.status_code == 200
    assert _item(client, emp, "справка.txt")["route"]["to"] == "a3"


def test_с_командой_документ_ждёт_в_приёмной_даже_при_активном_мероприятии(client, key, emp, event):
    client.post("/api/active", headers=emp, json={"event": event["id"]})
    r = _send(client, key, text=LONG, name="справка.txt", note="напиши бриф").json()
    assert r["where"] == "Приёмная"
    card = client.get(f"/api/events/{event['id']}", headers=emp).json()
    assert all("справка" not in f["name"] for f in card["files"])
    assert _item(client, emp, "справка.txt")["route"]["to"] == "brief"


def test_команда_главнее_вида_документа(client, key, emp):
    _send(client, key, text="ПОВЕСТКА\nсовещания", name="п.txt", note="напиши бриф")
    item = _item(client, emp, "п.txt")
    assert item["looks_like"] == "повестка" and item["route"]["to"] == "brief"


def test_повестка_без_команды_уходит_в_повестку_сама(client, key, emp):
    _send(client, key, text="ПОВЕСТКА\n" + "1. О ходе реализации программы. " * 3, name="п.txt")
    assert _item(client, emp, "п.txt")["route"] == {"to": "agenda", "ask": [], "why": "это повестку"}


def test_спорные_приметы_не_решают_за_человека(client, key, emp):
    _send(client, key, text="ПОВЕСТКА\nсовещания", name="Бриф_ЯНАО.txt")
    route = _item(client, emp, "Бриф_ЯНАО.txt")["route"]
    assert route["to"] == "" and route["ask"][0] == "agenda" and "keep" in route["ask"]


def test_справка_предлагает_выбор_короткое_нет(client, key, emp):
    _send(client, key, text=LONG, name="справка.txt")
    _send(client, key, text="коротко", name="записка.txt")
    assert _item(client, emp, "справка.txt")["route"]["ask"] == ["brief", "a3", "keep"]
    assert _item(client, emp, "записка.txt")["route"]["ask"] == []


def test_заявка_на_отправку_одна_второе_нажатие_409(client, key, emp):
    _send(client, key, text=LONG, name="справка.txt")
    first = client.post("/api/waiting/справка.txt/route", headers=emp, json={"to": "brief"})
    assert first.status_code == 200 and first.json()["sent"]["state"] == "sending"
    again = client.post("/api/waiting/справка.txt/route", headers=emp, json={"to": "brief"})
    assert again.status_code == 409
    # не вышло — сняли заявку, можно снова
    client.post("/api/waiting/справка.txt/route", headers=emp, json={"state": "release"})
    assert client.post("/api/waiting/справка.txt/route", headers=emp,
                       json={"to": "a3"}).status_code == 200
    done = client.post("/api/waiting/справка.txt/route", headers=emp,
                       json={"state": "done", "url": "/a3/editor?id=7"}).json()["sent"]
    assert done["state"] == "done" and done["url"] == "/a3/editor?id=7" and done["by"] == "emp"
    assert _item(client, emp, "справка.txt")["sent"]["to"] == "a3"
    # готовое повторной заявкой не сбивается
    assert client.post("/api/waiting/справка.txt/route", headers=emp,
                       json={"to": "brief"}).status_code == 409


def test_просто_сохранить_сразу_решено_а_чужой_адрес_отклоняется(client, key, emp):
    _send(client, key, text=LONG, name="справка.txt")
    assert client.post("/api/waiting/справка.txt/route", headers=emp,
                       json={"to": "telegram"}).status_code == 400
    kept = client.post("/api/waiting/справка.txt/route", headers=emp, json={"to": "keep"}).json()
    assert kept["sent"]["state"] == "done" and kept["sent"]["to"] == "keep"


def test_пометки_едут_за_именем_и_забываются_с_документом(client, key, emp):
    _send(client, key, text=LONG, name="справка.txt", note="напиши бриф")
    client.post("/api/waiting/справка.txt/route", headers=emp, json={"to": "keep"})
    client.post("/api/waiting/справка.txt/title", headers=emp, json={"title": "Справка о стройке"})
    moved = _item(client, emp, "Справка о стройке.txt")
    assert moved["note"] == "напиши бриф" and moved["sent"]["to"] == "keep"
    client.post("/api/waiting/Справка о стройке.txt/drop", headers=emp)
    _send(client, key, text=LONG, name="Справка о стройке.txt")
    fresh = _item(client, emp, "Справка о стройке.txt")
    assert fresh["note"] == "" and fresh["sent"] is None


def test_брошенная_заявка_не_держит_документ_вечно(client, key, emp, monkeypatch):
    """Вкладку закрыли посреди отправки: через четверть часа документ снова свободен."""
    from app import inbox
    _send(client, key, text=LONG, name="справка.txt")
    assert client.post("/api/waiting/справка.txt/route", headers=emp, json={"to": "brief"}).status_code == 200
    assert client.post("/api/waiting/справка.txt/route", headers=emp, json={"to": "a3"}).status_code == 409
    real = inbox.time.time
    monkeypatch.setattr(inbox.time, "time", lambda: real() + inbox.STALE + 1)
    assert client.post("/api/waiting/справка.txt/route", headers=emp, json={"to": "a3"}).status_code == 200


def test_подсказка_знает_совещание_папки(client, key, emp, event):
    """«Повестка» примет повестку только к совещанию — приёмная подсказывает и его."""
    client.post("/api/events/link", headers=emp,
                json={"meeting": "m-77", "date": "2026-09-02", "title": "Встреча с Музаевым"})
    _send(client, key, event=event["id"], text="ПОВЕСТКА\nсовещания", name="п.txt", note="это в повестку")
    item = _item(client, emp, "п.txt")
    assert item["suggest"]["id"] == event["id"] and item["suggest"]["meeting"] == "m-77"
    assert item["route"]["to"] == "agenda"


def test_скан_повестки_без_текста_сам_не_уходит(client, key, emp):
    """Вид узнан по имени файла, а текста нет — «Искре» разбирать нечего, решает человек."""
    _send(client, key, text="·", name="Повестка_скан.txt")
    route = _item(client, emp, "Повестка_скан.txt")["route"]
    assert route["to"] == "" and route["ask"] == ["agenda", "keep"] and "скан" in route["why"]


def test_команда_в_заголовке_не_теряется_при_форме(client, key, emp):
    from urllib.parse import quote
    _send(client, {**key, "X-Inbox-Note": quote("напиши бриф")}, text=LONG, name="справка.txt")
    assert _item(client, emp, "справка.txt")["route"]["to"] == "brief"


def test_каждый_шаг_отправки_в_журнале_и_неудача_тоже(client, key, emp, monkeypatch):
    from app import portal
    seen = []
    monkeypatch.setattr(portal, "send_audit", lambda **e: seen.append(e))
    _send(client, key, text=LONG, name="справка.txt")
    seen.clear()
    route = "/api/waiting/справка.txt/route"
    client.post(route, headers=emp, json={"to": "brief"})
    client.post(route, headers=emp, json={"to": "brief"})                 # 409 — в журнал не идёт
    client.post(route, headers=emp, json={"state": "release", "why": "«Бриф» не отвечает"})
    client.post(route, headers=emp, json={"to": "a3"})
    client.post(route, headers=emp, json={"state": "done", "url": "/a3/"})
    assert [e["action"] for e in seen] == ["Документ отправляется в «Бриф»", "Отправка в «Бриф» не удалась",
                                           "Документ отправляется в «A3»", "Документ отправлен в «A3»"]
    assert all(e["login"] == "emp" and e["object"] == "Приёмная" for e in seen)
    assert "не отвечает" in seen[1]["details"] and "/a3/" in seen[3]["details"]


def test_чужую_заявку_не_снять(client, key, emp):
    _send(client, key, text=LONG, name="справка.txt")
    client.post("/api/waiting/справка.txt/route", headers=emp, json={"to": "brief"})
    other = {**emp, "X-Portal-User": "boss"}
    r = client.post("/api/waiting/справка.txt/route", headers=other, json={"state": "release"})
    assert r.status_code == 403
    assert client.post("/api/waiting/справка.txt/route", headers=emp,
                       json={"state": "release"}).status_code == 200


def test_две_вкладки_в_один_миг_отправляют_один_раз(client, key, emp):
    """Заявка — одной транзакцией: из восьми одновременных нажатий проходит одно."""
    from concurrent.futures import ThreadPoolExecutor
    _send(client, key, text=LONG, name="справка.txt")
    go = lambda _: client.post("/api/waiting/справка.txt/route", headers=emp, json={"to": "a3"}).status_code
    with ThreadPoolExecutor(8) as pool:
        codes = list(pool.map(go, range(8)))
    assert codes.count(200) == 1 and codes.count(409) == 7
