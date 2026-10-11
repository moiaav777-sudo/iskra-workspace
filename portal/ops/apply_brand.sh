#!/bin/bash
# Переключить фирменный стиль рабочего пространства: bash portal/ops/apply_brand.sh <default|iskra> [--check]
#
# Тема — папка portal/static/brand/themes/<тема>/:
#   tokens.css   — палитра, шрифт и цвета общей шапки; встаёт в portal/static/base.css между маркерами
#                  BRAND-TOKENS (его подключают портал и все сервисы, поэтому цвета меняются везде разом);
#   mark.svg     — знак для тёмной шапки, mark-dark.svg — для светлых поверхностей,
#   wordmark.svg — словомарка на странице входа, favicon.svg — значок вкладки;
#   fonts.txt    — семейство для ссылки Google Fonts в страницах (у «Искры» — DM Sans).
# Активные файлы: portal/static/brand/{mark,mark-dark,wordmark}.svg и portal/static/favicon.svg.
# Все остальные правила сервисов остаются — они построены на токенах base.css (решение пользователя 18.09).
# После смены темы версии стилей в страницах портала проставляет scripts/stamp_static.py.
# --check — только сказать, какая тема стоит сейчас, ничего не менять (код возврата 0).
set -euo pipefail
W="$(cd "$(dirname "$0")/../.." && pwd)"
THEME="${1:-}"; MODE="${2:-}"
THEMES="$W/portal/static/brand/themes"
BASE="$W/portal/static/base.css"
ACTIVE="$W/portal/static/brand"

current() {   # какая тема стоит: по первой строке блока токенов
  local line; line="$(sed -n '/BRAND-TOKENS:start/,/BRAND-TOKENS:end/p' "$BASE" | grep -m1 -oE 'Тема «[^»]+»' || true)"
  case "$line" in *по\ умолчанию*) echo default ;; *Искра*) echo iskra ;; *) echo "неизвестно" ;; esac
}
if [ "$THEME" = "--check" ] || [ -z "$THEME" ]; then echo "тема сейчас: $(current)"; [ -n "$THEME" ] && exit 0; echo "использование: $0 <default|iskra>"; exit 2; fi
[ -d "$THEMES/$THEME" ] || { echo "!! нет темы '$THEME' (есть: $(ls "$THEMES" | tr '\n' ' '))" >&2; exit 2; }
if [ "$MODE" = "--check" ]; then [ "$(current)" = "$THEME" ] && echo "тема $THEME уже стоит" || echo "стоит $(current), просили $THEME"; exit 0; fi

# 1. токены в base.css между маркерами
python3 - "$BASE" "$THEMES/$THEME/tokens.css" <<'PY'
import sys, pathlib
base, tokens = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])
s = base.read_text(encoding="utf-8")
a = s.index("/* === BRAND-TOKENS:start"); a = s.index("*/\n", a) + 3
b = s.index("/* === BRAND-TOKENS:end === */")
s = s[:a] + tokens.read_text(encoding="utf-8").rstrip("\n") + "\n" + s[b:]
base.write_text(s, encoding="utf-8")
PY

# 2. знаки и значок
for f in mark.svg mark-dark.svg wordmark.svg; do cp "$THEMES/$THEME/$f" "$ACTIVE/$f"; done
cp "$THEMES/$THEME/favicon.svg" "$W/portal/static/favicon.svg"

# 3. ссылка на Google Fonts в страницах портала и сервисов (семейство из fonts.txt темы)
FAM="$(tr -d '\n' < "$THEMES/$THEME/fonts.txt")"
python3 - "$W" "$FAM" <<'PY'
import sys, re, pathlib
w, fam = pathlib.Path(sys.argv[1]), sys.argv[2]
pages = list(w.glob("portal/static/*.html")) + [w / "brief/app/ui.html"] + \
        [p for p in w.glob("*/static/index.html")] + [w / "a3/service/app/static/library.html"]
rx = re.compile(r'(fonts\.googleapis\.com/css2\?family=)[^"&]+')
n = 0
for p in pages:
    if not p.exists(): continue
    s = p.read_text(encoding="utf-8"); t = rx.sub(lambda m: m.group(1) + fam, s)
    if t != s: p.write_text(t, encoding="utf-8"); n += 1
print(f"   шрифт {fam}: страниц обновлено {n}")
PY

# 4. версии стилей в страницах портала
( cd "$W/portal" && python3 scripts/stamp_static.py >/dev/null 2>&1 || true )
echo "OK: тема $THEME применена (base.css, знаки, favicon, шрифты)"
