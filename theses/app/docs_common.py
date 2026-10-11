"""Общий модуль чтения документов: текст из типовых файлов и конвертация через LibreOffice.

Решение пользователя 05.10: поля загрузки во всех сервисах принимают типовые файлы любых типов —
Word (.docx, .doc), Excel (.xlsx, .xls), PDF, PowerPoint (.pptx, .ppt), а также .rtf, .odt, .txt, .md,
.csv. Раньше каждый сервис читал файлы по-своему и понимал разный набор форматов.

ЭТОТ ФАЙЛ ПРАВИТСЯ ТОЛЬКО В `portal/shared/docs_common.py`.
В сервисы он раскладывается скриптом `portal/scripts/sync_shared.sh` — правка копии будет затёрта.

Интерфейс:
    extract_text(data: bytes, name: str) -> str        текст документа; не вышло — DocsError (по-русски)
    convert(data, name, to) -> bytes                     через soffice: to = "docx" | "pdf" | "txt"
                                                         (и служебные "xlsx", "pptx")
    sniff(data, name) -> kind                            формат по содержимому, не по расширению:
                                                         "docx" "doc" "xlsx" "xls" "pptx" "ppt" "pdf" "rtf"
                                                         "odt" "ods" "odp" "html" "txt" "md" "csv", "ole"/"zip"
                                                         (незнакомый составной файл / архив) или ""
    SUPPORTED_EXT, ACCEPT                                расширения и строка для <input accept="…">
    soffice() -> путь | None                             есть ли LibreOffice
    to_docx(data, name) -> bytes                         .docx из .docx/.doc/.rtf/.odt
    read_zip_part(data, part, name) -> bytes | None      часть архива с пределами (для своих разборов)

Как читается:
- docx, pptx, xlsx — своими силами (zip + XML). У xlsx — текст ячеек по листам, ячейки строки
  через табуляцию. odt и rtf — LibreOffice, а без него — простым разбором своими силами.
- doc, ppt, xls — LibreOffice переводит в docx/pptx/xlsx (`--convert-to`), дальше свой разбор;
  без LibreOffice — понятная ошибка «сохраните как .docx».
- PDF — pdftotext, если есть; иначе PyMuPDF (fitz), если импортируется; иначе понятная ошибка.
  Текста нет — «похоже, это скан».
- txt, md, csv — UTF-8 (с BOM или без), иначе cp1251; двоичное под видом текста — отказ.

Защита от враждебных файлов (ревью RC-2026-10-05-4, Н-1/Н-2/Н-3/Н-7):
- пределы: входной файл (MAX_INPUT), число записей zip (MAX_ENTRIES), сумма распакованного
  (MAX_UNZIPPED) и каждая часть (MAX_PART) — размер из заголовка проверяется ДО чтения, а само чтение
  потоковое и обрывается на пределе (заголовок мог соврать); «mimetype-бомба» и «zip-бомба» — отказ
  без роста памяти;
- XML разбирается линейным проходом по тегам (`_tags`, через str.find), атрибуты — тоже линейно
  (`_attrs`, без регулярок: ревью RC-2026-10-05-8, Н-1); у тега длиннее MAX_TAG (64 КБ) атрибуты
  пропускаются, а разбор идёт дальше (у Word так бывает: o:gfxdata у фигур; RC-2026-10-05-10, Н-1);
  сущности не разворачиваются; на весь разбор — срок PARSE_TIMEOUT (10 с), дальше — «файл
  повреждён»; HTML — стандартным `html.parser` (тоже линейно). Оставшиеся регулярки (только RTF без
  LibreOffice) — без вложенных повторов и с ограниченными квантификаторами: откатов нет;
- LibreOffice запускается без сети: во временном профиле `registrymodifications.xcu` запрещает
  ссылки на внешние ресурсы (BlockUntrustedRefererLinks), обновление связей в Writer/Calc и задаёт
  прокси 127.0.0.1:9, в окружении процесса прокси — туда же: внешние картинки и связанные
  документы по http не скачиваются;
- текст, похожий на мусор (двоичный файл под видом текста, «иероглифы» после перевода OLE без потока
  Word), не отдаётся: проверяется доля обычных символов (`_readable`).

LibreOffice: каждой конвертации свой временный профиль и HOME (под службой без записываемого HOME
он падает), исходник — под латинским именем во временной папке, срок (DOCS_CONVERT_TIMEOUT,
по умолчанию 120 с) — по истечении снимается вся группа процессов, одновременно — одна конвертация
на процесс сервиса (сервер слабый). Устройство — как `materials/app/convert.py`.

Только стандартная библиотека; PyMuPDF — необязательно.
"""
from __future__ import annotations

import html
import html.parser
import io
import os
import re
import shutil
import signal
import struct
import subprocess
import tempfile
import threading
import time
import zipfile
import zlib
from pathlib import Path

SUPPORTED_EXT = (".docx", ".doc", ".xlsx", ".xls", ".pdf", ".pptx", ".ppt", ".rtf", ".odt", ".txt", ".md", ".csv")
ACCEPT = ",".join(SUPPORTED_EXT)
WORD_EXT = (".docx", ".doc", ".rtf", ".odt")          # что можно перевести в документ Word

