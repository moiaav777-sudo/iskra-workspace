"""Интерфейс «аудио → текст с таймкодами» — распознавание отделено от остального сервиса.

Решение пользователя 02.10 (меняет решение 27.09 о локальной модели): **распознаёт «Искра»** —
файл записи уходит в «Искру» (`/api/v1/chat/files` → фоновый прогон `/api/v1/runs` с опросом), она
сохраняет дословную расшифровку с таймкодами TXT-файлом, движок его скачивает (`iskra_engine.py`). Это движок по умолчанию (`TRANSCRIBER_ENGINE=iskra`).

Общая часть (интерфейс `Engine`, `Segment`, исключения, обёртка над `faster-whisper` и
подменная реализация) — общий модуль `portal/shared/speech_common.py`, копия в
`app/speech_common.py` раскладывается `portal/scripts/sync_shared.sh`; править только исходник.

Реализации:
- `iskra_engine.py` — «Искра» (по умолчанию); расход токенов — в `engine.spent`;
- `fake.py` — детерминированная подмена для тестов и показа интерфейса без сети;
- `whisper_engine.py` — локальная модель (faster-whisper), опциональный резерв: работает,
  только если библиотека поставлена на машине (`TRANSCRIBER_WHISPER_MODEL`, по умолчанию `small`).

`config.ENGINE` выбирает реализацию; `engine_status()` — что видно на странице.
"""
from __future__ import annotations

from .. import speech_common as _sc
from ..speech_common import (  # noqa: F401 — реэкспорт: main.py и тесты используют engine.*
    Cancelled,
    Engine,
    Segment,
    TranscribeError,
    TranscribeUnavailable,
)
from .iskra_engine import SpeakerSegment  # noqa: F401

ISKRA = ("iskra", "искра")


def get_engine(name: str = "") -> Engine:
    """Движок по имени или из настроек (`config.ENGINE`).

    `iskra` — новый движок «Искры» на каждую запись (без ключа — `TranscribeUnavailable`);
    `fake`/`whisper`/пусто — как в общем модуле (`speech_common.get_engine`).
    """
    from .. import config
    from . import iskra_engine
    choice = (name or "").strip().lower()
    if choice in ISKRA:
        return iskra_engine.get_engine()
    return _sc.get_engine(choice, model_size=config.WHISPER_MODEL)


def engine_status(name: str = "") -> dict:
    """Что показать человеку и в диагностике: какой движок работает и это подмена или нет."""
    from .. import config
    from . import iskra_engine
    choice = (name or "").strip().lower()
    if choice in ISKRA:
        return iskra_engine.status()
    return _sc.engine_status(choice, model_size=config.WHISPER_MODEL)
