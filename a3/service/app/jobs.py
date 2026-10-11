"""Долгие обращения к «Искре» — фоном и по одному за раз.

Разбор справки идёт минутами. Держать на нём соединение нельзя: браузер и шлюз
портала рвут запрос раньше, и разбор пропадает вместе с уже потраченными
токенами. Поэтому запрос только ставит работу в очередь и сразу отдаёт номер,
а браузер спрашивает о ходе дела короткими запросами.

Работник один: «Искра» на один ключ ведёт один прогон и на второй отвечает
«Предыдущий разбор ещё идёт». Очередь честная, человек видит своё место в ней.
"""
from __future__ import annotations

import queue
import threading
import time
import traceback
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

KEEP = 30 * 60      # сколько держим готовую запись, чтобы вкладка успела её забрать
MAX_WAITING = 8     # больше в очередь не берём: незачем копить разборы на час вперёд
BEAT = 30           # как часто напоминаем листу, что работа идёт

QUEUED, RUNNING, DONE, ERROR = "queued", "running", "done", "error"


@dataclass
class Job:
    id: str
    kind: str                     # draft — справка в лист, fix — круг доработки
    login: str
    sheet_id: int | None
    work: Callable[[], dict[str, Any]]
    beat: Callable[[], None] | None = None
    status: str = QUEUED
    result: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    created_at: float = field(default_factory=time.monotonic)
    started_at: float = 0.0
    finished_at: float = 0.0

    def seconds(self) -> int:
        """Сколько идёт работа — это видно человеку в шторке."""
        if not self.started_at:
            return 0
        return int((self.finished_at or time.monotonic()) - self.started_at)


class Busy(Exception):
    """Очередь заполнена — новый разбор не берём."""


_jobs: dict[str, Job] = {}
_waiting: list[str] = []
_line: "queue.Queue[str]" = queue.Queue()
_lock = threading.Lock()
_worker: threading.Thread | None = None
_beater: threading.Thread | None = None


def _sweep() -> None:
    """Готовые записи не копятся: вкладка забрала результат — запись не нужна."""
    now = time.monotonic()
    for job_id, job in list(_jobs.items()):
        if job.finished_at and now - job.finished_at > KEEP:
            _jobs.pop(job_id, None)


def _run_one(job: Job) -> None:
    job.started_at = time.monotonic()
    job.status = RUNNING
    try:
        job.result = job.work()
        job.status = DONE
    except Exception as e:  # ошибку показываем человеку, сервис от неё не падает
        job.error = str(e) or type(e).__name__
        job.status = ERROR
        traceback.print_exception(type(e), e, e.__traceback__)
    finally:
        job.finished_at = time.monotonic()


def _loop() -> None:
    while True:
        job_id = _line.get()
        try:
            with _lock:
                if job_id in _waiting:
                    _waiting.remove(job_id)
                job = _jobs.get(job_id)
            if job is not None:
                _run_one(job)
        finally:
            _line.task_done()


def _beat_loop() -> None:
    """Пока работа идёт, лист отмечается живым: иначе список сочтёт круг брошенным."""
    while True:
        time.sleep(BEAT)
        with _lock:
            running = [j for j in _jobs.values() if j.status == RUNNING and j.beat]
        for job in running:
            try:
                job.beat()  # type: ignore[misc]
            except Exception:
                pass


def _ensure_threads() -> None:
    global _worker, _beater
    with _lock:
        if _worker is None or not _worker.is_alive():
            _worker = threading.Thread(target=_loop, name="a3-spark", daemon=True)
            _worker.start()
        if _beater is None or not _beater.is_alive():
            _beater = threading.Thread(target=_beat_loop, name="a3-spark-beat", daemon=True)
            _beater.start()


def submit(kind: str, user: dict[str, str], work: Callable[[], dict[str, Any]],
           sheet_id: int | None = None, beat: Callable[[], None] | None = None) -> Job:
    job = Job(id=uuid.uuid4().hex, kind=kind, login=user.get("login", ""),
              sheet_id=sheet_id, work=work, beat=beat)
    with _lock:
        _sweep()
        if len(_waiting) >= MAX_WAITING:
            raise Busy("Сейчас в очереди на разбор слишком много справок. Попробуйте через несколько минут.")
        _jobs[job.id] = job
        _waiting.append(job.id)
    _ensure_threads()
    _line.put(job.id)
    return job


def get(job_id: str) -> Job | None:
    with _lock:
        return _jobs.get(job_id)


def position(job: Job) -> int:
    """Сколько разборов впереди: 0 — очередь дошла до нас.

    Тот, который «Искра» уже ведёт, тоже впереди: человек ждёт и его.
    """
    with _lock:
        if job.id not in _waiting:
            return 0
        running = sum(1 for other in _jobs.values() if other.status == RUNNING)
        return _waiting.index(job.id) + running


WORDS = {
    ("draft", QUEUED): "Справка в очереди на разбор",
    ("draft", RUNNING): "«Искра» собирает лист",
    ("fix", QUEUED): "Замечания в очереди к «Искре»",
    ("fix", RUNNING): "«Искра» исправляет замечания",
}


def view(job: Job) -> dict[str, Any]:
    """Что отдаём браузеру: состояние, место в очереди и результат."""
    ahead = position(job) if job.status == QUEUED else 0
    message = WORDS.get((job.kind, job.status), "")
    if job.status == QUEUED and ahead:
        message += f": впереди {ahead}"
    out: dict[str, Any] = {
        "id": job.id,
        "kind": job.kind,
        "status": job.status,
        "message": job.error if job.status == ERROR else message,
        "position": ahead,
        "seconds": job.seconds(),
    }
    if job.status == DONE:
        out.update(job.result)
    return out


def drain(timeout: float = 30.0) -> bool:
    """Дождаться, пока начатые разборы закончатся."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with _lock:
            if not any(job.status in (QUEUED, RUNNING) for job in _jobs.values()):
                return True
        time.sleep(0.01)
    return False


def reset(timeout: float = 30.0) -> None:
    """Только для тестов: дождаться начатых разборов и забыть их.

    Ждать обязательно. Работник один на весь сервис, и разбор, начатый в одной
    проверке, иначе допишет лист в базу следующей — а у неё свой файл.
    """
    drain(timeout)
    with _lock:
        _jobs.clear()
        _waiting.clear()
