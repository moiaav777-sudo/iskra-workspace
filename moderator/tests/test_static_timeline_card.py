"""Вкладка «Модератор»: таймлайн — отдельной карточкой под сценарием (просьба пользователя 02.10), пустой —
карточки нет. Логика в браузерном `static/app.js`, проверяем через node (`js_timeline_card_check.js`).
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
APP_JS = HERE.parent / "static" / "app.js"
CHECK_JS = HERE / "js_timeline_card_check.js"


@pytest.mark.skipif(shutil.which("node") is None, reason="node не найден в PATH — фронтенд-тест пропущен")
def test_timeline_is_a_separate_card():
    result = subprocess.run(["node", str(CHECK_JS), str(APP_JS)], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, "карточка таймлайна не прошла проверку:\n" + result.stdout + result.stderr
    assert "OK" in result.stdout
