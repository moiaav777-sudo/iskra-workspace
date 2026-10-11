"""Фронтенд: рамка «Совещания» и кнопки Word/PDF (решение и просьба пользователя 02.10).

Логика — в браузерном `static/app.js`, поэтому проверяем через node: `js_embed_check.js` грузит
настоящий файл в песочницу с подставным DOM и смотрит, что страница нарисовала, что запросила
и что отправила родителю. Приём тот же, что у `moderator/tests/js_*_check.js`.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
APP_JS = HERE.parent / "static" / "app.js"
CHECK_JS = HERE / "js_embed_check.js"


@pytest.mark.skipif(shutil.which("node") is None, reason="node не найден в PATH — фронтенд-тест пропущен")
def test_embed_and_word_pdf_frontend():
    result = subprocess.run(["node", str(CHECK_JS), str(APP_JS)],
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, ("рамка «Совещания» или Word/PDF не прошли проверку:\n"
                                    + result.stdout + result.stderr)
    assert "OK" in result.stdout
