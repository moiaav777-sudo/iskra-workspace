"""Вкладка «Результаты»: документы из «Транскрибатора» (решение пользователя 02.10).

`POST /api/meetings/{id}/results` — транскрипция, саммари, проект протокольных решений; повтор с тем же
`source.service + source.id` и `kind` заменяет документ; `DELETE …/results/{rid}`; ссылки соседа —
только `/transcriber/…`; html чистится на сервере; готовность — пункты секции `results` с весом 0.
"""
from __future__ import annotations

import json

import pytest

from app import deps, readiness, storage
from app.models import Meeting

from .test_api import audit, client, create, login, people  # noqa: F401  (фикстуры)

REC = "17"


def doc(kind="transcript", rec=REC, **kw):
    base = {
        "kind": kind,
        "title": {"transcript": "Транскрипция совещания 25.05.2026", "summary": "Саммари совещания 25.05.2026",
                  "decisions": "Перечень поручений 25.05.2026"}[kind],
        "html": "<p>[00:00:05] <b>Иванов:</b> Добрый день.</p><p>[00:01:10] Второй абзац.</p>",
        "text": "[00:00:05] Иванов: Добрый день.\n\n[00:01:10] Второй абзац.",
        "docx_url": f"/transcriber/api/records/{rec}/export/{kind}.docx",
        "source": {"service": "transcriber", "id": rec, "url": f"/transcriber/#record={rec}"},
    }
    base.update(kw)
    return base


def post(client, mid, body, **kw):
    return client.post(f"/api/meetings/{mid}/results", json=body, **kw)


def results(client, mid):
    return client.get(f"/api/meetings/{mid}").json()["meeting"]["results"]


def test_three_kinds_are_stored_and_returned(client, audit):
    login(client, "alice")
    mid = create(client)
    for kind in ("transcript", "summary", "decisions"):
        r = post(client, mid, doc(kind))
        assert r.status_code == 200, r.text
        assert r.json()["stored"] is True
        assert r.json()["meeting"]["id"] == mid                  # в ответе — совещание, как у других маршрутов
    got = results(client, mid)
    assert [x["kind"] for x in got] == ["transcript", "summary", "decisions"]
    t = got[0]
    assert t["title"] == "Транскрипция совещания 25.05.2026"
    assert t["docx_url"] == f"/transcriber/api/records/{REC}/export/transcript.docx"
    assert t["source"] == {"service": "transcriber", "id": REC, "url": f"/transcriber/#record={REC}"}
    assert t["id"] and t["created"] and t["created"] == t["updated"]
    assert "<b>Иванов:</b>" in t["html"] and t["text"].startswith("[00:00:05]")
    # журнал портала: своё действие и «откуда что получено»; история совещания — общим способом
    acts = [e for e in audit if e["action"] == "Получен документ во вкладку «Результаты» (Транскрибатор)"]
    assert len(acts) == 3
    assert "из «Транскрибатора» получена транскрипция: «Транскрипция совещания 25.05.2026»" in acts[0]["details"]
    assert "из «Транскрибатора» получено саммари" in acts[1]["details"]
    assert "из «Транскрибатора» получен проект протокольных решений" in acts[2]["details"]
    assert "результаты: 0→1" in acts[0]["details"]
    hist = client.get(f"/api/meetings/{mid}/history").json()["history"]
    assert any("результаты: 2→3" in h["summary"] for h in hist)


def test_repeat_with_same_source_and_kind_replaces(client):
    login(client, "alice")
    mid = create(client)
    post(client, mid, doc("transcript"))
    post(client, mid, doc("summary"))
    first = results(client, mid)[0]
    r = post(client, mid, doc("transcript", title="Транскрипция, правка", html="<p>Новая редакция</p>", text=""))
    assert r.status_code == 200
    got = results(client, mid)
    assert [x["kind"] for x in got] == ["transcript", "summary"]          # не дубль, а замена
    t = got[0]
    assert t["id"] == first["id"] and t["created"] == first["created"]    # место и дата первой присылки
    assert t["title"] == "Транскрипция, правка" and t["html"] == "<p>Новая редакция</p>"
    assert t["text"] == "Новая редакция"                                  # текста нет — из html
    # другая запись «Транскрибатора» — другой документ, ложится рядом
    post(client, mid, doc("transcript", rec="18"))
    assert [(x["kind"], x["source"]["id"]) for x in results(client, mid)] == \
        [("transcript", REC), ("summary", REC), ("transcript", "18")]


