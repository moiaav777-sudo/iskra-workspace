"""Распознавание речи (аудио → текст с таймкодами) — один интерфейс на всё пространство.

Решение пользователя 27.09.2026 (`../../CLAUDE.md`, п. 5а): распознавание речи — только
локально, движком `faster-whisper` на домашнем ПК, без платных облачных сервисов ни в
каком виде. Провайдер «Искры» (`iskra_common.py`) сейчас принимает только текстовые
запросы — ни файлов, ни аудио в его контракте нет, поэтому распознавание живёт отдельно.

Написан по образцу «Транскрибатора» (`app/engine/whisper_engine.py`, `fake.py`) —
первого сервиса, которому это понадобилось; следующие («Тезисы», «Повестка» — надиктовка)
подключаются к этому же модулю, а не пишут своё.

ЭТОТ ФАЙЛ ПРАВИТСЯ ТОЛЬКО В `portal/shared/speech_common.py`.
В сервисы он раскладывается скриптом `portal/scripts/sync_shared.sh` — правка копии
будет затёрта.

Что внутри:
- `Segment`, `Engine` — общий интерфейс: кусок распознанного текста с таймкодом начала
  и конца, и протокол `transcribe(path, cancelled) -> list[Segment]`;
- `TranscribeError`/`TranscribeUnavailable`/`Cancelled` — общие исключения;
- `WhisperEngine` — обёртка над `faster-whisper`: библиотека импортируется лениво
  (`AVAILABLE` — установлена ли), модель — `int8` на CPU (расчёт на домашний ПК без GPU),
  размер модели передаётся из настроек сервиса (`TRANSCRIBER_WHISPER_MODEL` и т. п.),
  по умолчанию `small` — компромисс скорости и качества без GPU;
  `medium`/`large-v3` точнее, но заметно медленнее;
- `fake_engine` — детерминированная подмена без сети и тяжёлых библиотек: тесты и
  показ интерфейса на стенде, пока модель не поставлена (как «Искра» без ключа —
  интерфейс работает, ответ демонстрационный);
- `get_engine`/`engine_status` — выбор реализации по имени (`""`/`fake`/`whisper`),
  сервис передаёт своё имя движка из `.env` и свой размер модели.

Что нужно на домашнем ПК для настоящего распознавания: `pip install faster-whisper`
(тянет `ctranslate2`), ffmpeg в PATH (декодирование любого формата записи), свободное
место под модель — она скачивается из интернета при первом запуске (`small` — около
500 МБ). Подробности и деньги/приватность уже решены пользователем 27.09 — здесь
только код.
"""
from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol

DEFAULT_MODEL = "small"


@dataclass(frozen=True)
class Segment:
    """Кусок распознанной речи с таймкодом начала и конца, в секундах."""

    start: float
    end: float
    text: str

    def to_dict(self) -> dict:
        return {"start": round(self.start, 1), "end": round(self.end, 1), "text": self.text}


class TranscribeError(Exception):
    """Распознавание не удалось."""


class TranscribeUnavailable(TranscribeError):
    """Движок не установлен или не настроен — это не ошибка конкретной записи."""


class Cancelled(TranscribeError):
    """Человек отменил распознавание."""


class Engine(Protocol):
    """Один метод: путь к аудио/видео файлу → список кусков текста с таймкодами.

    `cancelled()` вызывается движком между кусками; вернул True — нужно бросить
    `Cancelled` как можно быстрее, не теряя времени на уже ненужную работу.
    """

    name: str

    def transcribe(self, path: Path, cancelled: Callable[[], bool]) -> list[Segment]:
        ...


# -- настоящий движок: локальная модель, без внешнего платного сервиса --------------

INSTALL_HINT = (
    "Локальное распознавание не установлено. Нужно на домашнем ПК: `pip install "
    "faster-whisper`, ffmpeg в PATH и свободное место под модель (первый запуск "
    "качает её из интернета)."
)

try:
    import faster_whisper  # type: ignore
    AVAILABLE = True
except ImportError:
    faster_whisper = None  # type: ignore
    AVAILABLE = False


class WhisperEngine:
    """`faster-whisper` на CPU (`int8`) — расчёт на домашний ПК без GPU."""

    name = "whisper"

    def __init__(self, model_size: str = DEFAULT_MODEL) -> None:
        self._model_size = model_size or DEFAULT_MODEL
        self._model = None

    def _load(self):
        if self._model is None:
            if faster_whisper is None:
                raise TranscribeUnavailable(INSTALL_HINT)
            self._model = faster_whisper.WhisperModel(self._model_size, device="cpu",
                                                       compute_type="int8")
        return self._model

    def transcribe(self, path: Path, cancelled: Callable[[], bool]) -> list[Segment]:
        model = self._load()
        segments_iter, _info = model.transcribe(str(path), vad_filter=True)
        segments: list[Segment] = []
        for seg in segments_iter:
            if cancelled():
                raise Cancelled("Распознавание отменено")
            segments.append(Segment(start=float(seg.start), end=float(seg.end),
                                    text=str(seg.text).strip()))
        return segments


