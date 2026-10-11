"""Совещание — общая ось: у совещания ровно одна папка (контракт `../CLAUDE.md`, п. 9)."""


def _link(client, emp, meeting="m-1", date="07.10.2026", title="Совещание по детям-сиротам"):
    return client.post("/api/events/link", headers=emp,
                       json={"meeting": meeting, "date": date, "title": title})


def test_папка_заводится_сразу_и_привязывается(client, emp):
    card = _link(client, emp).json()
    assert card["event"]["folder"] == "2026 год/10. Октябрь/07.10.2026 Совещание по детям-сиротам"
    assert card["event"]["meeting"] == "m-1"
    # реестр есть с первого дня, даже у пустой папки
    assert [f["name"] for f in card["files"]] == ["00. Реестр материалов.docx"]


def test_вызов_идемпотентен(client, emp):
    """Два сервиса нажали кнопку подряд — совещание получает одну папку, а не две."""
    first = _link(client, emp).json()["event"]["id"]
    second = _link(client, emp).json()["event"]["id"]
    assert first == second
    assert len(client.get("/api/events", headers=emp).json()["items"]) == 1


def test_совещание_переименовали_папка_переехала(client, emp):
    card = _link(client, emp).json()
    client.post(f"/api/events/{card['event']['id']}/files", headers=emp,
                files=[("files", ("сводка.txt", "Сводка по школам".encode(), "text/plain"))])

    moved = _link(client, emp, date="14.10.2026", title="Совещание по сиротам").json()
    assert moved["event"]["folder"] == "2026 год/10. Октябрь/14.10.2026 Совещание по сиротам"
    assert moved["event"]["meeting"] == "m-1"
    assert "05. Сводка по школам.txt" in [f["name"] for f in moved["files"]]
    assert len(client.get("/api/events", headers=emp).json()["items"]) == 1


def test_готовая_папка_подхватывается_а_не_дублируется(client, emp):
    """Папку завели руками до совещания — привязываем её, а не заводим вторую."""
    made = client.post("/api/events", headers=emp,
                       json={"date": "2026-10-07", "title": "Совещание по детям-сиротам"}).json()
    card = _link(client, emp).json()
    assert card["event"]["id"] == made["event"]["id"]
    assert len(client.get("/api/events", headers=emp).json()["items"]) == 1


def test_у_папки_не_больше_одного_совещания(client, emp):
    _link(client, emp, meeting="m-1")
    r = _link(client, emp, meeting="m-2")
    assert r.status_code == 409


def test_папку_убрали_совещание_осталось_без_папки(client, emp):
    card = _link(client, emp).json()
    client.post(f"/api/events/{card['event']['id']}/drop", headers=emp)
    # зовём снова — заводится новая папка, а не воскресает убранная
    again = _link(client, emp).json()
    assert again["event"]["meeting"] == "m-1"
    assert again["event"]["folder"].endswith("07.10.2026 Совещание по детям-сиротам")


def test_без_ключа_совещания_и_без_даты_не_привязываем(client, emp):
    assert client.post("/api/events/link", headers=emp,
                       json={"date": "07.10.2026", "title": "Совещание"}).status_code == 400
    assert client.post("/api/events/link", headers=emp,
                       json={"meeting": "m-9", "title": "Совещание"}).status_code == 400
    assert client.post("/api/events/link", headers=emp,
                       json={"meeting": "m-9", "date": "07.10.2026"}).status_code == 400


def test_в_ответе_есть_плоский_id(client, emp):
    """«Модератор» читает `event.id`, но годится и плоский — так он и просил."""
    answer = _link(client, emp).json()
    assert answer["id"] == answer["event"]["id"]


def test_список_папок_говорит_чьё_совещание(client, emp):
    """Интерфейс по этому полю понимает, у какого совещания папка уже есть."""
    _link(client, emp, meeting="m-7")
    client.post("/api/events", headers=emp, json={"date": "2026-10-08", "title": "Склад"})
    rows = {e["title"]: e["meeting"] for e in client.get("/api/events", headers=emp).json()["items"]}
    assert rows["Совещание по детям-сиротам"] == "m-7"
    assert rows["Склад"] == ""


def test_дата_принимается_в_обоих_видах(client, emp):
    """«Модератор» шлёт ГГГГ-ММ-ДД, человек из браузера — тоже; карточка пишет ДД.ММ.ГГГГ."""
    card = _link(client, emp, meeting="m-8", date="2026-10-07").json()
    assert card["event"]["date"] == "07.10.2026"


def test_длинное_название_совещания_не_ломает_запись(client, emp, monkeypatch):
    """На боевом Windows не пишет путь длиннее 260 знаков, а название совещания
    попадает в путь дважды: в папку и в имя «Саммари_<название>.docx». При названии
    в 87 знаков «Бриф» получал 500. Предел здесь урезан, чтобы воспроизвести то же на Mac."""
    from app import tree
    title = ("Совещание по вопросам развития системы профессионального образования "
             "в субъектах России")
    card = _link(client, emp, meeting="m-long", title=title).json()
    folder = tree.root() / card["event"]["folder"]
    monkeypatch.setattr(tree, "MAX_PATH", len(str(folder)) + 40)

    r = client.post(f"/api/events/{card['event']['id']}/files", headers=emp,
                    files=[("files", (f"Саммари_{title}.txt", "Саммари докладов".encode(),
                                      "text/plain"))])
    assert r.status_code == 200, r.text
    for path in folder.rglob("*"):
        assert len(str(path)) <= tree.MAX_PATH, path.name


def test_сбой_объясняется_а_не_голое_500(client, emp, event, monkeypatch):
    """Соседний сервис показывает человеку наш ответ — в нём должна быть причина."""
    from app import main
    def broken(*_a, **_k):
        raise PermissionError(13, "Отказано в доступе")
    monkeypatch.setattr(main, "_save", broken)
    from fastapi.testclient import TestClient
    quiet = TestClient(main.app, raise_server_exceptions=False)
    r = quiet.post(f"/api/events/{event['id']}/files", headers=emp,
                   files=[("files", ("а.txt", "Сводка".encode(), "text/plain"))])
    assert r.status_code == 500
    assert "не удалось записать файл на диск" in r.json()["detail"]
