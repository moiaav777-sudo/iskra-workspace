"""SQLite портала: пользователи и журнал действий."""
from __future__ import annotations

import datetime as dt
import hmac
import json
import sqlite3
import threading

from . import config

_init_lock = threading.Lock()
_initialized = False


def now_iso() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def db_path():
    return config.DATA_DIR / "portal.db"


def conn() -> sqlite3.Connection:
    global _initialized
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(db_path(), timeout=15)
    c.row_factory = sqlite3.Row
    # SQLite сам не умеет приводить кириллицу к нижнему регистру — поиск по журналу
    # без этого не находил бы «Петрова» по запросу «петрова».
    c.create_function("ru_lower", 1, lambda v: v.lower() if isinstance(v, str) else v)
    c.execute("PRAGMA foreign_keys = ON")
    if not _initialized:
        with _init_lock:
            if not _initialized:
                c.execute("PRAGMA journal_mode = WAL")
                _init_schema(c)
                _initialized = True
    return c


def reset_for_tests() -> None:
    global _initialized
    _initialized = False


def _init_schema(c: sqlite3.Connection) -> None:
    c.executescript(
        """
        CREATE TABLE IF NOT EXISTS users (
            login TEXT PRIMARY KEY,
            full_name TEXT NOT NULL DEFAULT '',
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'employee',
            services TEXT NOT NULL DEFAULT '[]',
            active INTEGER NOT NULL DEFAULT 1,
            must_change_password INTEGER NOT NULL DEFAULT 0,
            totp_secret TEXT NOT NULL DEFAULT '',
            totp_pending TEXT NOT NULL DEFAULT '',
            session_gen INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            last_login_at TEXT NOT NULL DEFAULT '',
            manager TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS audit (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            login TEXT NOT NULL DEFAULT '',
            service TEXT NOT NULL DEFAULT 'portal',
            action TEXT NOT NULL,
            object TEXT NOT NULL DEFAULT '',
            details TEXT NOT NULL DEFAULT '',
            ip TEXT NOT NULL DEFAULT '',
            tokens_in INTEGER NOT NULL DEFAULT 0,
            tokens_out INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS audit_ts ON audit(ts);
        CREATE INDEX IF NOT EXISTS audit_login ON audit(login);
        CREATE INDEX IF NOT EXISTS audit_service ON audit(service);
        """
    )
    # Столбцы расхода «Искры» появились позже журнала — у ранних баз их нет.
    # Непосредственный руководитель появился позже — у ранних баз колонки нет.
    # Колонка называется manager, чтобы не путать с ролью chief («руководитель, который только смотрит»).
    if "manager" not in {r[1] for r in c.execute("PRAGMA table_info(users)")}:
        c.execute("ALTER TABLE users ADD COLUMN manager TEXT NOT NULL DEFAULT ''")
    # Ключ приёмной: по нему телефон складывает документы в хранилище, не вводя пароль.
    if "inbox_key" not in {r[1] for r in c.execute("PRAGMA table_info(users)")}:
        c.execute("ALTER TABLE users ADD COLUMN inbox_key TEXT NOT NULL DEFAULT ''")
    # Должность: человек пишет её сам в «Моём профиле». Она видна в шапке и в списках,
    # где раньше стоял один логин, — по должности коллегу узнают быстрее, чем по фамилии.
    if "position" not in {r[1] for r in c.execute("PRAGMA table_info(users)")}:
        c.execute("ALTER TABLE users ADD COLUMN position TEXT NOT NULL DEFAULT ''")
    # Почта: на неё уходят логин и временный пароль (решение пользователя 21.09).
    if "email" not in {r[1] for r in c.execute("PRAGMA table_info(users)")}:
        c.execute("ALTER TABLE users ADD COLUMN email TEXT NOT NULL DEFAULT ''")
    # Реестр людей — общий на всё пространство (решение пользователя 17.09), см. people.py.
    from . import people
    people.setup(c)
    # Объявления администратора (решение пользователя 02.10), см. notices.py.
    from . import notices
    notices.setup(c)
    have = {r[1] for r in c.execute("PRAGMA table_info(audit)")}
    for col in ("tokens_in", "tokens_out"):
        if col not in have:
            c.execute(f"ALTER TABLE audit ADD COLUMN {col} INTEGER NOT NULL DEFAULT 0")
    c.commit()


# ── пользователи ────────────────────────────────────────────────────────

def _user(row: sqlite3.Row | None) -> dict | None:
    if not row:
        return None
    u = dict(row)
    u["services"] = [s for s in json.loads(u["services"] or "[]") if s in config.SERVICE_IDS]
    u["active"] = bool(u["active"])
    u["must_change_password"] = bool(u["must_change_password"])
    return u


