"""Общая обвязка тестов: временное хранилище и подставные заголовки портала."""
from __future__ import annotations

import importlib
import sys
from pathlib import Path
from urllib.parse import quote

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

SECRET = "test-secret"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    monkeypatch.setenv("MATERIALS_STORE", str(tmp_path / "Хранилище"))
    monkeypatch.setenv("MATERIALS_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("PORTAL_SECRET", SECRET)
    monkeypatch.setenv("PORTAL_URL", "http://127.0.0.1:1")  # журнал никуда не уходит

    from app import store, tree  # noqa: F401
    importlib.reload(store)
    importlib.reload(tree)
    from app import main
    importlib.reload(main)
    return TestClient(main.app)


def headers(login: str = "emp", role: str = "employee", name: str = "Сотрудник") -> dict:
    return {"X-Portal-Secret": SECRET, "X-Portal-User": login,
            "X-Portal-Role": role, "X-Portal-Name": quote(name)}


@pytest.fixture()
def emp() -> dict:
    return headers()


@pytest.fixture()
def admin() -> dict:
    return headers("admin", "admin", "Администратор")


@pytest.fixture()
def event(client, emp):
    """Заведённое мероприятие: с него начинается почти каждая проверка."""
    r = client.post("/api/events", headers=emp,
                    json={"date": "2026-09-02", "title": "Встреча с Музаевым"})
    assert r.status_code == 200, r.text
    return r.json()["event"]
