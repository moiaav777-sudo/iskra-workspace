"""Уведомления сотрудникам (решение пользователя 02.10): объявления выкладки и администратора в общей шапке,
«доступна новая версия», предупреждение за минуту до выкладки, страница плановых работ.

Сервер — настоящими HTTP-запросами; удалённая часть выкладки — сам `ops/vps-deploy-remote.sh` на временном
каталоге (bash + python3, как на VPS); шапка — настоящий `static/workspace-header.js` в песочнице node.
"""
from __future__ import annotations

import base64
import datetime as dt
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from test_portal import client, env, login  # noqa: F401  (env — фикстура)

PORTAL = Path(__file__).resolve().parent.parent
REMOTE = PORTAL / "ops" / "vps-deploy-remote.sh"
SHIP = PORTAL / "ops" / "ship.sh"
HEADER = PORTAL / "static" / "workspace-header.js"


@pytest.fixture(autouse=True)
def _portal_in_tmp(tmp_path, monkeypatch):
    """Папка портала — во временном каталоге: `notices` рядом с настоящей рабочей папкой не должен влиять на тесты."""
    from app import config, main  # noqa: F401  (main — до подмены: статика берётся из настоящей папки)
    monkeypatch.setattr(config, "BASE_DIR", tmp_path / "opt" / "work" / "portal")


def _iso(minutes: float) -> str:
    return (dt.datetime.now().astimezone() + dt.timedelta(minutes=minutes)).isoformat(timespec="seconds")


def _notices_dir(env) -> Path:
    d = env["config"].DATA_DIR / "notices"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _admin(env):
    c = client(env)
    assert login(c, "boss", "Boss-password-1").status_code == 200
    return c


def _emp(env):
    c = client(env)
    assert login(c, "emp", "Emp-password-1").status_code == 200
    return c


# ── сервер ──────────────────────────────────────────────────────────────

def test_notice_and_version_need_login(env):
    c = client(env)
    assert c.get("/api/notice").status_code == 401
    assert c.get("/api/version").status_code == 401


def test_notice_shows_active_from_file_and_base_only(env):
    (_notices_dir(env) / "current.json").write_text(json.dumps([
        {"id": "deploy-1", "kind": "maintenance", "text": "Через 1 минуту будет обновление «Совещание»",
         "service": "moderator", "from": _iso(-1), "until": _iso(2), "starts": _iso(0.5), "source": "deploy"},
        {"id": "deploy-old", "kind": "update", "text": "Обновлено давно", "from": _iso(-120), "until": _iso(-60)},
        {"id": "deploy-future", "kind": "info", "text": "Ещё не время", "from": _iso(30), "until": _iso(60)},
        {"id": "deploy-forever", "kind": "update", "text": "Обновлено: «Тезисы»", "from": _iso(-5)},
    ], ensure_ascii=False), encoding="utf-8")
    from app import notices
    notices.create("Истёкшее объявление", "info", "", _iso(-1), "boss")      # срок вышел — не показываем
    removed, _ = notices.create("Снятое объявление", "warning", "", "", "boss")
    notices.remove(removed["number"], "boss")

    a = _admin(env)
    r = a.post("/api/admin/notices", json={"text": "В пятницу  с 18:00 — работы", "kind": "warning",
                                           "service": "", "until": ""})
    assert r.status_code == 200 and r.json()["created"] is True

    data = _emp(env).get("/api/notice").json()
    texts = [n["text"] for n in data["items"]]
    assert texts == ["Через 1 минуту будет обновление «Совещание»", "В пятницу с 18:00 — работы",
                     "Обновлено: «Тезисы»"]                           # по важности, лишних нет
    first = data["items"][0]
    assert first["kind"] == "maintenance" and first["source"] == "deploy" and first["starts"]
    assert first["service_name"] == "Совещание"
    assert data["items"][1]["source"] == "admin" and data["items"][1]["id"].startswith("admin-")
    assert data["now"]


