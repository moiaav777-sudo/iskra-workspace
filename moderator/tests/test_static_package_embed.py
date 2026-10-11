"""Вкладка «Пакет» — папка совещания из самих «Материалов», материалы к вопросам — в пакет (02.10, вечер).

Решение пользователя: «все загруженные материалы в совещании должны попадать в пакет; менять документы
местами, переименовывать и собирать реестр — та же логика, что в разделе «Материалы», один в один».
Решение Оркестратора: вкладка показывает их карточку папки в iframe (`/materials/embed/{id}`), а исходный
файл материала после сохранения уходит в папку с названием из разбора и номером вопроса.

Интерфейс — в браузерном `static/app.js`, поэтому через node: `js_package_embed_check.js` грузит настоящий
файл в песочницу (рамка, сообщения `materials:*`, `confirmMaterial`/`saveHtmlMaterial` → «Материалы»).
Здесь же — сервер: адрес встраивания в `/api/me` и пометка `package` у материала (PATCH).
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from .test_api import audit, client, create, login, people  # noqa: F401  (фикстуры)

HERE = Path(__file__).resolve().parent
APP_JS = HERE.parent / "static" / "app.js"
CHECK_JS = HERE / "js_package_embed_check.js"


@pytest.mark.skipif(shutil.which("node") is None, reason="node не найден в PATH — фронтенд-тест пропущен")
def test_package_tab_embeds_materials_and_materials_go_to_package():
    result = subprocess.run(
        ["node", str(CHECK_JS), str(APP_JS)],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, (
        "вкладка «Пакет» (рамка «Материалов») или отправка материала в пакет не прошли проверку:\n"
        + result.stdout + result.stderr
    )
    assert "OK" in result.stdout


def test_me_gives_embed_address(client):
    login(client, "alice")
    links = client.get("/api/me").json()["materials"]
    # адрес встраивания отдаёт сервер — в странице абсолютных адресов нет
    assert links["embed"] == "/materials/embed/{id}"


def test_new_material_id_in_response_not_last_in_list(client):
    """Пометку «в пакете» интерфейс ставит материалу из `material_id` ответа, а не последнему в списке
    (ревьюер RC-2026-10-02-28, Н-5): id отдают и `POST /materials`, и `apply`."""
    login(client, "alice")
    mid = create(client)
    first = client.post(f"/api/meetings/{mid}/materials",
                        json={"question_num": "1", "type": "Справка", "title": "Первая", "text": "т", "filename": "1.docx"})
    second = client.post(f"/api/meetings/{mid}/materials",
                         json={"question_num": "1", "type": "Справка", "title": "Вторая", "text": "т", "filename": "2.docx"})
    assert first.status_code == 200 and second.status_code == 200
    ids = {x["title"]: x["id"] for x in second.json()["meeting"]["materials"]}
    assert first.json()["material_id"] == ids["Первая"]
    assert second.json()["material_id"] == ids["Вторая"]
    assert first.json()["material_id"] != second.json()["material_id"]


def test_material_remembers_package_name(client):
    login(client, "alice")
    mid = create(client)
    r = client.post(f"/api/meetings/{mid}/materials",
                    json={"question_num": "1", "type": "Справка", "title": "Связь", "text": "текст",
                          "filename": "Справка.docx"})
    assert r.status_code == 200, r.text
    mat = r.json()["meeting"]["materials"][-1]
    assert r.json()["material_id"] == mat["id"]       # id нового материала — в ответе (RC-28, Н-5)
    assert mat["package"] == ""                       # новый материал — ещё не в пакете
    r = client.patch(f"/api/meetings/{mid}/materials/{mat['id']}",
                     json={"package": "05. Связь (к вопросу 1).docx\n"})
    assert r.status_code == 200, r.text
    after = r.json()["meeting"]["materials"][-1]
    assert after["package"] == "05. Связь (к вопросу 1).docx"
    for k in ("title", "text", "question_num", "type"):
        assert after[k] == mat[k], k                  # остальное не тронуто
    # правка без package пометку не стирает
    r = client.patch(f"/api/meetings/{mid}/materials/{mat['id']}", json={"title": "Связь в школах"})
    assert r.json()["meeting"]["materials"][-1]["package"] == "05. Связь (к вопросу 1).docx"
    # длинная строка — 422, а не запись в базу
    r = client.patch(f"/api/meetings/{mid}/materials/{mat['id']}", json={"package": "x" * 301})
    assert r.status_code == 422
