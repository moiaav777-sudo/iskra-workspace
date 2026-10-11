"""Фронтенд списка изменений с галочками (решение пользователя 28.09, перенос из сервиса
«Повестка», `app/agenda_changes.py`): `applyChanges` в `static/app.js` шлёт на сервер только
отмеченные изменения и разбирает ответ 409 «что применимо / что устарело» так же, как уже
проверенный `_sendDocToPackage`.

Логика — в браузерном `static/app.js` (без сборки, без модулей), поэтому проверяем через node,
как и другие фронтенд-тесты этого сервиса: `js_agenda_changes_check.js` грузит настоящий файл
в песочницу и дёргает настоящую функцию `applyChanges`, а не копию логики в тесте.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
APP_JS = HERE.parent / "static" / "app.js"
CHECK_JS = HERE / "js_agenda_changes_check.js"


@pytest.mark.skipif(shutil.which("node") is None, reason="node не найден в PATH — фронтенд-тест пропущен")
def test_apply_changes_sends_checked_ops_and_handles_stale_conflict():
    result = subprocess.run(
        ["node", str(CHECK_JS), str(APP_JS)],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, (
        "фронтенд списка изменений не прошёл проверку:\n" + result.stdout + result.stderr
    )
    assert "OK" in result.stdout
