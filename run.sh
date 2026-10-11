#!/bin/bash
# Запустить всё рабочее пространство одной командой (портал и все сервисы на одном адресе).
#
#   ./run.sh                 — поставить окружения, завести администратора, запустить всё; Ctrl+C останавливает
#   ./run.sh --install       — только поставить окружения (нужно для сборки контейнера)
#   ./run.sh --no-install    — только запустить (окружения уже стоят)
#
# Нужны: Linux или macOS, Python 3.11+ (лучше 3.12), доступ в сеть для pip при первой установке.
# Настройки — в файле .env в корне (образец — .env.example): ключ «Искры», порт, логин и пароль администратора.
# Без ключа «Искры» всё открывается и работает, кроме самих обращений к ИИ; ключ можно ввести и позже,
# на странице «Администрирование → Искра» (портал разошлёт его сервисам сам).
#
# Как устроено: каждый сервис — отдельный процесс uvicorn на 127.0.0.1 (порты ниже), перед ними шлюз
# portal/scripts/dev_gateway.py на одном порту (по умолчанию 8080): он спрашивает портал, кто вошёл,
# и передаёт сервисам проверенного пользователя заголовками X-Portal-* с общим секретом — ровно то, что
# на боевом делает nginx (portal/ops/nginx_work.conf). Данные — в ./data (у «Совещания» — moderator/data).
set -euo pipefail
W="$(cd "$(dirname "$0")" && pwd)"
cd "$W"

INSTALL=1; START=1
case "${1:-}" in
  --install) START=0 ;;
  --no-install) INSTALL=0 ;;
  "") ;;
  *) echo "использование: $0 [--install|--no-install]" >&2; exit 2 ;;
esac

# ── настройки из .env (не из окружения оболочки: так запуск воспроизводим) ──────────────────────────
ENV_FILE="${ENV_FILE:-$W/.env}"      # в контейнере — /app/data/.env (переживает пересборку образа)
[ -f "$ENV_FILE" ] || { mkdir -p "$(dirname "$ENV_FILE")"; cp "$W/.env.example" "$ENV_FILE"; echo "   создан $ENV_FILE из .env.example"; }
getenv() {   # значение из .env; пусто — из окружения (docker compose передаёт HOST/PORT/ISKRA_API_KEY так)
  local v; v="$({ grep -E "^$1=" "$ENV_FILE" || true; } | tail -1 | cut -d= -f2- | sed 's/^["'"'"']//; s/["'"'"']$//')"
  [ -n "$v" ] && { printf '%s' "$v"; return; }
  printf '%s' "${!1:-}"
}
setenv() {   # setenv ИМЯ значение — дописать или заменить строку в .env
  if grep -qE "^$1=" "$ENV_FILE"; then
    python3 - "$ENV_FILE" "$1" "$2" <<'PY'
import sys, pathlib, re
p, k, v = pathlib.Path(sys.argv[1]), sys.argv[2], sys.argv[3]
s = p.read_text(encoding="utf-8")
p.write_text(re.sub(rf"^{re.escape(k)}=.*$", f"{k}={v}", s, flags=re.M), encoding="utf-8")
PY
  else
    printf '%s=%s\n' "$1" "$2" >> "$ENV_FILE"
  fi
}
rand() { python3 -c "import secrets; print(secrets.token_urlsafe($1))"; }

PORT="$(getenv PORT)"; PORT="${PORT:-8080}"
HOST="$(getenv HOST)"; HOST="${HOST:-127.0.0.1}"
ADMIN_LOGIN="$(getenv ADMIN_LOGIN)"; ADMIN_LOGIN="${ADMIN_LOGIN:-admin}"
ADMIN_PASSWORD="$(getenv ADMIN_PASSWORD)"
[ -n "$ADMIN_PASSWORD" ] || { ADMIN_PASSWORD="$(rand 9)"; setenv ADMIN_PASSWORD "$ADMIN_PASSWORD"; }
# демонстрационный сотрудник (пользовательская часть: все сервисы открыты); пустой логин — не заводить
USER_LOGIN="$(getenv USER_LOGIN)"
USER_PASSWORD="$(getenv USER_PASSWORD)"
PORTAL_SECRET="$(getenv PORTAL_SECRET)"
[ -n "$PORTAL_SECRET" ] || { PORTAL_SECRET="$(rand 24)"; setenv PORTAL_SECRET "$PORTAL_SECRET"; }
SECRET_KEY="$(getenv SECRET_KEY)"
[ -n "$SECRET_KEY" ] || { SECRET_KEY="$(rand 24)"; setenv SECRET_KEY "$SECRET_KEY"; }
ISKRA_API_KEY="$(getenv ISKRA_API_KEY)"
ISKRA_URL="$(getenv ISKRA_URL)"; ISKRA_URL="${ISKRA_URL:-https://cloud.iskrabot.ru}"
PUBLIC_URL="$(getenv PUBLIC_URL)"; PUBLIC_URL="${PUBLIC_URL:-http://localhost:$PORT}"

