"""Пробный документ на выдуманных данных — посмотреть вёрстку глазами.

Реальные материалы для этого не нужны и не используются.

    .venv/bin/python tools/sample_docs.py [куда_положить]
"""
from __future__ import annotations

import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from app import docx_export  # noqa: E402
from app.models import ASIDE, Bullet, STRONG, Thesis  # noqa: E402


def sample() -> Thesis:
    thesis = Thesis(topic="О развитии школьных спортивных клубов", minutes=7,
                    event="совещание с регионами")
    bullets = [
        Bullet(text="Коллеги, добрый день. Тема сегодняшнего разговора — школьный спорт: "
                    "кто им занимается на местах и что мешает.", part="контекст"),
        Bullet(text="Сегодня клубы работают в 28 тысячах школ — это около 80 процентов "
                    "от общего числа.", emphasis=STRONG, part="масштаб"),
        Bullet(text="За два года их стало больше на 12 процентов.", part="динамика"),
        Bullet(text="Например, в одном из округов клуб открыли даже там, где нет "
                    "спортивного зала, — занимаются на базе колледжа.",
               emphasis=ASIDE, part="пример"),
        Bullet(text="При этом охват детей неравномерный: в сельских школах он ниже "
                    "[ТРЕБУЕТСЯ ФАКТУРА: охват в сельских школах, процентов].",
               part="проблема"),
        Bullet(text="Ставки тренеров закрыты не везде — "
                    "[ТРЕБУЕТСЯ ПЕРЕЧЕНЬ РЕГИОНОВ: где дефицит больше трети].",
               part="проблема"),
        Bullet(text="Динамика положительная. Но темпы работы необходимо повышать: "
                    "клуб без тренера — это вывеска, а не занятия.",
               emphasis=STRONG, part="вывод"),
        Bullet(text="Прошу глав регионов до конца года закрыть тренерские ставки "
                    "и доложить о результатах.", emphasis=STRONG, part="поручение"),
    ]
    thesis.bullets = bullets
    from app.models import sync_gaps_from_text
    sync_gaps_from_text(thesis)
    return thesis


def main() -> int:
    where = Path(sys.argv[1]) if len(sys.argv) > 1 else BASE / "data"
    where.mkdir(parents=True, exist_ok=True)
    thesis = sample()
    path = where / "Пробные тезисы.docx"
    path.write_bytes(docx_export.to_bytes(docx_export.build(thesis)))
    print(f"{path} — буллитов {len(thesis.bullets)}, "
          f"мест под фактуру {thesis.open_gaps}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
