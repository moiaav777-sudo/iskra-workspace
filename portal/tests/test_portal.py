import json
import sqlite3

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("PORTAL_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("SECRET_KEY", "test-secret")
    monkeypatch.setenv("PORTAL_SECRET", "nginx-secret")
    monkeypatch.setenv("PORTAL_INSECURE_COOKIES", "1")
    from app import auth, config, db
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "ENV_PATH", tmp_path / "portal.env")
    db.reset_for_tests()
    auth.reset_limits_for_tests()
    auth._used_codes.clear()
    db.create_user("boss", auth.hash_password("Boss-password-1"), full_name="Иванов Иван Иванович", role="admin",
                   must_change_password=False)
    db.create_user("emp", auth.hash_password("Emp-password-1"), role="employee", services=["brief"],
                   must_change_password=False)
    from app.main import app
    return {"app": app, "tmp": tmp_path, "db": db, "auth": auth, "config": config}


def client(env):
    return TestClient(env["app"], base_url="https://workspace.example.ru")


def login(c, user, pw, code=""):
    return c.post("/api/login", json={"login": user, "password": pw, "code": code})


def test_login_and_me(env):
    c = client(env)
    assert login(c, "boss", "wrong").status_code == 401
    r = login(c, "boss", "Boss-password-1")
    assert r.status_code == 200
    me = c.get("/api/me").json()
    assert me["role"] == "admin" and set(me["services"]) == set(env["config"].SERVICE_IDS)
    actions = [a["action"] for a in env["db"].audit_list()[0]]
    assert "Вход" in actions and "Неудачная попытка входа" in actions


