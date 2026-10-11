"""Проставить версии стилям и скриптам портала: /static/portal.css?v=<хеш файла>.

Браузер обязан перечитать файл, если он изменился, и может держать его в кэше, пока он тот же.
Запускается сам при выкладке; вручную: portal/.venv/bin/python scripts/stamp_static.py
"""
from __future__ import annotations

import hashlib
import re
import sys
from pathlib import Path

STATIC = Path(__file__).resolve().parent.parent / "static"
REF = re.compile(r'(href|src)="/static/([A-Za-z0-9_.-]+\.(?:css|js))(?:\?v=[^"]*)?"')


def main() -> int:
    changed = []
    for page in sorted(STATIC.glob("*.html")):
        text = page.read_text(encoding="utf-8")

        def stamp(m: re.Match) -> str:
            attr, name = m.group(1), m.group(2)
            f = STATIC / name
            if not f.exists():
                return m.group(0)
            ver = hashlib.md5(f.read_bytes()).hexdigest()[:8]
            return f'{attr}="/static/{name}?v={ver}"'

        new = REF.sub(stamp, text)
        if new != text:
            page.write_text(new, encoding="utf-8")
            changed.append(page.name)
    print("обновлены версии в:", ", ".join(changed) if changed else "нет изменений")
    return 1 if changed else 0


if __name__ == "__main__":
    sys.exit(main())
