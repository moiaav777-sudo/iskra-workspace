"""Кэш прочитанного текста.

Хранилище — это папки на диске, база ничего не решает: она только помнит, что было
в документе, чтобы поиск не перечитывал каждый раз все файлы. Потеряется — сервис
перечитает документы заново, ничего не пропадёт.
"""
from __future__ import annotations

import os
import sqlite3
import threading
from pathlib import Path

from . import doc_text, similar, tree

_local = threading.local()
MAX_READ = 30 * 1024 * 1024      # больше 30 МБ не читаем: это уже не справка
# Правила чтения названия (doc_text). Поменяли их — прочитанное раньше название
# устарело, и документы нужно перечитать: иначе на ПК останутся старые предложения.
RULES = 3


def data_dir() -> Path:
    path = Path(os.environ.get("MATERIALS_DATA") or (Path(__file__).resolve().parent.parent / "data"))
    path.mkdir(parents=True, exist_ok=True)
    return path


def conn() -> sqlite3.Connection:
    if getattr(_local, "conn", None) is None:
        c = sqlite3.connect(data_dir() / "materials.db", timeout=15)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("""CREATE TABLE IF NOT EXISTS text_cache (
            path TEXT PRIMARY KEY, mtime REAL, size INTEGER, title TEXT, text TEXT,
            rules INTEGER DEFAULT 0, origin TEXT DEFAULT '', hash TEXT DEFAULT '')""")
        for column, kind in (("rules", "INTEGER DEFAULT 0"), ("origin", "TEXT DEFAULT ''"),
                             ("hash", "TEXT DEFAULT ''")):
            try:
                c.execute(f"ALTER TABLE text_cache ADD COLUMN {column} {kind}")
            except sqlite3.OperationalError:
                pass                  # столбец уже есть — база новая
        c.execute("""CREATE TABLE IF NOT EXISTS settings (
            login TEXT, key TEXT, value TEXT, PRIMARY KEY (login, key))""")
        c.commit()
        _local.conn = c
    return _local.conn


def setting(login: str, key: str) -> str:
    """Мелкая настройка человека. Живёт в кэше: потеряется — вернётся обычный ход."""
    row = conn().execute("SELECT value FROM settings WHERE login = ? AND key = ?",
                         (login, key)).fetchone()
    return (row["value"] if row else "") or ""


def set_setting(login: str, key: str, value: str) -> None:
    c = conn()
    c.execute("INSERT OR REPLACE INTO settings (login, key, value) VALUES (?,?,?)",
              (login, key, value or ""))
    c.commit()


def remember(path: Path, data: bytes, origin: str = "") -> str:
    """Запомнить текст только что загруженного файла; вернуть предложенное название.

    `origin` — имя, под которым файл прислали. На диске оно после раскладки не живёт
    (там уже новое), поэтому помним здесь: человеку важно видеть, из чего получилось
    предложенное название. Перечитываем файл — прежнее исходное имя не затираем.
    """
    title = doc_text.propose(origin or path.name, data)
    text = doc_text.text(path.name, data)
    try:
        stat = path.stat()
    except OSError:
        return title
    c = conn()
    if not origin:
        row = c.execute("SELECT origin FROM text_cache WHERE path = ?", (_key(path),)).fetchone()
        origin = (row["origin"] if row else "") or path.name
    c.execute("INSERT OR REPLACE INTO text_cache"
              " (path, mtime, size, title, text, rules, origin, hash) VALUES (?,?,?,?,?,?,?,?)",
              (_key(path), stat.st_mtime, stat.st_size, title, text, RULES, origin,
               similar.digest(data)))
    c.commit()
    return title


def about(path: Path) -> dict:
    """Всё, что помним о файле: название, текст и отпечаток. Не помним — перечитаем."""
    known = cached(path)
    row = conn().execute("SELECT origin, hash FROM text_cache WHERE path = ?",
                         (_key(path),)).fetchone()
    return {**known, "origin": (row["origin"] if row else "") or "",
            "hash": (row["hash"] if row else "") or ""}


def set_title(path: Path, title: str) -> None:
    """Название, вписанное человеком. Оно главнее того, что сервис вычитал
    из документа: человек видел документ и решил, как его звать."""
    c = conn()
    c.execute("UPDATE text_cache SET title = ? WHERE path = ?", (title, _key(path)))
    c.commit()


def origin(path: Path) -> str:
    """Имя, под которым файл прислали. Не помним — вернём пустое, и строки просто не будет."""
    row = conn().execute("SELECT origin FROM text_cache WHERE path = ?", (_key(path),)).fetchone()
    return (row["origin"] if row else "") or ""


def _key(path: Path) -> str:
    try:
        return str(path.relative_to(tree.root())).replace("\\", "/")
    except ValueError:
        return str(path)


def cached(path: Path) -> dict:
    """Текст и заголовок документа: из кэша, а если файл изменился — перечитать."""
    try:
        stat = path.stat()
    except OSError:
        return {"title": "", "text": ""}
    key = _key(path)
    row = conn().execute("SELECT * FROM text_cache WHERE path = ?", (key,)).fetchone()
    if (row and row["rules"] == RULES
            and abs(row["mtime"] - stat.st_mtime) < 1 and row["size"] == stat.st_size):
        return {"title": row["title"] or "", "text": row["text"] or ""}
    if stat.st_size > MAX_READ or doc_text.kind_of(path.name) not in doc_text.READABLE:
        return {"title": "", "text": ""}
    try:
        data = path.read_bytes()
    except OSError:
        return {"title": "", "text": ""}
    title = remember(path, data)
    return {"title": title, "text": doc_text.text(path.name, data)}


def forget(path: Path) -> None:
    c = conn()
    c.execute("DELETE FROM text_cache WHERE path = ?", (_key(path),))
    c.commit()


def move_folder(old: Path, new: Path) -> None:
    """Папку переименовали или перенесли — перенести и ключи кэша.

    Иначе вместе с папкой теряются исходные имена файлов: они привязаны к пути.
    """
    c = conn()
    old_key, new_key = _key(old) + "/", _key(new) + "/"
    # «_» и «%» в LIKE — подстановочные знаки, а в названии папки они обычные:
    # без экранирования «План_А» зацепил бы и «План-А»
    like = old_key.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    c.execute("DELETE FROM text_cache WHERE path LIKE ? || '%' ESCAPE '\\'",
              (new_key.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_"),))
    c.execute("UPDATE text_cache SET path = ? || substr(path, ?)"
              " WHERE path LIKE ? || '%' ESCAPE '\\'",
              (new_key, len(old_key) + 1, like))
    c.commit()


def rename(old: Path, new: Path) -> None:
    """Файл переехал — переносим и запись о нём.

    Сначала убираем запись на новом месте: файлы перекладываются в два прохода,
    а кэш в один, и при обмене местами двух одинаковых названий вторая запись
    упиралась бы в занятый ключ.
    """
    c = conn()
    c.execute("DELETE FROM text_cache WHERE path = ?", (_key(new),))
    c.execute("UPDATE text_cache SET path = ? WHERE path = ?", (_key(new), _key(old)))
    c.commit()