def team_of(login: str) -> list[str]:
    """Кто под человеком по цепочке: прямые подчинённые, их подчинённые и так далее.

    Круг нужен для показа списков («мои / отдела / все»). Защита от колец обязательна:
    руководителем случайно можно назначить того, кто уже подчинён — тогда обход зациклится.
    """
    c = conn()
    try:
        rows = c.execute("SELECT login, manager FROM users WHERE manager <> ''").fetchall()
    finally:
        c.close()
    under: dict[str, list[str]] = {}
    for row in rows:
        under.setdefault(row["manager"], []).append(row["login"])
    team, queue, seen = [], list(under.get(login, [])), {login}
    while queue:
        person = queue.pop(0)
        if person in seen:
            continue
        seen.add(person)
        team.append(person)
        queue.extend(under.get(person, []))
    return team


def _managers() -> dict[str, str]:
    c = conn()
    try:
        return {r["login"]: r["manager"] for r in c.execute("SELECT login, manager FROM users").fetchall()}
    finally:
        c.close()


def chain_up(login: str, managers: dict[str, str] | None = None) -> list[str]:
    """Цепочка начальников над человеком, снизу вверх. Кольцо обрывается молча."""
    managers = managers if managers is not None else _managers()
    chain, seen, current = [], {login}, managers.get(login, "")
    while current and current not in seen:
        chain.append(current)
        seen.add(current)
        current = managers.get(current, "")
    return chain


def place_of(login: str) -> dict:
    """Где человек стоит в трёхуровневой лестнице подчинения.

    Уровень считается сверху: без руководителя — первый, под ним второй, под тем третий
    (`config.MAX_CHAIN`). Нужен и человеку в профиле, и в списках сотрудников: по одному
    имени не понять, начальник это управления или специалист.
    """
    managers = _managers()
    chain = chain_up(login, managers)
    under: dict[str, list[str]] = {}
    for who, boss in managers.items():
        under.setdefault(boss, []).append(who)
    direct = sorted(under.get(login, []))
    return {
        "level": len(chain) + 1,
        "levels": config.MAX_CHAIN,
        "chain": chain,                    # начальники снизу вверх
        "manager": chain[0] if chain else "",
        "direct": direct,                  # прямые подчинённые
        "team": team_of(login),            # все под ним по цепочке
    }


def _depth_below(login: str, under: dict[str, list[str]], seen: set[str] | None = None) -> int:
    """Сколько уровней подчинённых под человеком (0 — никого)."""
    seen = seen or set()
    if login in seen:
        return 0
    seen.add(login)
    people = under.get(login, [])
    return 1 + max((_depth_below(p, under, seen) for p in people), default=0) if people else 0


def manager_problem(login: str, manager: str) -> str:
    """Почему такого руководителя назначить нельзя (пустая строка — можно)."""
    if not manager:
        return ""
    if manager == login:
        return "Человек не может быть руководителем сам себе."
    managers = _managers()
    if manager not in managers:
        return "Такого сотрудника нет."
    if login in chain_up(manager, managers):
        return "Так получится кольцо: этот сотрудник уже подчинён выбранному человеку."
    under: dict[str, list[str]] = {}
    for person, boss in managers.items():
        if boss and person != login:
            under.setdefault(boss, []).append(person)
    total = len(chain_up(manager, managers)) + 1 + 1 + _depth_below(login, under)
    if total > config.MAX_CHAIN:
        return (f"Слишком длинная цепочка подчинения: получится {total} уровня(ей), "
                f"а больше {config.MAX_CHAIN} мы не заводим.")
    return ""


def allowed_services(user: dict) -> list[str]:
    return list(config.SERVICE_IDS) if user["role"] == "admin" else user["services"]


def get_user(login: str) -> dict | None:
    c = conn()
    try:
        return _user(c.execute("SELECT * FROM users WHERE login = ?", (login,)).fetchone())
    finally:
        c.close()


def list_users() -> list[dict]:
    c = conn()
    try:
        return [_user(r) for r in c.execute("SELECT * FROM users ORDER BY role, login").fetchall()]
    finally:
        c.close()


def count_admins(active_only: bool = True) -> int:
    c = conn()
    try:
        q = "SELECT COUNT(*) FROM users WHERE role = 'admin'" + (" AND active = 1" if active_only else "")
        return c.execute(q).fetchone()[0]
    finally:
        c.close()


def create_user(login: str, password_hash: str, *, full_name: str = "", role: str = "employee",
                services: list[str] | None = None, must_change_password: bool = True) -> None:
    c = conn()
    try:
        c.execute(
            "INSERT INTO users(login, full_name, password_hash, role, services, must_change_password, created_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (login, full_name, password_hash, role, json.dumps(services or []), int(must_change_password), now_iso()),
        )
        c.commit()
    finally:
        c.close()