def test_pages_redirect_to_login(env):
    c = client(env)
    r = c.get("/", follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"].startswith("/login")
    assert c.get("/login").status_code == 200


def test_hidden_service_not_shown_to_employee(env, monkeypatch):
    """Сервис «в разработке»: права уже работают, плитку видит только администратор."""
    config = env["config"]
    hidden = {"id": "novyi", "name": "Новый сервис", "url": "/novyi/", "hidden": True, "description": "в разработке"}
    monkeypatch.setattr(config, "SERVICES", [*config.SERVICES, hidden])
    monkeypatch.setattr(config, "SERVICE_IDS", [*config.SERVICE_IDS, "novyi"])
    env["db"].update_user("emp", services=config.SERVICE_IDS)

    worker = client(env)
    login(worker, "emp", "Emp-password-1")
    me = worker.get("/api/me").json()
    assert not any(s.get("hidden") for s in me["service_list"])
    assert worker.get("/auth/check/novyi").status_code == 200   # доступ работает, плитки просто не видно

    boss = client(env)
    login(boss, "boss", "Boss-password-1")
    assert any(s.get("hidden") for s in boss.get("/api/me").json()["service_list"])


def test_agenda_removed_from_portal(env):
    """Решение пользователя 05.10: «повестку мы убили — удали её». Плитки нет ни у кого, доступ через портал
    закрыт даже тому, у кого сервис был в правах; юнит и данные остаются до снятия сервером."""
    from app import config
    assert not any(s["id"] == "agenda" for s in config.SERVICES)
    env["db"].update_user("emp", services=[*env["db"].get_user("emp")["services"], "agenda"])
    worker = client(env)
    login(worker, "emp", "Emp-password-1")
    assert not any(s["id"] == "agenda" for s in worker.get("/api/me").json()["service_list"])
    assert worker.get("/auth/check/agenda").status_code in (401, 403)
    boss = client(env)
    login(boss, "boss", "Boss-password-1")
    assert not any(s["id"] == "agenda" for s in boss.get("/api/me").json()["service_list"])


def test_hidden_service_tile_opens_for_admin(env, monkeypatch):
    """05.10: скрытый сервис администратор открывает с плитки (проверка до открытия всем) — ссылка на url есть."""
    import re
    from app import config
    monkeypatch.setattr(config, "SERVICES", [*config.SERVICES, {"id": "novyi", "name": "Новый", "url": "/novyi/", "hidden": True, "description": "в разработке"}])
    monkeypatch.setattr(config, "SERVICE_IDS", [*config.SERVICE_IDS, "novyi"])
    boss = client(env)
    login(boss, "boss", "Boss-password-1")
    hidden = [s for s in boss.get("/api/me").json()["service_list"] if s.get("hidden")]
    assert hidden and all(s.get("url") for s in hidden)
    js = (config.BASE_DIR / "static" / "home.js").read_text(encoding="utf-8")
    assert "Открыть для проверки" in js and "Пока делается" not in js


def test_auth_check_per_service(env):
    c = client(env)
    assert c.get("/auth/check/brief").status_code == 401
    login(c, "emp", "Emp-password-1")
    r = c.get("/auth/check/brief")
    assert r.status_code == 200 and r.headers["x-portal-user"] == "emp" and r.headers["x-portal-role"] == "employee"
    assert c.get("/auth/check/moderator").status_code == 403
    assert c.get("/auth/check/unknown").status_code == 403


def test_must_change_password_blocks_services(env):
    env["db"].update_user("emp", must_change_password=True)
    c = client(env)
    assert login(c, "emp", "Emp-password-1").json()["must_change_password"] is True
    assert c.get("/auth/check/brief").status_code == 403
    assert c.get("/", follow_redirects=False).headers["location"] == "/account?force=1"
    r = c.post("/api/me/password", json={"old_password": "Emp-password-1", "new_password": "emp12345678"})
    assert r.status_code == 400  # логин внутри пароля
    r = c.post("/api/me/password", json={"old_password": "Emp-password-1", "new_password": "Новый-пароль-42"})
    assert r.status_code == 200
    assert c.get("/auth/check/brief").status_code == 200


def test_password_change_revokes_other_sessions(env):
    a, b = client(env), client(env)
    login(a, "emp", "Emp-password-1"); login(b, "emp", "Emp-password-1")
    assert a.post("/api/me/password", json={"old_password": "Emp-password-1", "new_password": "Другой-пароль-7"}).status_code == 200
    assert a.get("/auth/check/brief").status_code == 200
    assert b.get("/auth/check/brief").status_code == 401


def test_bruteforce_limit(env):
    c = client(env)
    for _ in range(8):
        login(c, "emp", "bad")
    assert login(c, "emp", "Emp-password-1").status_code == 429


def test_totp_flow(env):
    auth = env["auth"]
    c = client(env)
    login(c, "boss", "Boss-password-1")
    secret = c.post("/api/me/totp/start").json()["secret"]
    assert c.post("/api/me/totp/confirm", json={"code": "000000"}).status_code == 400
    assert c.post("/api/me/totp/confirm", json={"code": auth.totp_now(secret)}).status_code == 200
    auth._used_codes.clear()
    c2 = client(env)
    r = login(c2, "boss", "Boss-password-1")
    assert r.status_code == 401 and r.json()["need_code"]
    assert login(c2, "boss", "Boss-password-1", "123456").status_code == 401
    assert login(c2, "boss", "Boss-password-1", auth.totp_now(secret)).status_code == 200
    # тот же код второй раз не принимается
    assert login(client(env), "boss", "Boss-password-1", auth.totp_now(secret)).status_code == 401


def test_admin_user_management_and_audit(env):
    c = client(env)
    login(c, "boss", "Boss-password-1")
    r = c.post("/api/admin/users", json={"login": "petrova", "full_name": "Петрова Анна", "password": "short",
                                         "role": "employee", "services": ["moderator"]})
    assert r.status_code == 400
    r = c.post("/api/admin/users", json={"login": "petrova", "full_name": "Петрова Анна", "password": "Временный-пароль-1",
                                         "role": "employee", "services": ["moderator", "a3"]})
    assert r.status_code == 200
    u = env["db"].get_user("petrova")
    assert u["must_change_password"] and u["services"] == ["moderator", "a3"]
    assert c.patch("/api/admin/users/petrova", json={"services": ["a3"], "active": False}).status_code == 200
    p = client(env)
    assert login(p, "petrova", "Временный-пароль-1").status_code == 401  # заблокирован
    assert c.patch("/api/admin/users/boss", json={"role": "employee"}).status_code == 400  # последний админ
    items = c.get("/api/admin/audit?q=petrova").json()["items"]
    assert any(i["action"] == "Изменён пользователь" and "заблокирован" in i["details"] for i in items)
    assert c.get("/api/admin/audit.csv").status_code == 200


def test_employee_cannot_admin(env):
    c = client(env)
    login(c, "emp", "Emp-password-1")
    assert c.get("/api/admin/users").status_code == 403
    assert c.get("/admin", follow_redirects=False).headers["location"] == "/"


def test_internal_audit_requires_secret(env):
    c = client(env)
    body = {"service": "brief", "login": "emp", "action": "Загружена справка", "object": "Удмуртия.docx"}
    assert c.post("/internal/audit", json=body).status_code == 403
    assert c.post("/internal/audit", json=body, headers={"X-Portal-Secret": "nginx-secret"}).status_code == 200
    assert env["db"].audit_list(service="brief")[1] == 1


def test_cross_origin_rejected(env):
    c = client(env)
    r = c.post("/api/login", json={"login": "boss", "password": "Boss-password-1"}, headers={"Origin": "https://evil.example"})
    assert r.status_code == 403


def test_iskra_key_written_everywhere(env, monkeypatch):
    tmp = env["tmp"]
    (tmp / "moderator").mkdir(); (tmp / "moderator" / ".env").write_text("SECRET_KEY=x\nISKRA_API_KEY=old\n")
    (tmp / "brief").mkdir(); (tmp / "brief" / "settings.json").write_text(json.dumps({"iskra_key": "old", "verified_sig": "s", "provider": "iskra"}))
    monkeypatch.setenv("MODERATOR_DIR", str(tmp / "moderator"))
    monkeypatch.setenv("BRIEF_HOME", str(tmp / "brief"))
    c = client(env)
    login(c, "boss", "Boss-password-1")
    r = c.post("/api/admin/iskra-key", json={"key": "isk_newkey_123456"})
    assert r.status_code == 200 and r.json()["same"]
    assert "ISKRA_API_KEY=isk_newkey_123456" in (tmp / "moderator" / ".env").read_text()
    assert "SECRET_KEY=x" in (tmp / "moderator" / ".env").read_text()
    st = json.loads((tmp / "brief" / "settings.json").read_text())
    assert st["iskra_key"] == "isk_newkey_123456" and "verified_sig" not in st and st["provider"] == "iskra"


def test_migrate_users(env, tmp_path):
    auth, db = env["auth"], env["db"]
    src = tmp_path / "meetings.db"
    con = sqlite3.connect(src)
    con.execute("CREATE TABLE users(login TEXT, password_hash TEXT, is_admin INTEGER, created_at TEXT)")
    con.execute("INSERT INTO users VALUES('admin', ?, 1, '')", (auth.hash_password("admin123"),))
    con.execute("INSERT INTO users VALUES('user1', ?, 0, '')", (auth.hash_password("user1"),))
    con.execute("INSERT INTO users VALUES('user9', ?, 0, '')", (auth.hash_password("Сложный-пароль-99"),))
    con.commit(); con.close()
    from scripts.migrate_users import main
    main(str(src)); main(str(src))
    assert db.get_user("admin")["role"] == "admin" and db.get_user("admin")["must_change_password"]
    assert db.get_user("user1")["services"] == ["moderator"] and db.get_user("user1")["must_change_password"]
    assert not db.get_user("user9")["must_change_password"]
    c = client(env)
    assert login(c, "user1", "user1").json()["must_change_password"]


def test_a3_page_access(env, monkeypatch):
    kit = env["tmp"] / "kit.html"
    kit.write_text("<html><body>A3 kit</body></html>", encoding="utf-8")
    monkeypatch.setenv("A3_KIT", str(kit))
    c = client(env)
    assert c.get("/a3/", follow_redirects=False).status_code == 302
    login(c, "emp", "Emp-password-1")
    assert c.get("/a3/", follow_redirects=False).headers["location"] == "/?denied=a3"
    c2 = client(env)
    login(c2, "boss", "Boss-password-1")
    assert "A3 kit" in c2.get("/a3/").text


# ── проверки, добавленные после ревью ───────────────────────────────────

def test_cookie_flags(env):
    c = client(env)
    r = login(c, "boss", "Boss-password-1")
    cookie = r.headers["set-cookie"]
    assert "HttpOnly" in cookie and "SameSite=lax" in cookie and "Path=/" in cookie


def test_forged_forwarded_host_does_not_disable_origin_check(env):
    c = client(env)
    r = c.post("/api/login", json={"login": "boss", "password": "Boss-password-1"},
               headers={"Origin": "https://evil.example", "X-Forwarded-Host": "evil.example"})
    assert r.status_code == 403


def test_unknown_login_is_not_written_to_audit(env):
    """В поле логина легко набрать пароль — в журнал он попасть не должен."""
    c = client(env)
    login(c, "MyS3cret-Password", "whatever")
    items, _ = env["db"].audit_list()
    assert all("MyS3cret" not in (i["login"] + i["details"]) for i in items)
    login(c, "emp", "wrong")
    assert any(i["action"] == "Неудачная попытка входа" and i["login"] == "emp" for i in env["db"].audit_list()[0])


def test_audit_search_is_case_insensitive_in_russian(env):
    env["db"].audit("Изменён пользователь", login="boss", obj="Петрова Анна", details="сервисы: нет → Бриф")
    c = client(env)
    login(c, "boss", "Boss-password-1")
    for query in ("петрова", "ПЕТРОВА", "бриф"):
        assert c.get(f"/api/admin/audit?q={query}").json()["total"] == 1, query


def test_totp_disable_is_rate_limited(env):
    auth = env["auth"]
    c = client(env)
    login(c, "boss", "Boss-password-1")
    secret = c.post("/api/me/totp/start").json()["secret"]
    c.post("/api/me/totp/confirm", json={"code": auth.totp_now(secret)})
    for _ in range(auth.LOGIN_MAX_PER_LOGIN):
        assert c.post("/api/me/totp/disable", json={"code": "000000"}).status_code == 400
    assert c.post("/api/me/totp/disable", json={"code": "000000"}).status_code == 429
    assert env["db"].get_user("boss")["totp_secret"]


def test_blocked_user_with_live_cookie_is_refused(env):
    c = client(env)
    login(c, "emp", "Emp-password-1")
    env["db"].update_user("emp", active=False)
    assert c.get("/auth/check/brief").status_code == 401
    assert c.get("/api/me").status_code == 401


def test_logout_clears_session(env):
    c = client(env)
    login(c, "emp", "Emp-password-1")
    assert c.post("/api/logout").status_code == 200
    assert c.get("/auth/check/brief").status_code == 401


def test_internal_audit_rejects_wrong_secret_of_same_length(env):
    c = client(env)
    body = {"service": "brief", "action": "Проверка"}
    assert c.post("/internal/audit", json=body, headers={"X-Portal-Secret": "nginx-secreT"}).status_code == 403


def test_portal_secret_must_be_configured(env, monkeypatch):
    monkeypatch.delenv("PORTAL_SECRET", raising=False)
    import pytest as _pytest
    with _pytest.raises(env["config"].SecretMissing):
        env["config"].portal_secret()


def test_csv_neutralizes_formulas(env):
    env["db"].audit("Изменён пользователь", login="boss", obj="=2+5+cmd|' /C calc'!A0")
    c = client(env)
    login(c, "boss", "Boss-password-1")
    text = c.get("/api/admin/audit.csv").text
    assert "'=2+5" in text and "\n=2+5" not in text


def test_status_is_admin_only(env):
    c = client(env)
    login(c, "emp", "Emp-password-1")
    assert c.get("/api/admin/status").status_code == 403


def test_status_reports_services_and_backups(env, monkeypatch):
    """Страница «Состояние»: живость сервисов, размер данных и итоги ночных копий."""
    from app import status
    status._cache.update(time=0, data=None)
    monkeypatch.setattr(status, "_healthz", lambda port: {"ok": port == 8700, "ms": 3, "error": "" if port == 8700 else "нет ответа"})
    logs = env["tmp"] / "logs"
    logs.mkdir()
    monkeypatch.setattr(env["config"], "BASE_DIR", env["tmp"])
    (logs / "backup.state").write_text('{"time": "2026-09-18T03:45:00", "files": 5, "problems": []}', encoding="utf-8")
    (logs / "backup-check.state").write_text('{"time": "2026-09-18T04:30:00", "bad": 1, "lines": ["!! копии на VPS нет"]}', encoding="utf-8")
    (logs / "iskra.state").write_text('{"time": "2026-09-19T07:00:00", "ok": false, "detail": "не оплачено"}', encoding="utf-8")

    c = client(env)
    login(c, "boss", "Boss-password-1")
    r = c.get("/api/admin/status")
    assert r.status_code == 200
    data = r.json()
    ids = [s["id"] for s in data["services"]]
    assert ids == ["portal", *env["config"].SERVICE_IDS]
    assert data["services"][0]["ok"] is True
    assert data["services"][1]["ok"] is False
    assert data["backup"]["files"] == 5
    assert data["backup_check"]["bad"] == 1
    assert data["notifications"] is False
    assert data["iskra"]["ok"] is False and data["iskra"]["detail"] == "не оплачено"


def test_status_survives_missing_files(env, monkeypatch):
    """Ни один сервис ещё не выложен, логов нет — страница всё равно открывается."""
    from app import status
    status._cache.update(time=0, data=None)
    monkeypatch.setattr(status, "_healthz", lambda port: {"ok": False, "ms": None, "error": "нет ответа"})
    monkeypatch.setattr(env["config"], "BASE_DIR", env["tmp"] / "нет-такой-папки")
    c = client(env)
    login(c, "boss", "Boss-password-1")
    data = c.get("/api/admin/status").json()
    assert data["backup"] is None and data["backup_check"] is None
    assert all(s["version"] == "" for s in data["services"])


def test_iskra_usage_counts_by_employee_and_service(env):
    """Расход «Искры» приходит вместе с записью журнала и складывается по людям и сервисам."""
    c = client(env)
    for who, service, tin, tout in [("ivanova", "brief", 1000, 200), ("ivanova", "a3", 300, 100),
                                    ("petrov", "brief", 50, 10)]:
        r = c.post("/internal/audit", json={"service": service, "login": who, "action": "Разбор Искрой",
                                            "object": "справка", "tokens_in": tin, "tokens_out": tout},
                   headers={"X-Portal-Secret": "nginx-secret"})
        assert r.status_code == 200
    c.post("/internal/audit", json={"service": "brief", "login": "ivanova", "action": "Скачан бриф"},
           headers={"X-Portal-Secret": "nginx-secret"})

    login(c, "boss", "Boss-password-1")
    data = c.get("/api/admin/iskra-usage").json()
    assert data["total"]["total"] == 1660 and data["total"]["runs"] == 3   # действие без расхода не считается
    by_login = {r["key"]: r["total"] for r in data["by_login"]}
    assert by_login == {"ivanova": 1600, "petrov": 60}
    by_service = {r["key"]: r["total"] for r in data["by_service"]}
    assert by_service == {"brief": 1260, "a3": 400}


def test_iskra_usage_is_admin_only(env):
    c = client(env)
    login(c, "emp", "Emp-password-1")
    assert c.get("/api/admin/iskra-usage").status_code == 403


def test_iskra_usage_empty_month(env):
    c = client(env)
    login(c, "boss", "Boss-password-1")
    data = c.get("/api/admin/iskra-usage?month=2020-01").json()
    assert data["total"]["total"] == 0 and data["by_login"] == [] and data["months"] == []


def test_ask_requires_login(env):
    c = client(env)
    assert c.post("/api/ask", json={"question": "Что умеет A3?"}).status_code == 401


def test_ask_answers_and_records_spend(env, monkeypatch):
    """Ответ уходит пользователю, а расход «Искры» — в журнал, как у сервисов."""
    from app import ask
    ask.reset_limits_for_tests()
    asked = {}
    monkeypatch.setattr(ask, "ask", lambda q: asked.update(q=q) or
                        {"answer": "«Инфографика A3» — /a3/ — делает лист A3 из справки.",
                         "tokens_in": 900, "tokens_out": 120})
    c = client(env)
    login(c, "emp", "Emp-password-1")
    r = c.post("/api/ask", json={"question": "Как сделать одностраничник?"})
    assert r.status_code == 200 and r.json()["answer"].startswith("«Инфографика A3»")
    assert asked["q"] == "Как сделать одностраничник?"

    rows, _ = env["db"].audit_list(login="emp")
    last = rows[0]
    assert last["action"] == "Вопрос Искре"
    assert last["object"] == "Как сделать одностраничник?"
    assert (last["tokens_in"], last["tokens_out"]) == (900, 120)

    usage = c.get("/api/admin/iskra-usage")     # сотруднику админка закрыта
    assert usage.status_code == 403


def test_ask_explains_when_iskra_is_down(env, monkeypatch):
    """«Искра» падает — портал говорит об этом по-человечески, а не 500."""
    from app import ask
    ask.reset_limits_for_tests()

    def упала(question):
        raise ask.AskError("«Искра» сейчас не отвечает (ошибка 500). Попробуйте позже — сервисы работают.")

    monkeypatch.setattr(ask, "ask", упала)
    c = client(env)
    login(c, "emp", "Emp-password-1")
    r = c.post("/api/ask", json={"question": "Что тут есть?"})
    assert r.status_code == 503 and "не отвечает" in r.json()["detail"]


def test_ask_is_rate_limited(env, monkeypatch):
    """Ключ «Искры» один на всех — один человек не должен его выжечь."""
    from app import ask
    ask.reset_limits_for_tests()
    monkeypatch.setattr(ask, "ask", lambda q: {"answer": "ок", "tokens_in": 1, "tokens_out": 1})
    monkeypatch.setattr(ask, "LIMIT_PER_HOUR", 3)
    c = client(env)
    login(c, "emp", "Emp-password-1")
    codes = [c.post("/api/ask", json={"question": "вопрос"}).status_code for _ in range(4)]
    assert codes == [200, 200, 200, 429]


def test_ask_question_length_is_capped(env):
    c = client(env)
    login(c, "emp", "Emp-password-1")
    assert c.post("/api/ask", json={"question": "я" * 5000}).status_code == 422


def test_search_requires_login(env):
    assert client(env).get("/api/search?q=Удмуртия").status_code == 401


def test_search_asks_only_allowed_services(env, monkeypatch):
    """Портал спрашивает лишь те сервисы, что открыты человеку, и складывает ответы."""
    from app import search
    asked = []

    def fake(service_id, user, query):
        asked.append(service_id)
        return {"service": service_id, "name": service_id, "error": "",
                "items": [{"title": "Удмуртия · бриф", "url": "/brief/#brief=2026", "when": "2026-09-18",
                           "who": "ivanova", "kind": "бриф"}] if service_id == "brief" else []}

    monkeypatch.setattr(search, "_ask_service", fake)
    c = client(env)
    login(c, "emp", "Emp-password-1")          # у сотрудника открыт только «Бриф»
    data = c.get("/api/search?q=Удмуртия").json()
    assert asked == ["brief"]
    assert data["found"] == 1 and data["groups"][0]["items"][0]["url"] == "/brief/#brief=2026"


def test_search_survives_dead_service(env, monkeypatch):
    """Один сервис молчит — остальные результаты всё равно показываем."""
    from app import search
    monkeypatch.setattr(search, "_ask_service",
                        lambda s, u, q: {"service": s, "name": s, "items": [], "error": "не отвечает"})
    c = client(env)
    login(c, "boss", "Boss-password-1")
    data = c.get("/api/search?q=Удмуртия").json()
    assert data["found"] == 0
    assert all(g["error"] == "не отвечает" for g in data["groups"])


def test_search_ignores_foreign_links(env, monkeypatch):
    """Сервис не должен уводить человека на чужой сайт — внешние адреса выбрасываем."""
    from app import search

    class FakeResponse:
        status = 200

        def read(self):
            return b'{"items": [{"title": "\u0447\u0443\u0436\u043e\u0435", "url": "https://evil.example"},' \
                   b' {"title": "\u0441\u0432\u043e\u0451", "url": "/a3/editor?id=1"}]}'

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(search.urllib.request, "urlopen", lambda *a, **kw: FakeResponse())
    group = search._ask_service("a3", {"login": "boss", "role": "admin", "full_name": "Босс"}, "тест")
    assert [i["url"] for i in group["items"]] == ["/a3/editor?id=1"]


def test_search_too_short(env):
    c = client(env)
    login(c, "boss", "Boss-password-1")
    data = c.get("/api/search?q=у").json()
    assert data["short"] is True and data["groups"] == []


def test_recent_work_is_last_touch_per_object(env):
    """«Вы недавно работали»: по одной строке на объект, свежие сверху, только свои сервисы."""
    c = client(env)
    for service, obj, action in [("moderator", "m1 · Совещание по сиротам", "Изменена повестка"),
                                 ("moderator", "m1 · Совещание по сиротам", "Изменён сценарий"),
                                 ("brief", "Удмуртия", "Собран бриф"),
                                 ("a3", "Лист по округам", "сохранён лист A3")]:
        assert c.post("/internal/audit", json={"service": service, "login": "emp", "action": action, "object": obj},
                      headers={"X-Portal-Secret": "nginx-secret"}).status_code == 200

    login(c, "emp", "Emp-password-1")                   # сотруднику открыт только «Бриф»
    items = c.get("/api/me/recent").json()["items"]
    assert [i["object"] for i in items] == ["Удмуртия"]
    assert items[0]["name"] == "Бриф из справки" and items[0]["url"] == "/brief/"

    env["db"].update_user("emp", services=["brief", "moderator", "a3"])
    items = c.get("/api/me/recent").json()["items"]
    assert [i["object"] for i in items] == ["Лист по округам", "Удмуртия", "m1 · Совещание по сиротам"]
    assert items[2]["action"] == "Изменён сценарий"     # последнее действие по объекту, а не первое


def test_recent_work_requires_login(env):
    assert client(env).get("/api/me/recent").status_code == 401


def test_services_carry_quick_action(env):
    c = client(env)
    login(c, "boss", "Boss-password-1")
    services = c.get("/api/me").json()["service_list"]
    assert all(s["new_url"].endswith("#new") for s in services)


def test_summary_is_for_admin_only(env):
    """Роль «руководитель» убрана 19.09: наблюдает администратор, сотруднику сводка закрыта."""
    c = client(env)
    login(c, "emp", "Emp-password-1")
    assert c.get("/api/summary").status_code == 403
    assert c.get("/summary", follow_redirects=False).status_code == 302
    assert set(env["config"].ROLES) == {"admin", "employee"}


def test_summary_counts_documents_not_clicks(env):
    """Десять правок одного совещания — это один документ, а не десять."""
    c = client(env)
    for obj, action in [("m1 · Совещание", "Изменена повестка"), ("m1 · Совещание", "Изменён сценарий"),
                        ("m2 · Другое", "Создано совещание")]:
        c.post("/internal/audit", json={"service": "moderator", "login": "emp", "action": action, "object": obj},
               headers={"X-Portal-Secret": "nginx-secret"})
    c.post("/internal/audit", json={"service": "brief", "login": "boss", "action": "Собран бриф",
                                    "object": "Удмуртия", "tokens_in": 100, "tokens_out": 20},
           headers={"X-Portal-Secret": "nginx-secret"})

    login(c, "boss", "Boss-password-1")
    data = c.get("/api/summary").json()
    assert data["total"] == {"actions": 4, "objects": 3, "people": 2}
    by_service = {x["key"]: x for x in data["by_service"]}
    assert by_service["moderator"]["objects"] == 2 and by_service["moderator"]["actions"] == 3
    assert by_service["brief"]["name"] == "Бриф из справки"
    by_login = {x["key"]: x["objects"] for x in data["by_login"]}
    assert by_login == {"emp": 2, "boss": 1}
    assert data["iskra"]["total"] == 120


def test_summary_is_closed_for_employee(env):
    c = client(env)
    login(c, "emp", "Emp-password-1")
    assert c.get("/api/summary").status_code == 403
    assert c.get("/summary", follow_redirects=False).status_code == 302


def test_iskra_lamp_checks_once_for_everyone(env, monkeypatch):
    """Лампочка «Искра на связи»: настоящая проверка одна на всех, не на каждое открытие страницы."""
    from app import ask
    ask.reset_health_for_tests()
    calls = []
    monkeypatch.setattr(ask, "ping", lambda: (calls.append(1), (True, "отвечает"))[1])
    monkeypatch.setattr(env["config"], "BASE_DIR", env["tmp"] / "нет-логов")

    c = client(env)
    login(c, "emp", "Emp-password-1")
    first = c.get("/api/iskra").json()
    assert first["ok"] is True and first["source"] == "проверено сейчас"
    for _ in range(5):
        c.get("/api/iskra")
    assert len(calls) == 1                      # остальные страницы взяли готовый ответ


def test_iskra_lamp_uses_watchdog_check_when_it_is_fresh(env, monkeypatch):
    """Сторож проверил только что — своей проверкой не тратимся."""
    import json
    from datetime import datetime
    from app import ask
    ask.reset_health_for_tests()
    monkeypatch.setattr(ask, "ping", lambda: (_ for _ in ()).throw(AssertionError("лишняя проверка")))
    logs = env["tmp"] / "logs"
    logs.mkdir(exist_ok=True)
    (logs / "iskra.state").write_text(json.dumps(
        {"time": datetime.now().astimezone().isoformat(timespec="seconds"), "ok": False, "detail": "не оплачено"}),
        encoding="utf-8")
    monkeypatch.setattr(env["config"], "BASE_DIR", env["tmp"])

    c = client(env)
    login(c, "emp", "Emp-password-1")
    state = c.get("/api/iskra").json()
    assert state["ok"] is False and state["detail"] == "не оплачено" and state["source"] == "сторож"


def test_iskra_lamp_learns_from_real_question(env, monkeypatch):
    """Отказ на настоящем вопросе сразу гасит лампочку — это точнее любой проверки."""
    from app import ask
    ask.reset_health_for_tests()
    ask.reset_limits_for_tests()
    monkeypatch.setattr(ask, "ping", lambda: (True, "отвечает"))
    monkeypatch.setattr(ask, "ask", lambda q: (_ for _ in ()).throw(ask.AskError("«Искра» сейчас не отвечает")))

    c = client(env)
    login(c, "emp", "Emp-password-1")
    assert c.post("/api/ask", json={"question": "что тут есть"}).status_code == 503
    state = c.get("/api/iskra").json()
    assert state["ok"] is False and "не отвечает" in state["detail"]


def test_iskra_lamp_requires_login(env):
    assert client(env).get("/api/iskra").status_code == 401


def test_iskra_ping_uses_short_timeout_not_shared_pool(env):
    """СРЕ 28.09, P1 (продолжение): живой пинг лампочки идёт из общего пула потоков Starlette,
    тем же путём, что и /internal/auth. Неотвечающая cloud.iskrabot.ru держала поток портала
    секундами на общем 25-секундном сроке — короткий срок нужен здесь так же, как у виджетов
    главной (test_widgets_use_short_timeout_not_shared_pool)."""
    from app import ask
    # 05.10: запрос страницы держится не дольше PING_WAIT, сама Искра ждётся в фоне до PING_TIMEOUT
    # (с VPS живой ответ идёт ~10 с — при потолке 10 лампочка гасла при живой Искре).
    assert ask.PING_WAIT <= 3
    assert ask.PING_TIMEOUT <= 30
    assert ask.PING_TIMEOUT < ask.TIMEOUT      # настоящий вопрос человека таймаут не сократили


def test_iskra_lamp_slow_iskra_is_pending_then_on(env, monkeypatch):
    """05.10: Искра отвечает дольше PING_WAIT — страница получает «проверяем…», а не красную лампочку;
    следующий опрос видит готовый итог «на связи» с пометкой о медленном ответе."""
    import time
    from app import ask
    ask.reset_health_for_tests()
    monkeypatch.setattr(ask, "PING_WAIT", 0.2)
    monkeypatch.setattr(ask, "PING_SLOW", 0.3)
    monkeypatch.setattr(env["config"], "BASE_DIR", env["tmp"] / "нет-логов")
    calls = []
    def _slow():
        calls.append(1); time.sleep(0.6); return True, "отвечает"
    monkeypatch.setattr(ask, "ping", _slow)
    c = client(env)
    login(c, "emp", "Emp-password-1")
    first = c.get("/api/iskra").json()
    assert first["pending"] is True and first["ok"] is False and first["checked"] == ""
    second = c.get("/api/iskra").json()          # проверка ещё идёт — новую не запускаем
    assert second["pending"] is True and len(calls) == 1
    time.sleep(0.7)
    third = c.get("/api/iskra").json()
    assert third["pending"] is False and third["ok"] is True and "медленно" in third["detail"]
    assert third["source"] == "проверено сейчас" and len(calls) == 1


def test_iskra_lamp_one_check_under_simultaneous_requests(env, monkeypatch):
    """Ревьюер RC-2026-10-05-3, Н-1: запросы в одно мгновение запускают одну проверку, не несколько."""
    import threading, time
    from app import ask
    ask.reset_health_for_tests()
    monkeypatch.setattr(ask, "PING_WAIT", 0.05)
    monkeypatch.setattr(env["config"], "BASE_DIR", env["tmp"] / "нет-логов")
    calls = []
    def _slow():
        calls.append(1); time.sleep(0.4); return True, "отвечает"
    monkeypatch.setattr(ask, "ping", _slow)
    barrier = threading.Barrier(12)
    def go():
        barrier.wait(); ask.health()
    ts = [threading.Thread(target=go) for _ in range(12)]
    for t in ts: t.start()
    for t in ts: t.join()
    time.sleep(0.5)
    assert len(calls) == 1 and ask.health()["ok"] is True


def test_iskra_lamp_late_check_does_not_override_fresh_live_answer(env, monkeypatch):
    """Ревьюер RC-2026-10-05-3, Н-2: пока шла проверка, живой ответ человека обновил лампочку —
    поздний (и худший) итог проверки его не затирает."""
    import time
    from app import ask
    ask.reset_health_for_tests()
    monkeypatch.setattr(ask, "PING_WAIT", 0.05)
    monkeypatch.setattr(env["config"], "BASE_DIR", env["tmp"] / "нет-логов")
    monkeypatch.setattr(ask, "ping", lambda: (time.sleep(0.4), (False, "нет ответа за отведённый срок"))[1])
    assert ask.health()["pending"] is True
    ask.note_result(True, "отвечает")
    time.sleep(0.5)
    state = ask.health()
    assert state["ok"] is True and state["detail"] == "отвечает" and state["pending"] is False


def auth(c, service, method, uri):
    return c.get(f"/auth/check/{service}", headers={"X-Original-Method": method, "X-Original-URI": uri}).status_code


def test_admin_does_not_create_or_edit_documents(env):
    """Админ администрирует: документы создаются и правятся только под личными учётками."""
    c = client(env)
    login(c, "boss", "Boss-password-1")
    assert auth(c, "moderator", "GET", "/moderator/api/meetings") == 200        # смотреть можно
    assert auth(c, "moderator", "POST", "/moderator/api/meetings") == 403       # создавать — нет
    assert auth(c, "moderator", "PUT", "/moderator/api/meetings/m1/header") == 403
    assert auth(c, "a3", "PUT", "/a3/api/sheets/3") == 403
    assert auth(c, "brief", "POST", "/brief/api/upload") == 403


def test_admin_keeps_irreversible_actions_and_settings(env):
    """Необратимое и настройки по контракту делает только администратор — это у него остаётся."""
    c = client(env)
    login(c, "boss", "Boss-password-1")
    assert auth(c, "moderator", "DELETE", "/moderator/api/meetings/m1/permanent") == 200
    assert auth(c, "a3", "DELETE", "/a3/api/sheets/12") == 200
    assert auth(c, "brief", "POST", "/brief/api/purge") == 200
    assert auth(c, "brief", "POST", "/brief/api/settings") == 200
    assert auth(c, "a3", "DELETE", "/a3/api/sheets/12/versions/2") == 403        # не из списка — нельзя


def test_employee_works_as_before(env):
    """Сотрудники равноправны: у них ничего не убавилось."""
    c = client(env)
    login(c, "emp", "Emp-password-1")
    assert auth(c, "brief", "POST", "/brief/api/upload") == 200
    assert auth(c, "brief", "PUT", "/brief/api/rebuild") == 200


def test_manager_chain_and_team(env):
    """Круг показа: под человеком видны его люди и люди его людей, но не глубже трёх уровней."""
    db, auth_ = env["db"], env["auth"]
    for who in ("nachalnik", "otdel", "spec"):
        db.create_user(who, auth_.hash_password(f"{who.title()}-password-1"), full_name=who, must_change_password=False)
    c = client(env)
    login(c, "boss", "Boss-password-1")

    assert c.patch("/api/admin/users/otdel", json={"manager": "nachalnik"}).status_code == 200
    assert c.patch("/api/admin/users/spec", json={"manager": "otdel"}).status_code == 200
    assert db.team_of("nachalnik") == ["otdel", "spec"]      # свои и своих людей
    assert db.team_of("otdel") == ["spec"]
    assert db.team_of("spec") == []

    # четвёртый уровень не заводим
    db.create_user("chetvertyi", auth_.hash_password("Chetvertyi-password-1"), must_change_password=False)
    r = c.patch("/api/admin/users/chetvertyi", json={"manager": "spec"})
    assert r.status_code == 400 and "цепочка" in r.json()["detail"]

    # кольцо тоже не заводим
    r = c.patch("/api/admin/users/nachalnik", json={"manager": "spec"})
    assert r.status_code == 400 and "кольцо" in r.json()["detail"]

    r = c.patch("/api/admin/users/spec", json={"manager": "spec"})
    assert r.status_code == 400 and "сам себе" in r.json()["detail"]


def test_team_goes_to_services(env):
    """Сервисы узнают круг из заголовка: у администратора звёздочка, у сотрудника — его люди."""
    db, auth_ = env["db"], env["auth"]
    db.create_user("pod", auth_.hash_password("Pod-password-1"), services=["brief"], must_change_password=False)
    db.update_user("pod", manager="emp")

    c = client(env)
    login(c, "emp", "Emp-password-1")
    assert c.get("/auth/check/brief").headers["X-Portal-Team"] == "pod"

    c2 = client(env)
    login(c2, "boss", "Boss-password-1")
    assert c2.get("/auth/check/brief").headers["X-Portal-Team"] == "*"


def test_services_get_people_list_with_secret(env):
    """Список сотрудников для окна «Доступ»: только по секрету и без лишнего."""
    c = client(env)
    assert c.get("/internal/users").status_code == 403
    r = c.get("/internal/users", headers={"X-Portal-Secret": "nginx-secret"})
    assert r.status_code == 200
    people = r.json()["users"]
    assert {"login", "name", "active", "manager"} == set(people[0])
    assert all("password_hash" not in p and "totp_secret" not in p for p in people)


def test_new_user_can_get_manager_at_once(env):
    c = client(env)
    login(c, "boss", "Boss-password-1")
    r = c.post("/api/admin/users", json={"login": "novyi", "full_name": "Новый", "password": "Vremenno-2026-x",
                                          "role": "employee", "services": ["brief"], "manager": "emp"})
    assert r.status_code == 200
    assert env["db"].get_user("novyi")["manager"] == "emp"

    r = c.post("/api/admin/users", json={"login": "krivoi", "password": "Vremenno-2026-y",
                                          "manager": "net-takogo"})
    assert r.status_code == 400
    assert env["db"].get_user("krivoi") is None       # полусозданного человека не остаётся


def test_denial_says_why(env):
    """Отказ несёт причину: nginx подставляет по ней текст, человек понимает, что произошло."""
    c = client(env)
    login(c, "boss", "Boss-password-1")
    r = c.get("/auth/check/moderator", headers={"X-Original-Method": "POST",
                                                 "X-Original-URI": "/moderator/api/meetings"})
    assert r.status_code == 403 and r.headers["X-Deny-Reason"] == "admin-readonly"

    c2 = client(env)
    login(c2, "emp", "Emp-password-1")          # сотруднику открыт только «Бриф»
    r2 = c2.get("/auth/check/a3")
    assert r2.status_code == 403 and r2.headers["X-Deny-Reason"] == "no-access"


def test_search_does_not_show_one_meeting_twice(env, monkeypatch):
    """Повестка уехала в «Модератор» — в поиске одна строка с двумя дверями, а не два дубля.
    (Сервис снят с портала 05.10 — для проверки склейки он подставляется в список обратно.)"""
    from app import search
    from app import config
    monkeypatch.setattr(config, "SERVICES", [*config.SERVICES, {"id": "agenda", "name": "Повестка и участники", "url": "/agenda/"}])
    monkeypatch.setattr(config, "SERVICE_IDS", [*config.SERVICE_IDS, "agenda"])

    def fake(service_id, user, query):
        if service_id == "agenda":
            return {"service": "agenda", "name": "Повестка и участники", "error": "", "items": [
                {"title": "Отопительный сезон", "url": "/agenda/#meeting=a1", "when": "", "who": "", "kind": "совещание",
                 "same_as": {"service": "moderator", "id": "m1"}}]}
        if service_id == "moderator":
            return {"service": "moderator", "name": "Модератор", "error": "", "items": [
                {"title": "Отопительный сезон", "url": "/moderator/#meeting=m1", "when": "", "who": "", "kind": "совещание"}]}
        return {"service": service_id, "name": service_id, "error": "", "items": []}

    monkeypatch.setattr(search, "_ask_service", fake)
    c = client(env)
    login(c, "boss", "Boss-password-1")
    data = c.get("/api/search?q=отопительн").json()
    rows = {g["service"]: g["items"] for g in data["groups"]}
    assert rows["agenda"] == []                                   # дубль убран
    assert rows["moderator"][0]["also"]["url"] == "/agenda/#meeting=a1"
    assert data["found"] == 1


def test_search_keeps_single_row_when_twin_not_found(env, monkeypatch):
    """Напарник не попал в выдачу — строку не теряем, но говорим, где ещё лежит документ."""
    from app import search
    from app import config
    monkeypatch.setattr(config, "SERVICES", [*config.SERVICES, {"id": "agenda", "name": "Повестка и участники", "url": "/agenda/"}])
    monkeypatch.setattr(config, "SERVICE_IDS", [*config.SERVICE_IDS, "agenda"])
    monkeypatch.setattr(search, "_ask_service", lambda s, u, q: {
        "service": s, "name": s, "error": "", "items": [
            {"title": "Отопительный сезон", "url": "/agenda/#meeting=a1", "when": "", "who": "", "kind": "совещание",
             "same_as": {"service": "moderator", "id": "m1"}}] if s == "agenda" else []})
    c = client(env)
    login(c, "boss", "Boss-password-1")
    data = c.get("/api/search?q=отопительн").json()
    agenda = next(g for g in data["groups"] if g["service"] == "agenda")
    assert agenda["items"][0]["also"]["url"] == "/moderator/#meeting=m1"


def test_package_collects_what_it_can_and_names_the_rest(env, monkeypatch):
    """Пакет к совещанию: что нашлось — в архив, чего нет — в записку внутри."""
    import io
    import zipfile
    from app import package

    def fake_ask(service_id, path, user):
        if service_id == "moderator" and path.endswith("/export"):
            return 200, b"<html>brief</html>", "text/html"
        if service_id == "agenda" and path.endswith("agenda.docx"):
            return 200, b"PK-agenda", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        return 404, b"", ""

    def fake_json(service_id, path, user):
        return {"meeting": {"title": "Отопительный сезон", "date": "15.10.2026",
                            "source": {"service": "agenda", "meeting_id": "a1"},
                            "materials": [{"title": "Бриф: Удмуртия",
                                           "source": {"kind": "бриф", "url": "/brief/#brief=x"}}]}}

    monkeypatch.setattr(package, "_ask", fake_ask)
    monkeypatch.setattr(package, "ask_json", fake_json)

    c = client(env)
    login(c, "boss", "Boss-password-1")
    r = c.get("/api/package/m1")
    assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
    names = zipfile.ZipFile(io.BytesIO(r.content)).namelist()
    assert "1. Бриф совещания.html" in names
    assert "2. Повестка.docx" in names
    assert "4. Материалы — список.txt" in names
    assert "Чего не хватает.txt" in names          # список участников не отдался — сказали об этом

    rows, _ = env["db"].audit_list(login="boss")
    assert rows[0]["action"] == "Скачан пакет к совещанию"


def test_package_needs_moderator_access(env):
    c = client(env)
    login(c, "emp", "Emp-password-1")             # у сотрудника открыт только «Бриф»
    assert c.get("/api/package/m1").status_code == 403


def test_orders_page_and_api_need_moderator(env, monkeypatch):
    """Реестр поручений открыт тем, кому открыт «Модератор»; портал только пересказывает его ответ."""
    from app import package, main
    monkeypatch.setattr(package, "ask_json", lambda service, path, user, timeout=None: {"orders": [
        {"text": "Подготовить свод", "assignee": "Иванов", "deadline": "01.01.2020", "deadline_iso": "2020-01-01",
         "overdue": True, "meeting_title": "Совещание", "meeting_date": "01.09.2026", "url": "/moderator/#meeting=m1",
         "agenda_num": "1", "archived": False}]})
    monkeypatch.setattr(main, "package", package)

    c = client(env)
    login(c, "emp", "Emp-password-1")                 # только «Бриф»
    assert c.get("/api/orders").status_code == 403
    assert c.get("/orders", follow_redirects=False).status_code == 302

    env["db"].update_user("emp", services=["brief", "moderator"])
    data = c.get("/api/orders").json()
    assert data["orders"][0]["overdue"] is True
    assert c.get("/orders").status_code == 200


def test_orders_survive_silent_moderator(env, monkeypatch):
    from app import package
    monkeypatch.setattr(package, "ask_json", lambda service, path, user, timeout=None: None)
    c = client(env)
    login(c, "boss", "Boss-password-1")
    r = c.get("/api/orders")
    assert r.status_code == 503 and "не ответил" in r.json()["detail"]


def test_shared_iskra_reads_trouble_and_usage():
    """Общий разговор с «Искрой»: причина отказа и расход считаются одинаково для всех сервисов."""
    import sys
    from pathlib import Path as _P
    sys.path.insert(0, str(_P(__file__).resolve().parents[1] / "shared"))
    import iskra_common as ik

    run = ik.unwrap({"run": {"status": "failed", "error": {"code": "internal", "message": "run failed"},
                             "events": [{"kind": "run_started"}, {"kind": "failed", "message": "агент недоступен"}],
                             "usage": {"prompt_tokens": 900, "completion_tokens": 120}}})
    assert ik.is_failed(run) and not ik.is_done(run)
    assert "run failed" in ik.trouble(run) and "агент недоступен" in ik.trouble(run)
    assert ik.usage(run) == {"in": 900, "out": 120, "total": 1020}
    assert ik.usage({"result": {"usage": {"input_tokens": 5, "output_tokens": 2, "total_tokens": 7}}})["total"] == 7
    assert "подписка" in ik.human_error(402)
    assert "не отвечает" in ik.human_error(500)


def test_shared_numbers_check_source():
    """Общая сверка чисел: запятая и пробелы не мешают, противоречие источника видно."""
    import sys
    from pathlib import Path as _P
    sys.path.insert(0, str(_P(__file__).resolve().parents[1] / "shared"))
    import numbers_common as nc

    source = "Обеспечено 62,4 % детей-сирот, всего 1 234 человека. Доля исполнения 146 %."
    assert nc.found_in_source("62.4", source)          # в ответе точка, в справке запятая
    assert nc.found_in_source("1234", source)          # в справке число с пробелом
    assert not nc.found_in_source("99", source)
    assert [c["kind"] for c in nc.conflicts_in(source)] == ["доля вне 0–100"]


# ── ключ приёмной: документы с телефона ─────────────────────────────────

def test_inbox_key_issued_and_resolved(env):
    """Сотруднику выдаётся ключ, сервис по нему узнаёт человека."""
    env["db"].update_user("emp", services=["brief", "materials"])
    c = client(env)
    login(c, "emp", "Emp-password-1")
    assert c.get("/api/me/inbox-key").json()["key"] == ""
    key = c.post("/api/me/inbox-key").json()["key"]
    assert len(key) >= 32
    assert c.get("/api/me/inbox-key").json()["key"] == key

    who = TestClient(env["app"], base_url="https://workspace.example.ru").get(
        "/internal/inbox-user", params={"key": key}, headers={"X-Portal-Secret": "nginx-secret"})
    assert who.status_code == 200 and who.json()["login"] == "emp"
    assert "Выдан ключ приёмной" in [a["action"] for a in env["db"].audit_list()[0]]


def test_inbox_key_needs_service_access(env):
    """Ключ без доступа к «Материалам» не работает: путь идёт мимо проверки nginx,
    поэтому доступ проверяется здесь."""
    env["db"].update_user("emp", services=["brief", "materials"])
    c = client(env)
    login(c, "emp", "Emp-password-1")
    key = c.post("/api/me/inbox-key").json()["key"]
    env["db"].update_user("emp", services=["brief"])
    inner = TestClient(env["app"], base_url="https://workspace.example.ru")
    assert inner.get("/internal/inbox-user", params={"key": key},
                     headers={"X-Portal-Secret": "nginx-secret"}).status_code == 404


def test_inbox_key_revoked_and_admin_refused(env):
    env["db"].update_user("emp", services=["materials"])
    c = client(env)
    login(c, "emp", "Emp-password-1")
    key = c.post("/api/me/inbox-key").json()["key"]
    c.delete("/api/me/inbox-key")
    inner = TestClient(env["app"], base_url="https://workspace.example.ru")
    assert inner.get("/internal/inbox-user", params={"key": key},
                     headers={"X-Portal-Secret": "nginx-secret"}).status_code == 404

    # администратор документы не ведёт — складывать ему некуда
    a = client(env)
    login(a, "boss", "Boss-password-1")
    assert a.post("/api/me/inbox-key").status_code == 403


def test_inbox_user_needs_shared_secret(env):
    env["db"].update_user("emp", services=["materials"])
    c = client(env)
    login(c, "emp", "Emp-password-1")
    key = c.post("/api/me/inbox-key").json()["key"]
    inner = TestClient(env["app"], base_url="https://workspace.example.ru")
    assert inner.get("/internal/inbox-user", params={"key": key}).status_code == 403
    assert inner.get("/internal/inbox-user", params={"key": key},
                     headers={"X-Portal-Secret": "wrong-secret"}).status_code == 403


def test_waiting_count_without_materials_access(env):
    """Нет доступа к «Материалам» — значка нет и сервис не дёргаем."""
    c = client(env)
    login(c, "emp", "Emp-password-1")
    assert c.get("/api/waiting-count").json() == {"count": 0}


def test_waiting_count_asks_service(env, monkeypatch):
    env["db"].update_user("emp", services=["materials"])
    from app import package
    monkeypatch.setattr(package, "ask_json",
                        lambda service, path, user, timeout=None: {"items": [{"name": "а"}, {"name": "б"}]})
    c = client(env)
    login(c, "emp", "Emp-password-1")
    assert c.get("/api/waiting-count").json()["count"] == 2


def test_waiting_count_survives_silent_service(env, monkeypatch):
    """Сервис молчит — главная не ломается, значка просто нет."""
    env["db"].update_user("emp", services=["materials"])
    from app import package
    monkeypatch.setattr(package, "ask_json", lambda service, path, user, timeout=None: None)
    c = client(env)
    login(c, "emp", "Emp-password-1")
    assert c.get("/api/waiting-count").json()["count"] == 0


def test_widgets_use_short_timeout_not_shared_pool(env, monkeypatch):
    """СРЕ 28.09, П1: сосед под нагрузкой держал поток портала до 30 с (общий `TIMEOUT`),
    а тот же пул потоков ждёт и auth_request на весь портал. Лёгкие виджеты (значок «Приёмной»,
    ближайшие совещания, поручения) должны звать соседа с коротким сроком, а не общим."""
    env["db"].update_user("emp", services=["materials", "moderator"])
    from app import package
    seen = []
    monkeypatch.setattr(package, "ask_json",
                        lambda service, path, user, timeout=None: seen.append(timeout) or None)
    c = client(env)
    login(c, "emp", "Emp-password-1")
    c.get("/api/waiting-count")
    c.get("/api/upcoming")
    c.get("/api/orders")
    assert seen == [3, 3, 3]
    assert all(t < package.TIMEOUT for t in seen)


def test_admin_may_delete_in_materials(env):
    """Необратимые действия «Материалов» администратору открыты: до 20.09 список путей
    не совпадал с сервисом, и стереть не мог никто."""
    from app import config
    import re as _re
    paths = config.ADMIN_WRITE_PATHS["materials"]
    for method, path in (("DELETE", "/api/events/abc123"),
                         ("DELETE", "/api/events/abc123/archive/Справка.docx"),
                         ("DELETE", "/api/waiting/Справка.docx")):
        assert any(m == method and _re.fullmatch(p, path) for m, p in paths), path


def test_admin_may_not_write_documents_in_materials(env):
    """А вот заводить и править документы администратору по-прежнему нельзя."""
    from app import config
    import re as _re
    paths = config.ADMIN_WRITE_PATHS["materials"]
    for method, path in (("POST", "/api/events"),
                         ("POST", "/api/events/abc123/files"),
                         ("POST", "/api/events/abc123/arrange")):
        assert not any(m == method and _re.fullmatch(p, path) for m, p in paths), path


# ── кто я в пространстве: должность и лестница подчинения ───────────────

def test_position_is_written_by_the_person(env):
    c = client(env)
    login(c, "emp", "Emp-password-1")
    r = c.post("/api/me/profile", json={"full_name": "Сотрудникова Анна Петровна",
                                        "position": "начальник отдела общего образования"})
    assert r.status_code == 200
    me = c.get("/api/me").json()
    assert me["full_name"] == "Сотрудникова Анна Петровна"
    assert me["position"] == "начальник отдела общего образования"
    assert "Изменён свой профиль" in [a["action"] for a in env["db"].audit_list()[0]]


def test_profile_needs_a_name(env):
    c = client(env)
    login(c, "emp", "Emp-password-1")
    assert c.post("/api/me/profile", json={"full_name": "  ", "position": "кто-то"}).status_code == 400


def test_ladder_shows_level_and_manager(env):
    """Три уровня: без руководителя — первый, под ним второй, под тем третий."""
    db, auth = env["db"], env["auth"]
    db.create_user("chief1", auth.hash_password("Chief-password-1"), full_name="Первый Первый",
                   must_change_password=False)
    db.update_user("emp", manager="chief1", position="специалист")
    db.create_user("junior", auth.hash_password("Junior-password-1"), full_name="Третий Третий",
                   must_change_password=False)
    db.update_user("junior", manager="emp")

    c = client(env)
    login(c, "emp", "Emp-password-1")
    place = c.get("/api/me").json()["place"]
    assert place["level"] == 2 and place["levels"] == 3
    assert place["manager"]["name"] == "Первый Первый"
    assert [p["name"] for p in place["direct"]] == ["Третий Третий"]

    top = client(env)
    login(top, "chief1", "Chief-password-1")
    place_top = top.get("/api/me").json()["place"]
    assert place_top["level"] == 1 and place_top["manager"] is None
    assert place_top["team_size"] == 2          # сотрудник и его подчинённый


# ── короткое название совещания: одно правило на всё пространство ───────

def test_short_title_drops_the_host_and_keeps_the_topic():
    """Все совещания у одного человека (решение 21.09) — отличает их только тема."""
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "shared"))
    from meeting_title import short_title as s
    assert s("Совещание у Заместителя Председателя Правительства Российской Федерации "
             "Д.Н. Чернышенко по вопросам обеспечения жильем детей-сирот") == "Обеспечение жильем детей-сирот"
    assert s("Совещание по вопросам подготовки к Дню учителя Российской Федерации") == "Подготовка к Дню учителя РФ"
    assert s("Совещание по вопросам строительства школ в Республике Тыва") == "Строительство школ в Республике Тыва"


