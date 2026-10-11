"""Задачи пользователя 22.09: редактор с оформлением, без Markdown, метки «нужна цифра»,
одно предложение — один пункт, слова вперемешку исправляются при приходе текста,
«Искра» предлагает редакцию для заполненных тезисов."""
from __future__ import annotations

import io
import json
import time
import zipfile

from app import latin, richtext
from tests.conftest import put_html, put_lines

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


# ── разметка ─────────────────────────────────────────────────────────────

def test_чужая_разметка_не_проходит():
    html = ('<ul><li onclick="x()">Раз <script>alert(1)</script><b style="color:red;font-size:99px">'
            'два</b> <a href="javascript:evil()">три</a> <a href="https://a.ru">ок</a></li></ul>'
            '<img src=x onerror=alert(1)><p>Абзац</p>')
    clean = richtext.clean(html)
    assert "script" not in clean and "onclick" not in clean and "onerror" not in clean
    assert "javascript:" not in clean and "font-size" not in clean
    # цвет — только #rrggbb или rgb(): «red» из чужой разметки не берётся
    assert clean == ('<ul><li>Раз <strong>два</strong> три '
                     '<a href="https://a.ru" target="_blank" rel="noopener">ок</a></li></ul><p>Абзац</p>')
    assert richtext.clean(clean) == clean, "повторная чистка ничего не меняет"


def test_списки_quill_и_цвет_переживают_круг():
    quill = ('<ol><li data-list="bullet"><span class="ql-ui"></span>Пункт <em><span style="color: rgb(0, 112, 192);">'
             'справочно</span></em></li><li data-list="bullet" class="ql-indent-1">Вложенный</li>'
             '<li data-list="ordered">Первый</li></ol>')
    clean = richtext.clean(quill)
    assert clean == ('<ul><li>Пункт <em><span style="color:#0070c0">справочно</span></em></li>'
                     '<li class="ql-indent-1">Вложенный</li></ul><ol><li>Первый</li></ol>')
    assert richtext.lines(quill) == ["Пункт справочно", "Вложенный", "Первый"]


def test_markdown_становится_оформлением():
    assert richtext.markdown_inline("Это **747 тысяч** и *рост*, а 2*3 и `код`") == \
        "Это <strong>747 тысяч</strong> и <em>рост</em>, а 2*3 и код"
    assert richtext.strip_markdown("## Заголовок **жирный**") == "Заголовок жирный"


def test_предложения_режутся_без_порчи_сокращений():
    text = ("Д.Н. Чернышенко поручил. Бюджет 5 млн. руб., т.е. вырос на 2,5 %. В 2026 г. охват "
            "[нужна цифра: доля, шт. в 2026] сохранится! И.О. директора сказал: «Да». Конец.")
    assert richtext.sentences(text) == [
        "Д.Н. Чернышенко поручил.",
        "Бюджет 5 млн. руб., т.е. вырос на 2,5 %.",
        "В 2026 г. охват [нужна цифра: доля, шт. в 2026] сохранится!",
        "И.О. директора сказал: «Да».",
        "Конец.",
    ]


# ── сохранение из редактора ──────────────────────────────────────────────

def _new(client, emp, **kw):
    payload = {"topic": "О тестировании", "minutes": 5}
    payload.update(kw)
    return client.post("/api/theses", json=payload, headers=emp).json()


def test_редактор_шлёт_html_и_получает_его_же(client, emp):
    created = _new(client, emp)
    tid, rev = created["thesis"]["id"], created["rev"]
    body = put_html(client, emp, tid, '<ul><li>Раз <strong>два</strong></li><li><em>три</em></li></ul>'
                    '<p><br></p><script>x()</script>', rev).json()
    thesis = body["thesis"]
    assert thesis["html"] == '<ul><li>Раз <strong>два</strong></li><li><em>три</em></li></ul>'
    assert [b["text"] for b in thesis["bullets"]] == ["Раз два", "три"]
    assert thesis["bullets"][1]["emphasis"] == "aside", "весь пункт курсивом — справочное"
    # пункт узнаётся: id сохраняется при правке соседа и при перестановке
    ids = [b["id"] for b in thesis["bullets"]]
    moved = put_html(client, emp, tid, '<ul><li><em>три</em></li><li>Раз <strong>два</strong>, поправлено</li></ul>',
                     body["rev"]).json()["thesis"]["bullets"]
    assert [b["id"] for b in moved] == [ids[1], ids[0]]


