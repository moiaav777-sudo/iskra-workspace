"""Разделы тезисов — заголовки текста (решение пользователя 13.10, «каждый раздел — отдельным
блоком для форматирования, как в модераторе; единый формат»).

Хранение прежнее — один HTML; интерфейс делит его на блоки по заголовкам h1–h3. Отсюда
требования к серверу:
- документ «Искры» сразу делится на разделы: пункты одной части (`part`) — под `<h2>Часть</h2>`,
  если частей две и больше;
- заголовки — не пункты: не произносятся, в счёт слов и минут не идут, в «Совещание» строкой
  не уезжают, `has_text` их не считает; но в Word идут — полужирными и крупнее;
- белый список (`richtext.clean`) пропускает h1–h3 с их уровнем.
"""
from __future__ import annotations

import io
import re
import zipfile

from app import docx_export, moderator, richtext
from app.models import Bullet, Thesis
from tests.conftest import put_html


def _new(client, emp):
    created = client.post("/api/theses", json={"topic": "О школьных клубах", "minutes": 5}, headers=emp).json()
    return created["thesis"]["id"], created["rev"]


# ── «Искра»: пункты по частям → разделы ─────────────────────────────────

def test_пункты_двух_частей_идут_под_заголовками():
    bullets = [Bullet(text="Коллеги, добрый день.", part="контекст"),
               Bullet(text="Клубы есть в **80 %** школ.", part="масштаб", emphasis="strong"),
               Bullet(text="Охват растёт.", part="масштаб"),
               Bullet(text="Без части — к разделу выше."),
               Bullet(text="Нужна программа.", part=" вывод ")]
    assert richtext.html_from_bullets(bullets) == (
        "<h2>Контекст</h2><ul><li>Коллеги, добрый день.</li></ul>"
        "<h2>Масштаб</h2><ul><li><strong>Клубы есть в <strong>80 %</strong> школ.</strong></li>"
        "<li>Охват растёт.</li><li>Без части — к разделу выше.</li></ul>"
        "<h2>Вывод</h2><ul><li>Нужна программа.</li></ul>")


def test_одна_часть_или_ни_одной_как_раньше():
    one = [Bullet(text="Раз.", part="масштаб"), Bullet(text="Два.", part="Масштаб"), Bullet(text="Три.")]
    assert richtext.html_from_bullets(one) == "<ul><li>Раз.</li><li>Два.</li><li>Три.</li></ul>"
    none = [Bullet(text="Раз."), Bullet(text="Два.")]
    assert richtext.html_from_bullets(none) == "<ul><li>Раз.</li><li>Два.</li></ul>"
    assert richtext.html_from_bullets([]) == ""


def test_название_части_экранируется():
    html = richtext.html_from_bullets([Bullet(text="а", part="<b>итог</b>"), Bullet(text="б", part="риски")])
    assert html.startswith("<h2>&lt;b&gt;итог&lt;/b&gt;</h2>") and "<b>" not in html


def test_ответ_искры_собирается_разделами():
    """Ответ «Искры» с частями → html с заголовками → пункты без заголовков."""
    bullets = [Bullet(text="Коллеги, добрый день.", part="контекст"),
               Bullet(text="Клубы есть в 80 процентах школ.", part="масштаб")]
    html = richtext.html_from_bullets(bullets)
    thesis = Thesis(topic="О клубах")
    thesis.set_html(html)
    assert thesis.headings() == ["Контекст", "Масштаб"]
    assert [b.text for b in thesis.bullets] == ["Коллеги, добрый день.", "Клубы есть в 80 процентах школ."]


# ── белый список и пункты ───────────────────────────────────────────────

def test_clean_пропускает_заголовки_с_уровнем():
    html = ('<h1>Один</h1><h2 class="ql-align-center">Два</h2><h3 onclick="x()">Три <b>жирно</b></h3>'
            '<h4>Четыре</h4><p>Текст</p>')
    assert richtext.clean(html) == ('<h1>Один</h1><h2 class="ql-align-center">Два</h2>'
                                    '<h3>Три <strong>жирно</strong></h3><h3>Четыре</h3><p>Текст</p>')
    assert richtext.clean(richtext.clean(html)) == richtext.clean(html), "повторная чистка ничего не меняет"


