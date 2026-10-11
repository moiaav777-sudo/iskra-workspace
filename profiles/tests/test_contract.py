"""Контракт с порталом (`../../CLAUDE.md`): вход, секрет, Origin, /healthz, роли, журнал, поиск, адреса."""
from __future__ import annotations

import re
from pathlib import Path

from tests.conftest import headers, wait_done

ROOT = Path(__file__).resolve().parent.parent


def test_healthz_без_входа(client):
    r = client.get("/healthz")
    assert r.status_code == 200 and r.text == "ok"


def test_api_без_секрета_и_с_чужим_секретом_отклоняется(client):
    assert client.get("/api/profiles").status_code == 401
    assert client.get("/api/profiles", headers={"X-Portal-User": "emp"}).status_code == 401
    bad = headers()
    bad["X-Portal-Secret"] = "wrong"
    assert client.get("/api/profiles", headers=bad).status_code == 401


def test_страница_без_входа_ведёт_на_портал(client):
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login?next=/profiles/"


def test_страница_с_входом_отдаётся(client, emp):
    r = client.get("/", headers=emp)
    assert r.status_code == 200 and "Объективки" in r.text


def test_запрос_с_чужого_сайта_отклоняется(client, emp):
    r = client.post("/api/profiles", json={"fio": "Кравцов Сергей Сергеевич"},
                    headers=dict(emp, Origin="https://zloy.example", Host="testserver"))
    assert r.status_code == 403
    ok = client.post("/api/profiles", json={"fio": "Кравцов Сергей Сергеевич"},
                     headers=dict(emp, Origin="http://testserver", Host="testserver"))
    assert ok.status_code == 200


def test_администратор_и_руководитель_не_ведут_документы(client, admin, chief, emp):
    assert client.post("/api/profiles", json={"fio": "Кравцов Сергей Сергеевич"}, headers=admin).status_code == 403
    assert client.post("/api/profiles", json={"fio": "Кравцов Сергей Сергеевич"}, headers=chief).status_code == 403
    me = client.get("/api/me", headers=admin).json()
    assert me["is_admin"] and not me["can_edit"]
    assert client.get("/api/me", headers=emp).json()["can_edit"]


def test_стереть_навсегда_только_администратор(client, emp, admin):
    pid = client.post("/api/profiles", json={"fio": "Кравцов Сергей Сергеевич"}, headers=emp).json()["id"]
    wait_done(client, emp, pid)
    assert client.delete(f"/api/profiles/{pid}/permanent", headers=emp).status_code == 403
    assert client.delete(f"/api/profiles/{pid}", headers=emp).status_code == 200          # сотруднику — архив
    assert client.get(f"/api/profiles/{pid}", headers=emp).json()["archived"] is True
    assert client.delete(f"/api/profiles/{pid}/permanent", headers=admin).status_code == 200
    assert client.get(f"/api/profiles/{pid}", headers=emp).status_code == 404


def test_журнал_пишет_действия_и_расход_искры(client, emp):
    from app import portal
    pid = client.post("/api/profiles", json={"fio": "Кравцов Сергей Сергеевич", "hint": "министр"}, headers=emp).json()["id"]
    wait_done(client, emp, pid)
    actions = [r["action"] for r in portal.sent]
    assert "Заведена объективка" in actions
    done = [r for r in portal.sent if r["action"] == "Объективка собрана «Искрой»"]
    assert done and done[0]["tokens_in"] == 1000 and done[0]["tokens_out"] == 200
    assert done[0]["service"] == "profiles" and done[0]["login"] == "emp"
    assert done[0]["object"] == "Кравцов Сергей Сергеевич"
    client.delete(f"/api/profiles/{pid}", headers=emp)
    assert portal.sent[-1]["action"] == "Объективка удалена (в архив)"
    assert "в работе → архив" in portal.sent[-1]["details"]


def test_поиск_для_портала(client, emp):
    pid = client.post("/api/profiles", json={"fio": "Кравцов Сергей Сергеевич"}, headers=emp).json()["id"]
    wait_done(client, emp, pid)
    items = client.get("/api/search?q=кравц", headers=emp).json()["items"]
    assert items and items[0]["url"] == f"/profiles/#profile={pid}"
    assert set(items[0]) >= {"title", "url", "when", "who", "kind"}
    assert client.get("/api/search?q=к", headers=emp).json()["items"] == []


def test_без_ключа_искры_понятный_отказ(client, emp, monkeypatch):
    from app import iskra
    monkeypatch.setattr(iskra, "read_key", lambda: "")
    r = client.post("/api/profiles", json={"fio": "Кравцов Сергей Сергеевич"}, headers=emp)
    assert r.status_code == 503 and "Ключ «Искры»" in r.json()["detail"]


def test_ключ_искры_читается_из_env_папки_сервиса(tmp_path, monkeypatch):
    """Портал пишет ISKRA_API_KEY в <PROFILES_HOME>/.env — сервис читает его при каждом вызове."""
    from app import config, iskra
    monkeypatch.delenv("ISKRA_API_KEY", raising=False)
    monkeypatch.setenv("PROFILES_HOME", str(tmp_path))
    assert not iskra.key_ready()
    (tmp_path / ".env").write_text("PORTAL_SECRET=x\nISKRA_API_KEY=abc-key-0001\n", encoding="utf-8")
    assert iskra.read_key() == "abc-key-0001"
    assert config.db_path() == tmp_path / "data" / "profiles.db"


def test_адреса_в_интерфейсе_относительные():
    html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
    js = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
    # свои файлы — относительно; абсолютные только у портала (/static/base.css, шапка) и соседей
    assert 'src="static/app.js?v=4"' in html and 'href="static/style.css?v=4"' in html
    assert '/static/workspace-header.js' in html
    for m in re.finditer(r"""['"](/[a-z]+/[^'"]*)['"]""", js):
        assert m.group(1).startswith(("/moderator/", "/materials/", "/api/people")), m.group(1)
    # единственный абсолютный /api/ — реестр людей портала (контракт п. 5а: «Добавить» из браузера)
    assert "'api/profiles" in js and js.replace("'/api/people'", "").count("'/api/") == 0


def test_общие_модули_совпадают_с_portal_shared():
    shared = ROOT.parent / "portal" / "shared"
    if not shared.exists():
        return
    for name in ("iskra_common.py", "names_common.py", "docs_common.py"):
        assert (ROOT / "app" / name).read_bytes() == (shared / name).read_bytes(), name
