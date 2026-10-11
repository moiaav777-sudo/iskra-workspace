"""Общая обвязка тестов: временная папка данных, заголовки портала и подменная «Искра».

Сети в тестах нет: `iskra._http` подменяется (`FakeIskra`), журнал портала копится в
`portal.sent`, сверка с реестром людей подменяется там, где она нужна.
Синтетические документы делаются здесь же — образцы пользователя в репозиторий не кладутся.
"""
from __future__ import annotations

import io
import json
import struct
import sys
import threading
import zipfile
import zlib
from pathlib import Path
from urllib.parse import quote

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

SECRET = "test-secret"


def headers(login: str = "emp", role: str = "employee", name: str = "Сотрудник", team: str = "") -> dict:
    h = {"X-Portal-Secret": SECRET, "X-Portal-User": login, "X-Portal-Role": role, "X-Portal-Name": quote(name)}
    if team:
        h["X-Portal-Team"] = team
    return h


SAMPLE = {
    "fio": "Кравцов Сергей Сергеевич", "birth_date": "12 марта 1970 г.", "birth_place": "г. Москва",
    "position": "Министр просвещения Российской Федерации", "organization": "", "category": "official",
    "key_facts": [{"label": "Классный чин", "value": "действительный государственный советник 1 класса", "src": "https://edu.gov.ru/a"}],
    "powers": [{"text": "Член Правительственной комиссии по вопросам образования", "src": "https://edu.gov.ru/a"}],
    "education": [{"year": "1997", "text": "Московский государственный университет, математик", "src": "https://edu.gov.ru/a"}],
    "career": [{"period": "2018–2020", "text": "руководитель Рособрнадзора", "src": "https://edu.gov.ru/a"},
               {"period": "1997–2000", "text": "учитель математики", "src": "https://edu.gov.ru/a"}],
    "awards": [{"year": "2020", "text": "Орден Почёта", "src": "https://edu.gov.ru/a"}],
    "photo_url": "https://edu.gov.ru/photo.jpg",
    "refs": [{"name": "Минпросвещения России", "url": "https://edu.gov.ru/a", "about": "биография"}],
    "note": "",
}


class FakeIskra:
    """Подменная «Искра»: POST /runs → номер прогона, GET → готовый ответ (или то, что задали)."""

    def __init__(self):
        self.answers: list = []           # что отдавать по очереди (dict — ответ по схеме; callable(body))
        self.default = dict(SAMPLE)
        self.calls: list = []
        self.runs: dict = {}
        self.fail_with = None             # «failed» — прогон срывается
        self.interaction = False
        self.refuse_schema = 0            # сколько раз отвечать 422 на схему
        self.hold = None                  # threading.Event: прогон не заканчивается, пока не выставят
        self.usage = {"input_tokens": 1000, "output_tokens": 200}
        self.active = 0
        self.max_active = 0
        self.lock = threading.Lock()
        self.deleted: list = []

    def __call__(self, method, path, body=None, timeout=60):
        self.calls.append((method, path, body))
        if method == "POST" and path == "/api/v1/runs":
            if body.get("response_format") and self.refuse_schema > 0:
                self.refuse_schema -= 1
                return 422, {"error": {"code": "schema_unsupported", "message": "response_format"}}
            rid = f"run{len(self.runs) + 1}"
            answer = self.answers.pop(0) if self.answers else self.default
            if callable(answer):
                answer = answer(body)
            self.runs[rid] = {"body": body, "answer": answer, "polls": 0, "done": False}
            with self.lock:
                self.active += 1
                self.max_active = max(self.max_active, self.active)
            return 202, {"run_id": rid, "conversation_id": "c1"}
        rid = path.rsplit("/", 1)[-1].split("?")[0]
        run = self.runs.get(rid)
        if run is None:
            return 404, {"error": "no run"}
        if method == "DELETE":
            self.deleted.append(rid)
            self._finish(run)
            return 200, {"status": "cancelled", "usage": self.usage}
        run["polls"] += 1
        if self.hold is not None and not self.hold.is_set():
            return 200, {"status": "running", "usage": {"input_tokens": 10, "output_tokens": 1}}
        self._finish(run)
        if self.interaction:
            return 200, {"status": "interaction_required", "interaction": {"message": "О каком Кравцове речь?"},
                         "usage": self.usage}
        if self.fail_with:
            return 200, {"status": "failed", "error": {"message": self.fail_with}, "usage": self.usage}
        out = run["answer"]
        result = {"output": out} if isinstance(out, dict) else {"answer": out}
        return 200, {"status": "completed", "result": {**result, "usage": self.usage}}

    def _finish(self, run):
        if not run["done"]:
            run["done"] = True
            with self.lock:
                self.active -= 1

    def posts(self):
        return [c[2] for c in self.calls if c[0] == "POST"]


