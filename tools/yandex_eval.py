"""Проверка Flash на локальной выборке заказов. Каждый запрос расходует общий платный пилот.

Набор: tools/eval_orders.json, эталон: tools/eval_reference.json (fit по разметке Claude,
проверенной Иваном). Оба файла только локальные (в git не попадают).

Явный запуск из корня проекта с действующим пилотом и общей БД бота:
    .venv/Scripts/python.exe -m tools.yandex_eval --limit 2 --max-rub 8
--max-rub ограничивает сумму верхних оценок стоимости запросов этого запуска.
"""
import argparse
import json
import statistics
import time
from datetime import datetime
from pathlib import Path
from freelance import ai_scorer, paid_budget
from freelance.alot_client import FreelanceOrder

ROOT = Path(__file__).resolve().parent
GOOD_FIT = 5   # как FREELANCE_MIN_FIT: с этой оценки заказ уходит в чат
DEFAULT_MODELS = paid_budget.MODEL


def load_orders() -> list[FreelanceOrder]:
    raw = json.loads((ROOT / "eval_orders.json").read_text(encoding="utf-8"))
    return [FreelanceOrder(o["order_id"], o["site"], o["title"], o["body"], o["price_text"],
                           o["price_value"], o["categories"],
                           datetime.fromisoformat(o["published_at"]), o["url"])
            for o in raw]


def run_model(model: str, orders, max_rub: float, spent_before: float) -> dict:
    provider = ai_scorer.PROVIDERS.get("yandex")
    if not provider or not provider.key or "yandex" not in ai_scorer.CHAIN or not paid_budget.active():
        raise SystemExit("нужны действующий платный пилот и Яндекс в цепочке AI")
    if model != paid_budget.MODEL:
        raise SystemExit("в платном пилоте доступна только модель Flash")
    reserved = 0
    results = {}
    worst = paid_budget.INPUT_BOUND * paid_budget.INPUT_MICROS + paid_budget.OUTPUT_LIMIT * paid_budget.OUTPUT_MICROS
    for i, order in enumerate(orders, 1):
        if spent_before + (reserved + worst) / paid_budget.RUB > max_rub:
            break
        started = time.monotonic()
        window = ai_scorer._LiveWindow(started + ai_scorer.LIVE_DEADLINE_SEC,
                                       attempts=ai_scorer.LIVE_MAX_POSTS - 1)
        outcome = ai_scorer._ask_provider_model(provider, model, order, window=window)
        if window.attempts == ai_scorer.LIVE_MAX_POSTS:
            reserved += worst
        sec = time.monotonic() - started
        ai = outcome if isinstance(outcome, dict) else None
        results[str(order.order_id)] = {
            "fit": ai["fit"] if ai else None, "hours": ai["hours"] if ai else None,
            "sec": round(sec, 1), "error": "" if ai else provider.last_error}
        print(f"  {model}: {i}/{len(orders)}, верхняя оценка {reserved / paid_budget.RUB:.2f} ₽", end="\r")
    print()
    return {"results": results, "rub": reserved / paid_budget.RUB,
            "rub_per_order": round(reserved / paid_budget.RUB / len(results), 3) if results else None}


def metrics(results: dict, reference: dict) -> dict:
    fits, missed, false_pos, failed = [], [], [], []
    for oid, r in results.items():
        ref = reference[oid]["fit"]
        if r["fit"] is None:
            failed.append(oid)
            continue
        fits.append(abs(r["fit"] - ref))
        if ref >= GOOD_FIT > r["fit"]:
            missed.append(oid)
        if r["fit"] >= GOOD_FIT > ref:
            false_pos.append(oid)
    good_total = sum(1 for oid in results if reference[oid]["fit"] >= GOOD_FIT)
    secs = [r["sec"] for r in results.values()]
    return {"orders": len(results), "failed": len(failed), "good_total": good_total,
            "missed_good": missed, "false_positive": false_pos,
            "fit_mae": round(statistics.mean(fits), 2) if fits else None,
            "sec_avg": round(statistics.mean(secs), 1) if secs else None}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--models", default=DEFAULT_MODELS)
    ap.add_argument("--limit", type=int, default=0, help="сколько заказов (0 - все 60)")
    ap.add_argument("--max-rub", type=float, default=8.0)
    args = ap.parse_args()
    if not paid_budget.active() or not 0 < args.max_rub <= paid_budget.TOTAL / paid_budget.RUB:
        raise SystemExit("нужны действующий пилот и положительный лимит прогона до 100 ₽")

    orders = load_orders()
    if args.limit:
        orders = orders[:args.limit]
    reference = json.loads((ROOT / "eval_reference.json").read_text(encoding="utf-8"))
    titles = {str(o.order_id): o.title for o in orders}
    report, spent = {}, 0.0
    for model in [m.strip() for m in args.models.split(",") if m.strip()]:
        if model != paid_budget.MODEL:
            print(f"{model}: вне списка пилота, пропуск")
            continue
        print(f"{model}:")
        run = run_model(model, orders, args.max_rub, spent)
        spent += run["rub"]
        run["metrics"] = metrics(run["results"], reference)
        report[model] = run

    print("\nМодель                 пропущено хороших  лишних  ошибка fit  сбоев  ₽/заказ  сек")
    for model, run in report.items():
        m = run["metrics"]
        print(f"{model:22} {len(m['missed_good']):>3} из {m['good_total']:<11} {len(m['false_positive']):>5}"
              f"  {m['fit_mae']!s:>9}  {m['failed']:>5}  {run['rub_per_order']!s:>7}  {m['sec_avg']}")
        for oid in m["missed_good"]:
            print(f"    пропущен: fit {run['results'][oid]['fit']} (эталон {reference[oid]['fit']}) "
                  f"{titles[oid][:60]}")
    print(f"\nВерхняя оценка расхода: {spent:.2f} ₽")

    out_dir = ROOT / "eval_results"
    out_dir.mkdir(exist_ok=True)
    out = out_dir / f"yandex_{datetime.now():%Y%m%d_%H%M}.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"Результат: {out}")


if __name__ == "__main__":
    main()