MAX_INPUT = 60 * 1024 * 1024                          # сам загруженный файл
MAX_ENTRIES = 3000                                    # записей в архиве
MAX_UNZIPPED = 150 * 1024 * 1024                      # распакованный архив целиком: защита от «zip-бомбы»
MAX_PART = 40 * 1024 * 1024                           # одна часть архива
MAX_CHARS = 2_000_000
PARSE_TIMEOUT = 10.0                                  # на разбор одного документа своими силами
MAX_TAG = 64 * 1024                                   # тег длиннее — его атрибуты пропускаются (не отказ)
DEFAULT_TIMEOUT = 120

# Ошибки чтения, которые означают «файл повреждён», а не сбой программы: битый поток deflate
# (zlib.error), обрезанный архив, шифрованный zip, обрезанная структура старого формата.
BROKEN = (zipfile.BadZipFile, zlib.error, KeyError, RuntimeError, NotImplementedError, EOFError, OSError,
          ValueError, struct.error, IndexError, UnicodeDecodeError)

_OLE = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_BINARY = ("docx", "doc", "xlsx", "xls", "pptx", "ppt", "pdf", "odt", "ods", "odp")
_lock = threading.Lock()


class DocsError(ValueError):
    """Документ не читается или не переводится. Текст — для человека, по-русски."""


def _ext(name: str) -> str:
    return Path(str(name or "")).suffix.lower().lstrip(".")


def _too_big(name: str) -> DocsError:
    return DocsError(f"Файл «{name}» слишком большой: внутри больше {MAX_UNZIPPED // (1024 * 1024)} МБ "
                     "или слишком много частей. Сохраните документ заново без вложений.")


def _broken(name: str) -> DocsError:
    return DocsError(f"Файл «{name}» повреждён — откройте его и сохраните заново.")


# ── архив с пределами ──────────────────────────────────────────────────

def _open_zip(data: bytes, name: str) -> zipfile.ZipFile:
    """Архив с проверкой числа записей и суммы распакованного по заголовкам (до чтения)."""
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
        infos = z.infolist()
    except BROKEN as error:
        raise _broken(name) from error
    if len(infos) > MAX_ENTRIES or sum(i.file_size for i in infos) > MAX_UNZIPPED:
        raise _too_big(name)
    return z


def _read_member(z: zipfile.ZipFile, info: zipfile.ZipInfo, name: str, limit: int | None = None) -> bytes:
    """Часть архива: размер из заголовка — до чтения, само чтение потоковое и обрывается на пределе."""
    limit = MAX_PART if limit is None else limit
    if info.file_size > limit:
        raise _too_big(name)
    out, total = [], 0
    try:
        with z.open(info) as f:
            while True:
                chunk = f.read(1 << 16)
                if not chunk:
                    break
                total += len(chunk)
                if total > limit:
                    raise _too_big(name)
                out.append(chunk)
    except DocsError:
        raise
    except BROKEN as error:
        raise _broken(name) from error
    return b"".join(out)


def _part(z: zipfile.ZipFile, part: str, name: str, limit: int | None = None) -> str | None:
    try:
        info = z.getinfo(part)
    except KeyError:
        return None
    return _read_member(z, info, name, limit).decode("utf8", "replace")


def read_zip_part(data: bytes, part: str, name: str, limit: int | None = None) -> bytes | None:
    """Часть архива с теми же пределами — для своих разборов в сервисах (фото из docx и т.п.)."""
    z = _open_zip(data, name)
    try:
        info = z.getinfo(part)
    except KeyError:
        return None
    return _read_member(z, info, name, limit)


# ── формат ─────────────────────────────────────────────────────────────

def _ole_has(data: bytes, stream: str) -> bool:
    key = stream.encode("utf-16-le")
    return key in data[:2_000_000] or key in data[-2_000_000:]


