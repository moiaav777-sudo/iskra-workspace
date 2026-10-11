"""Встраивание карточки в «Совещание»: `GET /embed/{eid}` (решение пользователя 02.10).

Та же страница, что `/`, но с флагом `data-embed` и id папки на `<html>`, без шапки портала
и с `<base href="../">` — адреса в ней относительные, а живёт она на уровень глубже.
Права — те же, что у обычной страницы: без входа уводит в портал, API без входа — 401.
"""
from urllib.parse import urljoin

from conftest import headers


def test_embed_отдаёт_страницу_с_флагом(client, emp, event):
    eid = event["id"]
    r = client.get(f"/embed/{eid}", headers=emp)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    page = r.text
    assert f'data-embed="1" data-event="{eid}"' in page
    # шапку и объявления держит родитель — в рамке второй шапки быть не должно
    assert "workspace-header.js" not in page and "workspace.css" not in page
    # общая типографика и тема остаются
    assert 'href="/static/base.css"' in page and "static/app.js" in page
    # встраивать — только себе же
    assert r.headers["content-security-policy"] == "frame-ancestors 'self'"
    assert "deny" not in r.headers.get("x-frame-options", "").lower()


def test_embed_адреса_страницы_ведут_в_сервис(client, emp, event):
    """`<base>` стоит первым в <head>: иначе `static/…` и `api/…` ушли бы в /materials/embed/…"""
    page = client.get(f"/embed/{event['id']}", headers=emp).text
    head = page.split("<head>", 1)[1]
    assert head.lstrip().startswith('<base href="../">')
    here = f"https://work.example/materials/embed/{event['id']}"
    base = urljoin(here, "../")
    assert urljoin(base, "static/app.js?v=8") == "https://work.example/materials/static/app.js?v=8"
    assert urljoin(base, f"api/events/{event['id']}") == f"https://work.example/materials/api/events/{event['id']}"


def test_обычная_страница_без_флага(client, emp, event):
    page = client.get("/", headers=emp).text
    assert "data-embed" not in page and "<base" not in page
    assert "workspace-header.js" in page
    # а рамка — та же страница, только с флагом: тело и скрипт одни
    framed = client.get(f"/embed/{event['id']}", headers=emp).text
    assert framed.split("<body>", 1)[1] == page.split("<body>", 1)[1]


def test_embed_без_входа_как_обычная(client, emp, event):
    # со входом — открывается, без входа — ровно как обычная страница
    assert client.get(f"/embed/{event['id']}", headers=emp).status_code == 200
    plain = client.get("/", follow_redirects=False)
    framed = client.get(f"/embed/{event['id']}", follow_redirects=False)
    assert framed.status_code == plain.status_code == 303
    assert framed.headers["location"] == plain.headers["location"]
    bad = headers()
    bad["X-Portal-Secret"] = "wrong-secret"
    r = client.get(f"/embed/{event['id']}", headers=bad, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/login")
    # и карточка, которую страница читает, без входа не отдаётся
    assert client.get(f"/api/events/{event['id']}").status_code == 401


def test_embed_изменения_под_той_же_защитой(client, emp, event):
    """Рамка ничего не ослабляет: чужой сайт по-прежнему 403, стереть первичку — только админ."""
    eid = event["id"]
    assert client.get(f"/embed/{eid}", headers=emp).status_code == 200
    r = client.post(f"/api/events/{eid}/registry",
                    headers={**emp, "Origin": "https://evil.example"})
    assert r.status_code == 403
    client.post(f"/api/events/{eid}/files", headers=emp,
                files=[("files", ("Справка.txt", "Справка о школе".encode(), "text/plain"))])
    name = client.get(f"/api/events/{eid}", headers=emp).json()["archive"][0]["name"]
    assert client.delete(f"/api/events/{eid}/archive/{name}", headers=emp).status_code == 403


def test_embed_чужой_id_не_подставляется(client, emp, event):
    assert client.get(f"/embed/{event['id']}", headers=emp).status_code == 200
    for bad in ("nope", "ABCDEF012345", "abcdef0123456", '"><script>x', "abcdef01234%22"):
        assert client.get(f"/embed/{bad}", headers=emp).status_code == 404, bad


def test_embed_косая_черта_на_конце(client, emp, event):
    """`/materials/embed/<id>/` — относительной переадресацией на адрес без черты: Starlette
    увёл бы в `/embed/…` без префикса `/materials/`."""
    eid = event["id"]
    r = client.get(f"/embed/{eid}/", headers=emp, follow_redirects=False)
    assert r.status_code == 307 and r.headers["location"] == f"../{eid}"
    assert urljoin(f"https://work.example/materials/embed/{eid}/", r.headers["location"]) \
        == f"https://work.example/materials/embed/{eid}"


def test_embed_папки_уже_нет(client, emp):
    """Папку убрали — страница всё равно открывается и сама скажет об этом в карточке."""
    r = client.get("/embed/0123456789ab", headers=emp)
    assert r.status_code == 200 and 'data-event="0123456789ab"' in r.text
    assert client.get("/api/events/0123456789ab", headers=emp).status_code == 404