def test_заголовки_не_пункты_и_не_слова():
    thesis = Thesis(minutes=1)
    thesis.set_html("<h2>Длинное название раздела из многих слов</h2><ul><li>Один два три.</li></ul>"
                    "<h3>Выводы</h3><p>Четыре пять.</p>")
    assert [b.text for b in thesis.bullets] == ["Один два три.", "Четыре пять."]
    assert thesis.words == 5 and thesis.has_text
    only_heads = Thesis()
    only_heads.set_html("<h2>Новый раздел</h2><p></p>")
    assert only_heads.html == "<h2>Новый раздел</h2>"
    assert only_heads.bullets == [] and not only_heads.has_text, "одни названия — ещё не текст"


def test_пункты_узнаются_и_после_разбивки_на_разделы():
    """Разделили текст заголовками — пункты те же: места под цифру не отвязываются."""
    thesis = Thesis()
    thesis.set_html("<ul><li>Раз.</li><li>Два.</li></ul>")
    ids = [b.id for b in thesis.bullets]
    thesis.set_html("<h2>Контекст</h2><ul><li>Раз.</li></ul><h2>Вывод</h2><ul><li>Два.</li></ul>")
    assert [b.id for b in thesis.bullets] == ids


def test_слова_в_названии_раздела_проверяются():
    thesis = Thesis(topic="О клубах")
    thesis.set_html("<h2>Итоги zadания</h2><ul><li>Клубы работают.</li></ul>")
    assert any(m["word"] == "zadания" for m in thesis.mixed_words()), "в Word оно ушло бы — значит, проверяем"
    thesis.set_html("<h2>Roadmap</h2><ul><li>Клубы работают.</li></ul>")
    assert "Roadmap" in thesis.to_dict()["foreign_words"]


# ── API: сохранение, журнал, «Совещание», Word ──────────────────────────

def test_сохранение_с_разделами_через_api(client, emp):
    tid, rev = _new(client, emp)
    html = ("<p>Коллеги, добрый день.</p><h2>Контекст</h2><ul><li>Клубы есть в 80 % школ.</li></ul>"
            "<h3>Выводы</h3><ul><li>Нужна программа.</li></ul>")
    body = put_html(client, emp, tid, html, rev)
    assert body.status_code == 200
    got = body.json()["thesis"]
    assert got["html"] == html, "заголовки и их уровень сохраняются"
    assert [b["text"] for b in got["bullets"]] == ["Коллеги, добрый день.", "Клубы есть в 80 % школ.",
                                                   "Нужна программа."]
    assert got["words"] == 11, "3 + 6 + 2 слова пунктов; названия разделов не в счёт"
    # переименовали раздел — версия растёт, пункты те же
    renamed = put_html(client, emp, tid, html.replace("<h3>Выводы</h3>", "<h2>Итоги</h2>"), body.json()["rev"])
    assert renamed.status_code == 200
    assert [b["id"] for b in renamed.json()["thesis"]["bullets"]] == [b["id"] for b in got["bullets"]]
    # чужая версия — 409 с текущими тезисами (на этом стоит «ваш текст остался в редакторе»)
    stale = put_html(client, emp, tid, html, rev)
    assert stale.status_code == 409
    assert stale.json()["detail"]["current"]["thesis"]["html"].endswith("<h2>Итоги</h2><ul><li>Нужна программа.</li></ul>")


def test_журнал_называет_разделы(client, emp, monkeypatch):
    from app import main
    notes = []
    monkeypatch.setattr(main, "_save", lambda stored, rev, summary: notes.append(summary) or stored.to_dict())
    tid, rev = _new(client, emp)
    put_html(client, emp, tid, "<ul><li>Раз.</li></ul>", rev)
    put_html(client, emp, tid, "<h2>Контекст</h2><ul><li>Раз.</li></ul>", rev)
    assert notes[-1].endswith("разделы: нет → «Контекст»"), notes[-1]
    assert "изменено оформление" not in notes[-1]


def test_в_совещание_строками_без_названий_разделов(client, emp):
    tid, rev = _new(client, emp)
    html = "<h2>Контекст</h2><ul><li>Коллеги, добрый день.</li></ul><h2>Вывод</h2><ul><li>Нужна программа.</li></ul>"
    put_html(client, emp, tid, html, rev)
    payload = client.get(f"/api/theses/{tid}/moderator-payload", headers=emp).json()["payload"]
    assert payload["text"] == "Коллеги, добрый день.\nНужна программа.", "названия разделов не произносят"
    assert payload["html"] == html, "html — как есть, с разделами"
    thesis = Thesis()
    thesis.set_html(html)
    assert moderator.text_of(thesis) == payload["text"]