def test_short_title_does_not_mangle_unknown_words():
    """Незнакомое первое слово в именительный не ставим: лучше длиннее, чем с висящим падежом."""
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "shared"))
    from meeting_title import short_title as s
    assert s("Совещание у Министра Кравцова С.С. по вопросу итогового сочинения") == "По вопросу итогового сочинения"
    assert s("Рабочая встреча с Музаевым А.А.") == "Рабочая встреча с Музаевым А.А."


# ── реестр людей: один на всё пространство ──────────────────────────────

def _inner(env):
    return TestClient(env["app"], base_url="https://workspace.example.ru")


def test_people_hand_edit_beats_guess_and_guess_is_kept(env):
    """Рука главнее разбора, а расходящаяся догадка не теряется, а ложится в варианты."""
    inner, H = _inner(env), {"X-Portal-Secret": "nginx-secret"}
    inner.post("/internal/people", headers=H, json={
        "name": "Кравцов С.С.", "role": "Министр просвещения", "organization": "Минпросвещения России",
        "force": True, "login": "emp", "source": "agenda"})
    r = inner.post("/internal/people", headers=H, json={
        "name": "кравцов  с.с.", "role": "министр", "login": "emp", "source": "moderator"})
    assert r.json()["summary"] == ""                       # выверенное не переписано
    card = inner.get("/internal/people/one", headers=H, params={"name": "Кравцов С.С."}).json()
    assert card["role"] == "Министр просвещения"
    assert [v["role"] for v in card["variants"]] == ["министр"]


