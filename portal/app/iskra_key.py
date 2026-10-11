"""Единый ключ Искры: администратор меняет его в портале, портал записывает его во все сервисы.

Ключ нужен шести местам: портал («Спросить Искру»), «Совещание», «Бриф», A3, «Повестка»,
«Тезисы». «Бриф» читает iskra_key из settings.json, остальные — ISKRA_API_KEY из своего .env.
Все читают ключ при каждом вызове, перезапуск не нужен. «Материалы» с «Искрой» не работают.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
from pathlib import Path

from . import config


class KeyError_(Exception):
    pass


# Срок ключа (справка «Искры», 05.10): ключ живёт 7–365 дней, по умолчанию 30, API срок не отдаёт (/me — только права).
# Администратор записывает дату окончания сам при вводе ключа; за EXPIRES_WARN_DAYS портал предупреждает
# на главной (лампочка), в админке и в суточном отчёте. Хранится в .env портала строкой ISKRA_KEY_EXPIRES=ГГГГ-ММ-ДД.
EXPIRES_VAR = "ISKRA_KEY_EXPIRES"
EXPIRES_WARN_DAYS = 7


def parse_expires(raw: str) -> dt.date | None:
    raw = (raw or "").strip()
    if not raw:
        return None
    for fmt in ("%Y-%m-%d", "%d.%m.%Y"):
        try:
            return dt.datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    raise KeyError_("Дата окончания ключа — в виде ГГГГ-ММ-ДД или ДД.ММ.ГГГГ")


def expires_info(today: dt.date | None = None) -> dict:
    """{expires: 'ГГГГ-ММ-ДД' | '', days_left: int | None, warn: bool, expired: bool}."""
    today = today or dt.date.today()
    try:
        when = parse_expires(config.read_env(EXPIRES_VAR))
    except KeyError_:
        when = None
    if not when:
        return {"expires": "", "days_left": None, "warn": False, "expired": False}
    days = (when - today).days
    return {"expires": when.isoformat(), "days_left": days, "warn": days <= EXPIRES_WARN_DAYS, "expired": days < 0}


def expires_note(today: dt.date | None = None) -> str:
    """Короткая фраза для лампочки и отчёта; пустая, если предупреждать не о чем."""
    info = expires_info(today)
    if not info["expires"]:
        return ""
    d = dt.date.fromisoformat(info["expires"]).strftime("%d.%m.%Y")
    if info["expired"]:
        return f"срок ключа истёк {d} — нужен новый ключ"
    if info["warn"]:
        n = info["days_left"]
        return f"ключ истекает {d} (через {n} дн.) — выпустите новый в Искре" if n else f"ключ истекает сегодня ({d}) — выпустите новый в Искре"
    return ""


def set_expires(raw: str) -> dict:
    """Записать (или стереть пустой строкой) дату окончания ключа в .env портала."""
    when = parse_expires(raw)
    _set_env_line(config.ENV_PATH, EXPIRES_VAR, when.isoformat() if when else "")
    return expires_info()


# Куда портал записывает ключ: имя сервиса → (переменная с его папкой, файл внутри папки).
# «Бриф» хранит ключ в settings.json, остальные — в .env рядом с приложением.
KEY_TARGETS = [
    ("Совещание", "MODERATOR_DIR", ".env"),
    ("Бриф", "BRIEF_HOME", "settings.json"),
    ("Инфографика A3", "A3_HOME", ".env"),
    ("Тезисы", "THESES_HOME", ".env"),
    ("Транскрибатор", "TRANSCRIBER_HOME", ".env"),   # решение пользователя 02.10: распознаёт Искра
    ("Объективки", "PROFILES_HOME", ".env"),          # решение пользователя 05.10: ищет и разбирает Искра
]


def _targets() -> dict[str, Path]:
    t = {"портал": config.ENV_PATH}
    for name, env_var, filename in KEY_TARGETS:
        home = config.read_env(env_var)
        if home:
            t[name] = Path(home) / filename
    return t


def _replace_file(path: Path, text: str) -> None:
    """Атомарная запись секретного файла. Временный файл создаётся с правами 600 (замечание сессии сервера 05.10:
    запись через tmp + os.replace давала .env права 664 — читали все локальные учётки); у существующего файла
    права сохраняются (на VPS это 660 terra:deploy)."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.fchmod(fd, 0o600)            # ревью RC-23, Н-2: старый .tmp с 664 не должен протащить свои права
    except OSError:
        pass
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    if path.exists():
        try:
            st = os.stat(path)
            os.chown(tmp, -1, st.st_gid)    # ревью RC-23, Н-1: группу (terra:deploy) тоже сохраняем, не только права
            os.chmod(tmp, st.st_mode & 0o777)
        except OSError:
            pass
    os.replace(tmp, path)


