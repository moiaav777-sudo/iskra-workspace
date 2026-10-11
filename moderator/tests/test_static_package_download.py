"""Вкладка «Пакет»: кнопки «Word» и «PDF» у документов (просьба пользователя 02.10) — в «Материалах».

С вечера 02.10 вкладка показывает карточку папки самих «Материалов» в iframe (там и Word/PDF),
своей таблицы со скачиванием у «Совещания» нет. `js_package_download_check.js` грузит настоящий
`static/app.js` в песочницу и проверяет: в теле вкладки — рамка с адресом встраивания и id папки,
ни таблицы, ни `downloadPkgFile`.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
APP_JS = HERE.parent / "static" / "app.js"
CHECK_JS = HERE / "js_package_download_check.js"


@pytest.mark.skipif(shutil.which("node") is None, reason="node не найден в PATH — фронтенд-тест пропущен")
def test_package_word_pdf_download_buttons():
    result = subprocess.run(
        ["node", str(CHECK_JS), str(APP_JS)],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, (
        "«Пакет» без своей таблицы скачивания не прошёл проверку:\n" + result.stdout + result.stderr
    )
    assert "OK" in result.stdout