def test_оформление_один_в_один_в_word(client, emp):
    created = _new(client, emp)
    tid, rev = created["thesis"]["id"], created["rev"]
    put_html(client, emp, tid, '<ul><li>Обеспечено <strong>747 тысяч</strong> — <em>рост</em> '
             '<u>под</u> <span style="background-color:#ffff00">залито</span>.</li></ul>'
             '<p class="ql-align-center">Абзац [нужна цифра: доля семей].</p>', rev)
    data = client.get(f"/api/theses/{tid}/export/theses.docx", headers=emp).content
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        body = z.read("word/document.xml").decode("utf-8")
    assert "**" not in body
    import re
    runs = re.findall(r"<w:r>(.*?)</w:r>", body, re.S)
    def run_of(text):
        return next(r for r in runs if f">{text}<" in r)
    assert "<w:b/>" in run_of("747 тысяч") and "<w:i/>" not in run_of("747 тысяч")
    assert "<w:i/>" in run_of("рост")
    assert "<w:u " in run_of("под")
    assert 'w:highlight w:val="yellow"' in run_of("залито")
    marker = run_of("[нужна цифра: доля семей]")
    assert "<w:b/>" in marker and 'w:val="C00000"' in marker and 'w:highlight w:val="yellow"' in marker
    assert '<w:jc w:val="center"/>' in body


def test_старый_текст_с_markdown_и_прежними_метками_читается_оформлением(client, emp):
    """Записанное до редактора: пункты с выделением, в тексте ** и [ТРЕБУЕТСЯ …]."""
    from app import storage
    from app.models import Bullet, Thesis
    thesis = Thesis(topic="Старые", bullets=[
        Bullet(text="Это **747 тысяч** человек. Второе предложение.", emphasis="normal"),
        Bullet(text="Охват ниже [ТРЕБУЕТСЯ ФАКТУРА: доля семей].")])
    stored = storage.create(thesis)
    raw = json.loads(storage._conn().execute("SELECT data FROM theses WHERE id = ?",
                                              (thesis.id,)).fetchone()["data"])
    assert raw["html"], "с 22.09 текст хранится оформлением"
    got = client.get(f"/api/theses/{stored.thesis.id}", headers=emp).json()["thesis"]
    assert "**" not in got["html"] and "<strong>747 тысяч</strong>" in got["html"]
    assert "[нужна цифра: доля семей]" in got["bullets"][1]["text"]
    assert 'color:#c00000">[нужна цифра: доля семей]' in got["html"]
    assert got["gaps"][0]["marker"] == "нужна цифра"


def test_записи_до_редактора_переводятся_при_запуске(client, emp):
    """В базе — запись без html (старый код). Запуск переводит её и записывает."""
    import datetime as dt
    from app import storage
    from app.models import Thesis
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    data = {"id": "oldrec000001", "topic": "Старая", "minutes": 5,
            "bullets": [{"id": "b1", "text": "Это **важно**. [ТРЕБУЕТСЯ СРОК]", "emphasis": "normal"}]}
    conn = storage._conn()
    conn.execute("INSERT INTO theses (id, data, rev, archived, created_at, created_by, updated_at,"
                 " updated_by) VALUES (?,?,?,?,?,?,?,?)",
                 ("oldrec000001", json.dumps(data, ensure_ascii=False), 1, 0, now, "emp", now, "emp"))
    conn.commit(); conn.close()
    assert storage.heal_mixed(lambda t: set()) == 1
    got = client.get("/api/theses/oldrec000001", headers=emp).json()
    assert got["rev"] == 2 and "<strong>важно</strong>" in got["thesis"]["html"]
    assert got["thesis"]["gaps"][0]["marker"] == "нужен срок"
    history = client.get("/api/theses/oldrec000001/history", headers=emp).json()["history"]
    assert any("переведён в редактор" in h.get("summary", "") for h in history)