def test_delete_result(client, audit):
    login(client, "alice")
    mid = create(client)
    post(client, mid, doc("transcript"))
    post(client, mid, doc("decisions"))
    rid = results(client, mid)[1]["id"]
    r = client.delete(f"/api/meetings/{mid}/results/{rid}")
    assert r.status_code == 200, r.text
    assert [x["kind"] for x in r.json()["meeting"]["results"]] == ["transcript"]
    assert [x["kind"] for x in results(client, mid)] == ["transcript"]
    ev = [e for e in audit if e["action"] == "Убран документ из вкладки «Результаты»"]
    assert len(ev) == 1 and "убран документ «Перечень поручений 25.05.2026» (проект протокольных решений)" in ev[0]["details"]
    # второй раз (двойное нажатие, коллега успел) — 409 «уже изменили», а не 500
    assert client.delete(f"/api/meetings/{mid}/results/{rid}").status_code == 409


@pytest.mark.parametrize("field", ["docx_url", "source.url"])
@pytest.mark.parametrize("url", [
    "https://evil.example/x.docx", "//evil.example/transcriber/x", "http:/transcriber/x", "javascript:alert(1)",
    "/materials/api/x.docx", "transcriber/api/x.docx", "/transcriber/../api/x", "/transcriber/api/..",
    "/transcriber/%2e%2e/api", "/transcriber/a\\b", "/transcriber/a b", "/transcriberX/a",
])
def test_neighbour_links_only_relative_under_transcriber(client, field, url):
    login(client, "alice")
    mid = create(client)
    body = doc("summary")
    if field == "docx_url":
        body["docx_url"] = url
    else:
        body["source"]["url"] = url
    r = post(client, mid, body)
    assert r.status_code == 400, (url, r.text)
    assert "/transcriber/" in r.json()["detail"]
    assert results(client, mid) == []


def test_unknown_service_or_missing_record_rejected(client):
    login(client, "alice")
    mid = create(client)
    body = doc("summary")
    body["source"]["service"] = "brief"
    assert post(client, mid, body).status_code == 400
    body = doc("summary")
    body["source"]["id"] = ""
    assert post(client, mid, body).status_code == 400
    body = doc("summary")
    del body["source"]
    assert post(client, mid, body).status_code == 422
    assert results(client, mid) == []


@pytest.mark.parametrize("kind", ["protocol", "", "TRANSCRIPT", None])
def test_kind_outside_list_rejected(client, kind):
    login(client, "alice")
    mid = create(client)
    r = post(client, mid, dict(doc("transcript"), kind=kind))
    assert r.status_code == 422
    assert results(client, mid) == []


@pytest.mark.parametrize("field", ["html", "text"])
def test_too_big_document_413(client, field):
    login(client, "alice")
    mid = create(client)
    big = "а" * (deps.MAX_RESULT_CHARS + 1)
    r = post(client, mid, doc("transcript", **{field: big}))
    assert r.status_code == 413
    assert "слишком большой" in r.json()["detail"] and "1 000 000" in r.json()["detail"]
    assert results(client, mid) == []
    # на пределе — принимается
    assert post(client, mid, doc("transcript", **{field: "а" * deps.MAX_RESULT_CHARS})).status_code == 200


def test_html_is_cleaned_on_server(client):
    login(client, "alice")
    mid = create(client)
    dirty = ('<p onclick="steal()" style="color:red">Начало <span class="tc">[00:01]</span>'
             '<script>alert(1)</script><style>body{display:none}</style></p>'
             '<img src=x onerror="alert(2)"><a href="javascript:alert(3)">ссылка</a>'
             '<iframe src="https://evil.example">внутри</iframe><!-- комментарий -->'
             '<div>Абзац &lt;b&gt;</div><ul><li>один<li>два</ul><h1>Заголовок</h1>')
    post(client, mid, doc("summary", html=dirty, text="Начало"))
    html = results(client, mid)[0]["html"]
    for bad in ("<script", "alert(1)", "<style", "display:none", "onclick", "style=", "<img", "onerror",
                "javascript:", "<a ", "<iframe", "evil.example", "внутри", "<!--", "<div"):
        assert bad not in html, bad
    assert '<span class="tc">[00:01]</span>' in html                      # класс таймкода остаётся
    assert "<p>Абзац &lt;b&gt;</p>" in html and "ссылка" in html
    assert "<ul><li>один</li><li>два</li></ul>" in html and "<h2>Заголовок</h2>" in html