def _set_env_line(path: Path, name: str, value: str) -> None:
    lines = path.read_text(encoding="utf-8-sig").splitlines() if path.exists() else []
    lines = [l for l in lines if not l.strip().startswith(name + "=")]
    lines.append(f"{name}={value}")
    _replace_file(path, "\n".join(lines) + "\n")


def _read_env_line(path: Path, name: str) -> str:
    if not path.exists():
        return ""
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        if line.strip().startswith(name + "="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


def _brief_key(path: Path) -> str:
    try:
        return str(json.loads(path.read_text(encoding="utf-8-sig")).get("iskra_key") or "")
    except (OSError, ValueError):
        return ""


def _hint(key: str) -> str:
    return ("•••• " + key[-4:]) if len(key) >= 8 else ("задан" if key else "не задан")


def _fingerprint(key: str) -> str:
    """Отпечаток ключа: по нему видно, везде ли ключ один, не показывая сам ключ."""
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:12] if key else ""


def _key_of(path: Path) -> str:
    return _brief_key(path) if path.name == "settings.json" else _read_env_line(path, "ISKRA_API_KEY")


def status() -> dict:
    """Кому разослан ключ и один ли он у всех. Сам ключ наружу не уходит — только хвост и
    признак «отличается от ключа портала». 21.09 нашлось, что у «Брифа» свой ключ: он был
    введён в его собственных настройках в обход портала, и расход делился на два ключа."""
    targets = _targets()
    portal_print = _fingerprint(_key_of(targets["портал"]))
    out, prints = [], set()
    for name, path in targets.items():
        fp = _fingerprint(_key_of(path))
        out.append({"service": name, "hint": _hint(_key_of(path)), "exists": path.exists(),
                    "differs": fp != portal_print})
        prints.add(fp)
    # сравниваем отпечатки, а не последние четыре знака: разные ключи могут кончаться одинаково
    return {"targets": out, "same": len(prints) == 1, **expires_info()}


def spread() -> dict:
    """Разослать всем тот ключ, что стоит в портале, — свести разошедшиеся одним нажатием,
    не вводя ключ заново."""
    key = _key_of(_targets()["портал"]).strip()
    if not key:
        raise KeyError_("В портале ключ не задан — сначала введите его")
    return set_key(key)


def set_key(key: str) -> dict:
    """Записать ключ во все сервисы. Если куда-то не записалось — сказать прямо, куда именно."""
    if any(ch.isspace() for ch in key):
        raise KeyError_("В ключе не должно быть пробелов и переносов строк")
    written, failed = [], []
    for name, path in _targets().items():
        if name != "портал" and not path.parent.exists():
            continue
        try:
            if path.name == "settings.json":
                try:
                    data = json.loads(path.read_text(encoding="utf-8-sig")) if path.exists() else {}
                except ValueError:
                    data = {}
                data["iskra_key"] = key
                data.pop("verified_sig", None)
                _replace_file(path, json.dumps(data, ensure_ascii=False, indent=2))
            else:
                _set_env_line(path, "ISKRA_API_KEY", key)
            written.append(name)
        except OSError as e:
            failed.append(f"{name} ({e.strerror or e})")
    if failed:
        # Часть сервисов уже получила новый ключ — говорим прямо, где он не записался.
        raise KeyError_("Ключ записан в: " + (", ".join(written) or "нигде")
                        + ". Не удалось записать в: " + ", ".join(failed))
    return {"written": written}