def test_people_guess_only_suggests(env):
    """Решение 21.09: разбор реестр не меняет, только предлагает; записывает человек."""
    inner, H = _inner(env), {"X-Portal-Secret": "nginx-secret"}
    r = inner.post("/internal/people", headers=H, json={"name": "Музаев А.А.", "role": "руководитель", "source": "brief"})
    assert r.json() == {"summary": "", "suggest": "new"}
    assert inner.get("/internal/people/one", headers=H, params={"name": "Музаев А.А."}).status_code == 404
    # подтверждение без логина не принимается: у записи должен быть ответственный
    assert inner.post("/internal/people", headers=H, json={"name": "Музаев А.А.", "role": "руководитель",
                                                           "force": True}).status_code == 400
    inner.post("/internal/people", headers=H, json={"name": "Музаев А.А.", "organization": "Рособрнадзор",
                                                    "force": True, "login": "emp", "document": "Список участников 23.09"})
    r = inner.post("/internal/people", headers=H, json={"name": "Музаев А.А.", "role": "руководитель", "source": "brief"})
    assert r.json()["suggest"] == "differs"
    card = inner.get("/internal/people/one", headers=H, params={"name": "Музаев А.А."}).json()
    assert card["role"] == "" and card["variants"][0]["role"] == "руководитель"
    assert card["updated_by"] == "emp" and card["document"] == "Список участников 23.09"


