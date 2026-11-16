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
    ap.add_argument("--mix1", type=float, default=0.25, help="доля группы приоритета 1 в спросе (допущение)")
    ap.add_argument("--mix2", type=float, default=0.35, help="доля группы 2 (допущение; группа 3 - остаток)")
    ap.add_argument("--good1", type=float, default=0.40, help="доля подходящих в группе 1 (допущение)")
    ap.add_argument("--good2", type=float, default=0.20, help="доля подходящих в группе 2 (допущение)")
    ap.add_argument("--good3", type=float, default=0.08, help="доля подходящих в группе 3 (допущение)")
    args = ap.parse_args(argv)

    import database
    from config import FREELANCE_QUEUE_MAX_HOURS
    from freelance import ai_scorer, capacity, pipeline, report, yandex_budget

    demand = None
    source = args.db or database.DB_PATH
    if os.path.exists(source):
        report.use_copy_of(source)   # боевая БД не меняется: спрос считается по копии
        demand = capacity.demand_per_day(args.days)
    overhead = capacity.measure_overhead(args.days) if demand is not None else None
    f = capacity.forecast(ai_scorer.status(), demand or 0.0, pipeline.TRIAGE_SHADOW_MAX_PER_DAY,
                          date.today(), yandex_budget.until(), overhead=overhead)
    print(capacity.format_forecast(f))
    if f["demand_per_day"] > 0:
        mix = {1: args.mix1, 2: args.mix2, 3: 1.0 - args.mix1 - args.mix2}
        good = {1: args.good1, 2: args.good2, 3: args.good3}
        print("\nУстойчивый спрос при этой ёмкости (симуляция очереди, правила бота; доли групп и подходящих - "
              "ДОПУЩЕНИЕ, подставьте замер через --mix1/--mix2/--good1..3):")
        for title, skip in (("как сейчас", None), ("триаж снимает 40% группы 3", {3: 0.4}),
                            ("триаж снимает 40% группы 3 и 25% группы 2", {3: 0.4, 2: 0.25})):
            r = capacity.simulate_queue(f["demand_per_day"], f["net_without_yandex"], mix, good, skip_share=skip,
                                        aging_hours=pipeline.AGING_HOURS, aging_boost=pipeline.AGING_MAX_BOOST,
                                        queue_max_hours=float(FREELANCE_QUEUE_MAX_HOURS), reserve=pipeline.RESERVE_SHARE)
            g = r["groups"]
            print(f"- {title}: оценено {r['scored']} из {r['arrived']}, истекло без оценки {r['expired']}, "
                  f"ждёт {r['waiting_at_end']}; потеряно подходящих ≈{r['good_lost']} из {r['good_arrived']} "
                  f"({(r['good_lost_share'] or 0) * 100:.0f}%); истекло по группам 1/2/3: "
                  f"{g[1]['expired']}/{g[2]['expired']}/{g[3]['expired']}; ожидание до оценки, медиана/90% (ч): "
                  + "; ".join(f"гр{k} {v['wait_median_h']}/{v['wait_p90_h']}" for k, v in g.items()))
        print("Потери от самого триажа (пропуски подходящих) здесь НЕ учтены: они неизвестны до разметки.")
    mode = pipeline.effective_triage_mode()
    print(f"Триаж: запрошен {pipeline.TRIAGE_MODE}, применяется {mode}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
