"""Реестр людей: «ФИО → должность → ведомство», один на всё пространство.

Решение пользователя от 17.09: справочник общий для всех сервисов и живёт в портале.
До этого реестров было два — в «Повестке» и у «Модератора», и человек, найденный
«Искрой» в справке «Модератора», в общий реестр не попадал.

Это реестр **внешних** людей — участников и докладчиков совещаний. Наши сотрудники
с их должностями — это учётные записи портала и «Кто я в пространстве», их не смешиваем.

Правило пополнения то же, что было в «Повестке» (решение 17.09):
- догадка разбора (`force=False`) только дозаполняет пустое;
- человек, набравший должность руками, её переписывает (`force=True`);
- догадка, разошедшаяся с уже записанным, **не выбрасывается**, а ложится в варианты —
  человек выберет в окне расхождений. Раньше такая догадка молча терялась.
Каждое изменение уходит в журнал портала.
"""
from __future__ import annotations

import datetime as dt

from . import db

SEARCH_LIMIT = 20


def key(name: str) -> str:
    """Ключ записи: ФИО без регистра, лишних пробелов и «ё» — как было в «Повестке»."""
    return " ".join((name or "").strip().lower().replace("ё", "е").split())


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def setup(c) -> None:
    c.executescript("""
        CREATE TABLE IF NOT EXISTS people (
            key TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT '',
            organization TEXT NOT NULL DEFAULT '',
            search TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL,
            updated_by TEXT NOT NULL DEFAULT '',
            source TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS people_variants (
            key TEXT NOT NULL,
            role TEXT NOT NULL,
            organization TEXT NOT NULL DEFAULT '',
            source TEXT NOT NULL DEFAULT '',
            seen_at TEXT NOT NULL,
            PRIMARY KEY (key, role, organization)
        );
    """)
    # Документ-источник (решение пользователя 21.09): после переноса старых списков реестр
    # оказался засорён тестовыми записями. Теперь новый человек попадает в реестр только
    # из настоящего документа — повестки, списка участников — или руками.
    if "document" not in {r[1] for r in c.execute("PRAGMA table_info(people)")}:
        c.execute("ALTER TABLE people ADD COLUMN document TEXT NOT NULL DEFAULT ''")


def _card(c, row) -> dict:
    variants = [{"role": v["role"], "organization": v["organization"], "source": v["source"],
                 "seen_at": v["seen_at"]}
                for v in c.execute("SELECT * FROM people_variants WHERE key = ? ORDER BY seen_at DESC",
                                   (row["key"],))]
    return {"name": row["name"], "role": row["role"], "organization": row["organization"],
            "updated_at": row["updated_at"], "updated_by": row["updated_by"],
            "source": row["source"], "document": row["document"], "variants": variants}


def get(name: str) -> dict | None:
    c = db.conn()
    try:
        row = c.execute("SELECT * FROM people WHERE key = ?", (key(name),)).fetchone()
        return _card(c, row) if row else None
    finally:
        c.close()


def search(query: str, limit: int = SEARCH_LIMIT) -> list[dict]:
    """По фамилии, должности или ведомству. Нижний регистр — силами Python:
    `lower()` в SQLite кириллицу не трогает."""
    q = key(query)
    if len(q) < 2:
        return []
    c = db.conn()
    try:
        rows = c.execute("SELECT * FROM people WHERE search LIKE ? ORDER BY name LIMIT ?",
                         (f"%{q}%", max(1, min(int(limit), 100)))).fetchall()
        return [_card(c, r) for r in rows]
    finally:
        c.close()


def entries(limit: int = 1000) -> list[dict]:
    c = db.conn()
    try:
        rows = c.execute("SELECT * FROM people ORDER BY name LIMIT ?", (limit,)).fetchall()
        return [_card(c, r) for r in rows]
    finally:
        c.close()


def count() -> int:
    c = db.conn()
    try:
        return c.execute("SELECT COUNT(*) FROM people").fetchone()[0]
    finally:
        c.close()


