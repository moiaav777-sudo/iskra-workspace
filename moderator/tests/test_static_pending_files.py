"""Вкладка «Материалы»: у выбранного, но не разобранного файла под вопросом есть кнопка «Убрать» —
передумал загружать (просьба пользователя 02.10). Логика в браузерном `static/app.js`, поэтому проверяем
через node (`js_pending_files_check.js` грузит настоящий файл в песочницу), как и другие фронтенд-тесты.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
APP_JS = HERE.parent / "static" / "app.js"
CHECK_JS = HERE / "js_pending_files_check.js"


@pytest.mark.skipif(shutil.which("node") is None, reason="node не найден в PATH — фронтенд-тест пропущен")
def test_pending_material_file_can_be_removed():
    result = subprocess.run(["node", str(CHECK_JS), str(APP_JS)], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, "кнопка «Убрать» у файла материалов не прошла проверку:\n" + result.stdout + result.stderr
    assert "OK" in result.stdout