# ── Python и окружения сервисов ────────────────────────────────────────────────────────────────────
PY=""
for c in python3.12 python3.13 python3.11 python3; do
  if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then PY="$c"; break; fi
done
[ -n "$PY" ] || { echo "!! нужен Python 3.11 или новее" >&2; exit 1; }

# сервис → папка с requirements.txt и приложением app.main:app (у «Брифа» свой сервер без зависимостей)
SERVICES=(portal moderator a3/service theses materials transcriber profiles)
declare -A PORTS=([portal]=8701 [moderator]=8767 [a3/service]=8941 [theses]=8961 [materials]=8971 [transcriber]=8981 [profiles]=8991)
BRIEF_PORT=8932

venv_py() { echo "$W/$1/.venv/bin/python"; }

if [ "$INSTALL" = 1 ]; then
  echo "== окружения ($($PY --version))"
  for svc in "${SERVICES[@]}"; do
    d="$W/$svc"
    if [ ! -x "$d/.venv/bin/python" ]; then
      if command -v uv >/dev/null 2>&1; then uv venv -q -p "$PY" "$d/.venv"; else "$PY" -m venv "$d/.venv"; fi
    fi
    stamp="$d/.venv/.requirements.sha"
    want="$(sha256sum "$d/requirements.txt" | cut -c1-16)"
    if [ ! -f "$stamp" ] || [ "$(cat "$stamp")" != "$want" ]; then
      echo "   $svc: зависимости…"
      if command -v uv >/dev/null 2>&1; then uv pip install -q -p "$d/.venv/bin/python" -r "$d/requirements.txt"
      else "$d/.venv/bin/python" -m pip install -q --disable-pip-version-check -r "$d/requirements.txt"; fi
      echo "$want" > "$stamp"
    else
      echo "   $svc: готово"
    fi
  done
fi
[ "$START" = 1 ] || { echo "OK: окружения поставлены"; exit 0; }

# ── данные и переменные сервисов ───────────────────────────────────────────────────────────────────
DATA="$W/data"
mkdir -p "$DATA"/{portal,brief,a3,theses,materials,transcriber,profiles,logs}
export PORTAL_SECRET SECRET_KEY ISKRA_URL
export PORTAL_INSECURE_COOKIES=1                     # без https cookie не Secure (за своим https-прокси уберите)
export PORTAL_DATA="$DATA/portal" PORTAL_URL="http://127.0.0.1:${PORTS[portal]}" PORTAL_PUBLIC_URL="$PUBLIC_URL"
export A3_KIT="$W/a3/a3-kit.html" A3_DB="$DATA/a3/a3.db" A3_HOME="$W/a3/service"
export BRIEF_HOME="$DATA/brief" BRIEF_NO_BROWSER=1 BRIEF_PORT="$BRIEF_PORT"
export THESES_DATA="$DATA/theses" THESES_HOME="$W/theses"
export MATERIALS_STORE="$DATA/materials/Хранилище" MATERIALS_DATA="$DATA/materials" MATERIALS_HOME="$W/materials"
export TRANSCRIBER_HOME="$DATA/transcriber" PROFILES_HOME="$DATA/profiles" MODERATOR_DIR="$W/moderator"
export DEV_PORTAL_PORT="${PORTS[portal]}" DEV_MODERATOR_PORT="${PORTS[moderator]}" DEV_BRIEF_PORT="$BRIEF_PORT"
export DEV_A3_PORT="${PORTS[a3/service]}" DEV_THESES_PORT="${PORTS[theses]}" DEV_MATERIALS_PORT="${PORTS[materials]}"
export DEV_TRANSCRIBER_PORT="${PORTS[transcriber]}" DEV_PROFILES_PORT="${PORTS[profiles]}"
export DEV_GATEWAY_PORT="$PORT" DEV_GATEWAY_HOST="$HOST"
# ключ «Искры»: из .env — всем сервисам сразу; пусто — вводится в портале, который разложит его по .env сервисов
[ -n "$ISKRA_API_KEY" ] && export ISKRA_API_KEY