def remember(name: str, role: str = "", organization: str = "", *, force: bool = False,
             login: str = "", source: str = "", document: str = "") -> str:
    """Запомнить человека. Возвращает описание изменения для журнала («» — ничего)."""
    person = key(name)
    if not person:
        return ""
    role, organization = (role or "").strip(), (organization or "").strip()
    document = " ".join((document or "").split())[:300]
    display = " ".join((name or "").split())
    now = _now()
    c = db.conn()
    try:
        row = c.execute("SELECT * FROM people WHERE key = ?", (person,)).fetchone()
        if not force:
            # Догадка разбора реестр не меняет (решение пользователя 21.09: «за каждую запись
            # ответственный»). Нового человека сервис предлагает добавить в окне; отличие от
            # записанного ложится в варианты — человек выберет его на странице «Люди».
            if row and ((role and role != row["role"]) or (organization and organization != row["organization"])):
                c.execute("INSERT OR IGNORE INTO people_variants(key, role, organization, source, seen_at) "
                          "VALUES(?,?,?,?,?)", (person, role or row["role"],
                                                organization or row["organization"], source, now))
                c.commit()
            return ""
        if row:
            document = document or row["document"]
            new_role = role or row["role"]
            new_org = organization or row["organization"]
            changes = []
            if new_role != row["role"]:
                changes.append(f"должность «{row['role']}» → «{new_role}»")
            if new_org != row["organization"]:
                changes.append(f"ведомство «{row['organization']}» → «{new_org}»")
            if not changes:
                c.commit()
                return ""
            # выбранный человеком вариант больше не расхождение
            c.execute("DELETE FROM people_variants WHERE key = ? AND role = ? AND organization = ?",
                      (person, new_role, new_org))
            summary = f"{display} — " + "; ".join(changes)
            role, organization = new_role, new_org
        elif role or organization:
            summary = f"добавлен {display}" + (f" — {role}" if role else "") + (f" (из: {document})" if document else "")
            document = document or "внесено вручную"
        else:
            return ""                      # пустая запись реестру не нужна
        c.execute(
            "INSERT INTO people(key, name, role, organization, search, updated_at, updated_by, source, document) "
            "VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET name = excluded.name, "
            "role = excluded.role, organization = excluded.organization, search = excluded.search, "
            "updated_at = excluded.updated_at, updated_by = excluded.updated_by, source = excluded.source, "
            "document = excluded.document",
            (person, display, role, organization, key(f"{display} {role} {organization}"), now, login, source,
             document))
        c.commit()
    finally:
        c.close()
    action = "Реестр людей: добавлен человек" if summary.startswith("добавлен") else "Реестр людей: изменена запись"
    db.audit(action, login=login, service=source or "portal", obj=display, details=summary)
    return summary


def suggest(name: str, role: str = "", organization: str = "") -> str:
    """Что разбор предлагает по человеку: new — его нет в реестре, differs — должность или
    ведомство расходятся с записанным, same — совпадает. Сервис показывает окно по new/differs."""
    card = get(name)
    if not card:
        return "new" if key(name) else ""
    if (role and role.strip() != card["role"]) or (organization and organization.strip() != card["organization"]):
        return "differs"
    return "same"


def forget(name: str, *, login: str = "") -> str:
    """Убрать запись вместе с вариантами. Необратимо — только администратору."""
    person = key(name)
    c = db.conn()
    try:
        row = c.execute("SELECT name FROM people WHERE key = ?", (person,)).fetchone()
        if not row:
            return ""
        c.execute("DELETE FROM people WHERE key = ?", (person,))
        c.execute("DELETE FROM people_variants WHERE key = ?", (person,))
        c.commit()
    finally:
        c.close()
    db.audit("Реестр людей: запись удалена", login=login, obj=row["name"], details=row["name"])
    return row["name"]


# ── сверка: похожие записи и объединение ─────────────────────────────────
# Решение пользователя 21.09 после «Кадошникова Павела Анатольевича»: разбор дал опечатку
# в фамилии, она осела в реестре рядом с правильной записью, и в документ ушла неверная.
# Поэтому любое ФИО перед выводом сверяется с реестром, а похожие записи ищутся заранее.

def _levenshtein(a: str, b: str) -> int:
    if a == b:
        return 0
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def _parts(name: str) -> tuple[str, str]:
    """(фамилия, инициалы): «Кадочников Павел Анатольевич» и «Кадочников П.А.» → («кадочников», «па»)."""
    words = key(name).replace(".", ". ").split()
    if not words:
        return "", ""
    surname = words[0]
    initials = "".join(w[0] for w in words[1:3] if w)
    return surname, initials


def _alike(a: str, b: str) -> bool:
    """Похоже на одного человека: те же инициалы и фамилия отличается на одну-две буквы.

    Порог в две буквы — ровно под ошибки разбора и диктовки («ш» вместо «ч»,
    пропущенная буква). Больше — это уже, скорее, другой человек с теми же инициалами.
    """
    sa, ia = _parts(a)
    sb, ib = _parts(b)
    if not sa or not sb or sa == sb or len(sa) < 4 or len(sb) < 4:
        return False
    if ia and ib and ia != ib:
        return False
    return _levenshtein(sa, sb) <= 2


