"""Английские слова в тезисах (задача Оркестратора P1 от 21.09).

Три рубежа: правило в задании «Искре», проверка ответа с одним кругом правки и
пометкой для человека, и сборка Word, куда служебные поля не попадают вовсе.
"""
from __future__ import annotations

import io
import json
import re
import time
import zipfile

import pytest

from app import latin
from tests.conftest import put_lines

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
SOURCE = "Уроки идут на платформе Сферум (Sferum), связь — через Microsoft Teams."

DIRTY = {"bullets": [{"text": "Это important задача для регионов.", "emphasis": "strong"},
                     {"text": "Уроки идут на Sferum, связь через Teams."}],
         "gaps": [{"marker": "ТРЕБУЕТСЯ ФАКТУРА", "suggestion": "coverage в сельских школах"}]}
CLEAN = {"bullets": [{"text": "Это важная задача для регионов.", "emphasis": "strong"},
                     {"text": "Уроки идут на Sferum, связь через Teams."}],
         "gaps": [{"marker": "ТРЕБУЕТСЯ ФАКТУРА", "suggestion": "охват в сельских школах"}]}


# ── распознавание ────────────────────────────────────────────────────────

def test_что_считается_английским():
    known = latin.known_words(SOURCE)
    found = latin.foreign(
        "Это important задача: Sferum и Teams, IT-классы, пункт b, ГОСТ Р 7.0, "
        "www.gov.ru, почта info@mail.ru, пpограмма, наш approach.", known)
    assert found == ["important", "пpограмма", "approach"]


# ── белый список по находкам ревьюера (RC-2026-09-21-10, дополнение) ─────

def test_капс_из_обычного_английского_слова_это_вставка():
    """«Капс до шести букв» больше не пропуск: FINE и NICE — английские слова,
    AI (есть «ИИ»), CEO, CTO — английские аббревиатуры не из общего списка."""
    assert latin.foreign("FINE NICE AI CEO CTO ABCD", set()) == \
        ["FINE", "NICE", "AI", "CEO", "CTO", "ABCD"]


def test_общеупотребительные_аббревиатуры_остаются():
    assert latin.foreign("IT-классы, PDF, GPS, ISO, USB, QR-код", set()) == []


def test_аббревиатура_из_правил_или_темы_остаётся():
    """KPI — нет в общем списке, но есть в правилах ДНЧ: это слово человека, не вставка."""
    assert latin.foreign("KPI-трекинг", set()) == ["KPI"]
    assert latin.foreign("KPI-трекинг", latin.known_words("придуманные KPI")) == []


def test_адреса_единицы_формулы_и_составные_не_вставки():
    text = ("gosuslugi.ru, edu.gov.ru/news, kWh, Mbps, GHz, pH, NaCl, NaOH, CaCO3, H2O, CO2, "
            "sin, log, Wi-Fi, e-mail, 5G, COVID-19, XXI")
    assert latin.foreign(text, set()) == []


def test_имя_собственное_без_материалов_не_гоняет_круг_правки():
    """Microsoft и Python — имена, а не английские вставки: без этого любое название
    в тезисах без материалов запускало бы платный круг и становилось «Майкрософтом»."""
    assert latin.foreign("Таблицы в Microsoft Excel, практикум по Python, курсы в Moodle",
                         set()) == []
    # составное название: обычное слово рядом с именем собственным — часть названия (П2)
    assert latin.foreign("Microsoft Teams, Microsoft Office, Apple Pay, Yandex Market", set()) == []
    # а одно обычное слово с заглавной без имени рядом — по-прежнему вставка
    assert latin.foreign("Сделаем через Teams", set()) == ["Teams"]


def test_слова_из_темы_известны():
    from app.models import Bullet, Thesis
    thesis = Thesis(topic="О внедрении Moodle и dashboard для школ",
                    bullets=[Bullet(text="Moodle работает, dashboard готов.")])
    assert thesis.foreign_words() == {}, "слова темы дал человек — это не вставки"


def test_английское_с_заглавной_всё_равно_вставка():
    assert latin.foreign("Important: Dashboard и Deal", set()) == ["Important", "Dashboard", "Deal"]


def test_слово_с_буквами_вперемешку_ловится_даже_если_похоже_на_русское():
    assert latin.mixed("пpограмма") and latin.foreign("пpограмма", set()) == ["пpограмма"]


# ── задание «Искре» ──────────────────────────────────────────────────────

def test_правило_о_языке_в_системной_части():
    from app import iskra
    system = iskra._system("", "")
    assert "только по-русски" in system
    assert "служебные: в сам текст их не переносить" in system