_UPDATABLE = {"full_name", "password_hash", "role", "services", "active", "must_change_password",
              "totp_secret", "totp_pending", "last_login_at", "manager", "inbox_key", "position", "email"}


def user_by_inbox_key(key: str) -> dict | None:
    """Кто прислал документ с телефона. Ключ длинный и случайный, но сравниваем
    всё равно постоянным временем: подбор по времени ответа не должен ничего давать."""
    if not key or len(key) < 32:
        return None
    c = conn()
    try:
        for row in c.execute("SELECT * FROM users WHERE inbox_key <> '' AND active = 1"):
            if hmac.compare_digest(row["inbox_key"], key):
                return _user(row)
        return None
    finally:
        c.close()


def update_user(login: str, **fields) -> bool:
    fields = {k: v for k, v in fields.items() if k in _UPDATABLE}
    if not fields:
        return False
    if "services" in fields:
        fields["services"] = json.dumps([s for s in fields["services"] if s in config.SERVICE_IDS])
    for k in ("active", "must_change_password"):
        if k in fields:
            fields[k] = int(bool(fields[k]))
    sets = ", ".join(f"{k} = ?" for k in fields)
    c = conn()
    try:
        cur = c.execute(f"UPDATE users SET {sets} WHERE login = ?", (*fields.values(), login))
        c.commit()
        return cur.rowcount > 0
    finally:
        c.close()


def delete_user(login: str) -> list[str]:
    """Удалить учётную запись насовсем (решение пользователя 21.09). Журнал и документы
    в сервисах остаются: там логин — просто подпись. Подчинённые остаются без руководителя —
    возвращаем их, чтобы администратор видел, кого переназначить."""
    c = conn()
    try:
        orphans = [r[0] for r in c.execute("SELECT login FROM users WHERE manager = ?", (login,))]
        c.execute("UPDATE users SET manager = '' WHERE manager = ?", (login,))
        c.execute("DELETE FROM users WHERE login = ?", (login,))
        c.commit()
        return orphans
    finally:
        c.close()


def bump_session_gen(login: str) -> None:
    """Завершить все сессии пользователя (смена пароля, блокировка, «выйти везде»)."""
    c = conn()
    try:
        c.execute("UPDATE users SET session_gen = session_gen + 1 WHERE login = ?", (login,))
        c.commit()
    finally:
        c.close()


# ── журнал ──────────────────────────────────────────────────────────────

def audit(action: str, *, login: str = "", service: str = "portal", obj: str = "",
          details: str = "", ip: str = "", tokens_in: int = 0, tokens_out: int = 0) -> None:
    c = conn()
    try:
        c.execute(
            "INSERT INTO audit(ts, login, service, action, object, details, ip, tokens_in, tokens_out) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (now_iso(), login[:100], service[:40], action[:200], obj[:300], details[:4000], ip[:64],
             max(0, int(tokens_in)), max(0, int(tokens_out))),
        )
        c.commit()
    finally:
        c.close()


def recent_work(login: str, limit: int = 5) -> list[dict]:
    """Над чем человек работал последним — по журналу, без опроса сервисов.

    Берём последнее действие по каждому объекту: иначе список забивают
    десять правок одного и того же совещания.
    """
    c = conn()
    try:
        # Группируем по MAX(id), а не по MAX(ts): время пишется до секунды, и две правки
        # в одну секунду дали бы произвольное действие из пары.
        rows = c.execute(
            "SELECT service, object, action, ts, MAX(id) AS last_id FROM audit "
            "WHERE login = ? AND object <> '' AND service <> 'portal' "
            "GROUP BY service, object ORDER BY last_id DESC LIMIT ?",
            (login, max(1, min(limit, 20)))).fetchall()
        return [{"service": r["service"], "object": r["object"], "action": r["action"], "when": r["ts"]}
                for r in rows]
    finally:
        c.close()


def month_bounds(month: str) -> tuple[str, str]:
    year, mon = int(month[:4]), int(month[5:7])
    end = f"{year + 1:04d}-01-01" if mon == 12 else f"{year:04d}-{mon + 1:02d}-01"
    return month + "-01", end