def _textual(data: bytes) -> bool:
    """Похоже ли начало файла на текст: нет нулевых байтов и почти нет управляющих символов."""
    head = data[:8192]
    if head.startswith((b"\xff\xfe", b"\xfe\xff")):
        return True
    if b"\x00" in head:
        return False
    controls = sum(1 for b in head if b < 32 and b not in (9, 10, 13, 12))
    return controls <= max(2, len(head) // 200)


def sniff(data: bytes, name: str = "") -> str:
    """Формат по содержимому (расширению не верим: .docx бывает старым .doc, .doc — веб-страницей)."""
    data = data or b""
    if data[:4] == b"PK\x03\x04":
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                infos = z.infolist()
                if len(infos) > MAX_ENTRIES:
                    return "zip"
                names = {i.filename for i in infos}
                if "word/document.xml" in names:
                    return "docx"
                if "xl/workbook.xml" in names:
                    return "xlsx"
                if "ppt/presentation.xml" in names or any(n.startswith("ppt/slides/") for n in names):
                    return "pptx"
                if "mimetype" in names:
                    # только первые байты: часть «mimetype» бывает на гигабайт (ревью RC-2026-10-05-4, Н-1)
                    with z.open("mimetype") as f:
                        mime = f.read(100).decode("ascii", "replace")
                    for kind in ("text", "spreadsheet", "presentation"):
                        if mime.startswith(f"application/vnd.oasis.opendocument.{kind}"):
                            return {"text": "odt", "spreadsheet": "ods", "presentation": "odp"}[kind]
        except BROKEN:
            pass
        ext = _ext(name)
        return ext if ext in ("docx", "xlsx", "pptx", "odt", "ods", "odp") else "zip"
    if data[:8] == _OLE:
        if _ole_has(data, "WordDocument"):
            return "doc"
        if _ole_has(data, "Workbook") or _ole_has(data, "Book"):
            return "xls"
        if _ole_has(data, "PowerPoint Document"):
            return "ppt"
        if _ole_has(data, "EncryptedPackage"):
            return "encrypted"
        return "ole"                                     # составной файл без знакомого потока
    if data[:5] == b"%PDF-":
        return "pdf"
    if data[:5] == b"{\\rtf":
        return "rtf"
    head = data[:2048].lstrip().lower()
    if head.startswith(b"<!doctype html") or b"<html" in head[:1024]:
        return "html"
    if not data.strip():
        return _ext(name) if _ext(name) in ("txt", "md", "csv") else "txt"
    if not _textual(data):
        return ""                                        # двоичное неизвестное
    ext = _ext(name)
    return ext if ext in ("md", "csv") else "txt"


# ── LibreOffice ────────────────────────────────────────────────────────

def soffice() -> str | None:
    custom = os.environ.get("DOCS_SOFFICE", "").strip()
    if custom and os.path.isfile(custom) and os.access(custom, os.X_OK):
        return custom
    found = shutil.which("soffice") or shutil.which("libreoffice")
    if found:
        return found
    for path in (r"C:\Program Files\LibreOffice\program\soffice.exe",
                 "/Applications/LibreOffice.app/Contents/MacOS/soffice"):
        if os.path.isfile(path):
            return path
    return None


def _timeout() -> float:
    try:
        value = float(os.environ.get("DOCS_CONVERT_TIMEOUT", "") or DEFAULT_TIMEOUT)
    except ValueError:
        return DEFAULT_TIMEOUT
    return value if value > 0 else DEFAULT_TIMEOUT


TARGET = {"docx": "docx:MS Word 2007 XML", "pdf": "pdf", "txt": "txt:Text (encoded):UTF8",
          "xlsx": "xlsx:Calc MS Excel 2007 XML", "pptx": "pptx:Impress MS PowerPoint 2007 XML"}
INFILTER = {"pdf": "writer_pdf_import", "html": "HTML (StarWriter)"}
SOURCE_OF = {                                         # что во что LibreOffice переводит осмысленно
    "docx": ("doc", "rtf", "odt", "html", "docx", "txt"),
    "pdf": ("docx", "doc", "rtf", "odt", "html", "xlsx", "xls", "ods", "pptx", "ppt", "odp", "txt"),
    "txt": ("doc", "rtf", "odt", "html", "docx"),
    "xlsx": ("xls", "ods", "csv"),
    "pptx": ("ppt", "odp"),
}

# Настройки временного профиля LibreOffice: никакой сети и никаких внешних ссылок (ревью Н-3).
# BlockUntrustedRefererLinks — не грузить ресурсы документа по ссылкам (картинки по http и т.п.);
# Update/Link = 2 («никогда») — не обновлять связанные разделы и листы; прокси — в никуда;
# макросы выключены. Ключи — из официальной схемы настроек LibreOffice (officecfg).
_NO_NETWORK = """<?xml version="1.0" encoding="UTF-8"?>
<oor:items xmlns:oor="http://openoffice.org/2001/registry" xmlns:xs="http://www.w3.org/2001/XMLSchema" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
<item oor:path="/org.openoffice.Office.Common/Security/Scripting"><prop oor:name="BlockUntrustedRefererLinks" oor:op="fuse"><value>true</value></prop></item>
<item oor:path="/org.openoffice.Office.Common/Security/Scripting"><prop oor:name="MacroSecurityLevel" oor:op="fuse"><value>3</value></prop></item>
<item oor:path="/org.openoffice.Office.Common/Security/Scripting"><prop oor:name="DisableMacrosExecution" oor:op="fuse"><value>true</value></prop></item>
<item oor:path="/org.openoffice.Office.Writer/Content/Update"><prop oor:name="Link" oor:op="fuse"><value>2</value></prop></item>
<item oor:path="/org.openoffice.Office.Calc/Content/Update"><prop oor:name="Link" oor:op="fuse"><value>2</value></prop></item>
<item oor:path="/org.openoffice.Inet/Settings"><prop oor:name="ooInetProxyType" oor:op="fuse"><value>1</value></prop></item>
<item oor:path="/org.openoffice.Inet/Settings"><prop oor:name="ooInetHTTPProxyName" oor:op="fuse"><value>127.0.0.1</value></prop></item>
<item oor:path="/org.openoffice.Inet/Settings"><prop oor:name="ooInetHTTPProxyPort" oor:op="fuse"><value>9</value></prop></item>
<item oor:path="/org.openoffice.Inet/Settings"><prop oor:name="ooInetHTTPSProxyName" oor:op="fuse"><value>127.0.0.1</value></prop></item>
<item oor:path="/org.openoffice.Inet/Settings"><prop oor:name="ooInetHTTPSProxyPort" oor:op="fuse"><value>9</value></prop></item>
<item oor:path="/org.openoffice.Inet/Settings"><prop oor:name="ooInetNoProxy" oor:op="fuse"><value></value></prop></item>
</oor:items>
"""
_DEAD_PROXY = "http://127.0.0.1:9"


def _kill(proc: subprocess.Popen) -> None:
    try:
        if os.name != "nt":
            os.killpg(proc.pid, signal.SIGKILL)
        else:
            proc.kill()
    except (OSError, ProcessLookupError):
        pass


def convert(data: bytes, name: str, to: str) -> bytes:
    """Перевести документ через LibreOffice: to = "docx" | "pdf" | "txt" (и "xlsx", "pptx").

    Нет LibreOffice, пара форматов не переводится, не уложился в срок — DocsError с понятным текстом.
    """
    if to not in TARGET:
        raise DocsError(f"Перевод в «{to}» не поддерживается")
    if len(data or b"") > MAX_INPUT:
        raise DocsError(f"Файл «{name}» больше {MAX_INPUT // (1024 * 1024)} МБ.")
    kind = sniff(data, name)
    if kind == "encrypted":
        raise DocsError(f"Файл «{name}» защищён паролем — снимите пароль и сохраните заново.")
    if kind not in SOURCE_OF[to]:
        raise DocsError(f"Файл «{name}» ({kind or 'неизвестный формат'}) в {to.upper()} не переводится.")
    if kind in ("odt", "ods", "odp", "docx", "xlsx", "pptx"):
        _open_zip(data, name)                            # пределы архива — до LibreOffice
    exe = soffice()
    if not exe:
        raise DocsError(f"Файл «{name}» в формате .{kind}: чтобы его прочитать, нужен LibreOffice, а его нет. "
                        f"Откройте файл и сохраните как {'.xlsx' if kind in ('xls', 'ods') else '.pptx' if kind in ('ppt', 'odp') else '.docx'}.")
    limit = _timeout()
    if not _lock.acquire(timeout=limit * 2):
        raise DocsError("Конвертер документов занят — попробуйте через минуту.")
    try:
        with tempfile.TemporaryDirectory(prefix="docs-convert-") as tmp_name:
            tmp = Path(tmp_name)
            for sub in ("in", "out", "home", "profile/user"):
                (tmp / sub).mkdir(parents=True)
            (tmp / "profile" / "user" / "registrymodifications.xcu").write_text(_NO_NETWORK, encoding="utf-8")
            source = tmp / "in" / f"source.{kind}"
            source.write_bytes(data)
            cmd = [exe, "--headless", "--norestore", "--nologo", "--nodefault", "--nolockcheck",
                   f"-env:UserInstallation={(tmp / 'profile').as_uri()}"]
            if kind in INFILTER:
                cmd.append(f"--infilter={INFILTER[kind]}")
            cmd += ["--convert-to", TARGET[to], "--outdir", str(tmp / "out"), str(source)]
            env = {**os.environ, "HOME": str(tmp / "home"), "TMPDIR": str(tmp)}
            if _DEAD_PROXY:                              # вторая линия «без сети», независимая от профиля
                for var in ("http_proxy", "https_proxy", "ftp_proxy", "all_proxy",
                            "HTTP_PROXY", "HTTPS_PROXY", "FTP_PROXY", "ALL_PROXY"):
                    env[var] = _DEAD_PROXY
                env["no_proxy"] = env["NO_PROXY"] = ""
            try:
                proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
                                        cwd=tmp, env=env, start_new_session=os.name != "nt")
            except OSError as error:
                raise DocsError("LibreOffice не запустился — перевод документа недоступен.") from error
            try:
                proc.communicate(timeout=limit)
            except subprocess.TimeoutExpired:
                _kill(proc)
                try:
                    proc.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
                raise DocsError(f"Файл «{name}» не удалось перевести за {limit:.0f} с — сохраните его как .docx.")
            finally:
                if proc.poll() is None:
                    _kill(proc)
            result = tmp / "out" / f"source.{to}"
            if not result.is_file() or result.stat().st_size == 0:
                raise DocsError(f"LibreOffice не смог прочитать «{name}» — файл повреждён или защищён.")
            if result.stat().st_size > MAX_INPUT * 2:
                raise DocsError(f"Файл «{name}» после перевода слишком большой.")
            return result.read_bytes()
    finally:
        _lock.release()


