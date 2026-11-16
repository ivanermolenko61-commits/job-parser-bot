"""Отчёт потерь по журналу фриланс-заказов: воронка и задержки по источникам, куда делись заказы.

Читает БД (только чтение), секретов не использует. Названия в выводе обезличены.

  python -m tools.loss_report --db path/to/vacancies.db --days 14
  python -m tools.loss_report --db vacancies.db --csv journal.csv     # + CSV для ручной разметки

Что смотреть: примеры «отсеяно правилом» (нет ли подходящих), «устарели без оценки» (хватило ли квоты),
«оценены хорошо, но не показаны» (доставка). Старые записи до журнала помечены как legacy с неизвестной
причиной - история не выдумывается.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", help="путь к БД (по умолчанию DB_PATH из окружения/config)")
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--csv", help="записать журнал в CSV (названия обезличены, описаний нет)")
    args = ap.parse_args(argv)

    import database
    from freelance import report
    source = args.db or database.DB_PATH
    if not os.path.exists(source):
        print(f"БД не найдена: {source}")
        return 2
    report.use_copy_of(source)   # боевая БД не меняется: отчёт строится по копии

    rows = report.load_rows(args.days)
    excluded = report.load_excluded(args.days)
    print(report.format_funnel(rows, excluded, args.days).replace("<b>", "").replace("</b>", ""))
    print()
    print(report.format_losses(report.losses(rows)))
    if args.csv:
        with open(args.csv, "w", encoding="utf-8-sig", newline="") as f:
            f.write(report.export_csv(rows))
        print(f"\nCSV: {args.csv} ({len(rows)} строк)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
