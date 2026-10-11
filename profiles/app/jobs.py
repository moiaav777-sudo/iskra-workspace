"""Сбор объективки «Искрой» — фоном, с очередью.

Нажатие заводит задание и сразу отвечает; страница опрашивает карточку и показывает шаг
(«в очереди», «Искра ищет в открытых источниках…», «Проверяю ответ…»), сколько прошло,
и даёт «Отменить». Одновременно идёт не больше двух прогонов на сервис (`iskra.SLOTS`):
пакетная переработка десяти справок разбирается по очереди малыми партиями, а не одним запросом.

Состояние дублируется в базе (`status`, `status_text`): перезапуск сервиса обрывает поток,
и при запуске такие карточки помечаются прерванными (`db.init`).
"""
from __future__ import annotations

import threading
import time
import traceback
import uuid
from dataclasses import dataclass, field
from typing import Callable

from . import db, iskra

QUEUED, RUNNING, DONE, ERROR, CANCELLED = "queued", "running", "done", "error", "cancelled"


@dataclass
class Job:
    id: str
    profile_id: str
    login: str
    status: str = QUEUED
    stage: str = "В очереди к «Искре»"
    error: str = ""
    created: float = field(default_factory=time.monotonic)
    started: float = 0.0
    finished: float = 0.0
    cancel_event: threading.Event = field(default_factory=threading.Event)

    def seconds(self) -> int:
        if not self.started:
            return 0
        return int((self.finished or time.monotonic()) - self.started)


_jobs: dict[str, Job] = {}            # карточка → последнее задание
_lock = threading.Lock()


def view(job: Job | None) -> dict | None:
    if job is None:
        return None
    return {"id": job.id, "status": job.status, "stage": job.stage, "error": job.error,
            "seconds": job.seconds(), "active": job.status in (QUEUED, RUNNING),
            "deadline": int(iskra.DEADLINE)}


def of(profile_id: str) -> Job | None:
    with _lock:
        return _jobs.get(profile_id)


def active(profile_id: str) -> bool:
    job = of(profile_id)
    return bool(job and job.status in (QUEUED, RUNNING))


def start(profile_id: str, login: str, work: Callable[[Job, iskra.Control], None]) -> tuple[Job, bool]:
    """Завести задание по карточке. Уже идёт — вернуть его же (второе нажатие ничего не плодит)."""
    with _lock:
        cur = _jobs.get(profile_id)
        if cur and cur.status in (QUEUED, RUNNING):
            return cur, False
        job = Job(id=uuid.uuid4().hex[:12], profile_id=profile_id, login=login)
        _jobs[profile_id] = job
    db.set_fields(profile_id, status=db.QUEUED, status_text="")
    threading.Thread(target=_run, args=(job, work), name=f"profiles-job-{job.id}", daemon=True).start()
    return job, True


def _run(job: Job, work: Callable[[Job, iskra.Control], None]) -> None:
    # ждём свободного места у «Искры», откликаясь на отмену
    while not iskra.SLOTS.acquire(timeout=0.25):
        if job.cancel_event.is_set():
            job.status, job.error, job.finished = CANCELLED, "Сбор отменён", time.monotonic()
            db.set_fields(job.profile_id, status=CANCELLED, status_text="Сбор отменён до начала")
            return
    try:
        job.status, job.started = RUNNING, time.monotonic()
        db.set_fields(job.profile_id, status=db.RUNNING)

        def stage(text: str) -> None:
            job.stage = text

        ctl = iskra.Control(on_stage=stage, cancelled=job.cancel_event.is_set)
        work(job, ctl)
        if job.status == RUNNING:
            job.status = DONE
    except Exception as error:  # noqa: BLE001 — ошибка видна человеку, сервис не падает
        job.status, job.error = ERROR, str(error) or type(error).__name__
        traceback.print_exc()
    finally:
        job.finished = time.monotonic()
        iskra.SLOTS.release()


def cancel(profile_id: str) -> Job | None:
    job = of(profile_id)
    if job and job.status in (QUEUED, RUNNING):
        job.cancel_event.set()
    return job


def drain(timeout: float = 20.0) -> bool:
    """Тестам: дождаться, пока все задания закончатся."""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        with _lock:
            if not any(j.status in (QUEUED, RUNNING) for j in _jobs.values()):
                return True
        time.sleep(0.02)
    return False


def reset() -> None:
    with _lock:
        for job in _jobs.values():
            job.cancel_event.set()
    drain()
    with _lock:
        _jobs.clear()
