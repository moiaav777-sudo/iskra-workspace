"""Открытие предложения из «Приёмной» по ссылке `#meeting=<id>&proposal=<key>` (шаг 2 перехода
«Повестка → Совещание», решение пользователя 29.09): `_openInboxProposal` в `static/app.js`
заполняет то же состояние, что обычный «Умный разбор (Искра)», и переключает на вкладку
«Повестка» (вкладки «Первичка» нет с 01.10, карточка вставки документа живёт в «Повестке») —
дальше работает уже проверенный `renderSmartResult`.

Логика — в браузерном `static/app.js` (без сборки, без модулей), поэтому проверяем через node,
как и другие фронтенд-тесты этого сервиса: `js_inbox_proposal_check.js` грузит настоящий файл
в песочницу и дёргает настоящие функции `_hashParams`/`_openInboxProposal`, а не копию логики.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
APP_JS = HERE.parent / "static" / "app.js"
CHECK_JS = HERE / "js_inbox_proposal_check.js"


@pytest.mark.skipif(shutil.which("node") is None, reason="node не найден в PATH — фронтенд-тест пропущен")
def test_inbox_proposal_link_opens_smart_result():
    result = subprocess.run(
        ["node", str(CHECK_JS), str(APP_JS)],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, (
        "открытие предложения из «Приёмной» не прошло проверку:\n" + result.stdout + result.stderr
    )
    assert "OK" in result.stdout