def test_people_forget_is_admin_only(env):
    inner, H = _inner(env), {"X-Portal-Secret": "nginx-secret"}
    inner.post("/internal/people", headers=H, json={"name": "Лишний Л.Л.", "role": "никто", "force": True, "login": "emp"})
    assert inner.post("/internal/people/forget", headers=H, json={"name": "Лишний Л.Л.", "login": "emp"}).status_code == 403
    assert inner.post("/internal/people/forget", headers=H, json={"name": "Лишний Л.Л.", "login": "boss"}).json()["forgotten"]


def test_people_check_catches_a_typo_in_the_surname(env):
    """«Кадошников» вместо «Кадочников»: опечатка разбора должна всплыть до вывода в документ."""
    inner, H = _inner(env), {"X-Portal-Secret": "nginx-secret"}
    inner.post("/internal/people", headers=H, json={"name": "Кадочников Павел Анатольевич",
                                                    "role": "Заместитель Министра финансов", "force": True, "login": "emp"})
    items = inner.post("/internal/people/check", headers=H, json={"names": [
        "Кадошников П.А.", "Кадочников П.А.", "Кадочников Павел Анатольевич", "Иванов И.И."]}).json()["items"]
    assert [i["status"] for i in items] == ["close", "exact", "exact", "unknown"]
    assert items[0]["suggestions"][0]["name"] == "Кадочников Павел Анатольевич"


