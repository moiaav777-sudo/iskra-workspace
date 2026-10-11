"""Скачать документ пакета в другом формате: `download?as=pdf|docx` (вкладка «Пакет
к совещанию» в «Совещании»).

Настоящего LibreOffice тесты не зовут: вместо него — подменный `soffice`, shell-скрипт,
который пишет каждый вызов в журнал (по нему считаем запуски) и кладёт в `--outdir`
файл «CONVERTED <формат> » + содержимое исходника. Режимы — переменной FAKE_SOFFICE_MODE.
"""
import io
import os
import stat
import threading
import time
import zipfile
from pathlib import Path
from urllib.parse import quote

import pytest
from docx import Document

FAKE = r"""#!/bin/sh
log="$FAKE_SOFFICE_LOG"
echo "CALL HOME=$HOME $*" >> "$log"
fmt=""; outdir=""; src=""
while [ $# -gt 0 ]; do
  case "$1" in
    --convert-to) fmt="${2%%:*}"; shift 2;;
    --outdir) outdir="$2"; shift 2;;
    *) src="$1"; shift;;
  esac
done
case "${FAKE_SOFFICE_MODE:-ok}" in
  sleep) sleep 30;;
  fail) echo "convert /srv/materials/secret/Хранилище/file.docx -> pdf" >&2
        echo "Error: source file could not be loaded" >&2; exit 81;;
  silent) echo "Error: source file could not be loaded" >&2; exit 0;;
  slow) if [ -e "$log.busy" ]; then echo OVERLAP >> "$log"; fi
        touch "$log.busy"; sleep 1; rm -f "$log.busy";;
esac
base=$(basename "$src"); stem="${base%.*}"
printf 'CONVERTED %s ' "$fmt" > "$outdir/$stem.$fmt"
cat "$src" >> "$outdir/$stem.$fmt"
"""

DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def _docx(*paragraphs: str) -> bytes:
    doc = Document()
    for text in paragraphs:
        doc.add_paragraph(text)
    buffer = io.BytesIO()
    doc.save(buffer)
    return buffer.getvalue()


