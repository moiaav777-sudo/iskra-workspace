"""Вкладка «Результаты» (решение пользователя 02.10): после «Пакета», три карточки — транскрипция, саммари,
проект протокольных решений; пустое состояние со ссылкой на «Транскрибатор», транскрипция свёрнута,
«Скачать Word» / «Открыть в транскрибаторе» / «Убрать». Логика в браузерном `static/app.js`, проверяем через
node (`js_results_tab_check.js`).
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
APP_JS = HERE.parent / "static" / "app.js"
INDEX = HERE.parent / "static" / "index.html"
CHECK_JS = HERE / "js_results_tab_check.js"


@pytest.mark.skipif(shutil.which("node") is None, reason="node не найден в PATH — фронтенд-тест пропущен")
def test_results_tab():
    result = subprocess.run(["node", str(CHECK_JS), str(APP_JS)], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, "вкладка «Результаты» не прошла проверку:\n" + result.stdout + result.stderr
    assert "OK" in result.stdout


def test_results_tab_styles_and_one_row():
    css = INDEX.read_text(encoding="utf-8")
    # транскрипция сворачивается стилем, а вкладки держатся в одну строку (листаются вбок)
    assert re.search(r"\.res-text\.folded\{[^}]*max-height:[^}]*overflow:hidden", css)
    tabs = re.search(r"\.tabs\{([^}]*)\}", css).group(1)
    assert "flex-wrap:nowrap" in tabs and "overflow-x:auto" in tabs
    # восемь вкладок на 1024 px: боковой отступ вкладки поджат с 10 до 7 px (замер в Chromium — 605 из 612 px)
    tab = re.search(r"\.tab\{([^}]*)\}", css).group(1)
    assert "padding:9px 6px 10px" in tab and "white-space:nowrap" in tab
