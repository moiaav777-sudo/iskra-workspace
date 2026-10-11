"""Контракт с порталом: вход по заголовкам, защита от чужих сайтов, права."""
from conftest import headers


def test_healthz_без_входа(client):
    r = client.get("/healthz")
    assert r.status_code == 200 and r.text == "ok"


def test_без_секрета_api_не_отвечает(client):
    r = client.get("/api/events", headers={"X-Portal-User": "emp", "X-Portal-Role": "employee"})
    assert r.status_code == 401


def test_чужой_секрет_не_проходит(client):
    bad = headers()
    bad["X-Portal-Secret"] = "wrong-secret"
    assert client.get("/api/events", headers=bad).status_code == 401


def test_страница_без_входа_уводит_в_портал(client):
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/login")


def test_запрос_с_чужого_сайта_отклоняется(client, emp, event):
    r = client.post("/api/events", headers={**emp, "Origin": "https://evil.example"},
                    json={"date": "2026-09-03", "title": "Чужое"})
    assert r.status_code == 403


def test_me_отдаёт_пользователя(client, emp):
    body = client.get("/api/me", headers=emp).json()
    assert body["login"] == "emp" and body["is_admin"] is False


def test_стереть_первичку_может_только_админ(client, emp, admin, event):
    eid = event["id"]
    client.post(f"/api/events/{eid}/files", headers=emp,
                files=[("files", ("Справка.txt", "Справка о школе".encode(), "text/plain"))])
    card = client.delete(f"/api/events/{eid}/files/05. Справка о школе.txt", headers=emp).json()
    # из пакета документ ушёл, первичка осталась и её можно вернуть
    assert [f["number"] for f in card["files"]] == ["00"]
    assert [a["name"] for a in card["archive"]] == ["Справка.txt"]
    assert card["archive"][0]["in_package"] is False
    assert client.delete(f"/api/events/{eid}/archive/Справка.txt", headers=emp).status_code == 403
    assert client.delete(f"/api/events/{eid}/archive/Справка.txt", headers=admin).status_code == 200
