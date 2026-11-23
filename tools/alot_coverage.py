"""Диагностика полноты источника: что alot.pro отдаёт за последние часы и куда это уходит в префильтре.

Только чтение: ходит к alot.pro (публичный API, как и бот), БД и Telegram не трогает.
Отвечает на вопрос «заказ был на сайте, а бот его не показал»: если заказа нет в выводе
(`--grep`), его не отдал alot.pro; если есть, но с причиной отсева - виноват префильтр.

Запуск из корня проекта:
    .venv/Scripts/python.exe -m tools.alot_coverage
    .venv/Scripts/python.exe -m tools.alot_coverage --hours 24 --site youdoru --grep бот
"""
import argparse
import time
from collections import Counter
from datetime import datetime, timedelta, timezone

from freelance import ai_scorer
from freelance.alot_client import REQUEST_PAUSE_SEC, fetch_new_orders, find_upper_bound, site_name
from freelance.filters import reject_reason

MAX_BACK_IDS = 20000


SAFETY_MARGIN_IDS = 1500  # после того как край окна стал старше порога, просматриваем ещё столько id
EDGE_ORDERS = 20  # сколько заказов с наименьшими id должны быть старше окна, чтобы считать окно покрытым


def window_covered(orders, threshold) -> bool:
    """Окно покрыто, если самый «нижний» край просмотренных id уже старше порога.
    Порядок дат по id не гарантирован (старый заказ может появиться позже), а границы окна по
    дате API не даёт, поэтому это эвристика, а не доказательство: нужно не меньше EDGE_ORDERS
    заказов, и все наименьшие по id старше порога. Одного старого заказа недостаточно."""
    edge = sorted(orders, key=lambda o: o.order_id)[:EDGE_ORDERS]
    return len(edge) >= EDGE_ORDERS and all(o.published_at and o.published_at <= threshold for o in edge)


def collect(hours: float):
    """Заказы alot.pro, опубликованные за последние `hours` часов, и верхняя граница id.
    Расширяет просмотр назад, пока край окна не станет старше порога; результаты всех
    проходов накапливаются: оборванный проход не стирает то, что уже получено.
    Остановка по краю окна - эвристика (alot.pro не гарантирует порядок дат по id), поэтому
    после покрытия просматривается ещё SAFETY_MARGIN_IDS id: поздно импортированный свежий
    заказ ниже края попадёт в результат, а покрытие пересчитается."""
    threshold = datetime.now(timezone.utc) - timedelta(hours=hours)
    top = find_upper_bound()
    back = 1500
    seen: dict[int, object] = {}
    partial = False
    covered_at = None  # глубина, на которой край окна впервые стал старше порога
    while True:
        time.sleep(REQUEST_PAUSE_SEC)
        scan = fetch_new_orders(top - back, max_ahead=back + 400, look_back=0)
        for o in scan.orders:
            seen[o.order_id] = o
        partial = partial or scan.partial
        covered = window_covered(list(seen.values()), threshold)
        if not covered:
            covered_at = None  # свежий заказ на меньших id: край сдвинулся, покрытие пересчитываем
        elif covered_at is None:
            covered_at = back
        if scan.partial or back >= MAX_BACK_IDS:
            break
        if covered and back >= covered_at + SAFETY_MARGIN_IDS:
            break  # край старше порога и ещё SAFETY_MARGIN_IDS id ниже него просмотрены
        back = min(covered_at + SAFETY_MARGIN_IDS if covered else back * 2, MAX_BACK_IDS)
    orders = [o for o in seen.values() if o.published_at and o.published_at >= threshold]
    return sorted(orders, key=lambda o: o.order_id), top, covered, partial


def print_freshness(orders, now=None) -> None:
    """Давно ли alot.pro получал заказы: если выдача старая, бот не виноват - новых заказов нет."""
    now = now or datetime.now(timezone.utc)
    if not orders:
        print("Свежесть: за окно нет ни одного заказа")
        return
    last: dict[str, datetime] = {}
    for o in orders:
        if o.published_at and (o.site not in last or o.published_at > last[o.site]):
            last[o.site] = o.published_at
    if not last:
        print("Свежесть: даты публикации неизвестны")
        return
    newest = max(last.values())
    print(f"Свежесть: самый новый заказ в выдаче alot.pro старше на {(now - newest).total_seconds() / 3600:.1f} ч "
          f"({newest.astimezone():%d.%m %H:%M}); по сайтам, часов назад:")
    for site, t in sorted(last.items(), key=lambda x: x[1], reverse=True):
        print(f"  {site_name(site):<24} {(now - t).total_seconds() / 3600:.1f}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--hours", type=float, default=12, help="окно, часов (по умолчанию 12)")
    ap.add_argument("--site", default="youdoru", help="какой сайт показать списком (по умолчанию youdoru)")
    ap.add_argument("--grep", default="", help="показывать только заказы с этим текстом в названии")
    args = ap.parse_args()

    orders, top, covered, partial = collect(args.hours)
    print(f"Верхняя граница id: {top}. Заказов за {args.hours:g} ч: {len(orders)}")
    if covered:
        print("Окно: нижний край просмотренных id старше порога (эвристика: alot.pro не гарантирует "
              "порядок дат по id, полнота не доказана)")
    else:
        print("ВНИМАНИЕ: окно просмотрено не целиком (достигнут предел id или сбой alot.pro): "
              "цифры занижены")
    if partial:
        print("ВНИМАНИЕ: скан оборван сбоем alot.pro")

    print_freshness(orders)

    by_site = Counter(o.site for o in orders)
    print("\nПо сайтам (сколько отдал alot.pro):")
    for site, n in by_site.most_common():
        print(f"  {site_name(site):<24} {n}")

    reasons = {o.order_id: reject_reason(o) for o in orders}
    passed = sum(1 for r in reasons.values() if r is None)
    print(f"\nПрошли префильтр: {passed} из {len(orders)}")
    cnt = Counter(r.split(":", 1)[0] for r in reasons.values() if r)
    for why, n in cnt.most_common():
        print(f"  отсеяно {why}: {n}")

    needle = args.grep.lower()
    chosen = [o for o in orders if o.site == args.site and needle in (o.title or "").lower()]
    print(f"\nСайт {args.site}{f', в названии «{args.grep}»' if needle else ''}: {len(chosen)}")
    for o in chosen:
        t = o.published_at.astimezone().strftime("%d.%m %H:%M") if o.published_at else "дата неизвестна"
        why = reasons[o.order_id] or "прошёл"
        title = ai_scorer.mask_contacts(o.title or "")[:70]
        print(f"  {o.order_id} {t} | {why:<22} | {','.join(o.categories) or '-'} | {title}")


if __name__ == "__main__":
    main()