def summary(month: str) -> dict:
    """Сводка за месяц по журналу: сколько работы и у кого. Для руководителя.

    Считаем и действия, и объекты: десять правок одного совещания — это одно совещание,
    а не десять дел.
    """
    start, end = month_bounds(month)
    where = "WHERE ts >= ? AND ts < ? AND service <> 'portal' AND object <> ''"
    c = conn()
    try:
        def group(field: str) -> list[dict]:
            rows = c.execute(
                f"SELECT {field} AS key, COUNT(*) AS actions, COUNT(DISTINCT object) AS objects, "
                f"COUNT(DISTINCT login) AS people, MAX(ts) AS last_ts FROM audit {where} "
                f"GROUP BY {field} ORDER BY objects DESC, actions DESC", (start, end)).fetchall()
            return [{"key": r["key"], "actions": r["actions"], "objects": r["objects"],
                     "people": r["people"], "last": r["last_ts"]} for r in rows]

        total = c.execute(
            f"SELECT COUNT(*) AS actions, COUNT(DISTINCT object) AS objects, COUNT(DISTINCT login) AS people "
            f"FROM audit {where}", (start, end)).fetchone()
        return {"month": month,
                "total": {"actions": total["actions"], "objects": total["objects"], "people": total["people"]},
                "by_service": group("service"), "by_login": group("login")}
    finally:
        c.close()


def months_with_work() -> list[str]:
    c = conn()
    try:
        rows = c.execute("SELECT DISTINCT substr(ts, 1, 7) AS m FROM audit WHERE service <> 'portal' "
                         "ORDER BY m DESC LIMIT 24").fetchall()
        return [r["m"] for r in rows]
    finally:
        c.close()


def iskra_usage(month: str) -> dict:
    """Расход «Искры» за месяц (YYYY-MM): по сотрудникам, по сервисам и по дням.

    Считается по журналу: сервисы кладут расход в ту же запись, что и само действие,
    поэтому отдельной базы не нужно и цифра всегда привязана к человеку и к делу.
    """
    start, end = month_bounds(month)
    where = "WHERE ts >= ? AND ts < ? AND (tokens_in > 0 OR tokens_out > 0)"
    c = conn()
    try:
        def group(field: str) -> list[dict]:
            rows = c.execute(
                f"SELECT {field} AS key, SUM(tokens_in) AS tin, SUM(tokens_out) AS tout, COUNT(*) AS runs "
                f"FROM audit {where} GROUP BY {field} ORDER BY (SUM(tokens_in) + SUM(tokens_out)) DESC",
                (start, end)).fetchall()
            return [{"key": r["key"], "tokens_in": r["tin"], "tokens_out": r["tout"],
                     "total": r["tin"] + r["tout"], "runs": r["runs"]} for r in rows]

        total = c.execute(
            f"SELECT COALESCE(SUM(tokens_in), 0) AS tin, COALESCE(SUM(tokens_out), 0) AS tout, COUNT(*) AS runs "
            f"FROM audit {where}", (start, end)).fetchone()
        days = c.execute(
            f"SELECT substr(ts, 1, 10) AS key, SUM(tokens_in + tokens_out) AS total FROM audit {where} "
            f"GROUP BY substr(ts, 1, 10) ORDER BY key", (start, end)).fetchall()
        return {"month": month,
                "total": {"tokens_in": total["tin"], "tokens_out": total["tout"],
                          "total": total["tin"] + total["tout"], "runs": total["runs"]},
                "by_login": group("login"), "by_service": group("service"),
                "by_day": [{"key": r["key"], "total": r["total"]} for r in days]}
    finally:
        c.close()


def iskra_usage_months() -> list[str]:
    """Месяцы, за которые есть расход — чтобы в выпадающем списке не было пустых."""
    c = conn()
    try:
        rows = c.execute("SELECT DISTINCT substr(ts, 1, 7) AS m FROM audit "
                         "WHERE tokens_in > 0 OR tokens_out > 0 ORDER BY m DESC").fetchall()
        return [r["m"] for r in rows]
    finally:
        c.close()


def audit_list(*, login: str = "", service: str = "", q: str = "", date_from: str = "", date_to: str = "",
               limit: int = 100, offset: int = 0) -> tuple[list[dict], int]:
    where, args = [], []
    if login:
        where.append("login = ?"); args.append(login)
    if service:
        where.append("service = ?"); args.append(service)
    if q:
        where.append("(ru_lower(action) LIKE ru_lower(?) OR ru_lower(object) LIKE ru_lower(?) "
                     "OR ru_lower(details) LIKE ru_lower(?) OR ru_lower(login) LIKE ru_lower(?))")
        args += [f"%{q}%"] * 4
    if date_from:
        where.append("ts >= ?"); args.append(date_from)
    if date_to:
        where.append("ts < ?"); args.append(date_to)
    w = (" WHERE " + " AND ".join(where)) if where else ""
    c = conn()
    try:
        total = c.execute(f"SELECT COUNT(*) FROM audit{w}", args).fetchone()[0]
        rows = c.execute(f"SELECT * FROM audit{w} ORDER BY id DESC LIMIT ? OFFSET ?",
                         (*args, max(1, min(limit, 500)), max(0, offset))).fetchall()
        return [dict(r) for r in rows], total
    finally:
        c.close()