def test_people_check_offers_full_record_for_a_short_name(env):
    """Разбор со слов пишет докладчика коротко — «Кадочников» или «Кадочников Павел» (просьба
    пользователя 02.10). Это не опечатка и не новый человек: полная запись из реестра — как close,
    чтобы «Совещание» заменило её одним нажатием вместе с должностью. Другая фамилия и другое имя
    при той же фамилии — по-прежнему unknown."""
    inner, H = _inner(env), {"X-Portal-Secret": "nginx-secret"}
    inner.post("/internal/people", headers=H, json={"name": "Кадочников Павел Анатольевич",
                                                    "role": "Заместитель Министра финансов", "force": True, "login": "emp"})
    inner.post("/internal/people", headers=H, json={"name": "Ли Ен Сун", "role": "Эксперт", "force": True, "login": "emp"})
    items = inner.post("/internal/people/check", headers=H, json={"names": [
        "Кадочников", "Кадочников Павел", "Кадочников Пётр", "Кадочников П.", "Ли"]}).json()["items"]
    # «Ли» при записи «Ли Ен Сун» — unknown: фамилии короче трёх букв коротко не сопоставляем
    assert [i["status"] for i in items] == ["close", "close", "unknown", "close", "unknown"]
    for i in (0, 1, 3):
        assert [x["name"] for x in items[i]["suggestions"]] == ["Кадочников Павел Анатольевич"]
        assert items[i]["suggestions"][0]["role"] == "Заместитель Министра финансов"


def test_people_duplicates_are_found_and_merged_by_admin(env):
    inner, H = _inner(env), {"X-Portal-Secret": "nginx-secret"}
    for n in ("Кадочников Павел Анатольевич", "Кадошников Павел Анатольевич"):
        inner.post("/internal/people", headers=H, json={"name": n, "role": "Заместитель Министра финансов", "force": True, "login": "emp"})
    e = client(env)
    login(e, "emp", "Emp-password-1")
    assert len(e.get("/api/people/duplicates").json()["items"]) == 1
    assert e.post("/api/people/merge", json={"keep": "Кадочников Павел Анатольевич",
                                             "drop": "Кадошников Павел Анатольевич"}).status_code == 403
    a = client(env)
    login(a, "boss", "Boss-password-1")
    assert a.post("/api/people/merge", json={"keep": "Кадочников Павел Анатольевич",
                                             "drop": "Кадошников Павел Анатольевич"}).status_code == 200
    assert a.get("/api/people/duplicates").json()["items"] == []
    assert "Реестр людей: записи объединены" in [x["action"] for x in env["db"].audit_list()[0]]


# ── склонение ФИО: одно правило на всё пространство ─────────────────────

def _names():
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "shared"))
    import names_common
    return names_common


def test_names_fleeting_vowel():
    """«Павела» из документа ведущего: у Павла, Льва и Петра гласная выпадает."""
    n = _names()
    assert n.accusative("Кадочников Павел Анатольевич") == "Кадочникова Павла Анатольевича"
    assert n.accusative("Толстой Лев Николаевич") == "Толстого Льва Николаевича"
    assert n.genitive("Иванов Пётр Сергеевич") == "Иванова Петра Сергеевича"


def test_names_female_and_indeclinable():
    n = _names()
    assert n.accusative("Иванова Ольга Петровна") == "Иванову Ольгу Петровну"
    assert n.genitive("Смирнова Любовь Андреевна") == "Смирновой Любови Андреевны"
    assert n.accusative("Смирнова Любовь Андреевна") == "Смирнову Любовь Андреевну"
    assert n.genitive("Шевченко Илья Игоревич") == "Шевченко Ильи Игоревича"
    assert n.accusative("Шмидт Анна Викторовна") == "Шмидт Анну Викторовну"


def test_names_short_form_keeps_initials():
    n = _names()
    assert n.genitive("Кадочников П.А.") == "Кадочникова П.А."


def test_iskra_key_odd_service_is_marked_and_spread_fixes_it(env, tmp_path, monkeypatch):
    """21.09: у «Брифа» оказался свой ключ, введённый в обход портала. Выбившийся отмечается,
    а «разослать ключ портала» сводит всех к одному, не требуя вводить ключ заново."""
    import json
    from app import config, iskra_key
    brief = tmp_path / "brief"; brief.mkdir()
    (brief / "settings.json").write_text(json.dumps({"iskra_key": "brief-own-key-000"}), encoding="utf-8")
    env_path = tmp_path / "portal.env"
    env_path.write_text("ISKRA_API_KEY=portal-key-111111\nBRIEF_HOME=" + str(brief) + "\n", encoding="utf-8")
    monkeypatch.setattr(config, "ENV_PATH", env_path)
    monkeypatch.setattr(iskra_key, "KEY_TARGETS", [("Бриф", "BRIEF_HOME", "settings.json")])
    st = iskra_key.status()
    assert st["same"] is False
    assert [t["service"] for t in st["targets"] if t["differs"]] == ["Бриф"]
    a = client(env)
    login(a, "boss", "Boss-password-1")
    r = a.post("/api/admin/iskra-key/spread")
    assert r.status_code == 200 and r.json()["same"] is True
    assert json.loads((brief / "settings.json").read_text(encoding="utf-8"))["iskra_key"] == "portal-key-111111"


def _fake_smtp():
    """Крошечный SMTP-сервер в потоке: принимает одно письмо без TLS и отдаёт его текст."""
    import socket
    import threading
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    got = {}

    def run():
        s, _ = srv.accept()
        f = s.makefile("rwb")
        def say(line):
            f.write(line.encode() + b"\r\n"); f.flush()
        say("220 fake")
        data = False
        body = []
        for raw in f:
            line = raw.decode("utf-8", "replace").rstrip("\r\n")
            if data:
                if line == ".":
                    data = False; got["msg"] = "\n".join(body); say("250 ok")
                else:
                    body.append(line)
                continue
            cmd = line.split(" ")[0].upper()
            if cmd == "EHLO":
                f.write(b"250-fake\r\n250 AUTH PLAIN LOGIN\r\n"); f.flush()
            elif cmd == "AUTH":
                got["auth"] = True; say("235 ok")
            elif cmd == "DATA":
                data = True; say("354 go")
            elif cmd == "QUIT":
                say("221 bye"); break
            else:
                if cmd == "RCPT":
                    got["to"] = line
                say("250 ok")
        s.close(); srv.close()

    threading.Thread(target=run, daemon=True).start()
    return srv.getsockname()[1], got