def test_заголовки_в_word_полужирные_и_крупнее():
    thesis = Thesis(topic="О клубах")
    thesis.set_html("<h1>Главное</h1><h2>Контекст</h2><ul><li>Клубы есть.</li></ul><h3>Мелкий</h3><p>Текст.</p>")
    data = docx_export.to_bytes(docx_export.build(thesis))
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        body = z.read("word/document.xml").decode("utf-8")
    paras = re.findall(r"<w:p>(.*?)</w:p>|<w:p [^>]*>(.*?)</w:p>", body, re.S)
    paras = [a or b for a, b in paras]

    def para(text):
        return next(p for p in paras if f">{text}<" in p)

    sizes = {name: re.search(r'<w:sz w:val="(\d+)"/>', para(name)).group(1)
             for name in ("Главное", "Контекст", "Мелкий", "Клубы есть.", "Текст.")}
    assert sizes == {"Главное": "40", "Контекст": "36", "Мелкий": "34", "Клубы есть.": "32", "Текст.": "32"}, sizes
    for name in ("Главное", "Контекст", "Мелкий"):
        p = para(name)
        assert "<w:b/>" in p, name + " — полужирный"
        assert "<w:numPr>" not in p, name + " — не пункт списка"
        assert "<w:keepNext/>" in p, name + " — не отрывается от своего текста"
    assert "<w:numPr>" in para("Клубы есть."), "пункт — в списке, как раньше"


# ── ревью RC-2026-10-13-3 ───────────────────────────────────────────────

def test_метка_в_названии_раздела_попадает_в_доуточнить():
    """Н-3: строку с меткой сделали заголовком — место под цифру не закрывается молча:
    красная метка уходит в Word, значит, и в «Что необходимо доуточнить»."""
    from app.models import sync_gaps_from_text
    thesis = Thesis(topic="О клубах")
    thesis.set_html("<ul><li>Охват [нужна цифра: охват на селе] растёт.</li></ul>")
    sync_gaps_from_text(thesis)
    assert [(g.suggestion, g.resolved) for g in thesis.gaps] == [("охват на селе", False)]
    thesis.set_html("<h2>Охват [нужна цифра: охват на селе]</h2><ul><li>Клубы работают.</li></ul>")
    sync_gaps_from_text(thesis)
    open_ = [g for g in thesis.gaps if not g.resolved]
    assert [(g.marker, g.suggestion) for g in open_] == [("нужна цифра", "охват на селе")], thesis.gaps
    assert open_[0].bullet_id.startswith("head:")
    # переставили разделы — место то же, не заводится заново
    thesis.set_html("<h2>Итоги</h2><p>Текст.</p><h2>Охват [нужна цифра: охват на селе]</h2><ul><li>Клубы работают.</li></ul>")
    sync_gaps_from_text(thesis)
    assert [g.id for g in thesis.gaps if not g.resolved] == [open_[0].id]
    # метку убрали из названия — место закрыто
    thesis.set_html("<h2>Охват</h2><ul><li>Клубы работают.</li></ul>")
    sync_gaps_from_text(thesis)
    assert not [g for g in thesis.gaps if not g.resolved]


def test_метка_в_названии_через_api(client, emp):
    tid, rev = _new(client, emp)
    got = put_html(client, emp, tid, "<h2>Охват [нужна цифра: доля школ]</h2><ul><li>Клубы работают.</li></ul>", rev).json()
    assert [(g["suggestion"], g["resolved"]) for g in got["thesis"]["gaps"]] == [("доля школ", False)]


def test_журнал_о_разделах_не_разрастается(client, emp, monkeypatch):
    """Н-6: при 20 разделах — первые пять и «и ещё N»."""
    from app import main
    notes = []
    monkeypatch.setattr(main, "_save", lambda stored, rev, summary: notes.append(summary) or stored.to_dict())
    tid, rev = _new(client, emp)
    html = "".join(f"<h2>Раздел номер {i}</h2><p>Текст {i}.</p>" for i in range(20))
    put_html(client, emp, tid, html, rev)
    note = notes[-1]
    assert "разделы: нет → «Раздел номер 0», «Раздел номер 1», «Раздел номер 2», «Раздел номер 3», " \
           "«Раздел номер 4» и ещё 15" in note, note
    assert "Раздел номер 5" not in note
    assert len(note) < 400
