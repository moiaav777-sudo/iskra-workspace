"""Вкладка «Поручения» (решения пользователя 02.10) и докладчик нового вопроса из реестра людей: логика в браузерном
`static/app.js`, проверяем через node (`js_orders_tab_check.js`).
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
CHECK_JS = HERE / "js_orders_tab_check.js"


@pytest.mark.skipif(shutil.which("node") is None, reason="node не найден в PATH — фронтенд-тест пропущен")
def test_orders_tab():
    result = subprocess.run(["node", str(CHECK_JS), str(APP_JS)], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, "вкладка «Поручения» не прошла проверку:\n" + result.stdout + result.stderr
    assert "OK" in result.stdout


def test_orders_tab_styles():
    css = INDEX.read_text(encoding="utf-8")
    # блок поручения и поля в ряд; на узком экране — столбиком
    assert re.search(r"\.order-card\{[^}]*border:", css)
    assert re.search(r"\.order-fields\{[^}]*display:grid", css)
    assert re.search(r"@media \(max-width:720px\)\{\.order-fields\{grid-template-columns:minmax\(0,1fr\);\}\}", css)
    # подсказки реестра поверх формы и пометка «нет в реестре»
    assert re.search(r"\.spk-sug\{[^}]*position:absolute", css) and ".spk-chip.unknown" in css
    # правили app.js — подняли версию скрипта, иначе у людей останется старый
    assert re.search(r'static/app\.js\?v=(\d+)', css) and int(re.search(r'static/app\.js\?v=(\d+)', css).group(1)) >= 53
