"""Круг показа «Мои / Отдела / Все» (фильтр, а не запрет), правка с версией, архив."""
from __future__ import annotations

from tests.conftest import answer_json, headers, wait_done


def _make(client, head, fio="Кравцов Сергей Сергеевич"):
    pid = client.post("/api/profiles", json={"fio": fio}, headers=head).json()["id"]
    return wait_done(client, head, pid)


def ids(client, head, scope, **kw):
    params = {"scope": scope, **kw}
    return [x["id"] for x in client.get("/api/profiles", headers=head, params=params).json()["items"]]


def test_чужая_объективка_в_мои_не_видна(client, emp, emp2):
    mine = _make(client, emp)["id"]
    other = _make(client, emp2)["id"]
    assert ids(client, emp, "mine") == [mine]
    assert ids(client, emp2, "mine") == [other]
    assert set(ids(client, emp, "all")) == {mine, other}
    # «Отдела»: руководитель видит подчинённых по X-Portal-Team, «*» — всех
    boss = headers("boss", name="Начальник", team="emp")
    assert ids(client, boss, "team") == [mine] and ids(client, boss, "mine") == []
    assert set(ids(client, headers("big", team="*"), "team")) == {mine, other}
    # это фильтр показа, а не запрет: по ссылке открывается
    assert client.get(f"/api/profiles/{other}", headers=emp).status_code == 200


def test_поиск_по_фамилии_в_реестре(client, emp):
    a = _make(client, emp)["id"]
    p = client.get(f"/api/profiles/{a}", headers=emp).json()
    assert ids(client, emp, "mine", q="кравц") == [a]
    assert ids(client, emp, "mine", q="министр") == [a]        # и по должности
    assert ids(client, emp, "mine", q="иванов") == []
    assert p["data"]["fio"] == "Кравцов Сергей Сергеевич"


def test_правка_с_версией_и_журнал_было_стало(client, emp, emp2):
    from app import portal
    p = _make(client, emp)
    d = p["data"]
    d["position"] = "Советник Президента"
    d["career"].append({"period": "с 2024 г.", "text": "советник", "src": ""})
    r = client.put(f"/api/profiles/{p['id']}", headers=emp, json={"data": d, "rev": p["rev"]})
    assert r.status_code == 200 and r.json()["rev"] == p["rev"] + 1
    rec = portal.sent[-1]
    assert rec["action"] == "Изменена объективка"
    assert "должность: «Министр просвещения Российской Федерации» → «Советник Президента»" in rec["details"]
    assert "опыт работы: строк 2 → 3" in rec["details"]
    # вторая вкладка со старой версией не затирает правку
    stale = client.put(f"/api/profiles/{p['id']}", headers=emp2, json={"data": answer_json(), "rev": p["rev"]})
    assert stale.status_code == 409 and stale.json()["detail"]["current"]["data"]["position"] == "Советник Президента"
    assert client.put(f"/api/profiles/{p['id']}", headers=emp, json={"data": d}).status_code == 400
    # порядок строк — тоже правка
    d2 = r.json()["data"]
    d2["career"].reverse()
    r2 = client.put(f"/api/profiles/{p['id']}", headers=emp, json={"data": d2, "rev": r.json()["rev"]})
    assert r2.json()["data"]["career"][0]["text"] == "советник"
    assert "опыт работы: изменён порядок" in portal.sent[-1]["details"]


def test_архив_и_восстановление(client, emp):
    p = _make(client, emp)
    client.delete(f"/api/profiles/{p['id']}", headers=emp)
    assert ids(client, emp, "mine") == [] and ids(client, emp, "mine", archived="true") == [p["id"]]
    archived = client.get(f"/api/profiles/{p['id']}", headers=emp).json()
    assert client.put(f"/api/profiles/{p['id']}", headers=emp,
                      json={"data": answer_json(position="X"), "rev": archived["rev"]}).status_code == 409
    client.post(f"/api/profiles/{p['id']}/restore", headers=emp)
    assert ids(client, emp, "mine") == [p["id"]]