# ── линейный проход по XML ─────────────────────────────────────────────
# Без регулярных выражений с откатом: на незакрытых тегах `<w:p[ >].*?</w:p>` работал за квадратичное
# время (docx в 639 байт держал сервис 33 с — ревью RC-2026-10-05-4, Н-2). Здесь каждый символ
# просматривается один раз (str.find), сущности не разворачиваются (кроме стандартных в тексте),
# срок — на весь документ.

class _Clock:
    def __init__(self, name: str, seconds: float | None = None):
        self.name, self.n = name, 0
        self.until = time.monotonic() + (PARSE_TIMEOUT if seconds is None else seconds)

    def tick(self) -> None:
        self.n += 1
        if self.n & 1023 == 0 and time.monotonic() > self.until:
            raise DocsError(f"Файл «{self.name}» повреждён или слишком сложный — разбор прерван. "
                            "Откройте его и сохраните заново.")


def _tags(xml: str, clock: _Clock):
    """Поток (вид, имя, атрибуты-строка | текст): вид — "open", "close", "empty", "text", "cdata"."""
    pos, n = 0, len(xml)
    while pos < n:
        clock.tick()
        lt = xml.find("<", pos)
        if lt < 0:
            yield "text", "", xml[pos:]
            return
        if lt > pos:
            yield "text", "", xml[pos:lt]
        if xml.startswith("<!--", lt):
            end = xml.find("-->", lt + 4)
            pos = n if end < 0 else end + 3
            continue
        if xml.startswith("<![CDATA[", lt):
            end = xml.find("]]>", lt + 9)
            yield "cdata", "", xml[lt + 9:(n if end < 0 else end)]
            pos = n if end < 0 else end + 3
            continue
        gt = xml.find(">", lt + 1)
        if gt < 0:
            return
        if gt - lt > MAX_TAG:
            # Длинный тег — обычное дело у Word (o:gfxdata у фигур и надписей — base64 на сотни КБ):
            # тег не отклоняем, а пропускаем его атрибуты — имя и вид (открывающий/пустой) сохраняются,
            # разбор идёт дальше (ревью RC-2026-10-05-10, Н-1). Атрибуты длинного тега нам не нужны.
            pos = gt + 1
            k = lt + 1
            while k < gt and xml[k] not in " \t\r\n/>":
                k += 1
            if xml[lt + 1] in "?!/":
                if xml[lt + 1] == "/":
                    yield "close", xml[lt + 2:k].strip(), ""
                continue
            yield ("empty" if xml[gt - 1] == "/" else "open"), xml[lt + 1:k], ""
            continue
        body = xml[lt + 1:gt]
        pos = gt + 1
        if not body or body[0] in "?!":
            continue
        if body[0] == "/":
            yield "close", body[1:].strip(), ""
            continue
        empty = body.endswith("/")
        if empty:
            body = body[:-1]
        cut = len(body)
        for sep in (" ", "\t", "\n", "\r"):
            k = body.find(sep)
            if 0 <= k < cut:
                cut = k
        yield ("empty" if empty else "open"), body[:cut], body[cut:]


