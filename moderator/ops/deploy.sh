#!/bin/bash
# Выкладка «Модератора» с Mac на домашний ПК.
#   ops/deploy.sh            — выложить текущий коммит (HEAD)
# Требования: чистое рабочее дерево, проходящие тесты, VPN до <адрес ПК>.
# Ключи выкладки (решение пользователя 28.09): id_winpc_deploy умеет только запустить
# deploy-scripts\moderator\remote_deploy.ps1 через общий шлюз deploy-scripts\deploy_gatekeeper.ps1
# на ПК — канал выполнения команд по-настоящему сужен и проверен вживую. id_winpc_sftp_moderator
# кладёт пакет в moderator-deploy, но это не песочница: -d на Windows не chroot, user на ПК
# администратор, ключ по праву записи фактически равен старому широкому (проверено вживую).
# Честная оценка — ../../portal/README.md → «Ключи выкладки».
# На ПК: резервная копия кода и базы -> замена файлов -> перезапуск -> проверка /healthz -> откат при сбое.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ROLLBACK=""; COMMIT=""         # --rollback <коммит> — вернуть стоявшую версию; --commit <коммит> — одобренный, без checkout
[ "${1:-}" = --rollback ] && ROLLBACK="${2:?коммит для отката}"
[ "${1:-}" = --commit ] && COMMIT="${2:?одобренный коммит}"
PC="user@<адрес ПК>"
# Разделение ключей (решение пользователя 28.09): sftp-ключ умеет только класть файлы в
# REMOTE_DIR, exec-ключ — только запускать remote_deploy.ps1 (шлюз ../../portal/ops/
# deploy_gatekeeper.ps1 на ПК, общий с portal/ops/deploy.sh). Прежний широкий id_winpc для
# автоматической выкладки больше не используется.
SFTP_OPTS=(-i "$HOME/.ssh/id_winpc_sftp_moderator" -o BatchMode=yes -o ConnectTimeout=45 -o ServerAliveInterval=15 -o LogLevel=ERROR)
SSH_OPTS=(-i "$HOME/.ssh/id_winpc_deploy" -o BatchMode=yes -o ConnectTimeout=45 -o ServerAliveInterval=15 -o LogLevel=ERROR)
REMOTE_DIR="C:/Users/user/moderator-deploy"

retry() {  # связь через VPN нестабильна — повторяем сетевые шаги
  local n=0
  until "$@"; do
    n=$((n + 1)); [ $n -ge 6 ] && { echo "!! не удалось: $*" >&2; return 1; }
    echo "   повтор $n..." >&2; sleep 8
  done
}

if [ -z "$COMMIT$ROLLBACK" ] && [ -n "$(git -C "$ROOT" status --porcelain)" ]; then
  echo "!! Есть незакоммиченные изменения — выкладывается только закоммиченный код." >&2
  git -C "$ROOT" status --short >&2
  exit 1
fi

# Ворота выкладки общего трека (решение пользователя 21.09): только одобренный release candidate.
# Коммит фиксируется один раз и выкладывается именно он (замечание ревьюера RC-2026-09-21-1, К2).
FULL="$(git -C "$ROOT" rev-parse "${ROLLBACK:-${COMMIT:-HEAD}}^{commit}")"
SHA="$(git -C "$ROOT" rev-parse --short "$FULL")"
source "$ROOT/../portal/ops/release_gate.sh"
release_clean_portal
release_gate moderator "$SHA" ${ROLLBACK:+rollback}
if [ -n "$ROLLBACK" ]; then
  echo "== Откат на $SHA — тесты этой версии прошли при её прошлой выкладке"
else
  echo "== Тесты $SHA"
  TROOT="$ROOT"
  if [ "$FULL" != "$(git -C "$ROOT" rev-parse HEAD)" ] || [ -n "$(git -C "$ROOT" status --porcelain)" ]; then
    TROOT="$(mktemp -d)"; git -C "$ROOT" archive "$FULL" | tar -x -C "$TROOT"   # тестируем ровно выкладываемое
    trap 'rm -rf "$TROOT"' EXIT
  fi
  "$ROOT/.venv/bin/python" -m pytest -q -p no:warnings --rootdir "$TROOT" "$TROOT/tests"
fi

echo "== Сборка релиза $SHA"
PKG="$(mktemp -d)/release-$SHA.tar.gz"
git -C "$ROOT" archive --format=tar.gz -o "$PKG" "$FULL"

echo "== Отправка на ПК"
retry scp -q "${SFTP_OPTS[@]}" "$PKG" "$PC:$REMOTE_DIR/"

echo "== Установка на ПК"
set +e                          # как в portal/deploy.sh: пустой вывод grep не должен скрыть итог выкладки
ssh "${SSH_OPTS[@]}" "$PC" "powershell -NoProfile -ExecutionPolicy Bypass -File <папка на ПК>\deploy-scripts\\moderator\\remote_deploy.ps1 -Sha $SHA" \
  | grep -v -E '^#< CLIXML|<Objs'
rc=${PIPESTATUS[0]}
set -e
rm -f "$PKG"
[ "$rc" = 0 ] && release_done moderator "$SHA"
exit "$rc"
