"""Редактор тезисов и окно «Написать с «Искрой»» — жалобы пользователя 02.10.

1. «Не могу поменять цвет текста — при нажатии слетает выделение»: уход фокуса на палитру
   панели считался уходом из редактора, и редактор пересобирался.
2. Панель — единый набор, как в «Совещании»; очистка разметки пропускает всё, что панель
   ставит, и ничего лишнего.
3. Поле «приложить материалы» — общая зона .dropzone, как во всех разделах.
4. «Наговорить» — распознавание браузера, стартует только по кнопке; к устройствам сервис
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
    assert result.returncode == 0, "редактор / зона файлов / диктовка не прошли проверку:\n" + result.stdout + result.stderr
    assert "OK" in result.stdout


def test_page_does_not_open_devices_itself():
    """Микрофон открывает распознавание речи браузера по кнопке «Наговорить»; сам сервис
    к устройствам не обращается (вопрос пользователя 02.10)."""
    for path in [APP_JS, STATIC / "index.html"]:
        text = path.read_text(encoding="utf-8")
        assert not re.search(r"getUserMedia|mediaDevices|enumerateDevices|MediaRecorder", text), path.name
