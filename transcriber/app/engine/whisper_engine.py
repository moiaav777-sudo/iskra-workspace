"""Локальное распознавание речи (faster-whisper) — опциональный резерв, тонкая обёртка над общим модулем.

Решение пользователя 02.10: распознаёт «Искра» (`iskra_engine.py`, по умолчанию). Этот движок
остаётся резервом (`TRANSCRIBER_ENGINE=whisper`, зависимости — `requirements-whisper.txt`), на
сервере не ставится: VPS (1 ядро, 2 ГБ) локальную модель не вытянет.

Логика (защищённый импорт `faster-whisper`, модель на CPU/`int8`, подменная реализация)
живёт в `portal/shared/speech_common.py` (копия — `app/speech_common.py`, не править
руками). Здесь — только имена, которые ждут `app/engine/__init__.py` и тесты, и модель
берётся из настроек сервиса (`TRANSCRIBER_WHISPER_MODEL`, по умолчанию `small` —
компромисс скорости и качества на CPU без GPU; `medium`/`large-v3` точнее, но заметно
медленнее без GPU).

Что нужно на домашнем ПК, чтобы это заработало (см. README.md → «Распознавание речи»):
- `faster-whisper` в `requirements.txt` сервиса — ставится в `.venv` сервиса на ПК;
- ffmpeg в PATH (декодирование любого формата записи в аудио-поток);
- свободное место под модель — она качается из интернета при первом запуске.
"""
from __future__ import annotations

from .. import config
from ..speech_common import AVAILABLE, INSTALL_HINT, WhisperEngine, cached_whisper  # noqa: F401

engine = cached_whisper(config.WHISPER_MODEL)