# ── ответ с английским: один круг правки ─────────────────────────────────

@pytest.fixture()
def rounds(monkeypatch):
    from app import iskra
    box = {"answers": [], "asked": []}

    def fake_call(system, user):
        box["asked"].append(user)
        iskra.spend(1000, 200)
        return json.dumps(box["answers"].pop(0), ensure_ascii=False)

    monkeypatch.setattr(iskra, "_call", fake_call)
    monkeypatch.setattr(iskra, "key_ready", lambda: True)
    return box


def _run(sources_text=SOURCE):
    from app import iskra
    from app.models import MODE_DOCS, Thesis
    thesis = Thesis(topic="О связи в школах", mode=MODE_DOCS)
    ctl = iskra.Control()
    iskra.bind(ctl)
    try:
        return iskra.write(thesis, "", "", sources_text), ctl
    finally:
        iskra.bind(None)


def test_английские_вставки_правятся_вторым_кругом(rounds):
    rounds["answers"] = [DIRTY, CLEAN]
    result, ctl = _run()
    texts = [b.text for b in result["bullets"]]
    assert texts[0] == "Это важная задача для регионов."
    assert result["gaps"][0].suggestion == "охват в сельских школах"
    assert len(rounds["asked"]) == 2, "круг правки ровно один"
    assert "important" in rounds["asked"][1] and "coverage" in rounds["asked"][1]
    assert "Sferum" not in rounds["asked"][1].split("ТЕКУЩИЙ ОТВЕТ")[0], \
        "имя из материалов правке не подлежит"
    assert ctl.spent == {"in": 2000, "out": 400}, "расход обоих кругов — в одном счёте"
    assert not any(n.startswith("Проверьте") for n in result["notes"])


def test_имя_собственное_из_материалов_остаётся_без_лишнего_круга(rounds):
    rounds["answers"] = [CLEAN]
    result, _ = _run()
    assert result["bullets"][1].text == "Уроки идут на Sferum, связь через Teams."
    assert len(rounds["asked"]) == 1


def test_не_вычистилось_показываем_человеку(rounds):
    rounds["answers"] = [DIRTY, DIRTY]
    result, _ = _run()
    note = [n for n in result["notes"] if n.startswith("Проверьте: английские слова")]
    assert note and "important" in note[0] and "после круга правки" in note[0]


def test_в_тезисах_слова_подсвечены_и_уходят_после_правки(client, emp, rounds):
    rounds["answers"] = [DIRTY, DIRTY]
    created = client.post("/api/theses", json={"topic": "О связи", "minutes": 5},
                          headers=emp).json()
    tid, rev = created["thesis"]["id"], created["rev"]
    files = {"file": ("справка.txt", (SOURCE * 3).encode("utf-8"), "text/plain")}
    rev = client.post(f"/api/theses/{tid}/sources/file?rev={rev}", headers=emp,
                      files=files).json()["rev"]
    job = client.post(f"/api/theses/{tid}/write", json={"rev": rev}, headers=emp).json()["job"]
    deadline = time.time() + 10
    while job["active"] and time.time() < deadline:
        time.sleep(0.05)
        job = client.get(f"/api/jobs/{job['id']}", headers=emp).json()["job"]
    got = client.get(f"/api/theses/{tid}", headers=emp).json()
    thesis = got["thesis"]
    assert "important" in thesis["foreign_words"] and "Sferum" not in thesis["foreign_words"]
    first = thesis["bullets"][0]
    assert thesis["foreign"][first["id"]] == ["important"]
    # человек поправил — пометка ушла сама
    lines = [{"id": b["id"], "text": b["text"], "emphasis": b["emphasis"]}
             for b in thesis["bullets"]]
    lines[0]["text"] = "Это важная задача для регионов."
    fixed = put_lines(client, emp, tid, lines, got["rev"]).json()["thesis"]
    assert first["id"] not in fixed["foreign"]


# ── Word: служебное в документ не попадает ───────────────────────────────