def _attrs(s: str) -> dict:
    """Атрибуты тега линейным проходом (str.find): регулярка «имя = значение» с повтором по имени на длинной строке
    без «=» работала за квадратичное время и держала GIL — docx в 516 байт останавливал сервис
    на 21 с, xlsx в 1 КБ — на минуты (ревью RC-2026-10-05-8, Н-1). Каждый символ — один раз."""
    out: dict = {}
    if not s:
        return out
    i, n = 0, len(s)
    while i < n:
        eq = s.find("=", i)
        if eq < 0:
            break
        head = s[i:eq].split()
        q = eq + 1
        while q < n and s[q] in " \t\r\n":
            q += 1
        if q >= n or s[q] not in "\"'":
            i = q + 1
            continue
        end = s.find(s[q], q + 1)
        if end < 0:
            break
        if head:
            out[head[-1]] = html.unescape(s[q + 1:end])
        i = end + 1
    return out


def _text_of(xml: str, name: str, text_tags: set, breaks: set, spaces: set) -> str:
    """Текст документа: содержимое text_tags; закрытие тега из breaks — новая строка; spaces — пробел."""
    clock = _Clock(name)
    out, depth = [], 0
    for kind, tag, val in _tags(xml, clock):
        if kind == "text" or kind == "cdata":
            if depth:
                out.append(html.unescape(val) if kind == "text" else val)
        elif kind == "open":
            if tag in text_tags:
                depth += 1
            elif tag in spaces:
                out.append(" ")
        elif kind == "empty":
            if tag in spaces:
                out.append(" ")
        elif kind == "close":
            if tag in text_tags:
                depth = max(0, depth - 1)
            elif tag in breaks:
                out.append("\n")
    return "".join(out)


def _docx_xml(data: bytes, name: str) -> str:
    z = _open_zip(data, name)
    xml = _part(z, "word/document.xml", name)
    if xml is None:
        raise DocsError(f"Файл «{name}» не похож на документ Word.")
    return xml


def _docx(data: bytes, name: str) -> str:
    return _text_of(_docx_xml(data, name), name, {"w:t"}, {"w:p"}, {"w:tab", "w:br", "w:cr"})