# ── учётные записи из .env: администратор и демонстрационный сотрудник ─────────────────────────────
# Пароли каждый запуск приводятся к .env (стенд должен открываться тем, что написано в файле); сотрудник
# получает доступ ко всем сервисам. Остальных людей заводит администратор в портале.
( cd "$W/portal" && "$(venv_py portal)" - "$ADMIN_LOGIN" "$ADMIN_PASSWORD" "$USER_LOGIN" "$USER_PASSWORD" <<'PY'
import sys
from app import auth, db, config
admin, admin_pw, user, user_pw = sys.argv[1:5]
def ensure(login, pw, role, name, services):
    if not login or not pw: return
    if db.get_user(login):
        db.update_user(login, password_hash=auth.hash_password(pw), must_change_password=False, active=True)
        print(f"   {name}: {login} (пароль из .env)")
    else:
        db.create_user(login, auth.hash_password(pw), full_name=name, role=role, services=services, must_change_password=False)
        print(f"   {name} создан: {login}")
ensure(admin, admin_pw, "admin", "Администратор", None)
ensure(user, user_pw, "employee", "Сотрудник", list(config.SERVICE_IDS))
PY
)

# ── запуск ─────────────────────────────────────────────────────────────────────────────────────────
pids=()
cleanup() { echo; echo "== останавливаю…"; kill "${pids[@]}" 2>/dev/null || true; wait 2>/dev/null || true; }
trap cleanup EXIT INT TERM
start_py() {   # start_py <папка> <порт>: каждый сервис из своей папки (python кладёт её в sys.path первой)
  local dir="$1" port="$2" name; name="$(basename "$dir")"
  ( cd "$W/$dir" && exec "$(venv_py "$dir")" -m uvicorn app.main:app --host 127.0.0.1 --port "$port" --log-level warning ) \
    >>"$DATA/logs/$name.log" 2>&1 & pids+=($!)
}
echo "== запуск"
for svc in "${SERVICES[@]}"; do start_py "$svc" "${PORTS[$svc]}"; done
( cd "$W/brief" && exec "$(venv_py portal)" app/server.py ) >>"$DATA/logs/brief.log" 2>&1 & pids+=($!)
( cd "$W/portal" && exec "$(venv_py portal)" scripts/dev_gateway.py ) >>"$DATA/logs/gateway.log" 2>&1 & pids+=($!)

# ждём портал и шлюз
for i in $(seq 1 60); do
  if "$(venv_py portal)" - "$PORT" <<'PY' 2>/dev/null
import sys, urllib.request
urllib.request.urlopen(f"http://127.0.0.1:{sys.argv[1]}/healthz", timeout=2).read(); print("ok")
PY
  then break; fi
  sleep 1
done
echo
echo "Готово. Рабочее пространство: http://${HOST}:${PORT}"
echo "   администратор: ${ADMIN_LOGIN} / ${ADMIN_PASSWORD}   (люди, доступы, ключ «Искры», журнал; документы не правит)"
[ -n "$USER_LOGIN" ] && [ -n "$USER_PASSWORD" ] && echo "   сотрудник:     ${USER_LOGIN} / ${USER_PASSWORD}   (работа во всех сервисах)"
[ -n "$ISKRA_API_KEY" ] || echo "Ключ «Искры» не задан: впишите ISKRA_API_KEY в .env и перезапустите, или введите его в «Администрирование → Искра»."
echo "Журналы: $DATA/logs/*.log. Остановить: Ctrl+C."
wait