def check(name: str) -> dict:
    """Сверка одного ФИО с реестром перед выводом в документ.

    exact   — есть ровно такая запись;
    close   — точной нет, но есть похожая: почти наверняка опечатка, показать человеку;
    unknown — в реестре нет никого похожего: новый человек, предложить добавить.
    """
    card = get(name)
    if card:
        return {"name": name, "status": "exact", "person": card}
    surname, initials = _parts(name)
    c = db.conn()
    try:
        rows = c.execute("SELECT * FROM people").fetchall()
        # «Кадочников П.А.» и «Кадочников Павел Анатольевич» — один человек в короткой записи
        same = [r for r in rows if _parts(r["name"]) == (surname, initials) and initials]
        if len(same) == 1:
            return {"name": name, "status": "exact", "person": _card(c, same[0])}
        close = [_card(c, r) for r in rows if _alike(name, r["name"])]
        # Короткая запись — одна фамилия («Кравцов») или фамилия с именем («Кравцов Сергей»): так
        # пишет разбор со слов и из документа, где человек назван коротко, а в реестре он записан
        # полностью и с должностью. Это не exact (решает человек) и не unknown — показываем полную
        # запись как close, чтобы заменить одним нажатием (просьба пользователя 02.10).
        if len(surname) >= 3:
            short = key(name).replace(".", ". ").split()
            seen = {x["name"] for x in close}
            for r in rows:
                full = key(r["name"]).replace(".", ". ").split()
                if r["name"] in seen or not full or full[0] != surname or len(full) <= len(short):
                    continue
                # имя и отчество короткой записи — инициал («П.») совпадает по первой букве,
                # слово целиком («Павел») — только целиком: «Кадочников Пётр» — другой человек
                if all(fw.startswith(w) if len(w) == 1 else fw == w
                       for w, fw in zip((x.rstrip(".") for x in short[1:]), full[1:])):
                    close.append(_card(c, r))
    finally:
        c.close()
    if close:
        return {"name": name, "status": "close", "suggestions": close}
    return {"name": name, "status": "unknown"}


def duplicates() -> list[dict]:
    """Пары записей, похожих на одного человека, — на разбор администратору."""
    c = db.conn()
    try:
        rows = c.execute("SELECT * FROM people ORDER BY name").fetchall()
        cards = [_card(c, r) for r in rows]
    finally:
        c.close()
    pairs = []
    for i, a in enumerate(cards):
        for b in cards[i + 1:]:
            if _alike(a["name"], b["name"]):
                pairs.append({"a": a, "b": b, "same_role": bool(a["role"]) and a["role"] == b["role"]})
    return pairs


def merge(keep: str, drop: str, *, login: str = "") -> str:
    """Слить две записи в одну: остаётся `keep`, у неё дозаполняется пустое из `drop`,
    должность `drop`, если отличалась, уходит в варианты, сама `drop` удаляется."""
    k, d = key(keep), key(drop)
    if not k or not d or k == d:
        return ""
    c = db.conn()
    try:
        a = c.execute("SELECT * FROM people WHERE key = ?", (k,)).fetchone()
        b = c.execute("SELECT * FROM people WHERE key = ?", (d,)).fetchone()
        if not a or not b:
            return ""
        role = a["role"] or b["role"]
        org = a["organization"] or b["organization"]
        if b["role"] and a["role"] and b["role"] != a["role"]:
            c.execute("INSERT OR IGNORE INTO people_variants(key, role, organization, source, seen_at) "
                      "VALUES(?,?,?,?,?)", (k, b["role"], b["organization"] or org, "объединение", _now()))
        c.execute("UPDATE people_variants SET key = ? WHERE key = ?", (k, d))
        c.execute("UPDATE people SET role = ?, organization = ?, search = ?, updated_at = ?, updated_by = ? "
                  "WHERE key = ?", (role, org, key(f"{a['name']} {role} {org}"), _now(), login, k))
        c.execute("DELETE FROM people WHERE key = ?", (d,))
        c.commit()
        summary = f"«{b['name']}» слита в «{a['name']}»"
    finally:
        c.close()
    db.audit("Реестр людей: записи объединены", login=login, obj=a["name"], details=summary)
    return summary
