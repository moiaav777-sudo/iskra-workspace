"""Карточка записи блоками общего компонента (решение пользователя 13.10: «каждый раздел — отдельным
блоком для форматирования, как в модераторе; единый формат») — в настоящем Chromium.

`js_detail_check.js` грузит настоящие index.html, app.js, style.css сервиса и общие файлы портала
(`../portal/static`: blocks.js, blocks.css, Quill, DOMPurify), ответы сервиса подменные. Нет node,
playwright или папки портала рядом — пропуск (pytest.skip, не успех).
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
STATIC = HERE.parent / "static"
PORTAL_STATIC = HERE.parent.parent / "portal" / "static"


@pytest.mark.skipif(shutil.which("node") is None, reason="node не найден в PATH — проверка интерфейса пропущена")
@pytest.mark.skipif(not (PORTAL_STATIC / "blocks.js").exists(), reason="portal/static не рядом")
def test_kartochka_zapisi_blokami_v_brauzere():
    result = subprocess.run(["node", str(HERE / "js_detail_check.js"), str(STATIC), str(PORTAL_STATIC)],
                            capture_output=True, text=True, timeout=240)
    assert result.returncode == 0, result.stdout + result.stderr
    # нет playwright — тест пропущен, а не засчитан (ревью RC-2026-10-13-2, Н-4)
    if result.stdout.startswith("SKIP"):
        pytest.skip(result.stdout.strip())
    assert result.stdout.strip().endswith("OK"), result.stdout