def test_new_user_login_password_and_mail(env):
    import email as email_lib
    c = client(env)
    login(c, "boss", "Boss-password-1")
    # логин — из фамилии, занятый — с инициалами
    assert c.get("/api/admin/users/suggest-login?full_name=Кадочников Павел Анатольевич").json()["login"] == "kadochnikov"
    # почта портала не настроена — пароль возвращается администратору
    r = c.post("/api/admin/users", json={"full_name": "Кадочников Павел Анатольевич", "email": "k@example.ru"})
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["login"] == "kadochnikov" and not j["sent"] and "не настроена" in j["why"]
    assert login(client(env), "kadochnikov", j["password"]).status_code == 200
    assert c.get("/api/admin/users/suggest-login?full_name=Кадочников Пётр Юрьевич").json()["login"] == "kadochnikov.py"
    assert c.post("/api/admin/users", json={"full_name": "Щукина Юлия", "email": "плохо"}).status_code == 400
    assert c.post("/api/admin/users", json={"full_name": ""}).status_code == 400

    # настраиваем почту и выдаём новый пароль — он уходит письмом и на экран не попадает
    port, got = _fake_smtp()
    env["config"].write_env("SMTP_STARTTLS", "0")
    r = c.post("/api/admin/mail", json={"user": "portal@example.ru", "host": "127.0.0.1", "port": str(port),
                                        "password": "app-password"})
    assert r.status_code == 200 and r.json()["password_set"] and "password" not in r.json()
    r = c.post("/api/admin/users/kadochnikov/password", json={})
    j = r.json()
    assert j["sent"] and "password" not in j
    msg = email_lib.message_from_string(got["msg"])
    text = msg.get_payload(decode=True).decode(msg.get_content_charset())
    assert "k@example.ru" in got["to"] and got.get("auth")
    assert "Логин:             kadochnikov" in text and "https://workspace.example.ru" in text and "Павел" in text
    pw = text.split("Временный пароль:")[1].split()[0]
    assert login(client(env), "kadochnikov", pw).status_code == 200
    # чтобы у сотрудника была почта, её можно вписать и позже
    assert c.patch("/api/admin/users/emp", json={"email": "emp@example.ru"}).status_code == 200
    assert env["db"].get_user("emp")["email"] == "emp@example.ru"


def test_admin_deletes_user_with_confirmation(env):
    c = client(env)
    login(c, "boss", "Boss-password-1")
    env["db"].create_user("nach", env["auth"].hash_password("Nach-password-1"), must_change_password=False)
    assert c.patch("/api/admin/users/emp", json={"manager": "nach"}).status_code == 200
    victim = client(env)
    assert login(victim, "nach", "Nach-password-1").status_code == 200
    # без точного логина — не удаляет; себя — нельзя
    assert c.post("/api/admin/users/nach/delete", json={"confirm": "Nach"}).status_code == 400
    assert c.post("/api/admin/users/boss/delete", json={"confirm": "boss"}).status_code == 400
    r = c.post("/api/admin/users/nach/delete", json={"confirm": "nach"})
    assert r.status_code == 200 and r.json()["orphans"] == ["emp"]
    assert env["db"].get_user("nach") is None and env["db"].get_user("emp")["manager"] == ""
    assert victim.get("/api/me").status_code == 401            # его вход больше не действует


def _docx(body_xml: str) -> bytes:
    import io
    import zipfile
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/document.xml",
                   '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>'
                   + body_xml + "</w:body></w:document>")
    return buf.getvalue()


def _p(text):
    return f"<w:p><w:r><w:t>{text}</w:t></w:r></w:p>"


def _row(*cells):
    return "<w:tr>" + "".join("<w:tc>" + "".join(_p(x) for x in (c if isinstance(c, list) else [c])) + "</w:tc>"
                              for c in cells) + "</w:tr>"


def test_people_list_upload_parses_and_imports_only_confirmed(env):
    from app import people_import
    data = _docx(
        _p("СПИСОК участников совещания")
        + "<w:tbl>" + _row("№", "ФИО", "Должность")
        + _row("1.", "ЧЕРНЫШЕНКО Дмитрий Николаевич", "Заместитель Председателя Правительства")
        + _row("2", ["Кадочников Павел Анатольевич", "Заместитель Министра финансов"], "")
        + _row("3", "Музаев А.А.", "руководитель Рособрнадзора") + "</w:tbl>"
        + _p("Иванова Мария Сергеевна — директор департамента Минпросвещения")
        + _p("Петров Пётр Петрович") + _p("начальник управления")
        + _p("Министерство финансов Российской Федерации"))
    got = people_import.parse_docx(data)
    assert [p["name"] for p in got] == ["Чернышенко Дмитрий Николаевич", "Кадочников Павел Анатольевич",
                                        "Музаев А.А.", "Иванова Мария Сергеевна", "Петров Пётр Петрович"]
    assert got[0]["role"] == "Заместитель Председателя Правительства"
    assert got[1]["role"] == "Заместитель Министра финансов"
    assert got[3]["role"] == "директор департамента Минпросвещения"
    assert got[4]["role"] == "начальник управления"

    c = client(env)
    login(c, "boss", "Boss-password-1")
    c.post("/api/people", json={"name": "Кадочников Павел Анатольевич", "role": "Заместитель Министра финансов"})
    items = c.post("/api/admin/people/parse", content=data).json()["items"]
    st = {i["name"]: i["status"] for i in items}
    assert st["Кадочников Павел Анатольевич"] == "same" and st["Музаев А.А."] == "new"
    assert c.post("/api/admin/people/parse", content=b"not a docx").status_code == 400
    e = client(env)
    login(e, "emp", "Emp-password-1")
    assert e.post("/api/admin/people/parse", content=data).status_code == 403
    # записываются только отмеченные — и от имени того, кто нажал
    r = c.post("/api/admin/people/import", json={"items": [
        {"name": "Музаев А.А.", "role": "руководитель Рособрнадзора", "document": "Список 23.09.docx"}]})
    assert r.json()["added"] == 1
    card = c.get("/api/people?q=Музаев").json()["items"][0]
    assert card["updated_by"] == "boss" and card["document"] == "Список 23.09.docx"
    assert c.get("/api/people?q=Иванова").json()["items"] == []


def test_healthz_timing_is_median_of_three_after_warmup(env):
    """Замер /healthz: прогрев не считается, три попытки, медиана и максимум (находка SRE 21.09)."""
    import http.server
    import threading
    from app import status
    hits = []

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            hits.append(self.path)
            self.send_response(200 if self.path == "/healthz" else 404)
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        r = status._healthz(srv.server_address[1])
    finally:
        srv.shutdown()
    assert r["ok"] and len(hits) == 4 and r["ms"] is not None and r["ms_max"] >= r["ms"]
    assert status._healthz(1)["ok"] is False


def test_threadpool_raised_above_anyio_default(env):
    """СРЕ 28.09, P1 (остаток после коротких таймаутов): обычные def-обработчики (в т.ч.
    /internal/auth — вход во всё пространство) делят пул потоков anyio, по умолчанию 40 —
    портал поднимает его на старте, а не запускает несколько процессов (--workers сломал бы
    внутрипроцессные ask.rate_limited/ask._health, они не разделяются между процессами)."""
    from anyio import to_thread
    from app.main import THREADPOOL_SIZE
    with TestClient(env["app"], base_url="https://workspace.example.ru") as c:
        limit = c.portal.call(lambda: to_thread.current_default_thread_limiter().total_tokens)
    assert limit == THREADPOOL_SIZE
    assert THREADPOOL_SIZE > 40                # умолчание anyio, которого не хватало


def test_iskra_ping_has_a_real_wall_clock_bound(monkeypatch):
    """СРЕ 28.09: urlopen(timeout=...) не покрывает разрешение имени (socket.getaddrinfo) —
    при отказе DNS у cloud.iskrabot.ru пинг мог висеть по времени резолвера ОС, а не
    PING_TIMEOUT (p95 35 с при PING_TIMEOUT=6). ping() должен возвращаться за PING_TIMEOUT
    с запасом, даже если сам сетевой вызов внутри виснет намного дольше."""
    import time
    from app import ask
    monkeypatch.setattr(ask, "PING_TIMEOUT", 2)   # 05.10: боевой потолок 25 с — тесту хватит короткого
    def _hangs_forever():
        time.sleep(ask.PING_TIMEOUT * 5)
        return True, "отвечает"                 # до этого возврата вызывающий код уже не доживёт
    monkeypatch.setattr(ask, "_ping_call", _hangs_forever)
    started = time.perf_counter()
    ok, detail = ask.ping()
    elapsed = time.perf_counter() - started
    assert elapsed < ask.PING_TIMEOUT + 1        # реальный потолок по времени, не по сети
    assert ok is False and "срок" in detail


def test_iskra_ping_checks_key_and_scopes_without_spending_tokens(monkeypatch):
    """05.10, по справке «Искры» и пробе API с VPS: проверка лампочки — GET /api/v1/me (ключ и права),
    а не настоящий вопрос за ~51 тыс. входных токенов. Нет chat:run — лампочка красная; нет web_search /
    files:* — зелёная с перечнем недостающих прав; 401 — «ключ не принят (истёк или отозван)»."""
    import io, json, urllib.error
    from app import ask, config
    monkeypatch.setattr(config, "read_env", lambda name, default="": {"ISKRA_API_KEY": "isk_test"}.get(name, default))
    seen = []

    def fake_urlopen(request, timeout=None):
        seen.append((request.full_url, request.get_method(), timeout))
        body = fake_urlopen.payload
        if isinstance(body, int):
            raise urllib.error.HTTPError(request.full_url, body, "err", {}, io.BytesIO(b"{}"))
        return io.BytesIO(json.dumps(body).encode("utf-8"))
    monkeypatch.setattr(ask.urllib.request, "urlopen", lambda req, timeout=None: fake_urlopen(req, timeout))

    fake_urlopen.payload = {"profile_id": "p", "scopes": ["chat:run", "skills:run", "web_search", "files:write", "files:read"]}
    ok, detail = ask._ping_call()
    assert ok is True and "права полные" in detail
    assert seen[-1][0].endswith("/api/v1/me") and seen[-1][1] == "GET"      # не POST /chat: ни одного токена
    assert seen[-1][2] == ask.PING_TIMEOUT

    fake_urlopen.payload = {"scopes": ["chat:run", "skills:run"]}
    ok, detail = ask._ping_call()
    assert ok is True and "без прав" in detail and "web_search" in detail and "files:write" in detail

    fake_urlopen.payload = {"scopes": ["web_search"]}
    ok, detail = ask._ping_call()
    assert ok is False and "chat:run" in detail

    fake_urlopen.payload = 401
    ok, detail = ask._ping_call()
    assert ok is False and "ключ не принят" in detail and "истёк" in detail

    fake_urlopen.payload = {"status": "ok"}                 # контур без списка прав — связь есть, права не проверить
    assert ask._ping_call() == (True, "отвечает")