def test_служебные_ключи_в_word_не_попадают():
    from app import docx_export
    from app.models import ASIDE, STRONG, Bullet, Gap, Ref, Thesis, sync_gaps_from_text
    bullets = [Bullet(text="Коллеги, добрый день.", part="контекст"),
               Bullet(text="Клубы есть в 80 процентах школ.", emphasis=STRONG, part="масштаб"),
               Bullet(text="Например, клуб открыли при колледже.", emphasis=ASIDE),
               Bullet(text="Охват ниже [ТРЕБУЕТСЯ ФАКТУРА: охват на селе].")]
    thesis = Thesis(topic="О клубах", bullets=bullets,
                    refs=[Ref(name="Stat", url="https://example.org/stat", about="доля")])
    thesis.gaps.append(Gap(suggestion="охват по округам"))
    sync_gaps_from_text(thesis)
    data = docx_export.to_bytes(docx_export.build(thesis))
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        body = z.read("word/document.xml").decode("utf-8")
        core = z.read("docProps/core.xml").decode("utf-8")
    visible = " ".join(re.findall(r"<w:t(?: [^>]*)?>([^<]*)</w:t>", body))
    assert not re.search(r"[A-Za-z]", visible), f"латиница в тексте документа: {visible}"
    for key in ("bullets", "emphasis", "strong", "aside", "normal", "part", "gaps",
                "refs", "url", "about"):
        assert key not in visible
    assert "python-docx" not in core



# ── круг правки: структура должна уцелеть (Н2) ───────────────────────────

DIRTY_TWO = {"bullets": [{"text": "Это important задача."},
                         {"text": "Охват ниже [ТРЕБУЕТСЯ ФАКТУРА: охват на селе], рост 12 %."}],
             "gaps": [{"marker": "ТРЕБУЕТСЯ ФАКТУРА", "suggestion": "охват на селе"}]}


def test_круг_правки_потерял_тезис_и_пометку_оставляем_первый(rounds):
    rounds["answers"] = [DIRTY_TWO, {"bullets": [{"text": "Это важная задача."}], "gaps": []}]
    result, _ = _run()
    assert len(result["bullets"]) == 2, "потерянный тезис — не повод принять правку"
    assert "[нужна цифра" in result["bullets"][1].text
    note = [n for n in result["notes"] if n.startswith("Проверьте: английские слова")]
    assert note and "оставлен первый вариант" in note[0]


def test_круг_правки_изменил_числа_оставляем_первый(rounds):
    changed = json.loads(json.dumps(DIRTY_TWO))
    changed["bullets"][0]["text"] = "Это важная задача."
    changed["bullets"][1]["text"] = changed["bullets"][1]["text"].replace("12", "15")
    rounds["answers"] = [DIRTY_TWO, changed]
    result, _ = _run()
    assert "12 %" in result["bullets"][1].text


def test_круг_правки_с_целой_структурой_принимается(rounds):
    clean = json.loads(json.dumps(DIRTY_TWO))
    clean["bullets"][0]["text"] = "Это важная задача."
    rounds["answers"] = [DIRTY_TWO, clean]
    result, _ = _run()
    assert result["bullets"][0].text == "Это важная задача."
    assert not any(n.startswith("Проверьте") for n in result["notes"])


# ── срок — на весь прогон вместе с кругом правки (Н1) ────────────────────

def test_срок_общий_на_весь_прогон(monkeypatch):
    """Первый прогон съел почти весь срок — круг правки не получает ещё десять минут:
    не успел — остаётся первый ответ, и человек видит почему (а не молча)."""
    from app import iskra
    runs = {"n": 0}

    def fake_http(method, path, body=None, timeout=60):
        if method == "POST":
            runs["n"] += 1
            return 202, {"run_id": f"r{runs['n']}"}
        if method == "DELETE":
            return 200, {}
        if path.endswith("/r1"):
            time.sleep(0.4)                     # первый прогон тратит почти весь срок
            return 200, {"status": "completed", "answer": json.dumps(DIRTY, ensure_ascii=False)}
        return 200, {"status": "running"}        # круг правки не успевает

    monkeypatch.setattr(iskra, "_http", fake_http)
    monkeypatch.setattr(iskra, "POLL_MIN", 0.0)
    monkeypatch.setattr(iskra, "POLL_MAX", 0.0)
    from app.models import MODE_DOCS, Thesis
    ctl = iskra.Control(deadline=0.8)
    iskra.bind(ctl)
    started = time.monotonic()
    try:
        result = iskra.write(Thesis(topic="О связи", mode=MODE_DOCS), "", "", SOURCE)
    finally:
        iskra.bind(None)
    assert time.monotonic() - started < 2.5, "срок считается от начала, а не заново на круг"
    note = [n for n in result["notes"] if n.startswith("Проверьте: английские слова")]
    assert note and "круг правки не удался" in note[0] and "не ответила" in note[0]
    assert result["bullets"][0].text == DIRTY["bullets"][0]["text"], "остался первый ответ"


