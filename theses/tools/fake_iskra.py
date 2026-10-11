"""Запустить сервис с подменным ответом «Искры» — показать работу, пока её нет.

Зачем: «Искра» сейчас отвечает ошибкой 500 всем сервисам пространства, а на стенде
ещё и ключа нет (его записывает портал при выкладке). Без этого кнопка «Написать
«Искрой»» выключена, и не видно главного — как из приложенной справки получаются
тезисы, как числа сверяются с источником и что попадает в документ.

    .venv/bin/python tools/fake_iskra.py            # порт 8961, как на стенде
    .venv/bin/python tools/local_preview.py         # и открыть http://127.0.0.1:8971/theses/

Ответ заготовлен и не зависит от темы: это показ пути, а не работа модели. Сервис
сообщает об этом интерфейсу (`GET /api/iskra` → `demo: true`), и вверху карточки
висит предупреждение. В продуктиве такого запуска нет.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

# Заготовленный ответ: тот же вид, что присылает «Искра» — буллиты, показатели
# и места под недостающую фактуру. Данные выдуманные.
ANSWER = {
    "bullets": [
        {"text": "Коллеги, сегодня о школьном спорте: кто им занимается на местах "
                 "и что этому мешает.", "emphasis": "normal", "part": "контекст"},
        {"text": "Клубы созданы в 80 процентах школ.", "emphasis": "strong", "part": "масштаб"},
        {"text": "За два года их число выросло на 12 процентов.",
         "emphasis": "normal", "part": "результат"},
        {"text": "Например, в одном из округов клуб открыли на базе колледжа — "
                 "своего зала в школе нет.", "emphasis": "aside", "part": "пример"},
        {"text": "Охват на селе ниже [ТРЕБУЕТСЯ ФАКТУРА: охват в сельских школах, процентов].",
         "emphasis": "normal", "part": "проблема"},
        {"text": "Динамика положительная. Но клуб без тренера — это вывеска, а не занятия.",
         "emphasis": "strong", "part": "вывод"},
        {"text": "Прошу глав регионов закрыть тренерские ставки и доложить о результатах "
                 "[ТРЕБУЕТСЯ СРОК].", "emphasis": "strong", "part": "поручение"},
    ],
    "refs": [
        {"name": "Выдуманный источник для показа", "url": "https://example.org/sport",
         "about": "доля школ с клубами"},
    ],
    "gaps": [
        {"marker": "ТРЕБУЕТСЯ ФАКТУРА", "suggestion": "охват в сельских школах, процентов"},
        {"marker": "ТРЕБУЕТСЯ СРОК", "suggestion": "к какой дате закрыть ставки"},
    ],
}


def main() -> int:
    parser = argparse.ArgumentParser(description="Сервис «Тезисы» с подменным ответом «Искры»")
    parser.add_argument("--port", type=int, default=8961)
    parser.add_argument("--data", default=str(Path.home() / ".work-portal-dev" / "theses"),
                        help="папка с базой (по умолчанию стендовая)")
    parser.add_argument("--secret", default="dev-portal-secret")
    parser.add_argument("--delay", type=float, default=0,
                        help="сколько секунд «Искра» думает — показать индикатор и отмену")
    parser.add_argument("--english", choices=("fix", "stuck"),
                        help="английские вставки в ответе: fix — «Искра» исправит их вторым "
                             "кругом, stuck — не исправит, и слова увидит человек")
    parser.add_argument("--mixed", choices=("fix", "stuck"),
                        help="слово наполовину латиницей («zadанию»): fix — «Искра» исправит "
                             "вторым кругом, stuck — не исправит, и документ не соберётся, "
                             "пока слово не поправит человек")
    parser.add_argument("--fail", action="store_true",
                        help="прогон срывается — показать ошибку и «Попробовать снова»")
    opts = parser.parse_args()

    os.environ.setdefault("PORTAL_SECRET", opts.secret)
    os.environ["THESES_DATA"] = opts.data
    os.environ["THESES_DEMO_ISKRA"] = "1"     # интерфейс предупредит, что ответ подменён

    from app import iskra

    rounds = {"n": 0}

    def dirty(answer):
        """Тот же ответ, но с английскими вставками, как жаловался пользователь."""
        broken = json.loads(json.dumps(answer, ensure_ascii=False))
        broken["bullets"][0]["text"] = broken["bullets"][0]["text"].replace(
            "школьном спорте", "school sports")
        broken["bullets"][2]["text"] += " Это key результат."
        return broken

    def mixed(answer):
        """Слово наполовину латиницей — как «к zadанию» в документе пользователя."""
        broken = json.loads(json.dumps(answer, ensure_ascii=False))
        broken["bullets"][1]["text"] += " Треть экзаменуемых не приступают к zadанию вовсе."
        return broken

    def slow_call(system, user):
        """Думает `--delay` секунд, слушает «Отменить», тратит токены как настоящая."""
        import time
        ctl = iskra.control()
        until = time.monotonic() + opts.delay
        while time.monotonic() < until:
            if ctl.cancelled():
                iskra.spend(800, 60)
                raise iskra.Cancelled("Написание отменено")
            time.sleep(0.2)
        iskra.spend(2400, 900)
        if opts.fail:
            raise iskra.IskraError("«Искра» прервала прогон: run failed (так задано для показа)")
        rounds["n"] += 1
        if opts.english == "stuck" or (opts.english == "fix" and "ТЕКУЩИЙ ОТВЕТ" not in user):
            return json.dumps(dirty(ANSWER), ensure_ascii=False)
        if opts.mixed == "stuck" or (opts.mixed == "fix" and "ТЕКУЩИЙ ОТВЕТ" not in user):
            return json.dumps(mixed(ANSWER), ensure_ascii=False)
        return json.dumps(ANSWER, ensure_ascii=False)

    iskra._call = slow_call
    iskra.key_ready = lambda: True

    print(f"«Тезисы» на 127.0.0.1:{opts.port}, ответ «Искры» подменён (показ работы).")
    print(f"  база: {opts.data}")
    import uvicorn
    uvicorn.run("app.main:app", host="127.0.0.1", port=opts.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