def test_notices_dir_beside_portal_like_on_vps(env, tmp_path, monkeypatch):
    """Ревью RC-22, К-1: на VPS портал в /opt/work/portal, выкладка пишет в /opt/work/notices — без
    PORTAL_NOTICES_DIR портал должен читать именно его, иначе предупреждение за минуту молча не видно."""
    from app import notices
    config = env["config"]
    monkeypatch.delenv("PORTAL_NOTICES_DIR", raising=False)
    assert notices.notices_dir() == config.DATA_DIR / "notices"          # стенд: рядом каталога нет
    beside = tmp_path / "opt" / "work" / "notices"                       # как создаёт выкладка
    beside.mkdir(parents=True)
    (beside / "current.json").write_text(json.dumps([
        {"id": "deploy-1", "kind": "maintenance", "text": "Через 1 минуту будет обновление «Совещание»",
         "from": _iso(-1), "until": _iso(2)}], ensure_ascii=False), encoding="utf-8")
    (beside / "versions.json").write_text(json.dumps({"brief": {"commit": "abc1234", "at": _iso(0)}}),
                                          encoding="utf-8")
    assert notices.notices_dir() == beside
    c = _emp(env)
    assert [n["id"] for n in c.get("/api/notice").json()["items"]] == ["deploy-1"]
    assert c.get("/api/version").json()["versions"]["brief"]["commit"] == "abc1234"
    # явная переменная главнее
    explicit = tmp_path / "elsewhere"
    explicit.mkdir()
    monkeypatch.setenv("PORTAL_NOTICES_DIR", str(explicit))
    assert notices.notices_dir() == explicit
    assert c.get("/api/notice").json()["items"] == []


def test_notice_survives_broken_file(env):
    (_notices_dir(env) / "current.json").write_text("{не json", encoding="utf-8")
    (_notices_dir(env) / "versions.json").write_text("[1, 2]", encoding="utf-8")
    c = _emp(env)
    assert c.get("/api/notice").json()["items"] == []
    assert c.get("/api/version").json()["versions"] == {}


def test_admin_creates_and_removes_notice_once_each(env):
    a = _admin(env)
    body = {"text": "Плановые работы в 18:00", "kind": "maintenance", "service": "moderator", "until": _iso(60)}
    r1 = a.post("/api/admin/notices", json=body)
    r2 = a.post("/api/admin/notices", json=body)                       # двойное нажатие
    assert r1.status_code == r2.status_code == 200
    assert r1.json()["created"] is True and r2.json()["created"] is False
    number = r1.json()["item"]["number"]
    assert r2.json()["item"]["number"] == number
    items = a.get("/api/admin/notices").json()["items"]
    assert len([n for n in items if n["active"]]) == 1

    assert any(n["text"] == "Плановые работы в 18:00" for n in _emp(env).get("/api/notice").json()["items"])

    d1 = a.delete(f"/api/admin/notices/{number}")
    d2 = a.delete(f"/api/admin/notices/{number}")
    assert d1.json()["removed"] is True and d2.json()["removed"] is False
    assert a.delete("/api/admin/notices/9999").status_code == 404
    assert _emp(env).get("/api/notice").json()["items"] == []

    actions = [x["action"] for x in env["db"].audit_list()[0]]
    assert actions.count("Объявление добавлено") == 1
    assert actions.count("Объявление снято") == 1
    added = next(x for x in env["db"].audit_list()[0] if x["action"] == "Объявление добавлено")
    assert added["login"] == "boss" and "плановые работы" in added["details"] and "Совещание" in added["details"]


@pytest.mark.parametrize("body, why", [
    ({"text": "   ", "kind": "info"}, "текст"),
    ({"text": "x", "kind": "<script>"}, "вид"),
    ({"text": "x", "kind": "info", "service": "nope"}, "сервис"),
    ({"text": "x", "kind": "info", "until": "2020-01-01T10:00:00+03:00"}, "прошёл"),
    ({"text": "x", "kind": "info", "until": "завтра"}, "ошибкой"),
    ({"text": "я" * 501, "kind": "info"}, "длиннее"),
])
def test_admin_notice_validation(env, body, why):
    r = _admin(env).post("/api/admin/notices", json=body)
    assert r.status_code == 400 and why in r.json()["detail"]


def test_employee_cannot_manage_notices(env):
    from app import notices
    item, _ = notices.create("Объявление", "info", "", "", "boss")
    e = _emp(env)
    assert e.get("/api/admin/notices").status_code == 403
    assert e.post("/api/admin/notices", json={"text": "Моё", "kind": "info"}).status_code == 403
    assert e.delete(f"/api/admin/notices/{item['number']}").status_code == 403
    assert [n["text"] for n in e.get("/api/notice").json()["items"]] == ["Объявление"]