@pytest.fixture()
def fake(tmp_path, monkeypatch):
    """Подменный soffice через MATERIALS_SOFFICE. Возвращает функцию «сколько было запусков»."""
    script = tmp_path / "bin" / "soffice"
    script.parent.mkdir()
    script.write_text(FAKE, encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    log = tmp_path / "soffice.log"
    log.write_text("", encoding="utf-8")
    monkeypatch.setenv("MATERIALS_SOFFICE", str(script))
    monkeypatch.setenv("FAKE_SOFFICE_LOG", str(log))
    monkeypatch.delenv("FAKE_SOFFICE_MODE", raising=False)

    class Fake:
        path = script

        @staticmethod
        def lines() -> list[str]:
            return log.read_text(encoding="utf-8").splitlines()

        @classmethod
        def calls(cls) -> int:
            return sum(1 for line in cls.lines() if line.startswith("CALL "))

    return Fake


@pytest.fixture()
def doc(client, emp, event, tmp_path):
    """Документ в пакете: имя в папке и путь на диске."""
    r = client.post(f"/api/events/{event['id']}/files", headers=emp,
                    files=[("files", ("Справка о развитии округа.docx",
                                      _docx("Справка о развитии округа", "Текст справки"), DOCX))])
    assert r.status_code == 200, r.text
    name = next(f["name"] for f in r.json()["files"] if f["number"] != "00")
    return {"name": name, "path": tmp_path / "Хранилище" / event["folder"] / name}


def _get(client, emp, event, name, **params):
    return client.get(f"/api/events/{event['id']}/files/{name}/download", headers=emp,
                      params=params)


def test_без_as_и_с_тем_же_форматом_отдаётся_оригинал(client, emp, event, doc, fake):
    original = doc["path"].read_bytes()
    for params in ({}, {"as": "docx"}, {"as": "DOCX"}, {"as": ""}):
        r = _get(client, emp, event, doc["name"], **params)
        assert r.status_code == 200, (params, r.text)
        assert r.content == original
        assert quote(doc["name"]) in r.headers["content-disposition"]
    assert fake.calls() == 0


def test_docx_в_pdf_переводится_и_берётся_из_кэша(client, emp, event, doc, fake, tmp_path):
    r = _get(client, emp, event, doc["name"], **{"as": "pdf"})
    assert r.status_code == 200, r.text
    assert r.content.startswith(b"CONVERTED pdf ")
    assert r.content.endswith(doc["path"].read_bytes())
    assert r.headers["content-type"] == "application/pdf"
    pdf_name = doc["name"][:-len(".docx")] + ".pdf"
    assert quote(pdf_name) in r.headers["content-disposition"]
    assert fake.calls() == 1

    again = _get(client, emp, event, doc["name"], **{"as": "pdf"})
    assert again.status_code == 200
    assert again.content == r.content
    assert fake.calls() == 1                       # второй раз soffice не звали

    # кэш — у сервиса в data/, а не в папке: ни в карточке, ни в пакете его нет
    cached = list((tmp_path / "data" / "convert" / event["id"]).glob("*.pdf"))
    assert len(cached) == 1 and cached[0].name.startswith(doc["name"] + "~")
    card = client.get(f"/api/events/{event['id']}", headers=emp).json()
    assert not any(f["name"].endswith(".pdf") for f in card["files"])
    assert not any(a["name"].endswith(".pdf") for a in card["archive"])
    package = client.get(f"/api/events/{event['id']}/export/package.zip", headers=emp)
    assert not any(n.endswith(".pdf") for n in zipfile.ZipFile(io.BytesIO(package.content)).namelist())


def test_запуск_со_своим_профилем_и_нужными_ключами(client, emp, event, doc, fake):
    assert _get(client, emp, event, doc["name"], **{"as": "pdf"}).status_code == 200
    line = fake.lines()[0]
    assert "--headless" in line and "--convert-to pdf" in line
    assert "-env:UserInstallation=file://" in line
    home = line.split("HOME=", 1)[1].split(" ", 1)[0]
    assert "materials-convert-" in home and home != str(Path.home())   # HOME свой, временный
    assert "--infilter" not in line


def test_pdf_в_word_идёт_через_импорт_writer(client, emp, event, fake):
    pdf = b"%PDF-1.4\n%fake\n"
    r = client.post(f"/api/events/{event['id']}/files", headers=emp,
                    files=[("files", ("Расчёт потребности в жилье.pdf", pdf, "application/pdf"))])
    name = next(f["name"] for f in r.json()["files"] if f["ext"] == "pdf")
    r = _get(client, emp, event, name, **{"as": "docx"})
    assert r.status_code == 200, r.text
    assert r.content == b"CONVERTED docx " + pdf
    assert r.headers["content-type"] == DOCX
    assert quote(name[:-len(".pdf")] + ".docx") in r.headers["content-disposition"]
    line = fake.lines()[0]
    assert "--infilter=writer_pdf_import" in line
    assert "--convert-to docx:MS Word 2007 XML" in line


def test_исходник_изменился_переводим_заново(client, emp, event, doc, fake, tmp_path):
    first = _get(client, emp, event, doc["name"], **{"as": "pdf"})
    assert fake.calls() == 1

    # поправили файл через сетевой диск — другое содержимое и размер
    doc["path"].write_bytes(_docx("Справка о развитии округа", "Новая редакция, текст длиннее прежнего"))
    second = _get(client, emp, event, doc["name"], **{"as": "pdf"})
    assert second.status_code == 200
    assert fake.calls() == 2
    assert second.content != first.content
    assert second.content.endswith(doc["path"].read_bytes())

    # тот же размер, но другое время изменения — тоже новая версия
    st = doc["path"].stat()
    os.utime(doc["path"], ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
    assert _get(client, emp, event, doc["name"], **{"as": "pdf"}).status_code == 200
    assert fake.calls() == 3

    # прежние переводы не копятся
    assert len(list((tmp_path / "data" / "convert" / event["id"]).glob("*.pdf"))) == 1


@pytest.mark.parametrize("value", ["txt", "exe", "pdf;rm", "odt"])
def test_формат_не_из_списка_400(client, emp, event, doc, fake, value):
    r = _get(client, emp, event, doc["name"], **{"as": value})
    assert r.status_code == 400
    assert "PDF" in r.json()["detail"]
    assert fake.calls() == 0


def test_неподходящая_пара_400(client, emp, event, fake):
    r = client.post(f"/api/events/{event['id']}/files", headers=emp,
                    files=[("files", ("Заметка для себя.txt", "просто текст".encode(), "text/plain"))])
    name = next(f["name"] for f in r.json()["files"] if f["ext"] == "txt")
    for fmt in ("pdf", "docx"):
        r = _get(client, emp, event, name, **{"as": fmt})
        assert r.status_code == 400
        assert "«.txt»" in r.json()["detail"]
    assert fake.calls() == 0


def test_нет_libreoffice_501_и_в_карточке_недоступно(client, emp, event, doc, tmp_path, monkeypatch):
    empty = tmp_path / "empty-bin"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    monkeypatch.delenv("MATERIALS_SOFFICE", raising=False)
    card = client.get(f"/api/events/{event['id']}", headers=emp).json()
    assert card["convert"]["available"] is False
    assert card["convert"]["formats"] == ["pdf", "docx"]
    r = _get(client, emp, event, doc["name"], **{"as": "pdf"})
    assert r.status_code == 501
    assert r.json()["detail"] == ("На сервере нет LibreOffice — конвертация в PDF/Word пока "
                                  "недоступна, скачайте оригинал")
    # путь в MATERIALS_SOFFICE не туда — тоже «нет»
    monkeypatch.setenv("MATERIALS_SOFFICE", str(tmp_path / "нет-такого" / "soffice"))
    assert _get(client, emp, event, doc["name"], **{"as": "pdf"}).status_code == 501
    # оригинал при этом качается как всегда
    assert _get(client, emp, event, doc["name"]).status_code == 200


def test_в_карточке_конвертер_доступен(client, emp, event, fake):
    card = client.get(f"/api/events/{event['id']}", headers=emp).json()
    assert card["convert"]["available"] is True
    assert card["convert"]["formats"] == ["pdf", "docx"]


@pytest.mark.parametrize("mode", ["fail", "silent"])
def test_ошибка_libreoffice_502_без_путей(client, emp, event, doc, fake, monkeypatch, tmp_path, mode):
    monkeypatch.setenv("FAKE_SOFFICE_MODE", mode)
    r = _get(client, emp, event, doc["name"], **{"as": "pdf"})
    assert r.status_code == 502
    detail = r.json()["detail"]
    assert "could not be loaded" in detail
    assert "/srv/materials" not in detail and "Хранилище" not in detail
    assert str(tmp_path) not in detail
    assert not list((tmp_path / "data" / "convert").rglob("*.pdf"))   # неудача не кэшируется
    # починили — следующая попытка переводит
    monkeypatch.delenv("FAKE_SOFFICE_MODE")
    assert _get(client, emp, event, doc["name"], **{"as": "pdf"}).status_code == 200


def test_таймаут_502_и_процесс_снят(client, emp, event, doc, fake, monkeypatch):
    monkeypatch.setenv("FAKE_SOFFICE_MODE", "sleep")
    monkeypatch.setenv("MATERIALS_CONVERT_TIMEOUT", "1")
    started = time.monotonic()
    r = _get(client, emp, event, doc["name"], **{"as": "pdf"})
    elapsed = time.monotonic() - started
    assert r.status_code == 502
    assert "не уложился в 1 с" in r.json()["detail"]
    # скрипт спит 30 с: если бы «sleep» внутри пережил снятие, ответ ждал бы его
    assert elapsed < 15


def test_первичка_из_архива(client, emp, event, doc, fake, tmp_path):
    card = client.get(f"/api/events/{event['id']}", headers=emp).json()
    origin = card["archive"][0]["name"]
    assert origin == "Справка о развитии округа.docx"
    r = _get(client, emp, event, origin, archived=1, **{"as": "pdf"})
    assert r.status_code == 200, r.text
    archived = tmp_path / "Хранилище" / event["folder"] / "Архив" / origin
    assert r.content == b"CONVERTED pdf " + archived.read_bytes()
    assert quote("Справка о развитии округа.pdf") in r.headers["content-disposition"]
    assert list((tmp_path / "data" / "convert" / event["id"] / "Архив").glob("*.pdf"))
    # первички без archived=1 в пакете нет
    assert _get(client, emp, event, origin, **{"as": "pdf"}).status_code == 404


def test_две_конвертации_разом_не_идут(client, emp, event, doc, fake, monkeypatch):
    """Сервер слабый: второй запрос ждёт первого, а тот же файл второй раз не переводится."""
    monkeypatch.setenv("FAKE_SOFFICE_MODE", "slow")
    r = client.post(f"/api/events/{event['id']}/files", headers=emp,
                    files=[("files", ("Расчёт потребности в жилье.docx",
                                      _docx("Расчёт потребности", "Цифры"), DOCX))])
    other = next(f["name"] for f in r.json()["files"] if "Расчёт" in f["name"])
    results = []

    def fetch(name):
        results.append(_get(client, emp, event, name, **{"as": "pdf"}).status_code)

    threads = [threading.Thread(target=fetch, args=(n,)) for n in (doc["name"], doc["name"], other)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert results == [200, 200, 200]
    assert "OVERLAP" not in fake.lines()
    assert fake.calls() == 2                       # одинаковый файл — один запуск


def test_перевод_в_журнал_не_пишется_как_и_скачивание(client, emp, event, doc, fake, monkeypatch):
    from app import portal
    seen = []
    monkeypatch.setattr(portal, "send_audit", lambda **e: seen.append(e))
    assert _get(client, emp, event, doc["name"], **{"as": "pdf"}).status_code == 200
    assert _get(client, emp, event, doc["name"]).status_code == 200
    assert seen == []


# ── чистка кэша (ревьюер RC-2026-10-02-8, Н-1) ───────────────────────────

def _cache(tmp_path, event, sub=""):
    return sorted(p.name for p in (tmp_path / "data" / "convert" / event["id"] / sub).glob("*.*")) \
        if (tmp_path / "data" / "convert" / event["id"] / sub).is_dir() else []


def test_документ_убран_из_пакета_его_перевод_стёрт(client, emp, event, doc, fake, tmp_path):
    assert _get(client, emp, event, doc["name"], **{"as": "pdf"}).status_code == 200
    assert len(_cache(tmp_path, event)) == 1
    r = client.delete(f"/api/events/{event['id']}/files/{doc['name']}", headers=emp)
    assert r.status_code == 200, r.text
    assert _cache(tmp_path, event) == []            # копии документа, которого нет в пакете, нет и в кэше


def test_переименование_документа_не_оставляет_старого_перевода(client, emp, event, doc, fake, tmp_path):
    assert _get(client, emp, event, doc["name"], **{"as": "pdf"}).status_code == 200
    r = client.post(f"/api/events/{event['id']}/files/{doc['name']}/title", headers=emp,
                    json={"title": "Справка о развитии округа (новая)"})
    assert r.status_code == 200, r.text
    assert _cache(tmp_path, event) == []
    new_name = next(f["name"] for f in r.json()["files"] if f["number"] != "00")
    assert new_name != doc["name"]
    assert _get(client, emp, event, new_name, **{"as": "pdf"}).status_code == 200
    assert fake.calls() == 2 and len(_cache(tmp_path, event)) == 1


def test_первичка_стёрта_навсегда_вместе_с_переводом(client, emp, admin, event, doc, fake, tmp_path):
    card = client.get(f"/api/events/{event['id']}", headers=emp).json()
    origin = card["archive"][0]["name"]
    assert _get(client, emp, event, origin, archived=1, **{"as": "pdf"}).status_code == 200
    assert len(_cache(tmp_path, event, "Архив")) == 1
    r = client.delete(f"/api/events/{event['id']}/archive/{origin}", headers=admin)
    assert r.status_code == 200, r.text
    assert _cache(tmp_path, event, "Архив") == []


def test_мероприятие_стёрто_с_диска_вместе_с_кэшем(client, emp, admin, event, doc, fake, tmp_path):
    assert _get(client, emp, event, doc["name"], **{"as": "pdf"}).status_code == 200
    assert (tmp_path / "data" / "convert" / event["id"]).is_dir()
    r = client.delete(f"/api/events/{event['id']}", headers=admin)
    assert r.status_code == 200, r.text
    assert not (tmp_path / "data" / "convert" / event["id"]).exists()


def test_libreoffice_первым_под_нож_при_нехватке_памяти(client, emp, event, doc, fake, monkeypatch, tmp_path):
    """oom_score_adj процесса soffice поднимается до 1000 (ревьюер RC-2026-10-02-8, Н-2)."""
    if not Path("/proc/self/oom_score_adj").exists():
        pytest.skip("не Linux")
    seen = tmp_path / "oom"
    script = fake.path.read_text(encoding="utf-8").replace(
        'echo "CALL HOME=$HOME $*" >> "$log"',
        'echo "CALL HOME=$HOME $*" >> "$log"; sleep 0.3; cat /proc/self/oom_score_adj > "%s"' % seen)
    fake.path.write_text(script, encoding="utf-8")
    assert _get(client, emp, event, doc["name"], **{"as": "pdf"}).status_code == 200
    assert seen.read_text().strip() == "1000"


# ── кэш едет за папкой и не остаётся сиротой (ревьюер RC-2026-10-02-9, Н-7, Н-8) ──

def test_перенос_даты_мероприятия_переносит_кэш(client, emp, event, doc, fake, tmp_path):
    assert _get(client, emp, event, doc["name"], **{"as": "pdf"}).status_code == 200
    r = client.put(f"/api/events/{event['id']}", headers=emp, json={"date": "2026-11-20", "title": event["title"]})
    assert r.status_code == 200, r.text
    card = r.json()
    fresh = card["event"]
    assert fresh["id"] != event["id"]
    assert not (tmp_path / "data" / "convert" / event["id"]).exists()      # сироты под старым id нет
    assert len(_cache(tmp_path, fresh)) == 1
    name = next(f["name"] for f in card["files"] if f["number"] != "00")
    assert _get(client, emp, fresh, name, **{"as": "pdf"}).status_code == 200
    assert fake.calls() == 1                                                 # взяли из переехавшего кэша


def test_убранное_мероприятие_без_кэша(client, emp, event, doc, fake, tmp_path):
    assert _get(client, emp, event, doc["name"], **{"as": "pdf"}).status_code == 200
    r = client.post(f"/api/events/{event['id']}/drop", headers=emp)
    assert r.status_code == 200, r.text
    assert not (tmp_path / "data" / "convert" / event["id"]).exists()


def test_перенумерация_чистит_переводы_под_старыми_именами(client, emp, event, doc, fake, tmp_path):
    r = client.post(f"/api/events/{event['id']}/files", headers=emp,
                    files=[("files", ("Второй документ.docx", _docx("Второй документ"), DOCX))])
    assert r.status_code == 200, r.text
    names = [f["name"] for f in r.json()["files"] if f["number"] != "00"]
    assert len(names) == 2
    for n in names:
        assert _get(client, emp, event, n, **{"as": "pdf"}).status_code == 200
    assert len(_cache(tmp_path, event)) == 2
    r = client.post(f"/api/events/{event['id']}/order", headers=emp, json={"names": list(reversed(names))})
    assert r.status_code == 200, r.text
    moved = [f["name"] for f in r.json()["files"] if f["number"] != "00"]
    assert set(moved) != set(names)                                          # номера в именах сменились
    assert _cache(tmp_path, event) == []                                     # старые переводы стёрты


def test_при_старте_сносится_кэш_без_хозяина(client, emp, event, doc, fake, tmp_path):
    from app import convert
    assert _get(client, emp, event, doc["name"], **{"as": "pdf"}).status_code == 200
    orphan = tmp_path / "data" / "convert" / "2025-01-01-нет-такого"
    orphan.mkdir(parents=True)
    (orphan / "x~1-1.pdf").write_bytes(b"old")
    assert convert.prune_orphans({event["id"]}) == 1
    assert not orphan.exists() and len(_cache(tmp_path, event)) == 1
