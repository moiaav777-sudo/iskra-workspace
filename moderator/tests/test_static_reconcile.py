"""Сверка докладчиков с реестром после разбора и состояние карточки «Вставить документ» при перерисовке
(просьбы пользователя 02.10; задачи ревьюера RC-2026-10-01-1 Н1/Н3, RC-2026-10-02-2 Н-1/Н-2/Н-6): окна не
спорят, устаревшая сверка не повторяет окно, `replace`/`pending/resolve` уходят в совещание окна,
`_applyBusy` снят до сверки, второй разбор при перерисовке не запускается, галочки переживают перерисовку.
Логика — в браузерном `static/app.js`, проверяем через node (`js_reconcile_check.js`), как и остальные
фронтенд-тесты сервиса.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
APP_JS = HERE.parent / "static" / "app.js"
CHECK_JS = HERE / "js_reconcile_check.js"


@pytest.mark.skipif(shutil.which("node") is None, reason="node не найден в PATH — фронтенд-тест пропущен")
def test_reconcile_windows_and_import_card_state():
    result = subprocess.run(["node", str(CHECK_JS), str(APP_JS)], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, "сверка докладчиков / карточка вставки не прошли проверку:\n" + result.stdout + result.stderr
    assert "OK" in result.stdout