# ── слова вперемешку: однозначное исправляется само ──────────────────────

def test_транслит_исправляется_если_слово_есть_у_человека():
    words = latin.corpus("Треть участников не приступают к заданию. Задания ГИА.")
    assert latin.fix_mixed("к zadанию вовсе", words) == ("к заданию вовсе", [])
    # слова у человека нет — остаётся вопросом
    assert latin.fix_mixed("к zadанию вовсе", latin.corpus("о спорте")) == ("к zadанию вовсе", ["zadанию"])


def test_при_сохранении_слово_правится_по_справке(client, emp):
    created = _new(client, emp)
    tid, rev = created["thesis"]["id"], created["rev"]
    files = {"file": ("справка.txt", "К заданию не приступают.".encode("utf-8"), "text/plain")}
    rev = client.post(f"/api/theses/{tid}/sources/file?rev={rev}", headers=emp, files=files).json()["rev"]
    body = put_html(client, emp, tid, "<ul><li>Треть не приступают к zadанию.</li></ul>", rev).json()
    assert body["thesis"]["bullets"][0]["text"] == "Треть не приступают к заданию."
    assert body["thesis"]["mixed_words"] == []


def test_старые_тексты_лечатся_при_запуске(client, emp):
    """Слово из прогона «Искры» до исправлений: при старте — исправлено, запись в истории."""
    from app import storage
    from app.models import Bullet, Source, Thesis
    thesis = Thesis(topic="Старая", bullets=[Bullet(text="К zadанию не приступают.")],
                    sources=[Source(name="справка.txt", kind="txt", text="Задание части 2.")])
    stored = storage.create(thesis)
    fixed = storage.heal_mixed(lambda t: latin.corpus(*(s.text for s in t.sources)))
    assert fixed == 1
    got = client.get(f"/api/theses/{stored.thesis.id}", headers=emp).json()
    assert got["thesis"]["bullets"][0]["text"] == "К заданию не приступают."
    assert got["thesis"]["mixed_words"] == [] and got["rev"] == stored.rev + 1
    history = client.get(f"/api/theses/{stored.thesis.id}/history", headers=emp).json()["history"]
    assert any("zadанию → заданию" in h.get("summary", "") for h in history)


# ── «Искра»: предложения, метки, Markdown, редакция к принятию ───────────

ANSWER = {"bullets": [{"text": "Клубы созданы в **80 процентах** школ. За год их стало больше, т.е. рост.",
                       "emphasis": "normal"},
                      {"text": "Охват ниже [ТРЕБУЕТСЯ ФАКТУРА: охват на селе]."}],
          "gaps": [{"marker": "ТРЕБУЕТСЯ ФАКТУРА", "suggestion": "охват на селе"}]}


def _fake(monkeypatch, answer):
    from app import iskra
    monkeypatch.setattr(iskra, "_call", lambda system, user: json.dumps(answer, ensure_ascii=False))
    monkeypatch.setattr(iskra, "key_ready", lambda: True)


def _wait(client, emp, job):
    deadline = time.time() + 10
    while job["active"] and time.time() < deadline:
        time.sleep(0.05)
        job = client.get(f"/api/jobs/{job['id']}", headers=emp).json()["job"]
    return job


def test_ответ_искры_предложение_на_пункт_без_markdown_с_новыми_метками(client, emp, monkeypatch):
    _fake(monkeypatch, ANSWER)
    created = _new(client, emp)
    tid, rev = created["thesis"]["id"], created["rev"]
    job = _wait(client, emp, client.post(f"/api/theses/{tid}/write", json={"rev": rev},
                                         headers=emp).json()["job"])
    assert job["state"] == "done"
    thesis = client.get(f"/api/theses/{tid}", headers=emp).json()["thesis"]
    assert [b["text"] for b in thesis["bullets"]] == [
        "Клубы созданы в 80 процентах школ.", "За год их стало больше, т.е. рост.",
        "Охват ниже [нужна цифра: охват на селе]."]
    assert "**" not in thesis["html"] and "<strong>80 процентах</strong>" in thesis["html"]
    assert thesis["bullets"][0]["emphasis"] == "normal", "жирное слово — не весь пункт акцентом"
    assert [g["marker"] for g in thesis["gaps"] if not g["resolved"]] == ["нужна цифра"], "без дублей"


