"""Папка пакета переехала — «Совещание» идёт по новому id (ошибка на боевом 02.10).

id папки у «Материалов» — хэш пути (`materials/app/tree.py::event_id`): при смене даты или
названия совещания папка переезжает и получает новый id. Все вызовы к пакету в `static/app.js`
идут через `_withPackageFolder`: 404 по прежнему id — перепривязка по ключу совещания (`link`),
`PUT /folder` и повтор по новому. Логика браузерная, поэтому проверяем через node:
`js_package_folder_check.js` грузит настоящий файл в песочницу с подменным fetch.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
APP_JS = HERE.parent / "static" / "app.js"
CHECK_JS = HERE / "js_package_folder_check.js"


@pytest.mark.skipif(shutil.which("node") is None, reason="node не найден в PATH — фронтенд-тест пропущен")
def test_package_follows_moved_folder():
    result = subprocess.run(
        ["node", str(CHECK_JS), str(APP_JS)],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, (
        "вызовы к пакету после переезда папки не прошли проверку:\n" + result.stdout + result.stderr
    )
    assert "OK" in result.stdout
