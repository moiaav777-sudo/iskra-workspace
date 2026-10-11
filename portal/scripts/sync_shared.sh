#!/bin/bash
# Разложить общие модули по сервисам. Правим их только в portal/shared, копии не трогаем.
#   bash portal/scripts/sync_shared.sh          — разложить и показать, что изменилось
set -euo pipefail
W="$(cd "$(dirname "$0")/../.." && pwd)"
SRC="$W/portal/shared"

copy() {   # copy <файл> <куда>
  local file="$1" dest="$2"
  [ -d "$(dirname "$dest")" ] || return 0
  if [ -f "$dest" ] && cmp -s "$SRC/$file" "$dest"; then
    echo "  без изменений: $dest"
  else
    cp "$SRC/$file" "$dest"
    echo "  обновлено:     $dest"
  fi
}

echo "== общий разговор с «Искрой»"
for svc in moderator brief agenda theses profiles; do copy iskra_common.py "$W/$svc/app/iskra_common.py"; done
copy iskra_common.py "$W/a3/service/app/iskra_common.py"

echo "== общая сверка чисел"
for svc in brief theses; do copy numbers_common.py "$W/$svc/app/numbers_common.py"; done

echo "== короткое название совещания (считает «Модератор», остальные показывают его short_title)"
copy meeting_title.py "$W/moderator/app/meeting_title.py"

echo "== склонение ФИО (документ ведущего и заголовок саммари)"
copy names_common.py "$W/moderator/app/names_common.py"
copy names_common.py "$W/brief/app/names_common.py"
copy names_common.py "$W/profiles/app/names_common.py"

echo "== распознавание речи (аудио → текст, faster-whisper + подмена для тестов)"
copy speech_common.py "$W/transcriber/app/speech_common.py"

echo "== перечень поручений в Word (эталон «Перечень поручений 25.05.2026»; подключит и «Совещание»)"
copy orders_docx.py "$W/transcriber/app/orders_docx.py"
copy orders_docx.py "$W/moderator/app/orders_docx.py"

echo "== чтение документов: Word, Excel, PowerPoint, PDF, rtf, odt, txt (решение пользователя 05.10)"
for svc in brief theses moderator agenda profiles; do copy docs_common.py "$W/$svc/app/docs_common.py"; done
copy docs_common.py "$W/a3/service/app/docs_common.py"

echo
echo "Готово. Файлы правятся в portal/shared — копии в сервисах перезаписываются."
