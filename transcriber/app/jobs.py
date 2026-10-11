"""Распознавание и саммари — фоном и по одному за раз.

Устройство и мотивация — как у `a3/service/app/jobs.py`: обработка записи идёт минутами
(и может идти дольше при длинном файле на слабом ПК), держать на ней HTTP-соединение
нельзя — браузер и шлюз портала оборвут его раньше срока. Запрос на загрузку только
заводит работу и сразу отдаёт номер задания, а браузер спрашивает о ходе дела опросом.

В отличие от A3 здесь есть отмена (как в `theses/app/jobs.py`): распознавание длинной
записи может идти долго на домашнем ПК без GPU, и человеку нужно иметь возможность
передумать, не дожидаясь конца. Флаг отмены проверяется движком распознавания между
кусками и «Искрой» между опросами (см. `app/engine`, `app/summarizer.py`).
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
MAX_WAITING = 4      # больше в очередь не берём: распознавание тяжёлое для домашнего ПК
BEAT = 30

QUEUED, RUNNING, DONE, ERROR, CANCELLED = "queued", "running", "done", "error", "cancelled"


@dataclass
class Job:
    id: str
    login: str
    record_id: int
    work: Callable[[Callable[[], bool]], dict[str, Any]]
    beat: Callable[[], None] | None = None
    status: str = QUEUED
    result: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    stage: str = ""     # что сейчас происходит: «Распознаю речь…», «Искра» пишет саммари…»
    created_at: float = field(default_factory=time.monotonic)
    started_at: float = 0.0
    finished_at: float = 0.0
    cancel_event: threading.Event = field(default_factory=threading.Event)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def seconds(self) -> int:
        if not self.started_at:
            return 0
        return int((self.finished_at or time.monotonic()) - self.started_at)


class Busy(Exception):
    """Очередь заполнена — новую запись не берём."""


class Cancelled(Exception):
    """Работа прервана человеком — не ошибка, а осознанный отказ."""


_jobs: dict[str, Job] = {}
_waiting: list[str] = []
_line: "queue.Queue[str]" = queue.Queue()
_lock = threading.Lock()
_worker: threading.Thread | None = None
_beater: threading.Thread | None = None


def _sweep() -> None:
    now = time.monotonic()
    for job_id, job in list(_jobs.items()):
        if job.finished_at and now - job.finished_at > KEEP:
            _jobs.pop(job_id, None)


def _run_one(job: Job) -> None:
    job.started_at = time.monotonic()
    job.status = RUNNING
    try:
        if job.cancel_event.is_set():
            raise Cancelled("Отменено до начала обработки")
        job.result = job.work(job.cancel_event.is_set)
        with job.lock:
            if job.cancel_event.is_set() and job.status != DONE:
                job.status = CANCELLED
                job.error = "Отменено"
            else:
                job.status = DONE
    except Cancelled as e:
        job.error = str(e) or "Отменено"
        job.status = CANCELLED
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
            _worker = threading.Thread(target=_loop, name="transcriber-work", daemon=True)
            _worker.start()
        if _beater is None or not _beater.is_alive():
            _beater = threading.Thread(target=_beat_loop, name="transcriber-beat", daemon=True)
            _beater.start()


def new_id() -> str:
    """Номер задания, выдаваемый заранее — вызывающий код может передать его в свою
    работу (например, чтобы работа сама проверяла отмену по своему же номеру) без гонки
    между `submit()` и первым тактом очереди."""
    return uuid.uuid4().hex


def submit(login: str, record_id: int, work: Callable[[Callable[[], bool]], dict[str, Any]],
          beat: Callable[[], None] | None = None, job_id: str | None = None) -> Job:
    job = Job(id=job_id or new_id(), login=login, record_id=record_id, work=work, beat=beat)
    with _lock:
        _sweep()
        if len(_waiting) >= MAX_WAITING:
            raise Busy("Сейчас в очереди на распознавание слишком много записей."
                       " Попробуйте через несколько минут.")
        _jobs[job.id] = job
        _waiting.append(job.id)
    _ensure_threads()
    _line.put(job.id)
    return job


def get(job_id: str) -> Job | None:
    with _lock:
        return _jobs.get(job_id)


def set_stage(job_id: str, text: str) -> None:
    """Что сейчас происходит — видно человеку в индикаторе, пока задание в работе."""
    with _lock:
        job = _jobs.get(job_id)
    if job is not None:
        job.stage = text


def by_record(record_id: int) -> Job | None:
    """Активное или последнее задание по записи — страница восстанавливает индикатор."""
    with _lock:
        candidates = [j for j in _jobs.values() if j.record_id == record_id]
    if not candidates:
        return None
    active = [j for j in candidates if j.status in (QUEUED, RUNNING)]
    return (active or candidates)[-1]


def position(job: Job) -> int:
    with _lock:
        if job.id not in _waiting:
            return 0
        running = sum(1 for other in _jobs.values() if other.status == RUNNING)
        return _waiting.index(job.id) + running


def cancel(job_id: str) -> Job | None:
    """Попросить остановиться. Работник снимет прогон у «Искры» и остановит движок сам."""
    job = get(job_id)
    if job is None:
        return None
    if job.status not in (QUEUED, RUNNING):
        return job
    with job.lock:
        job.cancel_event.set()
        if job.status == QUEUED:
            # ещё не начали — закрываем сразу, работник её пропустит по флагу
            job.status = CANCELLED
            job.error = "Отменено"
            job.finished_at = time.monotonic()
    return get(job_id)


WORDS = {
    QUEUED: "Запись в очереди на обработку",
    RUNNING: "Идёт обработка",
}


def view(job: Job) -> dict[str, Any]:
    ahead = position(job) if job.status == QUEUED else 0
    message = job.stage or WORDS.get(job.status, "")
    if job.status == QUEUED and ahead:
        message += f": впереди {ahead}"
    out: dict[str, Any] = {
        "id": job.id,
        "status": job.status,
        "message": job.error if job.status in (ERROR, CANCELLED) else message,
        "position": ahead,
        "seconds": job.seconds(),
        "record_id": job.record_id,
    }
    if job.status == DONE:
        out.update(job.result)
    return out


def drain(timeout: float = 30.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with _lock:
            if not any(job.status in (QUEUED, RUNNING) for job in _jobs.values()):
                return True
        time.sleep(0.01)
    return False


def reset(timeout: float = 30.0) -> None:
    """Только для тестов: дождаться начатых заданий и забыть их."""
    with _lock:
        for job in _jobs.values():
            job.cancel_event.set()
    drain(timeout)
    with _lock:
        _jobs.clear()
        _waiting.clear()
