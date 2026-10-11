"""Перерисовка карточки совещания (просьба пользователя 02.10): список изменений разбора — `label.check`
(иначе чекбокс раздувается общим правилом форм), `renderMeeting` того же совещания возвращает прокрутку
на место и не стирает невставленный текст карточки «Вставить документ».

Логика — в браузерном `static/app.js`, поэтому проверяем через node (`js_agenda_rerender_check.js`
грузит настоящий файл в песочницу и дёргает настоящие функции), как и другие фронтенд-тесты сервиса.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
APP_JS = HERE.parent / "static" / "app.js"
CHECK_JS = HERE / "js_agenda_rerender_check.js"


@pytest.mark.skipif(shutil.which("node") is None, reason="node не найден в PATH — фронтенд-тест пропущен")
def test_agenda_rerender_keeps_scroll_and_checkbox_layout():
    result = subprocess.run(["node", str(CHECK_JS), str(APP_JS)], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, "перерисовка карточки совещания не прошла проверку:\n" + result.stdout + result.stderr
    assert "OK" in result.stdout