# ── RC-2026-09-21-13 ─────────────────────────────────────────────────────

def test_п6_жаргон_и_римские_буквы():
    assert latin.foreign("Stakeholders, Benchmark, Onboarding, Scrum", set()) == \
        ["Stakeholders", "Benchmark", "Onboarding", "Scrum"]
    assert latin.foreign("MID DID CIVIL", set()) == ["MID", "DID", "CIVIL"]
    assert latin.foreign("XXI век, глава XIV", set()) == []


# ── слова вперемешку: «к zadанию» (задача Оркестратора 21.09, п. 3) ──────
#
# Откуда берётся: у «z» и «d» нет русских двойников, поэтому это не шрифт, не
# распознавание PDF и не кодировка — чтение документа буквы не меняет (тест ниже).
# Такое слово порождает только тот, кто пишет текст: «Искра» (или сервис, чей готовый
# текст приложили материалом, — например, саммари «Брифа», его тоже пишет «Искра»).

EXAMPLE = "Треть экзаменуемых не приступают к zadанию вовсе."


def test_двойники_меняются_транслит_нет():
    assert latin.fix_mixed("IТ-классы, прoграмма, Miсrosoft") == \
        ("IT-классы, программа, Microsoft", [])
    text, left = latin.fix_mixed(EXAMPLE)
    assert text == EXAMPLE and left == ["zadанию"], "угадывать транслит сервис не вправе"
    assert latin.guess("zadанию") == "заданию", "но подсказать человеку может"
    assert latin.fix_mixed("Wi-Fi, COVID-19, e-mail и ГОСТ")[1] == []


def test_чтение_документа_буквы_не_меняет():
    """Доказательство для п. 3: в справке «к заданию» — в тексте «к заданию». Чтение docx,
    pdf и txt копирует знаки как есть; латиница из ниоткуда не появляется."""
    import docx
    from app import doc_text
    phrase = "Треть экзаменуемых не приступают к заданию вовсе."
    buffer = io.BytesIO()
    document = docx.Document()
    document.add_paragraph(phrase)
    document.save(buffer)
    assert doc_text.extract("справка.docx", buffer.getvalue()) == phrase
    assert doc_text.extract("справка.txt", phrase.encode("utf-8")) == phrase
    import pymupdf
    pdf = pymupdf.open()
    page = pdf.new_page()
    page.insert_font(fontname="ru", fontfile=_cyrillic_font())
    page.insert_text((72, 72), phrase, fontname="ru", fontsize=11)
    got = doc_text.extract("справка.pdf", pdf.tobytes())
    assert " ".join(got.split()) == phrase
    assert not re.search(r"[A-Za-z]", got)


def _cyrillic_font() -> str:
    from pathlib import Path
    for candidate in ("/System/Library/Fonts/Supplemental/Arial.ttf",
                      "/Library/Fonts/Arial.ttf",
                      "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
                      "C:/Windows/Fonts/arial.ttf"):
        if Path(candidate).is_file():
            return candidate
    pytest.skip("нет шрифта с кириллицей для пробного PDF")


def test_двойники_в_справке_правятся_при_чтении():
    """Автор справки набрал «IТ» с русской «Т» — «Искра» не должна переписать это как есть."""
    from app import doc_text
    assert doc_text.extract("справка.txt", "IТ-классы в школах".encode("utf-8")) == \
        "IT-классы в школах"


def test_слово_вперемешку_от_искры_уходит_в_круг_правки(rounds):
    """«Искра» вернула «к zadанию» — это ловится до применения и правится вторым кругом."""
    first = {"bullets": [{"text": EXAMPLE}]}
    fixed = {"bullets": [{"text": "Треть экзаменуемых не приступают к заданию вовсе."}]}
    rounds["answers"] = [first, fixed]
    result, _ = _run()
    assert result["bullets"][0].text == "Треть экзаменуемых не приступают к заданию вовсе."
    assert "zadанию" in rounds["asked"][1] and "вперемешку" in rounds["asked"][1]


def test_слово_вперемешку_не_исправилось_человек_видит_и_документ_ждёт(rounds):
    rounds["answers"] = [{"bullets": [{"text": EXAMPLE}]}, {"bullets": [{"text": EXAMPLE}]}]
    result, _ = _run()
    note = [n for n in result["notes"] if n.startswith("Поправьте: слова наполовину латиницей")]
    assert note and "zadанию" in note[0] and "документ не собирается" in note[0]