def _num(path: str) -> int:
    digits = "".join(ch for ch in path.rsplit("/", 1)[-1] if ch.isdigit())
    return int(digits) if digits else 0


def _pptx(data: bytes, name: str) -> str:
    z = _open_zip(data, name)
    inside = set(z.namelist())
    slides = sorted((n for n in inside if n.startswith("ppt/slides/slide") and n.endswith(".xml")
                     and n.count("/") == 2), key=_num)
    out = []
    for i, part in enumerate(slides, 1):
        body = [x.strip() for x in _text_of(_part(z, part, name) or "", name, {"a:t"}, {"a:p"}, {"a:br"}).split("\n")]
        body = [x for x in body if x]
        notes_xml = _part(z, f"ppt/notesSlides/notesSlide{_num(part)}.xml", name) or ""
        notes = [x.strip() for x in _text_of(notes_xml, name, {"a:t"}, {"a:p"}, set()).split("\n")]
        notes = [x for x in notes if x and not x.isdigit()]
        if not body and not notes:
            continue
        out.append(f"Слайд {i}.")
        out += body
        if notes:
            out.append("Заметки к слайду: " + " ".join(notes))
    return "\n".join(out)


def _col(ref: str) -> int:
    n = 0
    for ch in ref:
        if "A" <= ch <= "Z":
            n = n * 26 + ord(ch) - 64
        else:
            break
    return max(1, n)


def _rels(xml: str, name: str) -> dict:
    return {a.get("Id"): a.get("Target", "") for kind, tag, val in _tags(xml, _Clock(name))
            if kind in ("open", "empty") and tag == "Relationship" for a in [_attrs(val)]}


def _xlsx(data: bytes, name: str) -> str:
    """Текст ячеек по листам: «Лист «Имя»», строки — ячейки через табуляцию."""
    z = _open_zip(data, name)
    clock = _Clock(name)
    shared: list[str] = []
    sst = _part(z, "xl/sharedStrings.xml", name)
    if sst:
        cur, in_t, skip = [], 0, 0
        for kind, tag, val in _tags(sst, clock):
            if kind == "open" and tag == "rPh":
                skip += 1                                # фонетическая подсказка — не текст ячейки
            elif kind == "close" and tag == "rPh":
                skip = max(0, skip - 1)
            elif kind == "open" and tag == "t":
                in_t += 1
            elif kind == "close" and tag == "t":
                in_t = max(0, in_t - 1)
            elif kind == "text" and in_t and not skip:
                cur.append(html.unescape(val))
            elif kind == "close" and tag == "si":
                shared.append("".join(cur))
                cur = []
            elif kind == "empty" and tag == "si":
                shared.append("")
    targets = _rels(_part(z, "xl/_rels/workbook.xml.rels", name) or "", name)
    sheets = []
    for kind, tag, val in _tags(_part(z, "xl/workbook.xml", name) or "", clock):
        if kind in ("open", "empty") and tag == "sheet":
            a = _attrs(val)
            target = targets.get(a.get("r:id"), "")
            if target:
                path = target.lstrip("/") if target.startswith("/") else "xl/" + target
                while "/../" in path:
                    head, _, tail = path.partition("/../")
                    path = head.rsplit("/", 1)[0] + "/" + tail if "/" in head else tail
                sheets.append((a.get("name", ""), path))
    if not sheets:
        found = sorted((n for n in z.namelist() if n.startswith("xl/worksheets/sheet") and n.endswith(".xml")), key=_num)
        sheets = [(f"Лист{i}", n) for i, n in enumerate(found, 1)]
    out = []
    for title, path in sheets:
        xml = _part(z, path, name)
        if not xml:
            continue
        rows, cells, cell, value, grab = [], {}, None, [], False
        for kind, tag, val in _tags(xml, clock):
            if kind == "open" and tag == "row":
                cells = {}
            elif kind == "close" and tag == "row":
                if cells:
                    rows.append("\t".join(cells.get(i, "") for i in range(1, max(cells) + 1)))
                cells = {}
            elif kind in ("open", "empty") and tag == "c":
                a = _attrs(val)
                cell = {"col": _col(a.get("r", "")) if a.get("r") else (max(cells) + 1 if cells else 1),
                        "t": a.get("t", "")}
                value = []
                if kind == "empty":
                    cell = None
            elif kind == "open" and tag in ("v", "t") and cell is not None:
                grab = True
            elif kind == "close" and tag in ("v", "t"):
                grab = False
            elif kind == "text" and grab and cell is not None:
                value.append(html.unescape(val))
            elif kind == "close" and tag == "c" and cell is not None:
                text = "".join(value)
                if cell["t"] == "s" and text.strip().isdigit():
                    idx = int(text)
                    text = shared[idx] if idx < len(shared) else ""
                elif cell["t"] == "b":
                    text = "ИСТИНА" if text == "1" else "ЛОЖЬ"
                if text.strip() and cell["col"] <= 16384:
                    cells[cell["col"]] = text.replace("\t", " ").replace("\n", " ").strip()
                cell = None
        if rows:
            out.append(f"Лист «{title}»")
            out += rows
    return "\n".join(out)


