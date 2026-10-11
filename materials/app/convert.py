"""Документ пакета в другом формате: Word → PDF и PDF → Word.

«Совещание» на вкладке «Пакет к совещанию» даёт скачать каждый документ в Word
или PDF. Файлы лежат у нас, поэтому и переводим мы: `GET …/download?as=pdf|docx`.

Переводит LibreOffice без окон (`soffice --headless --convert-to`). Он тяжёлый,
а сервер слабый (1 CPU, 2 ГБ), поэтому:

- одновременно идёт одна конвертация (`_lock`), остальные ждут своей очереди;
- у каждой — срок (`MATERIALS_CONVERT_TIMEOUT`, по умолчанию 120 с), по истечении
  процесс снимается вместе с дочерними (`soffice` — обёртка над `soffice.bin`);
- результат кладётся в кэш `data/convert/<мероприятие>/` и отдаётся повторно, пока
  исходник тот же. Размер и mtime исходника записаны в имени результата: поменялся
  файл — имя другое, конвертация заново, а прежний результат стирается. Отдаваемый
  файл на месте не переписывается — тот, кто его сейчас качает, докачает целым.

Кэш живёт рядом с базой (`store.data_dir()`), а не в папке мероприятия: в пакет,
в zip и в карточку он не попадает. Потеряется — переведём заново.

LibreOffice без своего профиля падает под systemd (нет записываемого HOME), поэтому
каждой конвертации — свой временный профиль и свой HOME. Исходник копируется во
временную папку под латинским именем: имя по-русски с пробелами под службой с
`LANG=C` LibreOffice открывает не всегда.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from . import naming, portal, store

log = logging.getLogger("materials.convert")

FORMATS = ("pdf", "docx")
# Что во что умеем. Пары не из списка — 400: перевод xlsx в Word никому не нужен,
# а картинку LibreOffice «переведёт» в пустой документ.
SOURCES = {
    "pdf": ("docx", "doc", "odt", "rtf", "pptx", "xlsx", "html", "htm"),
    "docx": ("pdf", "odt", "doc", "rtf", "html", "htm"),
}
MEDIA = {
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}
# Фильтры вывода. Для docx фильтр назван явно: «docx» без имени LibreOffice
# сопоставляет с фильтром сам и после импорта PDF иногда не находит подходящего.
TARGET = {"pdf": "pdf", "docx": "docx:MS Word 2007 XML"}
# Фильтры ввода. PDF без подсказки открывается в Draw, а из Draw в Word не выводится —
# нужен импорт в Writer. HTML без подсказки открывается в Writer/Web, у которого
# нет вывода в Word, — открываем обычным Writer.
INFILTER = {"pdf": "writer_pdf_import", "html": "HTML (StarWriter)", "htm": "HTML (StarWriter)"}

NOT_INSTALLED = ("На сервере нет LibreOffice — конвертация в PDF/Word пока недоступна, "
                 "скачайте оригинал")
CHECK_TTL = 300                 # как долго верим найденному (или ненайденному) soffice, с
DEFAULT_TIMEOUT = 120
KEEP_NAME = 200                 # длиннее (в байтах) — имя в кэше заменяется отпечатком

_lock = threading.Lock()
_found: dict[tuple[str, str], tuple[str | None, float]] = {}


class Unsupported(ValueError):
    """Такую пару форматов не переводим (400)."""


class NotInstalled(RuntimeError):
    """LibreOffice на сервере нет (501)."""


class Failed(RuntimeError):
    """LibreOffice не справился или не уложился в срок (502). Текст — для человека."""


class Busy(RuntimeError):
    """Очередь на конвертацию не подошла за срок (503)."""


# ── где LibreOffice ──────────────────────────────────────────────────────

def _executable(path: str) -> bool:
    return bool(path) and os.path.isfile(path) and os.access(path, os.X_OK)


def _lookup() -> str | None:
    custom = portal.env("MATERIALS_SOFFICE").strip()
    if custom:
        if _executable(custom):
            return custom
        log.warning("MATERIALS_SOFFICE указывает не на исполняемый файл, ищем soffice в PATH")
    return shutil.which("soffice") or shutil.which("libreoffice")


def binary() -> str | None:
    """Путь к soffice или None. Запоминается на несколько минут: карточку открывают
    часто, а ходить на диск за ответом на один и тот же вопрос незачем. Ключ кэша —
    сами настройки поиска: поменяли MATERIALS_SOFFICE или PATH — ищем заново."""
    key = (portal.env("MATERIALS_SOFFICE"), os.environ.get("PATH", ""))
    hit = _found.get(key)
    now = time.monotonic()
    if hit is not None and now - hit[1] < CHECK_TTL:
        return hit[0]
    path = _lookup()
    if hit is None or hit[0] != path:
        log.info("LibreOffice для конвертации: %s", path or "не найден")
    _found[key] = (path, now)
    return path


def forget() -> None:
    """Забыть найденное: soffice пропал с диска — при следующем вопросе поищем заново."""
    _found.clear()


def status() -> dict:
    """Для карточки мероприятия: «Совещание» по нему решает, рисовать ли кнопки."""
    return {"available": binary() is not None, "formats": list(FORMATS),
            "sources": {fmt: list(src) for fmt, src in SOURCES.items()}}


def timeout() -> float:
    try:
        value = float(portal.env("MATERIALS_CONVERT_TIMEOUT", str(DEFAULT_TIMEOUT)))
    except ValueError:
        return DEFAULT_TIMEOUT
    return value if value > 0 else DEFAULT_TIMEOUT


# ── проверка и кэш ───────────────────────────────────────────────────────

def check(name: str, fmt: str) -> None:
    """Переводим ли этот файл в этот формат. Нет — Unsupported с понятным текстом."""
    ext = naming.split_ext(name)[1]
    if ext not in SOURCES[fmt]:
        what = f"«.{ext}»" if ext else "без расширения"
        can = ", ".join(SOURCES[fmt])
        raise Unsupported(f"Файл {what} в {fmt.upper()} не переводится — "
                          f"в {fmt.upper()} переводятся: {can}. Скачайте оригинал")


def _key(name: str) -> str:
    """Имя исходника в кэше: само имя, а очень длинное — отпечаток (иначе вместе
    с хвостом версии оно не влезет в предел имени файла)."""
    if len(name.encode("utf-8")) <= KEEP_NAME:
        return name
    return hashlib.sha1(name.encode("utf-8")).hexdigest()[:20]


def _cache_dir(eid: str, archived: bool) -> Path:
    path = store.data_dir() / "convert" / eid
    return path / "Архив" if archived else path


def cached_path(src: Path, eid: str, fmt: str, archived: bool = False) -> Path:
    """Где лежит (или ляжет) перевод именно этой версии исходника."""
    st = src.stat()
    return _cache_dir(eid, archived) / f"{_key(src.name)}~{st.st_size}-{st.st_mtime_ns}.{fmt}"


def _drop_old(target: Path) -> None:
    """Стереть переводы прежних версий того же исходника в тот же формат."""
    head, _, _ = target.name.rpartition("~")
    fmt = target.suffix
    for old in target.parent.glob("*" + fmt):
        stem, _, _ = old.name.rpartition("~")
        if stem == head and old.name != target.name:
            try:
                old.unlink()
            except OSError:
                pass            # на Windows открытый файл не стирается — сотрём в другой раз


_VERSION = re.compile(r"^(?P<key>.+)~(?P<size>\d+)-(?P<mtime>\d+)\.(?P<fmt>pdf|docx)$")


def _alive(folder: Path) -> set[str]:
    """Ключи и версии исходников, которые сейчас лежат в папке: «<ключ>~<размер>-<mtime>»."""
    out = set()
    if not folder.is_dir():
        return out
    for f in folder.iterdir():
        if f.is_file():
            st = f.stat()
            out.add(f"{_key(f.name)}~{st.st_size}-{st.st_mtime_ns}")
    return out


def prune(event_path: Path, eid: str) -> int:
    """Стереть переводы, чьего исходника (в той же версии) в папке мероприятия больше нет.

    Зовётся после любого изменения папки: документ убрали, переименовали, перенумеровали,
    первичку стёрли — иначе в `data/convert/` оставалась бы полная копия документа,
    которого в пакете уже нет (ревьюер RC-2026-10-02-8, Н-1). Возвращает число стёртых.
    """
    gone = 0
    for sub, folder in (("", event_path), ("Архив", event_path / "Архив")):
        cache = _cache_dir(eid, bool(sub))
        if not cache.is_dir():
            continue
        alive = _alive(folder)
        for f in cache.iterdir():
            if not f.is_file():
                continue
            m = _VERSION.match(f.name)
            if m and f"{m['key']}~{m['size']}-{m['mtime']}" in alive:
                continue
            try:
                f.unlink()
                gone += 1
            except OSError:
                pass            # открытый файл на Windows — сотрём в другой раз
    return gone


def forget_event(eid: str) -> None:
    """Мероприятие стёрто с диска или убрано — его переводы тоже (ревьюер RC-2026-10-02-8, Н-1; RC-9, Н-7)."""
    shutil.rmtree(store.data_dir() / "convert" / eid, ignore_errors=True)


def move_event(old_eid: str, new_eid: str) -> None:
    """Папка мероприятия переехала (дата или название → другой id): кэш едет следом,
    иначе он остался бы сиротой навсегда и «стереть с диска» его бы не увидело
    (ревьюер RC-2026-10-02-9, Н-7)."""
    if old_eid == new_eid:
        return
    base = store.data_dir() / "convert"
    src, dst = base / old_eid, base / new_eid
    if not src.is_dir():
        return
    if dst.exists():
        shutil.rmtree(dst, ignore_errors=True)   # переводы под новым id не могут быть свежее
    try:
        src.rename(dst)
    except OSError:
        shutil.rmtree(src, ignore_errors=True)


def prune_orphans(alive: set[str]) -> int:
    """Снести кэш мероприятий, которых больше нет (переехали мимо сервиса, убраны, стёрты
    старым кодом). Зовётся при старте сервиса (ревьюер RC-2026-10-02-9, Н-7)."""
    base = store.data_dir() / "convert"
    if not base.is_dir():
        return 0
    gone = 0
    for d in base.iterdir():
        if d.is_dir() and d.name not in alive:
            shutil.rmtree(d, ignore_errors=True)
            gone += 1
    if gone:
        log.info("кэш переводов: убрано осиротевших мероприятий — %d", gone)
    return gone


# ── сама конвертация ─────────────────────────────────────────────────────

_PATHS = re.compile(r"(?:[A-Za-z]:)?(?:[\\/][^\s\\/:'\"]+){2,}[\\/]?")


def _tail(text: str, lines: int = 5, limit: int = 400) -> str:
    """Последние строки вывода LibreOffice без путей сервера."""
    rows = [_PATHS.sub("…", r).strip() for r in (text or "").splitlines()]
    rows = [r for r in rows if r and "javaldx" not in r]     # шум, к делу не относится
    out = " | ".join(rows[-lines:])
    return out[-limit:]


def _kill(proc: subprocess.Popen) -> None:
    try:
        if os.name != "nt":
            os.killpg(proc.pid, signal.SIGKILL)
        else:
            proc.kill()
    except (OSError, ProcessLookupError):
        pass


def _expendable(pid: int) -> None:
    """При нехватке памяти на сервере первым должен погибнуть LibreOffice, а не сервис:
    поднимаем ему oom_score_adj до предела (потомки — soffice.bin — наследуют). Только Linux,
    без прав это разрешено только вверх (ревьюер RC-2026-10-02-8, Н-2)."""
    try:
        Path(f"/proc/{pid}/oom_score_adj").write_text("1000")
    except OSError:
        pass


def _run(soffice: str, src: Path, fmt: str, out_file: Path) -> None:
    """Один запуск LibreOffice. Результат — в `out_file` (через временный и replace)."""
    ext = naming.split_ext(src.name)[1]
    limit = timeout()
    with tempfile.TemporaryDirectory(prefix="materials-convert-") as tmp_name:
        tmp = Path(tmp_name)
        (tmp / "in").mkdir()
        (tmp / "out").mkdir()
        (tmp / "home").mkdir()
        source = tmp / "in" / f"source.{ext}"
        shutil.copy2(src, source)
        cmd = [soffice, "--headless", "--norestore", "--nologo", "--nodefault",
               "--nolockcheck", f"-env:UserInstallation={(tmp / 'profile').as_uri()}"]
        if ext in INFILTER:
            cmd.append(f"--infilter={INFILTER[ext]}")
        cmd += ["--convert-to", TARGET[fmt], "--outdir", str(tmp / "out"), str(source)]
        env = {**os.environ, "HOME": str(tmp / "home"), "TMPDIR": str(tmp)}
        started = time.monotonic()
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    stdin=subprocess.DEVNULL, cwd=tmp, env=env,
                                    start_new_session=os.name != "nt")
        except OSError as e:            # soffice убрали с диска после проверки
            forget()
            log.warning("LibreOffice не запустился: %s", e)
            raise NotInstalled(NOT_INSTALLED) from e
        _expendable(proc.pid)
        try:
            stdout, stderr = proc.communicate(timeout=limit)
        except subprocess.TimeoutExpired:
            _kill(proc)
            try:
                proc.communicate(timeout=5)     # без срока зависли бы, уйди потомок из группы (ревьюер RC-8, Н-6)
            except subprocess.TimeoutExpired:
                pass
            log.warning("конвертация в %s не уложилась в %.0f с", fmt, limit)
            raise Failed(f"LibreOffice не уложился в {limit:.0f} с — "
                         f"скачайте оригинал или попробуйте позже")
        finally:
            if proc.poll() is None:
                _kill(proc)
        said = _tail(stderr.decode("utf-8", "replace") + "\n" + stdout.decode("utf-8", "replace"))
        result = tmp / "out" / f"source.{fmt}"
        if proc.returncode != 0 or not result.is_file() or result.stat().st_size == 0:
            # LibreOffice часто отвечает кодом 0, но файла не кладёт («source file
            # could not be loaded»): судим по результату, а не по коду
            log.warning("конвертация .%s → %s не удалась, код %s: %s",
                        ext, fmt, proc.returncode, said)
            raise Failed(f"LibreOffice не смог перевести файл в {fmt.upper()} "
                         f"(код {proc.returncode})" + (f": {said}" if said else ""))
        out_file.parent.mkdir(parents=True, exist_ok=True)
        part = out_file.with_name(out_file.name + ".part")
        shutil.copyfile(result, part)
        os.replace(part, out_file)
        log.info("конвертация .%s → %s за %.1f с", ext, fmt, time.monotonic() - started)


def convert(src: Path, eid: str, fmt: str, archived: bool = False) -> Path:
    """Путь к переводу `src` в `fmt`: из кэша или свежий. Исключения — см. классы выше."""
    check(src.name, fmt)
    target = cached_path(src, eid, fmt, archived)
    if target.is_file():
        return target
    soffice = binary()
    if soffice is None:
        raise NotInstalled(NOT_INSTALLED)
    # ждём очереди не дольше двух сроков: дольше человек всё равно не ждёт
    if not _lock.acquire(timeout=timeout() * 2):
        raise Busy("Конвертер занят другим документом — попробуйте через минуту")
    try:
        target = cached_path(src, eid, fmt, archived)   # пока ждали, исходник мог смениться
        if target.is_file():                            # или его уже перевёл сосед по очереди
            return target
        _run(soffice, src, fmt, target)
        _drop_old(target)
        return target
    finally:
        _lock.release()
