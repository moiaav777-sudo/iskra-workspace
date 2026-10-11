"""Перенос пользователей из «Модератора» в портал (идемпотентно).

    python scripts/migrate_users.py путь\\к\\meetings.db

Хеши паролей переносятся как есть — пароли остаются прежними. Слабые пароли
(совпадает с логином или из стартового набора) помечаются: портал заставит сменить их при входе.
Администраторы «Модератора» становятся администраторами портала, остальные получают доступ к «Модератору».
"""
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import auth, db  # noqa: E402

WEAK = ["admin123", "admin", "password", "12345678", "123456"]


def weak(login: str, password_hash: str) -> bool:
    return any(auth.verify_password(p, password_hash) for p in [login, *WEAK])


def main(path: str) -> None:
    src = sqlite3.connect(path)
    src.row_factory = sqlite3.Row
    rows = src.execute("SELECT login, password_hash, is_admin FROM users ORDER BY login").fetchall()
    src.close()
    for r in rows:
        if db.get_user(r["login"]):
            print(f"уже есть: {r['login']}")
            continue
        role = "admin" if r["is_admin"] else "employee"
        must = weak(r["login"], r["password_hash"])
        db.create_user(r["login"], r["password_hash"], role=role, services=["moderator"], must_change_password=must)
        db.audit("Перенесён из «Модератора»", obj=r["login"],
                 details=f"роль: {role}" + ("; слабый пароль — смена при входе" if must else ""))
        print(f"перенесён: {r['login']} ({role}{', сменить пароль' if must else ''})")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    main(sys.argv[1])
