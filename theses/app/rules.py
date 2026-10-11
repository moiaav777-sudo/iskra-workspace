"""Правила написания тезисов: файлы из `docs/`.

Правила — закон (см. `CLAUDE.md`): формат, длина, порядок частей, обращения и запреты
берутся оттуда, а не из кода. Поэтому они лежат обычными файлами, правит их пользователь,
а сервис только читает и подставляет по выбранному человеку.

Под каждого человека может быть свой файл. Устройство — «общие правила + отличия»:
у профиля может быть базовый файл (общий для всех) и свой, который его дополняет.
Пока общего файла нет, профиль целиком описан своим файлом — дробить полученную
инструкцию на «общую» и «личную» части по своему усмотрению сервис не вправе.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

DOCS_DIR = Path(__file__).resolve().parent.parent / "docs"
SUFFIXES = (".md", ".txt", ".docx")
# файл-заглушка из заготовки репозитория: за правила не считается
PLACEHOLDER = "ПОЛОЖИТЕ-СЮДА-ПРАВИЛА"
# образец готового документа — это не правила, а эталон вёрстки для выгрузки
SAMPLE_WORDS = ("образец", "пример", "sample", "template", "шаблон")

_cache: dict[str, tuple[float, str]] = {}


@dataclass
class RulesFile:
    name: str          # имя файла в docs/
    title: str         # первый содержательный заголовок
    person: str        # для кого — если в тексте сказано «тезисы для …»
    chars: int
    mtime: float

    def to_dict(self) -> dict:
        return {"name": self.name, "title": self.title, "person": self.person,
                "chars": self.chars}


# ── чтение файлов ────────────────────────────────────────────────────────

def _read_docx(path: Path) -> str:
    """Текст из .docx без сторонних библиотек: абзацы из word/document.xml."""
    import zipfile
    from xml.etree import ElementTree as ET
    w = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    with zipfile.ZipFile(path) as z:
        root = ET.fromstring(z.read("word/document.xml"))
    lines = []
    for para in root.iter(w + "p"):
        lines.append("".join(node.text or "" for node in para.iter(w + "t")))
    return "\n".join(lines)


def _safe_path(name: str) -> Path | None:
    """Путь к файлу правил — только по имени файла внутри `docs/`.

    Имя приходит от человека (карточка, адрес запроса), а сервис на ПК работает от
    SYSTEM: «../../x.txt», «C:/…» или «..%5c» открывали бы любой файл (аудит Security
    21.09, high). Поэтому годится только голое имя без разделителей, и итоговый путь
    обязан остаться внутри `docs/`.
    """
    name = (name or "").strip()
    if (not name or name in (".", "..") or "/" in name or "\\" in name or "\0" in name
            or name != Path(name).name):
        return None
    docs = DOCS_DIR.resolve()
    path = (docs / name)
    try:
        resolved = path.resolve()
    except (OSError, RuntimeError):
        return None
    if resolved.parent != docs:
        return None
    return path


def read(name: str) -> str:
    """Текст файла правил с запоминанием по времени изменения."""
    path = _safe_path(name)
    if path is None or not _is_rules_file(path):
        raise FileNotFoundError(name)
    mtime = path.stat().st_mtime
    cached = _cache.get(name)
    if cached and cached[0] == mtime:
        return cached[1]
    text = _read_docx(path) if path.suffix.lower() == ".docx" else path.read_text(encoding="utf-8-sig")
    _cache[name] = (mtime, text)
    return text


def reload() -> None:
    """Забыть запомненное: правила поправили файлом, перечитываем без перезапуска."""
    _cache.clear()


def _is_docs_file(path: Path) -> bool:
    return (path.is_file() and path.suffix.lower() in SUFFIXES
            and PLACEHOLDER not in path.stem and not path.name.startswith("~$"))


def is_sample(path: Path | str) -> bool:
    """Образец оформления (`Пример оформления.docx`) — эталон вёрстки, а не правила."""
    stem = (path.stem if isinstance(path, Path) else Path(path).stem).lower()
    return any(word in stem for word in SAMPLE_WORDS)


def _is_rules_file(path: Path) -> bool:
    return _is_docs_file(path) and not is_sample(path)


# ── что лежит в docs/ ────────────────────────────────────────────────────

_HEADING = re.compile(r"^#{1,3}\s+(.+?)\s*$", re.MULTILINE)
_SERVICE_WORDS = re.compile(r"^(системная\s+инструкция|инструкция|правила|regulations?)\.?$", re.I)
_FOR_WHOM = re.compile(r"тезис\w*\s+(?:выступлени\w+\s+)?для\s+([^,;:\n]{3,60})", re.I)
_SPACES = re.compile(r"[ \t]+")


def _title_of(text: str) -> str:
    """Первый заголовок, который что-то называет: «СИСТЕМНАЯ ИНСТРУКЦИЯ» — не называет."""
    for match in _HEADING.finditer(text[:4000]):
        head = match.group(1).strip().strip("*")
        if head and not _SERVICE_WORDS.match(head):
            return head
    first = _HEADING.search(text[:4000])
    return first.group(1).strip() if first else ""


def _person_of(text: str) -> str:
    """Для кого правила: «Подготовка тезисов для Д.Н. Чернышенко» → «Д.Н. Чернышенко».

    Сначала смотрим заголовки: там имя записано коротко и без окончаний косвенного
    падежа, которые остаются во фразе «готовишь тезисы для Дмитрия Николаевича».
    """
    heads = "\n".join(m.group(1) for m in _HEADING.finditer(text[:4000]))
    for where in (heads, text[:4000]):
        match = _FOR_WHOM.search(where)
        if not match:
            continue
        name = _SPACES.sub(" ", match.group(1)).strip().strip("*").strip()
        name = re.sub(r"[\s.]+$", "", name)
        if name:
            return name
    return ""


def samples() -> list[str]:
    """Образцы готового документа, лежащие в `docs/`."""
    if not DOCS_DIR.exists():
        return []
    return [p.name for p in sorted(DOCS_DIR.iterdir())
            if _is_docs_file(p) and is_sample(p)]


def files() -> list[RulesFile]:
    if not DOCS_DIR.exists():
        return []
    out = []
    for path in sorted(DOCS_DIR.iterdir()):
        if not _is_rules_file(path):
            continue
        try:
            text = read(path.name)
        except Exception:
            continue
        out.append(RulesFile(name=path.name, title=_title_of(text), person=_person_of(text),
                             chars=len(text), mtime=path.stat().st_mtime))
    return out


def exists(name: str) -> bool:
    """Есть ли такой файл правил в `docs/`. Любой путь, а не имя, — нет."""
    path = _safe_path(name)
    return path is not None and _is_rules_file(path)


# ── сборка правил под человека ───────────────────────────────────────────

def compose(base_file: str = "", own_file: str = "", person: str = "") -> str:
    """Правила, по которым пишется этот текст: общие, затем отличия под человека.

    Отличия идут после общих намеренно: при расхождении сильнее то, что сказано
    про конкретного человека.
    """
    parts = []
    if base_file and exists(base_file):
        parts.append(read(base_file).strip())
    if own_file and exists(own_file):
        own = read(own_file).strip()
        if parts:
            who = f" для {person}" if person else ""
            parts.append(f"# Отличия{who}\n\nНиже — правила, которые сильнее общих.\n\n{own}")
        else:
            parts.append(own)
    return "\n\n---\n\n".join(parts)
