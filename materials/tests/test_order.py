"""Порядок документов: номер — это место в пакете, и его задаёт человек."""
import pytest


@pytest.fixture()
def key(client, monkeypatch):
    from app import portal
    monkeypatch.setattr(portal, "user_by_inbox_key", lambda k: {
        "login": "emp", "role": "employee", "is_admin": False, "is_chief": False,
        "name": "Сотрудник", "team": []} if k == "good-key" else None)
    return {"X-Inbox-Key": "good-key"}


def _upload(client, emp, eid, *files) -> dict:
    payload = [("files", (name, text.encode(), "text/plain")) for name, text in files]
    return client.post(f"/api/events/{eid}/files", headers=emp, files=payload).json()


def _names(card) -> list[str]:
    return [f["name"] for f in card["files"]]


def _question(client, emp, eid, card, title, number):
    name = next(f["name"] for f in card["files"] if f["title"] == title)
    return client.post(f"/api/events/{eid}/files/{name}/title", headers=emp,
                       json={"title": title, "question": number}).json()


def test_перестановка_меняет_номера(client, emp, event):
    eid = event["id"]
    card = _upload(client, emp, eid, ("а.txt", "Сводка по школам"),
                   ("б.txt", "Позиция Минстроя"), ("в.txt", "Справка Рособрнадзора"))
    assert _names(card) == ["00. Реестр материалов.docx", "05. Сводка по школам.txt",
                            "06. Позиция Минстроя.txt", "07. Справка Рособрнадзора.txt"]

    r = client.post(f"/api/events/{eid}/order", headers=emp, json={"names": [
        "07. Справка Рособрнадзора.txt", "05. Сводка по школам.txt", "06. Позиция Минстроя.txt"]})
    assert _names(r.json()) == ["00. Реестр материалов.docx",
                                "05. Справка Рособрнадзора.txt",
                                "06. Сводка по школам.txt",
                                "07. Позиция Минстроя.txt"]


def test_предпросмотр_показывает_план_и_не_трогает_диск(client, emp, event):
    eid = event["id"]
    _upload(client, emp, eid, ("а.txt", "Сводка по школам"), ("б.txt", "Позиция Минстроя"))
    moves = client.post(f"/api/events/{eid}/order/preview", headers=emp, json={"names": [
        "06. Позиция Минстроя.txt", "05. Сводка по школам.txt"]}).json()["moves"]
    assert {m["src"]: m["dst"] for m in moves} == {
        "06. Позиция Минстроя.txt": "05. Позиция Минстроя.txt",
        "05. Сводка по школам.txt": "06. Сводка по школам.txt"}
    card = client.get(f"/api/events/{eid}", headers=emp).json()
    assert "05. Сводка по школам.txt" in _names(card)


def test_повестка_и_бриф_всегда_впереди_материалов(client, emp, event):
    eid = event["id"]
    card = _upload(client, emp, eid, ("с.txt", "Сводка по школам"),
                   ("б.txt", "Бриф по ЯНАО"), ("п.txt", "Повестка совещания"))
    assert _names(card)[1:] == ["01. Повестка.txt", "04. Бриф по ЯНАО.txt",
                                "05. Сводка по школам.txt"]
    # перетащить сводку в голову пакета нельзя: старшинство сильнее
    r = client.post(f"/api/events/{eid}/order", headers=emp, json={"names": [
        "05. Сводка по школам.txt", "01. Повестка.txt", "04. Бриф по ЯНАО.txt"]})
    assert _names(r.json())[1:] == ["01. Повестка.txt", "04. Бриф по ЯНАО.txt",
                                    "05. Сводка по школам.txt"]


def test_материал_к_первому_вопросу_идёт_раньше_второго(client, emp, event):
    eid = event["id"]
    card = _upload(client, emp, eid, ("а.txt", "Позиция Минстроя"),
                   ("б.txt", "Справка Рособрнадзора"))
    card = _question(client, emp, eid, card, "Позиция Минстроя", 2)
    card = _question(client, emp, eid, card, "Справка Рособрнадзора", 1)
    assert _names(card)[1:] == ["05. Справка Рособрнадзора (к вопросу 1).txt",
                                "06. Позиция Минстроя (к вопросу 2).txt"]


def test_приложение_идёт_следом_и_наследует_вопрос(client, emp, event):
    eid = event["id"]
    card = _upload(client, emp, eid, ("а.txt", "Позиция Минстроя"), ("б.txt", "Сводка по школам"))
    card = _question(client, emp, eid, card, "Позиция Минстроя", 1)

    # общий материал встал впереди материала к вопросу — приложение кладём к нужному
    head = next(f for f in card["files"] if f["title"] == "Позиция Минстроя")
    from app import tree
    folder = tree.root() / card["event"]["folder"]
    (folder / f"{head['num']:02d}.1. Расчёт потребности.txt").write_text("Расчёт", encoding="utf-8")

    r = client.post(f"/api/events/{eid}/order", headers=emp, json={"names": []})
    assert _names(r.json())[1:] == [
        "05. Сводка по школам.txt",
        "06. Позиция Минстроя (к вопросу 1).txt",
        "06.1. Расчёт потребности (к вопросу 1).txt",
    ]


