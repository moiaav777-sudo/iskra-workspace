"""Текст справки и фото из неё.

Текст — общим модулем `docs_common.py` (копия `portal/shared/docs_common.py`, решение пользователя
05.10): Word (.docx, .doc), Excel (.xlsx, .xls), PDF, PowerPoint (.pptx, .ppt), .rtf, .odt, .txt, .md,
.csv; формат — по содержимому, а не по расширению. Старые форматы (.doc, .rtf, .odt) переводятся
LibreOffice в .docx один раз: из него берутся и текст, и фото. Пустой результат или битый файл —
SourceError с объяснением: придумывать сведения о человеке из пустоты нельзя.

Фото: первая картинка документа Word в порядке текста (JPEG или PNG) — она и становится фото
объективки. Размеры читаются из заголовка файла (`image_info`), без сторонних библиотек.
"""
from __future__ import annotations

import struct

from . import docs_common

MAX_IMAGE = 10 * 1024 * 1024
MIN_CHARS = 20               # меньше — сведений о человеке нет, «Искре» не шлём (ревью RC-2026-10-05-4, Н-7)
ACCEPT = docs_common.ACCEPT
MERGE_ACCEPT = ",".join(docs_common.WORD_EXT)


class SourceError(ValueError):
    """Файл не читается. Текст показывается человеку."""


def sniff(name: str, raw: bytes) -> str:
    return docs_common.sniff(raw, name)


def read_source(name: str, raw: bytes) -> tuple[str, tuple[bytes, str] | None]:
    """Справка → (текст, фото или None). Любая ошибка чтения — SourceError, а не 500."""
    try:
        kind = docs_common.sniff(raw, name)
        if kind in ("doc", "rtf", "odt") and docs_common.soffice():
            raw, kind = docs_common.to_docx(raw, name), "docx"
        text = docs_common.extract_text(raw, name)
        if len("".join(text.split())) < MIN_CHARS:
            raise docs_common.DocsError(f"В файле «{name}» почти нет текста — сведений о человеке в нём не найти.")
    except docs_common.DocsError as error:
        raise SourceError(str(error)) from error
    except docs_common.BROKEN as error:
        raise SourceError(f"Файл «{name}» повреждён — откройте его и сохраните заново.") from error
    return text, (first_photo(raw) if kind == "docx" else None)


def from_bytes(name: str, raw: bytes) -> str:
    return read_source(name, raw)[0]


def to_docx(name: str, raw: bytes) -> bytes:
    """Файл для объединения → .docx (doc/rtf/odt — через LibreOffice). PDF и прочее — SourceError."""
    kind = docs_common.sniff(raw, name)
    if kind == "pdf":
        raise SourceError(f"«{name}» — PDF. Объединяются документы Word (.docx, .doc, .rtf, .odt).")
    if kind not in ("docx", "doc", "rtf", "odt"):
        raise SourceError(f"«{name}» — не документ Word. Объединяются документы Word (.docx, .doc, .rtf, .odt).")
    try:
        return docs_common.to_docx(raw, name)
    except docs_common.DocsError as error:
        raise SourceError(str(error)) from error


# ── картинки ────────────────────────────────────────────────────────────

def image_info(raw: bytes) -> tuple[str, int, int] | None:
    """(«jpeg» | «png», ширина, высота) по заголовку файла; иное — None."""
    if raw[:8] == b"\x89PNG\r\n\x1a\n" and len(raw) >= 24 and raw[12:16] == b"IHDR":
        w, h = struct.unpack(">II", raw[16:24])
        return ("png", w, h) if w and h else None
    if raw[:2] == b"\xff\xd8":
        i = 2
        while i + 9 < len(raw):
            if raw[i] != 0xFF:
                i += 1
                continue
            marker = raw[i + 1]
            if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
                i += 2
                continue
            if i + 4 > len(raw):
                break
            size = struct.unpack(">H", raw[i + 2:i + 4])[0]
            if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                if i + 9 > len(raw):
                    break
                h, w = struct.unpack(">HH", raw[i + 5:i + 9])
                return ("jpeg", w, h) if w and h else None
            i += 2 + size
    return None


def first_photo(raw: bytes) -> tuple[bytes, str] | None:
    """Первая картинка документа Word в порядке текста: (байты, «jpeg»|«png») или None.

    Части архива читаются с пределами общего модуля, XML — его линейным проходом по тегам
    (без регулярок с откатом; ревью RC-2026-10-05-4, Н-2)."""
    try:
        z = docs_common._open_zip(raw, "справка")
        rels_xml = docs_common._part(z, "word/_rels/document.xml.rels", "справка")
        xml = docs_common._part(z, "word/document.xml", "справка")
        if not rels_xml or not xml:
            return None
        clock = docs_common._Clock("справка")
        rels = {}
        for kind, tag, val in docs_common._tags(rels_xml, clock):
            if kind in ("open", "empty") and tag == "Relationship":
                a = docs_common._attrs(val)
                if a.get("Type", "").endswith("/image") and a.get("TargetMode") != "External":
                    rels[a.get("Id")] = a.get("Target", "")
        for kind, tag, val in docs_common._tags(xml, clock):
            if kind not in ("open", "empty") or tag not in ("a:blip", "v:imagedata"):
                continue
            a = docs_common._attrs(val)
            target = rels.get(a.get("r:embed") or a.get("r:id") or "")
            if not target:
                continue
            path = target.lstrip("/") if target.startswith("/") else "word/" + target
            while "/../" in path:
                head, _, tail = path.partition("/../")
                path = head.rsplit("/", 1)[0] + "/" + tail if "/" in head else tail
            try:
                info = z.getinfo(path)
            except KeyError:
                continue
            if info.file_size > MAX_IMAGE:
                continue
            data = docs_common._read_member(z, info, "справка", MAX_IMAGE)
            got = image_info(data)
            if got:
                return data, got[0]
    except (docs_common.DocsError, *docs_common.BROKEN):
        return None
    return None