def _odt(data: bytes, name: str) -> str:
    z = _open_zip(data, name)
    xml = _part(z, "content.xml", name)
    if xml is None:
        raise DocsError(f"Файл «{name}» повреждён: в нём нет текста.")
    clock = _Clock(name)
    out, depth = [], 0
    for kind, tag, val in _tags(xml, clock):
        if kind == "open" and tag in ("text:p", "text:h"):
            depth += 1
        elif kind == "close" and tag in ("text:p", "text:h"):
            depth = max(0, depth - 1)
            out.append("\n")
        elif kind == "text" and depth:
            out.append(html.unescape(val))
        elif kind in ("empty", "open") and depth and tag in ("text:tab", "text:line-break"):
            out.append(" ")
        elif kind in ("empty", "open") and depth and tag == "text:s":
            c = _attrs(val).get("text:c", "1")
            out.append(" " * min(50, int(c) if c.isdigit() else 1))
    return "".join(out)


class _HtmlText(html.parser.HTMLParser):
    """Текст веб-страницы стандартным разборщиком (линейно), без script/style."""
    BLOCK = {"p", "div", "tr", "li", "br", "h1", "h2", "h3", "h4", "h5", "h6", "table"}

    def __init__(self, clock: _Clock):
        super().__init__(convert_charrefs=True)
        self.out, self.skip, self.clock = [], 0, clock

    def handle_starttag(self, tag, attrs):
        self.clock.tick()
        if tag in ("script", "style"):
            self.skip += 1
        elif tag in self.BLOCK:
            self.out.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self.skip = max(0, self.skip - 1)
        elif tag in self.BLOCK:
            self.out.append("\n")

    def handle_data(self, data):
        self.clock.tick()
        if not self.skip:
            self.out.append(data)


def _html(data: bytes, name: str) -> str:
    text = data.decode("utf8", "replace")
    if "\ufffd" in text[:4000]:
        try:
            text = data.decode("cp1251")
        except UnicodeDecodeError:
            pass
    parser = _HtmlText(_Clock(name))
    for i in range(0, len(text), 1 << 16):
        parser.feed(text[i:i + (1 << 16)])
        parser.clock.tick()
    parser.close()
    return "".join(parser.out)


def _rtf(data: bytes) -> str:
    text = data.decode("latin-1", "replace")
    text = re.sub(r"\\'([0-9a-fA-F]{2})", lambda m: bytes([int(m.group(1), 16)]).decode("cp1251", "replace"), text)
    text = re.sub(r"\\par[d]?\b", "\n", text)
    text = re.sub(r"\\u(-?\d{1,6})\s?\??", lambda m: chr(int(m.group(1)) % 65536), text)
    text = re.sub(r"\{\\\*[^{}]*\}", " ", text)
    text = re.sub(r"\\[a-zA-Z]{1,32}-?\d{0,10} ?", " ", text)
    return text.replace("{", " ").replace("}", " ")


def _plain(data: bytes) -> str:
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16", "replace")
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        pass
    # cp1251: русские буквы — байты C0–FF; байты 80–BF (знаки, редкие буквы) в настоящем тексте редки,
    # а у двоичного файла их столько же, сколько остальных — такой файл текстом не считаем (Н-7)
    sample = data[:65536]
    high = sum(1 for b in sample if b >= 0x80)
    middle = sum(1 for b in sample if 0x80 <= b < 0xC0)
    if high and middle > 0.25 * high:
        raise DocsError("Файл не похож на текст: двоичные данные или незнакомая кодировка. Сохраните его как .docx.")
    return data.decode("cp1251", "replace")


def _pdf(data: bytes, name: str) -> str:
    tool = shutil.which("pdftotext")
    if tool:
        handle, path = tempfile.mkstemp(suffix=".pdf")       # закрываем до запуска: так работает и на Windows
        try:
            with open(handle, "wb") as f:
                f.write(data)
            done = subprocess.run([tool, "-layout", "-enc", "UTF-8", path, "-"], capture_output=True, timeout=120)
            if done.returncode == 0:
                return done.stdout.decode("utf8", errors="replace")
        except subprocess.TimeoutExpired:
            raise DocsError(f"PDF «{name}» читается слишком долго — пришлите его в Word.") from None
        except OSError:
            pass
        finally:
            Path(path).unlink(missing_ok=True)
    fitz = None
    for module in ("pymupdf", "fitz"):
        try:
            fitz = __import__(module)
            break
        except ImportError:
            continue
    if fitz is None or not hasattr(fitz, "open"):
        raise DocsError(f"Файл «{name}» — PDF, а прочитать его нечем (нет ни pdftotext, ни PyMuPDF). "
                        "Пришлите документ в Word.")
    try:
        with fitz.open(stream=data, filetype="pdf") as pdf:
            if getattr(pdf, "needs_pass", False):
                raise DocsError(f"PDF «{name}» защищён паролем.")
            return "\n".join(page.get_text() for page in pdf)
    except DocsError:
        raise
    except Exception as error:  # noqa: BLE001 — любая ошибка разбора PDF: файл повреждён
        raise DocsError(f"PDF «{name}» не читается — файл повреждён.") from error