def test_version_from_file(env):
    (_notices_dir(env) / "versions.json").write_text(json.dumps({
        "moderator": {"commit": "69a5ca7aaaa", "at": "2026-10-02T18:00:00+03:00"},
        "portal": {"commit": "fd59833bbbb", "at": "2026-10-02T17:00:00+03:00"},
        "чужой": {"commit": "x"},
    }), encoding="utf-8")
    r = _emp(env).get("/api/version")
    assert r.status_code == 200
    data = r.json()
    assert data["versions"] == {"moderator": {"commit": "69a5ca7aaaa", "at": "2026-10-02T18:00:00+03:00"},
                                "portal": {"commit": "fd59833bbbb", "at": "2026-10-02T17:00:00+03:00"}}
    assert data["portal"]["commit"] == "fd59833bbbb" and data["portal"]["started"]


def test_admin_page_has_notices_tab():
    html = (PORTAL / "static" / "admin.html").read_text(encoding="utf-8")
    js = (PORTAL / "static" / "admin.js").read_text(encoding="utf-8")
    assert 'data-tab="notices"' in html and 'data-panel="notices"' in html
    assert "/api/admin/notices" in js and "Снять" in js


# ── удалённая часть выкладки: режим notice и запись версии ─────────────

def _remote(notices_dir: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", str(REMOTE), *args], capture_output=True, text=True, timeout=60,
                          env={**os.environ, "NOTICES_DIR": str(notices_dir)})


