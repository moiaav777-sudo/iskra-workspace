# Модератор совещаний

Веб-приложение для подготовки модератор-брифа: повестка, участники, материалы, сценарий, поручения → выгрузка HTML. Документы разбирает Искра.

Адрес: https://workspace.example.ru/moderator/ (старый <наш домен> перенаправляет туда).

Вход, пользователи, права и журнал действий живут в портале рабочего пространства (`../portal`). Сюда nginx передаёт проверенного пользователя в заголовках `X-Portal-*` с общим секретом `PORTAL_SECRET` (в `.env`). Все сотрудники видят и правят все совещания; удалить навсегда может только администратор; каждое изменение уходит в журнал портала.

## Где что работает

| Узел | Что там | Доступ |
|---|---|---|
| Домашний ПК `DESKTOP-AVGVPUT` | приложение, база, логи — `C:\Users\user\moderator` | SSH `user@<адрес ПК>` (VPN); выкладка — разделённые ключи `id_winpc_sftp_moderator`/`id_winpc_deploy` (см. `../portal/ops/deploy.sh`), ручная диагностика — `id_winpc` |
| VPS `<другой домен>` | nginx workspace.example.ru, конец обратного туннеля портала (127.0.0.1:8765) | см. `../portal/README.md` |

Задачи планировщика на ПК:
- `ModeratorServer` — `start.bat`, uvicorn на 127.0.0.1:8765;
- туннель и сторож — общие для рабочего пространства: `PortalTunnel`, `PortalWatchdog` (старые `ModeratorTunnel`, `ModeratorWatchdog` выключены);
- `ModeratorBackup` — `ops\backup.ps1` ежедневно, копии базы в `C:\Users\user\moderator-backups`.

Не в git и не трогаются выкладкой: `data\` (база), `.env` (ключ Искры, PORTAL_SECRET), `.venv\`, `logs\`, `id_vps`.

## Разработка

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
.venv/bin/python -m uvicorn app.main:app --port 8766
```

Вместе с порталом и «Брифом»: `bash ../portal/scripts/dev_stack.sh` → http://127.0.0.1:8080

## Выкладка

```bash
ops/deploy.sh
```

Выкладывает закоммиченный HEAD: тесты → архив → на ПК копия базы и кода → замена файлов → перезапуск → проверка `/healthz` → при сбое автоматический откат. Версия на сервере — файл `VERSION`.

Логи на ПК: `logs\server.log`, `logs\access.log`, `logs\watchdog.log`, `logs\tunnel.log`, `logs\console.log` (аварийный вывод).