def _readable(text: str) -> bool:
    """Обычный ли это текст: доля букв (кириллица, латиница), цифр, пробелов и знаков препинания.

    Двоичный файл под видом текста и «иероглифы» из составного файла без потока Word дают кашу,
    которую нельзя отдавать дальше (в «Искру» — это ещё и токены; ревью RC-2026-10-05-4, Н-7)."""
    sample = text[:20000]
    if not sample.strip():
        return False
    good = 0
    for ch in sample:
        o = ord(ch)
        if ch.isspace() or 32 <= o < 127 or 0x400 <= o <= 0x4FF or 0x2000 <= o <= 0x206F \
                or 0xA0 <= o <= 0xFF or ch in "№€₽":
            good += 1
    return good >= 0.85 * len(sample)


# ── текст документа ────────────────────────────────────────────────────

def extract_text(data: bytes, name: str) -> str:
    """Текст документа по его содержимому. Пусто или не читается — DocsError с объяснением."""
    data = data or b""
    if len(data) > MAX_INPUT:
        raise DocsError(f"Файл «{name}» больше {MAX_INPUT // (1024 * 1024)} МБ.")
    kind = sniff(data, name)
    ext = _ext(name)
    if kind in ("txt", "md", "csv", "") and ext in _BINARY:
        # «справка.docx», внутри которой не Word: битая загрузка или переименованный файл
        raise DocsError(f"Файл «{name}» повреждён: внутри не документ .{ext}. Откройте его и сохраните заново.")
    try:
        if kind == "docx":
            text = _docx(data, name)
        elif kind == "pptx":
            text = _pptx(data, name)
        elif kind == "xlsx":
            text = _xlsx(data, name)
        elif kind in ("doc", "rtf", "odt"):
            if soffice():
                text = _docx(convert(data, name, "docx"), name)
            elif kind == "rtf":
                text = _rtf(data)
            elif kind == "odt":
                text = _odt(data, name)
            else:
                raise DocsError(f"Файл «{name}» — старый формат Word (.doc), а LibreOffice для его чтения нет. "
                                "Откройте файл в Word и сохраните как .docx.")
        elif kind in ("xls", "ods"):
            text = _xlsx(convert(data, name, "xlsx"), name)
        elif kind in ("ppt", "odp"):
            text = _pptx(convert(data, name, "pptx"), name)
        elif kind == "pdf":
            text = _pdf(data, name)
        elif kind == "html":
            text = _html(data, name)
        elif kind in ("txt", "md", "csv"):
            text = _plain(data)
        elif kind == "encrypted":
            raise DocsError(f"Файл «{name}» защищён паролем — снимите пароль и сохраните заново.")
        elif kind in ("ole", "zip"):
            if kind == "zip":
                _open_zip(data, name)                    # слишком большой архив — так и сказать
            raise DocsError(f"Файл «{name}» не похож ни на документ Word, ни на Excel или PowerPoint — "
                            "возможно, он повреждён. Откройте его и сохраните заново.")
        else:
            raise DocsError(f"Формат «{('.' + ext) if ext else 'без расширения'}» не поддерживается. "
                            "Подходят Word, Excel, PowerPoint, PDF, а также .rtf, .odt, .txt, .md, .csv.")
    except DocsError:
        raise
    except BROKEN as error:
        raise _broken(name) from error
    # у таблиц ведущая табуляция — пустая первая ячейка, её не срезаем
    keep_tabs = kind in ("xlsx", "xls", "ods", "csv")
    lines = []
    for line in (text or "").replace("\r", "\n").split("\n"):
        line = line.replace("\u00a0", " ")
        line = "\t".join(" ".join(cell.split()) for cell in line.split("\t")) if keep_tabs else " ".join(line.split())
        if line.strip():
            lines.append(line)
    text = "\n".join(lines)
    if len("".join(text.split())) < 3:
        if kind == "pdf":
            raise DocsError(f"В файле «{name}» нет текста — похоже, это скан. Нужен PDF с текстовым слоем или Word.")
        raise DocsError(f"В файле «{name}» не нашлось текста.")
    if not _readable(text):
        raise DocsError(f"В файле «{name}» не текст, а нечитаемые знаки — файл повреждён или в незнакомой "
                        "кодировке. Откройте его и сохраните как .docx.")
    return text[:MAX_CHARS]


def to_docx(data: bytes, name: str) -> bytes:
    """Документ Word (.docx) из .docx/.doc/.rtf/.odt — для тех, кому нужен именно Word."""
    if len(data or b"") > MAX_INPUT:
        raise DocsError(f"Файл «{name}» больше {MAX_INPUT // (1024 * 1024)} МБ.")
    kind = sniff(data, name)
    if kind == "docx":
        _open_zip(data, name)
        return data
    if kind in ("doc", "rtf", "odt"):
        return convert(data, name, "docx")
    raise DocsError(f"Файл «{name}» — не документ Word. Подходят .docx, .doc, .rtf, .odt.")