def test_в_задании_искре_правило_предложение_на_пункт_и_новые_метки(client, emp, monkeypatch):
    from app import iskra
    asked = {}
    def call(system, user):
        asked["system"] = system
        return json.dumps(ANSWER, ensure_ascii=False)
    monkeypatch.setattr(iskra, "_call", call)
    monkeypatch.setattr(iskra, "key_ready", lambda: True)
    created = _new(client, emp)
    _wait(client, emp, client.post(f"/api/theses/{created['thesis']['id']}/write",
                                   json={"rev": created["rev"]}, headers=emp).json()["job"])
    system = asked["system"]
    assert "Каждое предложение — отдельный пункт" in system
    assert "Никаких знаков разметки" in system
    assert "нужна цифра" in system


def test_заполненные_тезисы_искра_не_трогает_а_предлагает_редакцию(client, emp, monkeypatch):
    _fake(monkeypatch, ANSWER)
    created = _new(client, emp)
    tid, rev = created["thesis"]["id"], created["rev"]
    rev = put_lines(client, emp, tid, ["Мой текст."], rev).json()["rev"]
    r = client.post(f"/api/theses/{tid}/write", json={"rev": rev, "propose": True}, headers=emp)
    assert r.status_code == 202
    job = _wait(client, emp, r.json()["job"])
    assert job["state"] == "proposed" and job["has_draft"] and not job["active"]
    got = client.get(f"/api/theses/{tid}", headers=emp).json()
    assert [b["text"] for b in got["thesis"]["bullets"]] == ["Мой текст."], "текст на месте"
    draft = client.get(f"/api/jobs/{job['id']}/draft", headers=emp).json()
    assert "<strong>80 процентах</strong>" in draft["html"]
    # взять — по версии; устаревшая версия не проходит
    assert client.post(f"/api/jobs/{job['id']}/apply", json={"rev": rev - 1}, headers=emp).status_code == 409
    taken = client.post(f"/api/jobs/{job['id']}/apply", json={"rev": got["rev"]}, headers=emp).json()
    assert taken["thesis"]["bullets"][0]["text"] == "Клубы созданы в 80 процентах школ."


def test_редакция_к_принятию_одно_событие_журнала_с_токенами(client, emp, monkeypatch):
    from app import iskra, portal
    seen = []
    monkeypatch.setattr(portal, "send_audit", lambda **e: seen.append(e))
    def call(system, user):
        iskra.spend(700, 200)
        return json.dumps(ANSWER, ensure_ascii=False)
    monkeypatch.setattr(iskra, "_call", call)
    monkeypatch.setattr(iskra, "key_ready", lambda: True)
    created = _new(client, emp)
    tid, rev = created["thesis"]["id"], created["rev"]
    rev = put_lines(client, emp, tid, ["Мой текст."], rev).json()["rev"]
    _wait(client, emp, client.post(f"/api/theses/{tid}/write", json={"rev": rev, "propose": True},
                                   headers=emp).json()["job"])
    events = [e for e in seen if "«Искрой»" in e["action"]]
    assert len(events) == 1 and events[0]["tokens_in"] == 700 and events[0]["tokens_out"] == 200
    assert "вариант" in events[0]["action"]



# ── ревьюер RC-2026-09-22-2 ──────────────────────────────────────────────