def test_cleaner_unit():
    from app import richtext
    assert richtext.clean_html("<script/>после") == "после"                # самозакрытый не съедает остальное
    assert richtext.clean_html('<p class="a b&quot;x" id="z">т</p>') == '<p class="a">т</p>'
    assert richtext.clean_html("<p>не закрыт <i>курсив") == "<p>не закрыт <i>курсив</i></p>"
    assert richtext.html_text("<p>а</p><p>б<br>в</p>") == "а\nб\nв"


def test_empty_document_is_not_an_error_and_keeps_previous(client):
    login(client, "alice")
    mid = create(client)
    post(client, mid, doc("summary"))
    # «Транскрибатор» шлёт все три вида, даже если саммари ещё нет: 200, но пустым не затираем
    r = post(client, mid, doc("summary", html="", text="  "))
    assert r.status_code == 200 and r.json()["stored"] is False
    assert results(client, mid)[0]["html"].startswith("<p>[00:00:05]")
    r = post(client, mid, doc("decisions", html="", text=""))
    assert r.status_code == 200 and [x["kind"] for x in results(client, mid)] == ["summary"]
    # пустая разметка — тоже пустой документ (ревьюер RC-2026-10-02-18, Н-4)
    r = post(client, mid, doc("decisions", html="<p>   </p><p><br></p>", text=" "))
    assert r.status_code == 200 and r.json()["stored"] is False
    assert [x["kind"] for x in results(client, mid)] == ["summary"]


def test_readiness_results_section(client):
    login(client, "alice")
    mid = create(client)
    rd = client.get(f"/api/meetings/{mid}").json()["readiness"]
    items = [i for i in rd["items"] if i["section"] == "results"]
    assert [i["id"] for i in items] == ["results_transcript", "results_summary", "results_decisions"]
    assert not any(i["done"] for i in items) and all(i["weight"] == 0 for i in items)
    before = rd["percent"]
    r = post(client, mid, doc("transcript"))
    rd = r.json()["readiness"]
    done = {i["id"]: i["done"] for i in rd["items"] if i["section"] == "results"}
    assert done == {"results_transcript": True, "results_summary": False, "results_decisions": False}
    # документы после совещания не двигают готовность модератора к совещанию
    assert rd["percent"] == before


def test_old_meeting_without_results_and_newer_fields(client):
    login(client, "alice")
    mid = create(client)
    post(client, mid, doc("summary"))
    conn = storage._conn()
    try:
        data = json.loads(conn.execute("SELECT data FROM meetings WHERE id = ?", (mid,)).fetchone()["data"])
        old = {k: v for k, v in data.items() if k != "results"}           # запись до 02.10
        conn.execute("UPDATE meetings SET data = ? WHERE id = ?", (json.dumps(old, ensure_ascii=False), mid))
        conn.commit()
    finally:
        conn.close()
    r = client.get(f"/api/meetings/{mid}")
    assert r.status_code == 200 and r.json()["meeting"]["results"] == []
    assert post(client, mid, doc("transcript")).status_code == 200
    # запись версии новее (после отката): незнакомое поле документа пропускается, мусор в списке — тоже
    raw = dict(data, results=[dict(data["results"][0], pinned=True, reviewer="x"), "мусор"])
    m = Meeting.from_dict(raw)
    assert len(m.results) == 1 and m.results[0].kind == "summary"
    assert not hasattr(m.results[0], "pinned")
    raw = dict(data, results=[{k: v for k, v in data["results"][0].items() if k != "id"}])
    m = Meeting.from_dict(raw)
    assert m.results[0].id and m.ids_assigned                           # без id — выдаётся и записывается


def test_rights_and_origin_like_materials(client):
    login(client, "alice")
    mid = create(client)
    r = post(client, mid, doc("summary"), headers={"Origin": "https://evil.example"})
    assert r.status_code == 403
    r = post(client, mid, doc("summary"), headers={"Origin": "http://testserver"})   # тот же сайт за порталом
    assert r.status_code == 200
    rid = results(client, mid)[0]["id"]
    assert client.delete(f"/api/meetings/{mid}/results/{rid}", headers={"Origin": "https://evil.example"}).status_code == 403
    assert post(client, "нет-такого", doc("summary")).status_code == 404
    client.headers.pop("X-Portal-Secret")
    assert post(client, mid, doc("summary")).status_code == 401


def test_me_gives_transcriber_address(client):
    login(client, "alice")
    me = client.get("/api/me").json()
    assert me["transcriber"]["home"] == "/transcriber/"


def test_readiness_function_on_plain_meeting():
    rd = readiness.readiness(Meeting(id="x", title="т"))
    assert sum(1 for i in rd["items"] if i["section"] == "results") == 3
