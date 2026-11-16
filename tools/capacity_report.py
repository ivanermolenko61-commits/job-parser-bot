"""Прогноз ёмкости AI-оценки после отключения Yandex (20.11.2026): хватит ли бесплатных провайдеров.

Грант не продлевается, оплата не включается. Читает настройки провайдеров из окружения (ключи не выводит:
только «есть/нет») и спрос из журнала БД. Допущения печатаются вместе с результатом.

  python -m tools.capacity_report --db path/to/vacancies.db
"""
import argparse
import os
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", help="путь к БД со спросом (по умолчанию DB_PATH)")
    ap.add_argument("--days", type=int, default=7, help="за сколько дней считать спрос")
    args = ap.parse_args(argv)

    import database
    if args.db:
        database.DB_PATH = args.db
    from freelance import ai_scorer, capacity, pipeline, yandex_budget

    demand = None
    if os.path.exists(database.DB_PATH):
        database.init_db()
        demand = capacity.demand_per_day(args.days)
    f = capacity.forecast(ai_scorer.status(), demand or 0.0, pipeline.TRIAGE_SHADOW_MAX_PER_DAY,
                          date.today(), yandex_budget.until())
    print(capacity.format_forecast(f))
    mode = pipeline.effective_triage_mode()
    print(f"Триаж: запрошен {pipeline.TRIAGE_MODE}, применяется {mode}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
