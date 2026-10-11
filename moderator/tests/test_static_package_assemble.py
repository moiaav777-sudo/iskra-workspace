"""«Собрать пакет» на вкладке «Пакет» (решение пользователя 07.10): одним нажатием свежие «Повестка»,
«Список участников», «Модератор», «Перечень поручений» и документы «Результатов» уходят в папку
совещания (каждый своим обычным путём, с той же сверкой ФИО, но без окон — причина строкой в отчёте),
материалы сверяются по вопросам с папкой, «Материалы» приводят папку в порядок (реестр 00), человек
видит состав пакета и чего не хватает. Архив по-прежнему собирают «Материалы» («Скачать пакет»).

Логика — в браузерном `static/app.js`, проверяем через node, как и соседние проверки:
`js_package_assemble_check.js` грузит настоящий файл в песочницу и дёргает настоящие функции.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from .test_api import audit, client, login, people  # noqa: F401  (фикстуры)

HERE = Path(__file__).resolve().parent
APP_JS = HERE.parent / "static" / "app.js"
CHECK_JS = HERE / "js_package_assemble_check.js"


@pytest.mark.skipif(shutil.which("node") is None, reason="node не найден в PATH — фронтенд-тест пропущен")
def test_assemble_package_sends_everything_and_reports():
    result = subprocess.run(["node", str(CHECK_JS), str(APP_JS)], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, "«Собрать пакет» не прошёл проверку:\n" + result.stdout + result.stderr
    assert "OK" in result.stdout


def test_me_gives_registry_address(client):
    """«Привести в порядок» у «Материалов» — адрес отдаёт сервер, в странице абсолютных адресов нет."""
    login(client, "alice")
    links = client.get("/api/me").json()["materials"]
    assert links["registry"] == "/materials/api/events/{id}/registry"


def test_page_has_new_script_version():
    html = (HERE.parent / "static" / "index.html").read_text(encoding="utf-8")
    assert "static/app.js?v=62" in html
    assert ".pkg-rows" in html
