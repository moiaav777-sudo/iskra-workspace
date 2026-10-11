"""Кнопка «Скачать .docx» на вкладках «Повестка»/«Участники» (просьба пользователя 29.09):
отдельное от «Отправить в пакет» действие, документ скачивается человеку напрямую, сверка ФИО
перед выдачей обязательна (то же поведение, что у кнопки «Отправить в пакет»), но в пакет
совещания ничего не уходит.

Логика — в браузерном `static/app.js` (без сборки, без модулей), поэтому проверяем её через
node: `js_download_docx_check.js` грузит настоящий файл в песочницу (минимум DOM-заглушек) и
прогоняет сценарии через настоящие функции `downloadAgendaDocx`/`downloadParticipantsDocx`,
а не копию логики в тесте.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
APP_JS = HERE.parent / "static" / "app.js"
CHECK_JS = HERE / "js_download_docx_check.js"


@pytest.mark.skipif(shutil.which("node") is None, reason="node не найден в PATH — фронтенд-тест пропущен")
def test_download_docx_buttons_are_independent_of_package():
    result = subprocess.run(
        ["node", str(CHECK_JS), str(APP_JS)],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, (
        "кнопка «Скачать .docx» не прошла проверку:\n" + result.stdout + result.stderr
    )
    assert "OK" in result.stdout
