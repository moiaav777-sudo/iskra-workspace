#!/bin/bash
# Локальный стенд всего рабочего пространства (только разработка): портал, сервисы и шлюз-имитация nginx.
# Запускается из любой папки. Данные — во временной папке DEV_HOME (по умолчанию ~/.work-portal-dev),
# продуктив не затрагивается.
set -euo pipefail
W="$(cd "$(dirname "$0")/../.." && pwd)"
DEV_HOME="${DEV_HOME:-$HOME/.work-portal-dev}"
mkdir -p "$DEV_HOME/portal" "$DEV_HOME/brief" "$DEV_HOME/a3" "$DEV_HOME/agenda" "$DEV_HOME/theses" "$DEV_HOME/materials" "$DEV_HOME/transcriber" "$DEV_HOME/profiles"
export PORTAL_SECRET="dev-portal-secret" SECRET_KEY="dev-secret-key" PORTAL_INSECURE_COOKIES=1
export PORTAL_DATA="$DEV_HOME/portal" PORTAL_URL="http://127.0.0.1:8701"
export A3_KIT="$W/a3/a3-kit.html" BRIEF_HOME="$DEV_HOME/brief" BRIEF_NO_BROWSER=1 BRIEF_PORT=8932
# без этого «Повестка» и A3 пишут базы прямо в репозиторий (просьба чата «Повестки» от 18.09)
export AGENDA_DATA="$DEV_HOME/agenda" A3_DB="$DEV_HOME/a3/a3.db" THESES_DATA="$DEV_HOME/theses"
export MATERIALS_STORE="$DEV_HOME/materials/Хранилище" MATERIALS_DATA="$DEV_HOME/materials"
export TRANSCRIBER_HOME="$DEV_HOME/transcriber" PROFILES_HOME="$DEV_HOME/profiles"
export DEV_MODERATOR_PORT=8767 DEV_BRIEF_PORT=8932
# порт сервиса задаём только когда он уже написан: иначе шлюз слал бы запросы в никуда
[ -f "$W/a3/service/app/main.py" ] && export DEV_A3_PORT=8941
[ -f "$W/agenda/app/main.py" ] && export DEV_AGENDA_PORT=8951
[ -f "$W/theses/app/main.py" ] && export DEV_THESES_PORT=8961
[ -f "$W/materials/app/main.py" ] && export DEV_MATERIALS_PORT=8971
[ -f "$W/transcriber/app/main.py" ] && export DEV_TRANSCRIBER_PORT=8981
[ -f "$W/profiles/app/main.py" ] && export DEV_PROFILES_PORT=8991

# первый запуск: администратор dev / dev-password-1, сотрудник emp / emp-password-1
( cd "$W/portal" && .venv/bin/python -c "
from app import auth, db
if not db.get_user('dev'):
    db.create_user('dev', auth.hash_password('dev-password-1'), full_name='Тестов Тест Тестович', role='admin', must_change_password=False)
    db.create_user('emp', auth.hash_password('emp-password-1'), full_name='Сотрудникова Анна', services=['brief','a3'], must_change_password=False)
" )

pids=()
cleanup() { kill "${pids[@]}" 2>/dev/null || true; }
trap cleanup EXIT INT TERM

# Каждый сервис запускается из своей папки: python кладёт текущую папку в sys.path раньше --app-dir,
# и стенд, запущенный из чужой папки, поднимал бы чужой пакет app.
start_py() {   # start_py <папка> <порт> [интерпретатор]
  local dir="$1" port="$2" py="${3:-$1/.venv/bin/python}"
  [ -x "$py" ] || py="$W/portal/.venv/bin/python"
  ( cd "$dir" && exec "$py" -m uvicorn app.main:app --host 127.0.0.1 --port "$port" --log-level warning ) & pids+=($!)
}

start_py "$W/portal" 8701
start_py "$W/moderator" 8767
( cd "$W/brief" && exec python3 app/server.py ) & pids+=($!)
[ -n "${DEV_A3_PORT:-}" ] && start_py "$W/a3/service" "$DEV_A3_PORT"
[ -n "${DEV_AGENDA_PORT:-}" ] && start_py "$W/agenda" "$DEV_AGENDA_PORT"
[ -n "${DEV_THESES_PORT:-}" ] && start_py "$W/theses" "$DEV_THESES_PORT"
[ -n "${DEV_MATERIALS_PORT:-}" ] && start_py "$W/materials" "$DEV_MATERIALS_PORT"
[ -n "${DEV_TRANSCRIBER_PORT:-}" ] && start_py "$W/transcriber" "$DEV_TRANSCRIBER_PORT"
[ -n "${DEV_PROFILES_PORT:-}" ] && start_py "$W/profiles" "$DEV_PROFILES_PORT"
( cd "$W/portal" && exec .venv/bin/python scripts/dev_gateway.py ) & pids+=($!)
wait
