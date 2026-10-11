#!/bin/bash
# Рендер A3-листа через headless Chrome: HTML -> PDF + PNG для самопроверки.
#
#   render.sh <input.html> [outdir]
#
# Кладёт рядом <имя>.pdf (печать) и <имя>.png (2x, для визуального контроля)
# и ПРОВЕРЯЕТ, что вышел ровно один лист формата A3. Если страниц больше одной
# или формат не A3 — значит в HTML нет @page/фиксированного холста либо контент
# вылез за обрез. Скрипт падает с понятной ошибкой: чинить надо HTML.
#
# Имя скрипта стабильное — одного разрешения хватает на все перезапуски.

set -uo pipefail

# Chrome ищется по обычным местам; переопределить — переменной CHROME.
if [ -z "${CHROME:-}" ]; then
  for c in "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
           "/Applications/Chromium.app/Contents/MacOS/Chromium" \
           "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge" \
           "$(command -v google-chrome 2>/dev/null)" \
           "$(command -v chromium 2>/dev/null)"; do
    [ -n "$c" ] && [ -x "$c" ] && CHROME="$c" && break
  done
fi
if [ -z "${CHROME:-}" ]; then
  echo "ОШИБКА: не найден Chrome. Установите Google Chrome или задайте CHROME=/путь" >&2
  exit 1
fi

IN="${1:?нужен путь к html}"
OUTDIR="${2:-$(dirname "$IN")}"
BASE="$(basename "${IN%.html}")"

case "$IN" in /*) ABS="$IN" ;; *) ABS="$PWD/$IN" ;; esac
URL="file://$(python3 -c 'import sys,urllib.parse;print(urllib.parse.quote(sys.argv[1]))' "$ABS")"

PROFILE="$(mktemp -d)"
trap 'rm -rf "$PROFILE"' EXIT

COMMON=(--headless=new --disable-gpu --no-sandbox --no-first-run
        --disable-extensions --disable-background-networking
        --user-data-dir="$PROFILE" --allow-file-access-from-files
        --hide-scrollbars --force-color-profile=srgb)

# Headless Chrome часто не выходит сам, хотя файл уже записан. Поэтому ждём
# не выхода процесса, а готовности результата: файл появился и перестал расти
# (для --dump-dom — в выводе есть </html>). Жёсткий предел — secs секунд.
# GUARD_OUT — куда писать stdout команды; GUARD_FILE — файл-результат.
run_guarded() {
  local secs="$1"; shift
  local out="${GUARD_OUT:-/dev/null}" file="${GUARD_FILE:-}"
  "$@" >"$out" 2>/dev/null &
  local pid=$! t=0 last=-1 size
  while kill -0 "$pid" 2>/dev/null && [ "$t" -lt $((secs * 4)) ]; do
    sleep 0.25; t=$((t + 1))
    if [ -n "$file" ] && [ -s "$file" ]; then
      size=$(wc -c <"$file" 2>/dev/null)
      [ "$size" = "$last" ] && break
      last="$size"
    elif [ "$out" != /dev/null ] && grep -q "</html>" "$out" 2>/dev/null; then
      sleep 0.25; break
    fi
  done
  kill -9 "$pid" 2>/dev/null
  wait "$pid" 2>/dev/null
}

rm -f "$OUTDIR/$BASE.pdf" "$OUTDIR/$BASE.png"

GUARD_FILE="$OUTDIR/$BASE.pdf" run_guarded 60 "$CHROME" "${COMMON[@]}" --virtual-time-budget=5000 \
  --no-pdf-header-footer --print-to-pdf-no-header \
  --print-to-pdf="$OUTDIR/$BASE.pdf" "$URL"

# окно под ориентацию, которую выбрал a3.js (видна по MediaBox в PDF)
WIN="1123,1587"
if python3 -c 'import re,sys; m=re.search(rb"/MediaBox\s*\[\s*[-\d.]+\s+[-\d.]+\s+([-\d.]+)\s+([-\d.]+)", open(sys.argv[1],"rb").read()); sys.exit(0 if m and float(m.group(1)) > float(m.group(2)) else 1)' "$OUTDIR/$BASE.pdf" 2>/dev/null; then
  WIN="1587,1123"
fi

# A3 @ 96dpi = 1123 x 1587 px; scale 2 -> читаемый скрин для самопроверки
GUARD_FILE="$OUTDIR/$BASE.png" run_guarded 60 "$CHROME" "${COMMON[@]}" --virtual-time-budget=5000 \
  --window-size="$WIN" --force-device-scale-factor=2 \
  --screenshot="$OUTDIR/$BASE.png" "$URL"

[ -s "$OUTDIR/$BASE.pdf" ] || { echo "ОШИБКА: PDF не создан ($URL)"; exit 1; }

# Панель самопроверки, которую нарисовал a3.js: обрезанные подписи,
# переполненные блоки, вылезший за обрез контент.
DOM="$(mktemp)"
GUARD_OUT="$DOM" run_guarded 60 "$CHROME" "${COMMON[@]}" --virtual-time-budget=5000 \
  --dump-dom "$URL"
python3 - "$DOM" "$ABS" <<'PY'
import re, sys
dom = open(sys.argv[1], encoding="utf-8", errors="replace").read()
src = open(sys.argv[2], encoding="utf-8", errors="replace").read()
uses_js = 'id="a3-data"' in src or "A3.render" in src
m = re.search(r'<div class="a3-report[^"]*">(.*?)</div>', dom, re.S)
if not m:
    # Лист собран через a3.js, а панели нет — проверка не выполнилась.
    # Молчать нельзя: иначе «ноль замечаний» и «проверка не запускалась»
    # выглядят одинаково.
    if uses_js:
        print("САМОПРОВЕРКА: НЕ ВЫПОЛНЕНА — a3.js не отработал или DOM не снят"
              f" (получено {len(dom)} байт)")
    sys.exit(0)
import html
body = html.unescape(re.sub(r"<[^>]+>", "\n", m.group(1)))
lines = [l.strip() for l in body.split("\n") if l.strip()]
if lines and lines[0].startswith("Проверка пройдена"):
    print("САМОПРОВЕРКА: ок — замечаний нет")
else:
    print("САМОПРОВЕРКА: " + (lines[0] if lines else "—"))
    for l in lines[1:]:
        print("   ", l) if l.startswith("A3 ") else print("  !", l)
PY
rm -f "$DOM"

python3 - "$OUTDIR/$BASE.pdf" <<'PY'
import re, sys
raw = open(sys.argv[1], 'rb').read()
pages = len(re.findall(rb'/Type\s*/Page[^s]', raw))
boxes = {b.decode('latin1') for b in re.findall(rb'/MediaBox\s*\[[^\]]*\]', raw)}

