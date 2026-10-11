"""ВЫВЕДЕН ИЗ РАБОТЫ 21.09: старые списки были засорены тестовыми записями, реестр очищен
и собирается заново только из настоящих документов. Не запускать.

Собрать реестр людей из двух прежних: «Повестки» и «Модератора» (сервис «Совещание»).

    .venv/bin/python scripts/import_people.py            — перенести
    .venv/bin/python scripts/import_people.py --dry-run  — только посчитать

Порядок важен. Реестр «Повестки» — выверенный: туда должности набирали руками, поэтому он
ложится первым и как правка человека. Реестр «Модератора» наполняла в основном «Искра»,
поэтому он только дозаполняет пустое, а разошедшиеся должности ложатся в варианты —
человек выберет на странице «Люди». Ничего не выбрасывается.

Источники открываются только на чтение. Повторный запуск безопасен: совпадающее не
меняется. Печатаются одни числа — фамилии реальных людей в вывод не попадают.
"""
from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import config, db, people  # noqa: E402


def _rows(path: Path) -> list[dict]:
    if not path.exists():
        print(f"  нет файла: {path.name}")
        return []
    c = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    try:
        if not c.execute("SELECT name FROM sqlite_master WHERE name = 'registry'").fetchone():
            print(f"  в {path.name} реестра нет")
            return []
        return [dict(r) for r in c.execute("SELECT display_name, role, organization FROM registry")]
    finally:
        c.close()


def main() -> None:
    if "--i-know" not in sys.argv:
        sys.exit("Импорт выведен из работы 21.09: он вернул бы в реестр тестовые записи.")
    dry = "--dry-run" in sys.argv
    # На ПК база «Повестки» лежит в <AGENDA_HOME>\data, на стенде — прямо в AGENDA_DATA.
    agenda_dir = os.environ.get("AGENDA_DATA") or str(Path(config.read_env("AGENDA_HOME")) / "data")
    agenda = Path(agenda_dir) / "agenda.db"
    moderator = Path(config.read_env("MODERATOR_DIR") or os.environ.get("MODERATOR_DIR", "")) / "data" / "meetings.db"
    before = people.count()

    stats = {}
    for label, path, force in (("Повестка", agenda, True), ("Совещание", moderator, False)):
        rows = _rows(path)
        changed = 0
        if not dry:
            for r in rows:
                if people.remember(r["display_name"], r["role"], r["organization"], force=force,
                                   login="перенос", source="agenda" if force else "moderator"):
                    changed += 1
        stats[label] = (len(rows), changed)

    c = db.conn()
    try:
        variants = c.execute("SELECT COUNT(DISTINCT key) FROM people_variants").fetchone()[0]
    finally:
        c.close()
    for label, (seen, changed) in stats.items():
        print(f"  {label}: записей {seen}, внесено или дополнено {changed}")
    print(f"  в реестре было {before}, стало {people.count()}; людей с расхождением в должности: {variants}")
    if dry:
        print("  (пробный прогон — ничего не записано)")


if __name__ == "__main__":
    main()
