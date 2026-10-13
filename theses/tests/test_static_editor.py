"""Текст тезисов блоками по разделам и окно «Написать с «Искрой»».

1. Решение пользователя 13.10 («каждый раздел — отдельным блоком, как в модераторе; единый
   формат»): HTML тезисов делится на разделы по заголовкам и собирается обратно без потерь;
   сохранение блока шлёт весь HTML с версией, 409 оставляет мой текст в редакторе, удаление
   и «Добавить раздел», замена слова наполовину латиницей. Сам компонент (панель, Quill) —
   общий, его проверяет браузерный тест (`test_static_blocks_ui.py`).
2. Поле «приложить материалы» — общая зона .dropzone, как во всех разделах.
3. «Наговорить» — распознавание браузера, стартует только по кнопке; к устройствам сервис
   сам не обращается.

Логика в браузерном `static/app.js`, поэтому проверяем через node (`js_editor_check.js`
грузит настоящий файл в песочницу), как фронтенд-тесты «Модератора».
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
STATIC = HERE.parent / "static"
APP_JS = STATIC / "app.js"
CHECK_JS = HERE / "js_editor_check.js"


@pytest.mark.skipif(shutil.which("node") is None, reason="node не найден в PATH — фронтенд-тест пропущен")
def test_editor_toolbar_dropzone_and_dictation():
    result = subprocess.run(["node", str(CHECK_JS), str(APP_JS)], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, "разделы / сохранение / зона файлов / диктовка не прошли проверку:\n" + result.stdout + result.stderr
    assert "OK" in result.stdout


def test_page_does_not_open_devices_itself():
    """Микрофон открывает распознавание речи браузера по кнопке «Наговорить»; сам сервис
    к устройствам не обращается (вопрос пользователя 02.10)."""
    for path in [APP_JS, STATIC / "index.html"]:
        text = path.read_text(encoding="utf-8")
        assert not re.search(r"getUserMedia|mediaDevices|enumerateDevices|MediaRecorder", text), path.name