def wh(box):
    nums = [float(x) for x in re.findall(r'-?[\d.]+', box)]
    return (abs(nums[2] - nums[0]), abs(nums[3] - nums[1])) if len(nums) == 4 else (0, 0)

def is_a3(box):   # A3 в любой ориентации, pt
    w, h = sorted(wh(box))
    return abs(w - 841.89) < 3 and abs(h - 1190.55) < 3

ok = pages == 1 and boxes and all(is_a3(b) for b in boxes)
print(f"страниц: {pages}   MediaBox: {', '.join(sorted(boxes)) or '—'}")
if ok:
    w, h = wh(next(iter(boxes)))
    print("ПРОВЕРКА ФОРМАТА: ок — один лист A3 " + ("горизонтальный" if w > h else "вертикальный"))
else:
    print("ПРОВЕРКА ФОРМАТА: ПРОВАЛЕНА")
    if pages != 1:
        print(f"  ! страниц {pages}, а должна быть 1 — контент вылез за холст")
        print("    или у .sheet не зафиксирована height: 420mm")
    if not all(is_a3(b) for b in boxes):
        print("  ! формат не A3 (ожидается 841.89 x 1190.55 pt в любой ориентации)")
        print("    проверь, что в CSS есть @page { size: A3 portrait; margin: 0 }")
    sys.exit(2)
PY
rc=$?

echo "PDF: $OUTDIR/$BASE.pdf"
[ -s "$OUTDIR/$BASE.png" ] && echo "PNG: $OUTDIR/$BASE.png" || echo "PNG: не создан"
exit $rc
