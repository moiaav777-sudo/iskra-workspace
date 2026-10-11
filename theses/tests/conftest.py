"""Общая обвязка тестов: временная база и подставные заголовки портала."""
from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

SECRET = "test-secret"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    monkeypatch.setenv("THESES_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("PORTAL_SECRET", SECRET)
    monkeypatch.setenv("PORTAL_URL", "http://127.0.0.1:1")  # журнал никуда не уходит

    from app import models, portal, rules, storage  # noqa: F401
    importlib.reload(storage)
    from app import main
    importlib.reload(main)
    return TestClient(main.app)


def headers(login: str = "emp", role: str = "employee", name: str = "Сотрудник") -> dict:
    from urllib.parse import quote
    return {"X-Portal-Secret": SECRET, "X-Portal-User": login,
            "X-Portal-Role": role, "X-Portal-Name": quote(name)}


@pytest.fixture()
def emp() -> dict:
    return headers()


@pytest.fixture()
def admin() -> dict:
    return headers("admin", "admin", "Администратор")


@pytest.fixture()
def chief() -> dict:
    return headers("chief", "chief", "Руководитель")


@pytest.fixture(autouse=True)
def _no_leftover_jobs():
    """Фоновые задания одного теста не должны дописывать в базу следующего:
    после теста ждём, пока его потоки закончатся."""
    yield
    import threading
    import time
    deadline = time.time() + 10
    while time.time() < deadline:
        alive = [t for t in threading.enumerate() if t.name.startswith("theses-job-")]
        if not alive:
            return
        time.sleep(0.02)


def add_line(client, head: dict, tid: str, text: str = "", rev=None, emphasis: str = "normal"):
    """Дописать строку в конец текста — так, как это делает редактор: весь текст разом
    (`PUT /api/theses/{id}/text`) с той версией, которую передали."""
    current = client.get(f"/api/theses/{tid}", headers=head).json()["thesis"]["bullets"]
    lines = [{"id": b["id"], "text": b["text"], "emphasis": b["emphasis"]} for b in current]
    lines.append({"text": text, "emphasis": emphasis})
    return client.put(f"/api/theses/{tid}/text", headers=head,
                      json={"bullets": lines, "rev": rev})


def put_html(client, head: dict, tid: str, html: str, rev):
    """Как шлёт редактор: HTML целиком."""
    return client.put(f"/api/theses/{tid}/text", headers=head, json={"html": html, "rev": rev})


def put_lines(client, head: dict, tid: str, lines: list, rev):
    """Весь текст целиком: строки — это str или {"id", "text", "emphasis"}."""
    body = [line if isinstance(line, dict) else {"text": line} for line in lines]
    return client.put(f"/api/theses/{tid}/text", headers=head, json={"bullets": body, "rev": rev})
