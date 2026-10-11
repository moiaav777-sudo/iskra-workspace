"""Автоотправка «Повестки» и «Списка участников» в пакет после сохранённого изменения
(решение пользователя 28.09): если у совещания уже есть папка «Материалов», свежая версия
документа уходит в пакет сама, без отдельного нажатия «Отправить в пакет» (кнопка остаётся —
способ отправить прямо сейчас). Задержка после последнего изменения — debounce, а не на
каждую букву; предохранитель «чужой документ того же жанра» (ревьюер RC-2026-09-28-6, К1)
в фоне не блокирует модальным окном, а оставляет статус для значка у вкладки «Пакет».

Логика — в браузерном `static/app.js` (без сборки, без модулей), поэтому проверяем через node,
как и `test_static_package_guard.py`: `js_auto_package_check.js` грузит настоящий файл
в песочницу и дёргает настоящие функции, а не копию логики в тесте.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
APP_JS = HERE.parent / "static" / "app.js"
CHECK_JS = HERE / "js_auto_package_check.js"


@pytest.mark.skipif(shutil.which("node") is None, reason="node не найден в PATH — фронтенд-тест пропущен")
def test_agenda_and_participants_auto_send_to_package():
    result = subprocess.run(
        ["node", str(CHECK_JS), str(APP_JS)],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, (
        "автоотправка в пакет не прошла проверку:\n" + result.stdout + result.stderr
    )
    assert "OK" in result.stdout