def test_название_документа_правится_а_номер_ставит_сервис(client, emp, event):
    eid = event["id"]
    _upload(client, emp, eid, ("а.txt", "Сводка по школам"))
    r = client.post(f"/api/events/{eid}/files/05. Сводка по школам.txt/title", headers=emp,
                    json={"title": "Сводка по школам региона", "question": 2})
    assert "05. Сводка по школам региона (к вопросу 2).txt" in _names(r.json())


def test_документ_становится_приложением_и_обратно(client, emp, event):
    eid = event["id"]
    _upload(client, emp, eid, ("а.txt", "Позиция Минстроя"), ("б.txt", "Расчёт потребности"))
    r = client.post(f"/api/events/{eid}/files/06. Расчёт потребности.txt/attach", headers=emp)
    assert _names(r.json())[1:] == ["05. Позиция Минстроя.txt", "05.1. Расчёт потребности.txt"]

    r = client.post(f"/api/events/{eid}/files/05.1. Расчёт потребности.txt/detach", headers=emp)
    assert _names(r.json())[1:] == ["05. Позиция Минстроя.txt", "06. Расчёт потребности.txt"]


def test_первый_документ_приложением_не_станет(client, emp, event):
    eid = event["id"]
    _upload(client, emp, eid, ("а.txt", "Позиция Минстроя"))
    r = client.post(f"/api/events/{eid}/files/05. Позиция Минстроя.txt/attach", headers=emp)
    assert r.status_code == 400


def test_можно_попросить_предложить_название_заново(client, emp, event):
    """Правила именования меняются, а имена на диске — нет."""
    eid = event["id"]
    _upload(client, emp, eid, ("Бриф_ЯНАО.txt", "О системе образования округа"))
    client.post(f"/api/events/{eid}/files/04. Бриф о системе образования округа.txt/title",
                headers=emp, json={"title": "Кое-как названный документ"})
    r = client.get(f"/api/events/{eid}/files/04. Кое-как названный документ.txt/proposal",
                   headers=emp)
    assert r.json()["title"] == "Бриф о системе образования округа"


def test_кнопки_работают_у_имени_с_процентами(client, emp, event):
    """«Макет экрана %22Стоимость обучения%22.pdf»: процентная запись в имени
    раскрывалась дважды, и файл переставал находиться — не работало ничего."""
    eid = event["id"]
    _upload(client, emp, eid, ("09. Макет экрана %22Стоимость обучения%22.txt", "Схема"))
    name = "05. Макет экрана «Стоимость обучения».txt"
    assert _names(client.get(f"/api/events/{eid}", headers=emp).json())[1] == name

    from urllib.parse import quote
    safe = quote(name)
    assert client.get(f"/api/events/{eid}/files/{safe}/download", headers=emp).status_code == 200
    assert client.get(f"/api/events/{eid}/files/{safe}/proposal", headers=emp).status_code == 200
    r = client.post(f"/api/events/{eid}/files/{safe}/title", headers=emp,
                    json={"title": "Макет экрана «Стоимость обучения в вузах»"})
    assert r.status_code == 200
    assert _names(r.json())[1] == "05. Макет экрана «Стоимость обучения в вузах».txt"


def test_сохранение_названия_без_правок_ничего_не_меняет(client, emp, event):
    """Самое частое действие: открыл, посмотрел, нажал «Сохранить» — и получал «(2)»."""
    eid = event["id"]
    _upload(client, emp, eid, ("а.txt", "Сводка по школам"))
    name = "05. Сводка по школам.txt"
    for _ in range(3):
        r = client.post(f"/api/events/{eid}/files/{name}/title", headers=emp,
                        json={"title": "Сводка по школам"})
        assert _names(r.json()) == ["00. Реестр материалов.docx", name]


def test_остаток_перекладки_не_считается_документом(client, emp, event):
    """Файл держал Word, переименование сорвалось — остаток не должен лезть в пакет."""
    eid = event["id"]
    _upload(client, emp, eid, ("а.txt", "Сводка по школам"))
    from app import tree
    folder = tree.root() / event["folder"]
    (folder / "~перекладка~05. Сводка по школам.txt").write_text("хвост", encoding="utf-8")

    card = client.post(f"/api/events/{eid}/registry", headers=emp).json()
    assert _names(card) == ["00. Реестр материалов.docx", "05. Сводка по школам.txt"]


def test_обмен_одинаковыми_названиями_не_роняет_запрос(client, emp, event):
    """Два письма с одной темой — обычное дело, а кэш падал на занятом ключе."""
    eid = event["id"]
    _upload(client, emp, eid, ("а.txt", "Сводка по школам"), ("б.txt", "Сводка по школам"))
    names = _names(client.get(f"/api/events/{eid}", headers=emp).json())[1:]
    r = client.post(f"/api/events/{eid}/order", headers=emp,
                    json={"names": [names[1], names[0]]})
    assert r.status_code == 200, r.text


def test_чужая_папка_как_цель_решения_отвергается(client, emp, event, key):
    eid = event["id"]
    _upload(client, emp, eid, ("а.txt", "Сводка по школам"))
    client.post("/api/inbox", headers=key,
                files=[("files", ("новая.txt", "Уточнение".encode(), "text/plain"))])
    r = client.post("/api/waiting/новая.txt/move", headers=emp, json={
        "event": eid, "how": "replace", "target": "../соседняя/05. Чужой.txt"})
    assert r.status_code == 404