@pytest.fixture()
def fake_iskra(monkeypatch):
    from app import iskra
    fake = FakeIskra()
    monkeypatch.setattr(iskra, "_http", fake)
    monkeypatch.setattr(iskra, "POLL_MIN", 0.0)
    monkeypatch.setattr(iskra, "POLL_MAX", 0.0)
    return fake


@pytest.fixture()
def client(tmp_path, monkeypatch, fake_iskra):
    from fastapi.testclient import TestClient
    monkeypatch.setenv("PROFILES_HOME", str(tmp_path))
    monkeypatch.setenv("PORTAL_SECRET", SECRET)
    monkeypatch.setenv("PORTAL_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("ISKRA_API_KEY", "test-key-123456")
    monkeypatch.delenv("PROFILES_DEV", raising=False)
    from app import jobs, main, portal
    monkeypatch.setattr(portal, "_deliver", lambda: None)      # журнал копится в portal.sent
    monkeypatch.setattr(portal, "check_people", lambda names: None)
    portal.sent.clear()
    with TestClient(main.app) as c:
        yield c
    jobs.reset()


@pytest.fixture()
def emp():
    return headers()


@pytest.fixture()
def emp2():
    return headers("emp2", name="Второй Сотрудник")


@pytest.fixture()
def admin():
    return headers("admin", "admin", "Администратор", team="*")


@pytest.fixture()
def chief():
    return headers("chief", "chief", "Руководитель", team="*")


def wait_done(client, head, pid, timeout=10.0):
    import time
    from app import jobs
    jobs.drain(timeout)
    end = time.time() + timeout
    while time.time() < end:
        p = client.get(f"/api/profiles/{pid}", headers=head).json()
        if not p["running"]:
            return p
        time.sleep(0.02)
    raise AssertionError("сбор не закончился")


# ── синтетические файлы ─────────────────────────────────────────────────

def png(width: int, height: int) -> bytes:
    """Настоящий PNG заданного размера (серый)."""
    raw = b"".join(b"\x00" + b"\x80" * width for _ in range(height))

    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def jpeg_header(width: int, height: int) -> bytes:
    """Минимальный заголовок JPEG с размерами (для проверки чтения размеров)."""
    sof = b"\xff\xc0" + struct.pack(">HBHHB", 11, 8, height, width, 1) + b"\x01\x11\x00"
    return b"\xff\xd8" + b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00" + sof + b"\xff\xd9"


W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def docx(paragraphs: list[str], image: bytes | None = None, extra_body: str = "", rels_extra: str = "",
         first_image: bool = True) -> bytes:
    """Простой документ Word: абзацы, по желанию картинка первым абзацем."""
    def p(t):
        return f'<w:p><w:r><w:t xml:space="preserve">{t}</w:t></w:r></w:p>'
    pic = ""
    if image is not None:
        pic = ('<w:p><w:r><w:drawing><wp:inline><wp:extent cx="1440000" cy="1800000"/><wp:docPr id="1" name="p"/>'
               '<a:graphic><a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/picture"><pic:pic>'
               '<pic:blipFill><a:blip r:embed="rIdImg"/></pic:blipFill></pic:pic></a:graphicData></a:graphic>'
               '</wp:inline></w:drawing></w:r></w:p>')
    body = (pic if first_image else "") + "".join(p(t) for t in paragraphs) + extra_body
    doc = (f'<?xml version="1.0" encoding="UTF-8"?><w:document xmlns:w="{W}" '
           'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" '
           'xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing" '
           'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
           'xmlns:pic="http://schemas.openxmlformats.org/drawingml/2006/picture">'
           f'<w:body>{body}<w:sectPr/></w:body></w:document>')
    rels = ('<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            + ('<Relationship Id="rIdImg" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="media/image1.png"/>'
               if image is not None else "") + rels_extra + '</Relationships>')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:      # сжатый, как у Word: битый поток — zlib.error
        z.writestr("[Content_Types].xml", '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                   '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                   '<Default Extension="xml" ContentType="application/xml"/><Default Extension="png" ContentType="image/png"/>'
                   '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>')
        z.writestr("_rels/.rels", '<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                   '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>')
        z.writestr("word/document.xml", doc)
        z.writestr("word/_rels/document.xml.rels", rels)
        if image is not None:
            z.writestr("word/media/image1.png", image)
    return buf.getvalue()


def spravka() -> bytes:
    return docx(["Справка", "Кравцов Сергей Сергеевич, родился 12 марта 1970 года в Москве.",
                 "С 2020 года — Министр просвещения Российской Федерации.",
                 "Окончил Московский государственный университет в 1997 году."], image=png(30, 40))


def answer_json(**over) -> dict:
    out = json.loads(json.dumps(SAMPLE))
    out.update(over)
    return out