# кэш по размеру модели — модель загружается один раз и переиспользуется между
# запросами (загрузка медленная), а не поднимается заново на каждую запись
_whisper_instances: dict[str, WhisperEngine] = {}


def cached_whisper(model_size: str = DEFAULT_MODEL) -> WhisperEngine:
    """Тот же экземпляр `WhisperEngine` для одного размера модели — модель не перезагружается."""
    size = model_size or DEFAULT_MODEL
    inst = _whisper_instances.get(size)
    if inst is None:
        inst = WhisperEngine(size)
        _whisper_instances[size] = inst
    return inst


# -- подменная реализация: без сети и тяжёлых библиотек -----------------------------

CHUNK_SECONDS = 12.0   # длина одного «куска» демонстрационного текста


def _digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        h.update(f.read(1024 * 1024))
    h.update(str(path.stat().st_size).encode())
    return h.hexdigest()[:8]


class FakeEngine:
    """Детерминированный ответ без установки чего-либо — тесты и показ интерфейса.

    Ответ читает сам файл (первые байты и размер), чтобы разные записи давали разный,
    но воспроизводимый текст — этого достаточно, чтобы отличить одну загрузку от
    другой на экране, не обрабатывая по-настоящему звук.
    """

    name = "fake"

    def transcribe(self, path: Path, cancelled: Callable[[], bool]) -> list[Segment]:
        size = path.stat().st_size
        # без настоящего разбора длительности берём условную оценку по размеру файла —
        # только чтобы получить несколько кусков текста для демонстрации интерфейса
        seconds = max(CHUNK_SECONDS, min(2 * 60 * 60, size / (16 * 1024)))
        pieces = max(1, int(seconds // CHUNK_SECONDS))
        tag = _digest(path)
        segments: list[Segment] = []
        for i in range(pieces):
            if cancelled():
                raise Cancelled("Распознавание отменено")
            start = i * CHUNK_SECONDS
            text = (f"[демонстрационная расшифровка, движок не настроен] "
                    f"фрагмент {i + 1} из {pieces}, запись {tag}")
            segments.append(Segment(start=start, end=start + CHUNK_SECONDS, text=text))
            time.sleep(0.001)   # символическая работа — тесты не должны ждать реально
        return segments


fake_engine = FakeEngine()


# -- выбор реализации ----------------------------------------------------------------

def get_engine(name: str = "", *, model_size: str = DEFAULT_MODEL) -> Engine:
    """Движок по имени: `fake` — подмена, `whisper`/`faster-whisper` — локальная модель.

    Не указано явно — выбираем по тому, что реально доступно: настоящий движок, если
    его библиотека установлена, иначе подменный (как «Искра» без ключа у остальных
    сервисов — интерфейс работает, ответ демонстрационный).
    """
    choice = (name or "").strip().lower()
    if not choice:
        return cached_whisper(model_size) if AVAILABLE else fake_engine
    if choice == "fake":
        return fake_engine
    if choice in ("whisper", "faster-whisper", "faster_whisper"):
        if not AVAILABLE:
            raise TranscribeUnavailable(INSTALL_HINT)
        return cached_whisper(model_size)
    raise TranscribeUnavailable(f"Неизвестный движок распознавания: «{choice}»")


def engine_status(name: str = "", *, model_size: str = DEFAULT_MODEL) -> dict:
    """Что показать человеку и в диагностике: какой движок работает и это подмена или нет."""
    choice = (name or "").strip().lower()
    if choice == "fake":
        return {"engine": "fake", "demo": True, "ready": True}
    if choice in ("whisper", "faster-whisper", "faster_whisper") or not choice:
        if AVAILABLE:
            return {"engine": "whisper", "demo": False, "ready": True,
                    "model": model_size or DEFAULT_MODEL}
        if choice:
            return {"engine": "whisper", "demo": False, "ready": False, "note": INSTALL_HINT}
        return {"engine": "fake", "demo": True, "ready": True,
                "note": "Локальный движок распознавания не установлен — работает подмена. "
                        + INSTALL_HINT}
    return {"engine": choice, "demo": False, "ready": False, "note": "Неизвестный движок"}
