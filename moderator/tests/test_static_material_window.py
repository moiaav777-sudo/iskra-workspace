"""Окно «Разбор Искрой – проверьте и подтвердите», карточка и правка материала, кнопка «Собрать вступительное
слово» (решения пользователя 02.10). Логика в браузерном `static/app.js`, поэтому проверяем через node
(`js_material_window_check.js` грузит настоящий файл в песочницу с маленьким DOM), как и другие фронтенд-тесты.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
APP_JS = HERE.parent / "static" / "app.js"
CHECK_JS = HERE / "js_material_window_check.js"


@pytest.mark.skipif(shutil.which("node") is None, reason="node не найден в PATH — фронтенд-тест пропущен")
def test_material_window_sections_orders_and_opening():
    result = subprocess.run(["node", str(CHECK_JS), str(APP_JS)], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, "окно разбора материала не прошло проверку:\n" + result.stdout + result.stderr
    assert "OK" in result.stdout