def test_iskra_lamp_key_check_does_not_clear_recent_live_failure(env, monkeypatch):
    """Ревьюер RC-2026-10-05-14, Н-1: /me отвечает 200 и при кончившейся оплате — живой отказ на вопрос человека
    плановая проверка ключа не затирает LIVE_FAIL_HOLD; живой успех — затирает сразу. Н-3: медленный /me не стирает
    список недостающих прав."""
    import time
    from app import ask
    ask.reset_health_for_tests()
    monkeypatch.setattr(ask, "PING_WAIT", 0.3)
    monkeypatch.setattr(ask, "LIVE_EVERY", 0.0)                   # каждая health() — новая плановая проверка
    monkeypatch.setattr(env["config"], "BASE_DIR", env["tmp"] / "нет-логов")
    monkeypatch.setattr(ask, "ping", lambda: (True, "ключ действует, права полные"))

    ask.note_result(False, "«Искра» ответила 402")
    state = ask.health()
    assert state["ok"] is False and "402" in state["detail"] and "ключ действует" in state["detail"]
    for _ in range(3):                                            # ревью RC-15: подсказка не растёт, /me не зовётся каждый опрос
        again = ask.health()
    assert again["detail"] == state["detail"] and again["detail"].count("ключ действует") == 1
    assert again["checked"] != ""                                 # time обновлён — плановая проверка снова не раньше LIVE_EVERY
    ask.note_result(True, "отвечает")                             # живой успех — лампочка зелёная сразу
    assert ask.health()["ok"] is True

    ask.reset_health_for_tests()
    ask.note_result(False, "«Искра» ответила 402")
    monkeypatch.setattr(ask, "LIVE_FAIL_HOLD", 0.0)               # срок удержания вышел — верим проверке ключа
    time.sleep(0.01)
    assert ask.health()["ok"] is True

    ask.reset_health_for_tests()
    monkeypatch.setattr(ask, "PING_SLOW", 0.05)
    monkeypatch.setattr(ask, "ping", lambda: (time.sleep(0.1), (True, "ключ действует, но без прав: web_search (поиск)"))[1])
    state = ask.health()
    assert state["ok"] is True and "без прав" in state["detail"] and "медленно" in state["detail"]


def test_ask_input_errors_do_not_touch_lamp(env, monkeypatch):
    """Ревью RC-15: пустой или длинный вопрос — отказ до Искры (400), лампочку он не гасит."""
    from app import ask
    ask.reset_health_for_tests()
    ask.reset_limits_for_tests()
    monkeypatch.setattr(ask, "ping", lambda: (True, "ключ действует, права полные"))
    monkeypatch.setattr(env["config"], "BASE_DIR", env["tmp"] / "нет-логов")
    c = client(env)
    login(c, "emp", "Emp-password-1")
    assert c.get("/api/iskra").json()["ok"] is True
    assert c.post("/api/ask", json={"question": "   "}).status_code == 400
    assert c.post("/api/ask", json={"question": "х" * (ask.MAX_QUESTION + 1)}).status_code in (400, 422)  # 422 — отказ модели запроса
    assert c.get("/api/iskra").json()["ok"] is True


def test_iskra_key_expiry_is_stored_and_warns(env, monkeypatch):
    """05.10, правила API Искры: ключ живёт 7–365 дней (по умолчанию 30), срок API не отдаёт — дату вводит администратор.
    Портал хранит её в .env, за 7 дней предупреждает (status, лампочка, отчёт); кривая дата — отказ без замены ключа."""
    import datetime as dt
    from app import ask, config, iskra_key
    env_path = env["tmp"] / "portal.env"
    monkeypatch.setattr(config, "ENV_PATH", env_path)
    monkeypatch.setattr(iskra_key, "KEY_TARGETS", [])
    c = client(env)
    login(c, "boss", "Boss-password-1")
    far = (dt.date.today() + dt.timedelta(days=200)).isoformat()
    r = c.post("/api/admin/iskra-key", json={"key": "isk_newkey_123456", "expires": far})
    assert r.status_code == 200 and r.json()["expires"] == far and r.json()["warn"] is False
    assert f"ISKRA_KEY_EXPIRES={far}" in env_path.read_text(encoding="utf-8")
    assert "ISKRA_API_KEY=isk_newkey_123456" in env_path.read_text(encoding="utf-8")

    bad = c.post("/api/admin/iskra-key", json={"key": "isk_otherkey_654321", "expires": "скоро"})
    assert bad.status_code == 400
    assert "ISKRA_API_KEY=isk_newkey_123456" in env_path.read_text(encoding="utf-8")   # ключ не тронут

    soon = (dt.date.today() + dt.timedelta(days=3)).isoformat()
    r = c.post("/api/admin/iskra-key/expires", json={"expires": soon})
    assert r.status_code == 200 and r.json()["warn"] is True and r.json()["days_left"] == 3
    assert "истекает" in iskra_key.expires_note() and "3 дн." in iskra_key.expires_note()
    assert c.get("/api/admin/iskra-key").json()["expires"] == soon

    # лампочка дописывает предупреждение к итогу проверки ключа
    import io, json as _json
    monkeypatch.setattr(ask.urllib.request, "urlopen", lambda req, timeout=None: io.BytesIO(
        _json.dumps({"scopes": ["chat:run", "web_search", "files:write", "files:read"]}).encode("utf-8")))
    ok, detail = ask._ping_call()
    assert ok is True and detail.startswith("ключ действует") and "истекает" in detail

    gone = (dt.date.today() - dt.timedelta(days=1)).isoformat()
    c.post("/api/admin/iskra-key/expires", json={"expires": gone})
    assert iskra_key.expires_info()["expired"] is True and "истёк" in iskra_key.expires_note()
    c.post("/api/admin/iskra-key/expires", json={"expires": ""})
    assert iskra_key.expires_info() == {"expires": "", "days_left": None, "warn": False, "expired": False}
    assert c.post("/api/admin/iskra-key/expires", json={"expires": soon}, headers={"Origin": "https://evil.example"}).status_code in (400, 403)


def test_iskra_key_expiry_edges_and_rights(env, monkeypatch):
    """Ревью RC-18: границы 7/8 дней, не-админ — 403; отчёт молчит на срезе без поля iskra_key."""
    import datetime as dt
    import subprocess, sys, json as _json
    from app import config, iskra_key
    monkeypatch.setattr(config, "ENV_PATH", env["tmp"] / "portal.env")
    monkeypatch.setattr(iskra_key, "KEY_TARGETS", [])
    today = dt.date.today()
    iskra_key.set_expires((today + dt.timedelta(days=7)).isoformat())
    assert iskra_key.expires_info(today)["warn"] is True
    iskra_key.set_expires((today + dt.timedelta(days=8)).isoformat())
    assert iskra_key.expires_info(today)["warn"] is False
    c = client(env)
    login(c, "emp", "Emp-password-1")
    assert c.post("/api/admin/iskra-key/expires", json={"expires": today.isoformat()}).status_code == 403
    assert c.get("/api/admin/iskra-key").status_code == 403
    # суточный отчёт: срез без поля iskra_key — ни слова о сроке; с пустой датой — напоминание
    snap = env["tmp"] / "s.json"
    base = {"services": [], "taken_at": "2026-10-05T08:00:00+03:00"}
    snap.write_text(_json.dumps(base), encoding="utf-8")
    script = str(config.BASE_DIR.parent / "portal" / "ops" / "daily_report.py") if (config.BASE_DIR.parent / "portal").exists() else str(config.BASE_DIR / "ops" / "daily_report.py")
    out = subprocess.run([sys.executable, script, str(snap)], capture_output=True, text=True).stdout
    assert "ключ" not in out.lower()
    snap.write_text(_json.dumps({**base, "iskra_key": {"expires": "", "days_left": None, "warn": False, "expired": False}}), encoding="utf-8")
    out = subprocess.run([sys.executable, script, str(snap)], capture_output=True, text=True).stdout
    assert "Срок ключа Искры не указан" in out


def test_iskra_key_files_are_written_with_private_permissions(env, monkeypatch, tmp_path):
    """Замечание сессии сервера 05.10: запись .env через tmp + os.replace давала права 664. Новый файл — 600,
    у существующего права сохраняются (на VPS 660)."""
    import os, stat
    from app import config, iskra_key
    env_path = tmp_path / "portal.env"
    monkeypatch.setattr(config, "ENV_PATH", env_path)
    brief = tmp_path / "brief"; brief.mkdir()
    (brief / "settings.json").write_text("{}", encoding="utf-8"); os.chmod(brief / "settings.json", 0o660)
    svc = tmp_path / "svc"; svc.mkdir()
    (svc / ".env").write_text("PORTAL_SECRET=x\n", encoding="utf-8"); os.chmod(svc / ".env", 0o660)
    monkeypatch.setenv("BRIEF_HOME", str(brief)); monkeypatch.setenv("SVC_HOME", str(svc))
    monkeypatch.setattr(iskra_key, "KEY_TARGETS", [("Бриф", "BRIEF_HOME", "settings.json"), ("Сервис", "SVC_HOME", ".env")])
    iskra_key.set_key("isk_private_key_000")
    assert stat.S_IMODE(os.stat(env_path).st_mode) == 0o600            # новый файл портала
    assert stat.S_IMODE(os.stat(svc / ".env").st_mode) == 0o660         # права существующего сохранены
    assert stat.S_IMODE(os.stat(brief / "settings.json").st_mode) == 0o660
    assert "PORTAL_SECRET=x" in (svc / ".env").read_text(encoding="utf-8")
    assert not list(tmp_path.rglob("*.tmp"))
    # ревью RC-23: группа существующего файла сохраняется; оставшийся от старого кода .tmp с 664 не протаскивает права
    other = next((g for g in os.getgroups() if g != os.getgid()), None)
    if other is not None:
        os.chown(svc / ".env", -1, other)
        iskra_key.set_key("isk_private_key_001")
        assert os.stat(svc / ".env").st_gid == other
    stale = tmp_path / "new.env.tmp"
    stale.write_text("x", encoding="utf-8"); os.chmod(stale, 0o664)
    iskra_key._set_env_line(tmp_path / "new.env", "A", "1")
    assert stat.S_IMODE(os.stat(tmp_path / "new.env").st_mode) == 0o600


def test_new_services_are_open_to_all_with_badge(env):
    """Решение пользователя 05.10 (вечер): «Транскрибатор» и «Объективки» открыты всем с плашкой «новое, проверяется»,
    а не спрятаны; доступ, как у всех сервисов, выдаёт администратор галочками."""
    from app import config
    for sid in ("transcriber", "profiles"):
        s = next(x for x in config.SERVICES if x["id"] == sid)
        assert s.get("new") is True and not s.get("hidden")
    env["db"].update_user("emp", services=[*env["db"].get_user("emp")["services"], "transcriber", "profiles"])
    worker = client(env)
    login(worker, "emp", "Emp-password-1")
    ids = {s["id"]: s for s in worker.get("/api/me").json()["service_list"]}
    assert ids["transcriber"]["new"] is True and ids["profiles"]["new"] is True
    assert worker.get("/auth/check/profiles").status_code == 200
    js = (config.BASE_DIR / "static" / "home.js").read_text(encoding="utf-8")
    assert "новое, проверяется" in js