def test_двойники_от_искры_правятся_без_платного_круга(rounds):
    rounds["answers"] = [{"bullets": [{"text": "IТ-классы и прoграмма развития."}]}]
    result, ctl = _run()
    assert result["bullets"][0].text == "IT-классы и программа развития."
    assert len(rounds["asked"]) == 1 and ctl.spent == {"in": 1000, "out": 200}


def test_в_тезисах_слово_вперемешку_отдельным_списком(client, emp):
    created = client.post("/api/theses", json={"topic": "Об экзаменах"}, headers=emp).json()
    body = put_lines(client, emp, created["thesis"]["id"], [EXAMPLE], created["rev"]).json()
    thesis = body["thesis"]
    assert thesis["mixed_words"] == [{"word": "zadанию", "guess": "заданию", "where": ["текст"]}]
    assert "zadанию" not in thesis["foreign_words"], "в английские его не записываем дважды"


# ── ревьюер RC-2026-09-21-17 ─────────────────────────────────────────────

def test_п1_русское_окончание_у_латинского_названия_не_портится():
    """«в Excelе» — склонение, а не буква-двойник: раньше «е» становилась латинской."""
    for text in ("данные в Excelе", "на iPhoneе", "в PDFе", "в Windowsах"):
        assert latin.fix_mixed(text) == (text, []), text
    # название — не повод пропускать: сбой генерации строчной латиницей держит документ
    assert latin.fix_mixed("к zadачу")[1] == ["zadачу"]
    # а двойники по-прежнему правятся
    assert latin.fix_mixed("IТ-классы")[0] == "IT-классы"
    # английское слово с окончанием — подсветка, как у английского
    assert latin.foreign("в Dashboardе", set()) == ["Dashboardе"]
    assert latin.foreign("в Excelе", set()) == []


def test_п1_окончание_не_меняется_при_сохранении(client, emp):
    created = client.post("/api/theses", json={"topic": "О таблицах"}, headers=emp).json()
    body = put_lines(client, emp, created["thesis"]["id"], ["Данные сводим в Excelе."],
                     created["rev"]).json()
    assert body["thesis"]["bullets"][0]["text"] == "Данные сводим в Excelе."
    assert body["thesis"]["mixed_words"] == []


def test_п2_тема_и_место_проверяются(client, emp):
    """«zadанию» в теме ушло бы в имя файла Word, в месте — в название совещания."""
    created = client.post("/api/theses", headers=emp, json={
        "topic": "О zadаниях ЕГЭ", "event": "Совещание по IТ"}).json()
    tid, rev = created["thesis"]["id"], created["rev"]
    thesis = created["thesis"]
    assert thesis["event"] == "Совещание по IT", "двойник в месте правится сам"
    assert thesis["mixed_words"][0]["word"] == "zadаниях"
    assert thesis["mixed_words"][0]["where"] == ["тема"]
    put_lines(client, emp, tid, ["Коллеги."], rev)
    assert client.get(f"/api/theses/{tid}/export/theses.docx", headers=emp).status_code == 409
    assert client.get(f"/api/theses/{tid}/moderator-payload", headers=emp).status_code == 409
    fresh = client.get(f"/api/theses/{tid}", headers=emp).json()
    client.put(f"/api/theses/{tid}", headers=emp, json={
        "topic": "О заданиях ЕГЭ", "event": "Совещание по IT", "minutes": 7, "rev": fresh["rev"]})
    assert client.get(f"/api/theses/{tid}/export/theses.docx", headers=emp).status_code == 200


def test_н5_обычное_слово_рядом_с_названием_не_прячется():
    """Ревьюер RC-16, Н5: «Microsoft Dashboard», «Google Benchmark» проходили как названия."""
    assert latin.foreign("Microsoft Dashboard, Google Benchmark", set()) == ["Dashboard", "Benchmark"]
    # настоящие составные названия по-прежнему не вставки
    assert latin.foreign("Microsoft Teams, Microsoft Office, Apple Pay, Yandex Market", set()) == []
    # а если такое название дал сам человек в материалах — это его слово
    assert latin.foreign("Microsoft Dashboard", latin.known_words("в справке: Microsoft Dashboard")) == []



def test_rc18_заглавная_не_делает_обрывок_окончанием():
    """«Zadачу» с заглавной проходила как «название с окончанием»: «ачу» — не падежное окончание."""
    assert latin.fix_mixed("к Zadачу")[1] == ["Zadачу"]
    for text in ("в Excelе", "в PDFке", "в Windowsах", "с iPhoneом"):
        assert latin.fix_mixed(text) == (text, []), text