def _b64(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode()


def test_remote_notice_mode_adds_expires_and_update_clears_maintenance(tmp_path):
    d = tmp_path / "notices"
    d.mkdir()
    (d / "current.json").write_text(json.dumps([
        {"id": "deploy-old", "kind": "info", "text": "старое", "from": _iso(-60), "until": _iso(-1)},
        {"id": "admin-keep", "kind": "maintenance", "text": "чужой сервис", "service": "brief",
         "from": _iso(-1), "until": _iso(30)},
    ]), encoding="utf-8")
    warn = "Через 1 минуту будет обновление «Совещание». Пожалуйста, приостановите работу и сохраните текущий этап."
    r = _remote(d, "work", "moderator", "-", "notice", "maintenance", "3", _b64(warn))
    assert r.returncode == 0, r.stdout + r.stderr
    items = json.loads((d / "current.json").read_text(encoding="utf-8"))
    assert [n["id"] for n in items if n["id"] == "deploy-old"] == []          # просроченное снято
    m = [n for n in items if n["kind"] == "maintenance" and n["service"] == "moderator"]
    assert len(m) == 1 and m[0]["text"] == warn and m[0]["id"].startswith("deploy-")
    assert m[0]["source"] == "deploy"
    start, until, starts = (dt.datetime.fromisoformat(m[0][k]) for k in ("from", "until", "starts"))
    assert 170 <= (until - start).total_seconds() <= 190 and 55 <= (starts - start).total_seconds() <= 65
    assert oct(os.stat(d / "current.json").st_mode & 0o777) == "0o644"

    r = _remote(d, "work", "moderator", "-", "notice", "update", "1440", _b64("Обновлено: «Совещание» — вкладка"))
    assert r.returncode == 0, r.stdout + r.stderr
    items = json.loads((d / "current.json").read_text(encoding="utf-8"))
    kinds = sorted((n["kind"], n.get("service")) for n in items)
    assert kinds == [("maintenance", "brief"), ("update", "moderator")]      # своё снято, чужое на месте
    assert len({n["id"] for n in items}) == len(items)

    # Н-5: следующее «Обновлено» того же сервиса заменяет прежнее, а не копится; у чужого — своё
    old_id = next(n["id"] for n in items if n["kind"] == "update")
    for svc, text in (("moderator", "Обновлено: «Совещание» — ещё"), ("theses", "Обновлено: «Тезисы»")):
        assert _remote(d, "work", svc, "-", "notice", "update", "1440", _b64(text)).returncode == 0
    items = json.loads((d / "current.json").read_text(encoding="utf-8"))
    updates = {n["service"]: n for n in items if n["kind"] == "update"}
    assert sorted(updates) == ["moderator", "theses"]
    assert updates["moderator"]["text"] == "Обновлено: «Совещание» — ещё"
    assert updates["moderator"]["id"] != old_id                               # новый id, «Скрыть» старого не прячет
    assert re.fullmatch(r"deploy-\d{8}-\d{6}-\d{3}-[0-9a-f]{6}", updates["moderator"]["id"])


def test_remote_deployed_writes_version_and_clears_own_maintenance(tmp_path):
    d = tmp_path / "notices"
    for svc in ("moderator", "brief"):
        assert _remote(d, "work", svc, "-", "notice", "maintenance", "3", _b64("скоро " + svc)).returncode == 0
    r = _remote(d, "work", "moderator", "69a5ca7" + "0" * 33, "deployed")
    assert r.returncode == 0, r.stdout + r.stderr
    versions = json.loads((d / "versions.json").read_text(encoding="utf-8"))
    assert versions["moderator"]["commit"] == "69a5ca7" + "0" * 33 and versions["moderator"]["at"]
    items = json.loads((d / "current.json").read_text(encoding="utf-8"))
    assert [(n["kind"], n["service"]) for n in items] == [("maintenance", "brief")]


def test_remote_notice_mode_rejects_bad_input(tmp_path):
    d = tmp_path / "n"
    ok = _remote(d, "work", "moderator", "-", "notice", "info", "3", _b64("проверка"))
    assert ok.returncode == 0, ok.stdout + ok.stderr
    before = (d / "current.json").read_text(encoding="utf-8")
    for args in [
        ("work", "bogus", "-", "notice", "info", "3", "dGVzdA=="),
        ("work", "moderator", "-", "notice", "loud", "3", "dGVzdA=="),
        ("work", "moderator", "-", "notice", "info", "3;echo pwned", "dGVzdA=="),
        ("work", "moderator", "-", "notice", "info", "3", "не base64!"),
        ("work", "moderator", "-", "notice", "info", "3", "dGVzdA==", "1;echo"),
        ("prod", "moderator", "-", "notice", "info", "3", "dGVzdA=="),
        ("work", "moderator", "-", "launch"),
        ("work", "moderator", "не-коммит", "deployed"),
    ]:
        r = _remote(d, *args)
        assert r.returncode != 0, args
        assert (d / "current.json").read_text(encoding="utf-8") == before, args
    assert not (d / "versions.json").exists()


def test_remote_files_are_read_by_portal(env):
    """Сквозной путь: то, что пишет выкладка, портал отдаёт шапке."""
    d = _notices_dir(env)
    assert _remote(d, "work", "all", "-", "notice", "maintenance", "3", _b64("Через 1 минуту обновление")).returncode == 0
    assert _remote(d, "work", "brief", "abcdef1234567", "deployed").returncode == 0
    c = _emp(env)
    items = c.get("/api/notice").json()["items"]
    assert [(n["kind"], n["service"], n["service_name"]) for n in items] == [("maintenance", "", "все сервисы")]
    assert c.get("/api/version").json()["versions"]["brief"]["commit"] == "abcdef1234567"


def test_vps_notice_checks_names_before_building_ssh(tmp_path):
    """Ревью RC-22, Н-6: удалённая оболочка разбирает ssh-строку заново — имя сервиса и цель проверяются
    по белому списку до её сборки, ssh с «moderator;touch …» не вызывается вовсе."""
    stub = tmp_path / "bin"
    stub.mkdir()
    log = tmp_path / "ssh.log"
    (stub / "ssh").write_text(f'#!/bin/bash\necho "$@" >> "{log}"\n', encoding="utf-8")
    (stub / "ssh").chmod(0o755)
    transport = PORTAL / "ops" / "deploy_transport.sh"

    def notice(*args):
        cmd = 'source "$1"; shift; vps_notice "$@"'
        return subprocess.run(["bash", "-c", cmd, "x", str(transport), *args], capture_output=True, text=True,
                              timeout=30, env={**os.environ, "PATH": f"{stub}:{os.environ['PATH']}",
                                               "DEPLOY_VIA": "ssh", "HOME": str(tmp_path)})

    assert notice("work", "moderator", "info", "5", "Проверка").returncode == 0
    assert log.read_text(encoding="utf-8").count("\n") == 1                # хорошее имя — ssh вызван
    for args in [("work", "moderator;touch /tmp/x", "info", "5", "т"), ("prod", "moderator", "info", "5", "т"),
                 ("work", "moderator", "info;id", "5", "т"), ("work", "moderator", "info", "5 6", "т"),
                 ("work", "moderator", "info", "5", "т", "1;id"), ("work", "all", "info", "5", "")]:
        assert notice(*args).returncode == 2, args
    assert log.read_text(encoding="utf-8").count("\n") == 1                # ни одного лишнего вызова ssh


# ── ship.sh: разбор аргументов и тексты объявлений ─────────────────────

def _ship(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", str(SHIP), *args], capture_output=True, text=True, timeout=30)


def test_ship_help_and_plan():
    h = _ship("--help")
    assert h.returncode == 0 and "--note" in h.stdout and "--now" in h.stdout

    p = _ship("RC-x", "moderator=69a5ca7", "--note", "вкладка «Результаты»", "--plan")
    assert p.returncode == 0, p.stderr
    assert ("предупреждение (moderator, maintenance, 3 мин): Через 1 минуту будет обновление «Совещание». "
            "Пожалуйста, приостановите работу и сохраните текущий этап.") in p.stdout
    assert "пауза: 60 с" in p.stdout
    assert "после выкладки (moderator, update, 1440 мин): Обновлено: «Совещание» — вкладка «Результаты»" in p.stdout

    many = _ship("RC-x", "moderator=69a5ca7", "brief=1234567", "theses=89abcde", "--plan", "--now")
    assert many.returncode == 0
    assert "(all, maintenance, 7 мин): Сейчас будет обновление «Совещание», «Бриф из справки» и «Тезисы»" in many.stdout
    assert "пауза: нет (--now)" in many.stdout
    assert "Обновлены сервисы «Совещание», «Бриф из справки» и «Тезисы»" in many.stdout


def test_ship_rejects_bad_arguments():
    assert _ship("RC-x", "moderator=69a5ca7", "--note=правка", "--plan").returncode == 0
    for args in [
        ("RC-x", "nope=69a5ca7", "--plan"),
        ("RC-x", "moderator=не-коммит", "--plan"),
        ("moderator=69a5ca7", "--plan"),
        ("RC-x", "--plan"),
        ("RC-x", "moderator=69a5ca7", "--nginx"),
        ("RC-x", "moderator=69a5ca7", "--note"),
        ("RC-x", "moderator=69a5ca7", "--later", "--plan"),
    ]:
        assert _ship(*args).returncode == 2, args


def test_service_names_match_config(env):
    """Названия в шапке и в ship.sh — как в config.SERVICES: иначе плашка «новая версия» не узнает свой сервис."""
    config = env["config"]
    want = {s["name"]: s["id"] for s in config.SERVICES}
    js = HEADER.read_text(encoding="utf-8")
    block = re.search(r"var SERVICE_IDS = \{(.*?)\};", js, re.S).group(1)
    assert dict(re.findall(r'"([^"]+)":\s*"([a-z0-9]+)"', block)) == want
    sh = SHIP.read_text(encoding="utf-8")
    got = dict(re.findall(r'^\s+([a-z0-9]+)\) echo "([^"]+)" ;;', sh, re.M))
    assert {v: k for k, v in got.items() if k != "portal"} == want
    # и ни один сервис не забыт в режиме notice удалённого скрипта
    remote = REMOTE.read_text(encoding="utf-8")
    line = re.search(r'case "\$svc" in all\|([a-z0-9|]+)\)', remote).group(1)
    assert set(line.split("|")) == set(config.SERVICE_IDS) | {"portal"}


# ── страница плановых работ ────────────────────────────────────────────

def test_down_page_is_self_contained():
    html = (PORTAL / "ops" / "down.html").read_text(encoding="utf-8")
    assert "Сервис временно недоступен" in html and "Попробовать снова" in html
    assert 'fetch("/api/notice"' in html and '"maintenance"' in html
    assert not re.search(r'<(script|link|img)[^>]+(src|href)=', html)        # ни одного внешнего файла


# ── шапка: настоящий workspace-header.js в node ────────────────────────

@pytest.mark.skipif(shutil.which("node") is None, reason="node не найден в PATH — фронтенд-тест пропущен")
def test_header_notices_in_node():
    check = Path(__file__).resolve().parent / "js_header_notices_check.js"
    r = subprocess.run(["node", str(check), str(HEADER)], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, "полоса объявлений в шапке не прошла проверку:\n" + r.stdout + r.stderr
    assert "OK" in r.stdout