def test_rc22_перевод_при_старте_шлёт_событие_в_журнал(client, emp, monkeypatch):
    import datetime as dt
    from app import portal, storage
    seen = []
    monkeypatch.setattr(portal, "send_audit", lambda **e: seen.append(e))
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    conn = storage._conn()
    for tid, text in (("oldrec000002", "Просто **текст**."), ("oldrec000003", "К zadанию.")):
        data = {"id": tid, "topic": "Старая", "minutes": 5,
                "bullets": [{"id": "b1", "text": text, "emphasis": "normal"}],
                "sources": [{"id": "s1", "name": "с.txt", "kind": "txt", "text": "задание", "added_at": now}]}
        conn.execute("INSERT INTO theses (id, data, rev, archived, created_at, created_by, updated_at,"
                     " updated_by) VALUES (?,?,?,?,?,?,?,?)",
                     (tid, json.dumps(data, ensure_ascii=False), 1, 0, now, "emp", now, "emp"))
    conn.commit(); conn.close()
    storage.heal_mixed(lambda t: latin.corpus(*(s.text for s in t.sources)))
    actions = sorted(e["action"] for e in seen)
    assert actions == ["Исправлены слова наполовину латиницей", "Тезисы переведены в редактор с оформлением"]
    assert all(e["tokens_in"] == 0 and e["tokens_out"] == 0 for e in seen)


def test_rc22_белый_текст_и_чужие_цвета_не_проходят():
    html = ('<p><span style="color:#ffffff">скрытое</span> <span style="color:#e60000">красное</span> '
            '<span style="background-color:#000000">тёмная заливка</span> '
            '<span style="background-color:#ffff00">жёлтая</span> <span style="color:#123456">чужой</span></p>')
    assert richtext.clean(html) == ('<p>скрытое <span style="color:#e60000">красное</span> тёмная заливка '
                                    '<span style="background-color:#ffff00">жёлтая</span> чужой</p>')


def test_rc22_подчёркивания_в_имени_не_жирный():
    assert richtext.markdown_inline("метод __init__ и __жирный__") == "метод __init__ и <strong>жирный</strong>"
    assert richtext.sentences("См. рис. 3. Второе предложение.") == ["См. рис. 3.", "Второе предложение."]
    assert richtext.sentences("В п. 3. Далее.") == ["В п. 3. Далее."]


def test_rc22_число_и_размер_документов_ограничены(client, emp, monkeypatch):
    from app import main
    monkeypatch.setattr(main, "MAX_SOURCE_BYTES", 100)
    monkeypatch.setattr(main, "MAX_SOURCES", 1)
    created = _new(client, emp)
    tid, rev = created["thesis"]["id"], created["rev"]
    big = {"file": ("б.txt", b"x" * 101, "text/plain")}
    assert client.post(f"/api/theses/{tid}/sources/file?rev={rev}", headers=emp, files=big).status_code == 413
    ok = {"file": ("а.txt", "Справка о ГИА, коротко.".encode("utf-8"), "text/plain")}
    rev = client.post(f"/api/theses/{tid}/sources/file?rev={rev}", headers=emp, files=ok).json()["rev"]
    assert client.post(f"/api/theses/{tid}/sources/file?rev={rev}", headers=emp, files=ok).status_code == 400



# ── ревьюер RC-2026-09-22-3 ──────────────────────────────────────────────

def test_rc23_цвет_вровень_с_заливкой_снимается():
    html = ('<p><span style="background-color:#66b966"><span style="color:#66b966">невидимое</span></span> '
            '<span style="color:#000000;background-color:#ffff00">видимое</span></p>')
    assert richtext.clean(html) == ('<p><span style="background-color:#66b966">невидимое</span> '
                                    '<span style="color:#000000;background-color:#ffff00">видимое</span></p>')


def test_rc23_не_больше_двух_документов(client, emp):
    """Решение пользователя 22.09: к тезисам прикладывают один-два документа."""
    from app import main
    assert main.MAX_SOURCES == 2
    created = _new(client, emp)
    tid, rev = created["thesis"]["id"], created["rev"]
    ok = {"file": ("а.txt", "Справка о ГИА, коротко.".encode("utf-8"), "text/plain")}
    for _ in range(2):
        rev = client.post(f"/api/theses/{tid}/sources/file?rev={rev}", headers=emp, files=ok).json()["rev"]
    r = client.post(f"/api/theses/{tid}/sources/file?rev={rev}", headers=emp, files=ok)
    assert r.status_code == 400 and "не больше 2" in r.json()["detail"]
