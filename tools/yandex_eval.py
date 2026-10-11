"""Сравнение моделей Yandex AI Studio на 60 заказах с эталонной оценкой (ПЛАТНО, из гранта).

Набор: tools/eval_orders.json, эталон: tools/eval_reference.json (fit по разметке Claude,
проверенной Иваном). Оба файла только локальные (в git не попадают).

Запуск из корня проекта (ключи берутся из .env, YANDEX_ENABLED не нужен):
    .venv/Scripts/python.exe -m tools.yandex_eval
    .venv/Scripts/python.exe -m tools.yandex_eval --models aliceai-llm-flash --limit 10

Стоимость по умолчанию (3 модели x 60 заказов): ~25 ₽ Flash + ~40 ₽ Lite + ~115 ₽ Alice AI LLM.
Прогон сам останавливается, если траты дойдут до --max-rub (по умолчанию 200 ₽). Траты прогона
не входят в учёт бота: на них в бюджете гранта отложено ~180 ₽ (yandex_budget.DEFAULT_BUDGET_RUB).
Учёт бюджета бота (kv в базе) не трогает. Результат: tools/eval_results/yandex_<время>.json.
"""
import argparse
import json
import os
import statistics
import time
from datetime import datetime
from pathlib import Path
from unittest import mock

from freelance import ai_scorer, yandex_budget
from freelance.alot_client import FreelanceOrder

ROOT = Path(__file__).resolve().parent
GOOD_FIT = 5   # как FREELANCE_MIN_FIT: с этой оценки заказ уходит в чат
DEFAULT_MODELS = "aliceai-llm-flash,yandexgpt-5-lite,aliceai-llm"


def load_orders() -> list[FreelanceOrder]:
    raw = json.loads((ROOT / "eval_orders.json").read_text(encoding="utf-8"))
    return [FreelanceOrder(o["order_id"], o["site"], o["title"], o["body"], o["price_text"],
                           o["price_value"], o["categories"],
                           datetime.fromisoformat(o["published_at"]), o["url"])
            for o in raw]


def run_model(model: str, orders, max_rub: float, spent_before: float) -> dict:
    env = dict(os.environ, YANDEX_ENABLED="1", YANDEX_MODELS=model,
               YANDEX_MIN_INTERVAL_SEC="0.5", YANDEX_MAX_PER_DAY="100000")
    provider = ai_scorer._build_providers(env)["yandex"]
    if not provider.key:
        raise SystemExit("нет YANDEX_API_KEY / YANDEX_FOLDER_ID в .env")
    costs: list[float] = []

    def reserve(m, max_tokens, now=None):
        return yandex_budget.worst_cost(m, max_tokens)

    def settle(m, reserved, usage, now=None):
        spent = yandex_budget.usage_cost(m, usage)
        costs.append(reserved if spent is None else spent)
        return costs[-1]

    results = {}
    with mock.patch.object(ai_scorer, "_load_count"), mock.patch.object(ai_scorer, "_save_count"), \
            mock.patch.object(yandex_budget, "reserve", side_effect=reserve), \
            mock.patch.object(yandex_budget, "settle", side_effect=settle), \
            mock.patch.object(yandex_budget, "release"):
        for i, order in enumerate(orders, 1):
            if spent_before + sum(costs) >= max_rub:
                print(f"\n  стоп: траты дошли до {max_rub:g} ₽")
                break
            started = time.monotonic()
            outcome = ai_scorer._ask_provider_model(provider, model, order)
            sec = time.monotonic() - started
            ai = outcome if isinstance(outcome, dict) else None
            results[str(order.order_id)] = {
                "fit": ai["fit"] if ai else None, "hours": ai["hours"] if ai else None,
                "sec": round(sec, 1), "error": "" if ai else provider.last_error}
            print(f"  {model}: {i}/{len(orders)}, {sum(costs):.2f} ₽", end="\r")
    print()
    return {"results": results, "rub": round(sum(costs), 2),
            "rub_per_order": round(sum(costs) / len(costs), 3) if costs else None}


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
    ap.add_argument("--max-rub", type=float, default=200.0)
    args = ap.parse_args()
    if yandex_budget.expired():
        raise SystemExit(f"срок гранта вышел ({yandex_budget.until():%d.%m.%Y}), Яндекс не вызываем")

    orders = load_orders()
    if args.limit:
        orders = orders[:args.limit]
    reference = json.loads((ROOT / "eval_reference.json").read_text(encoding="utf-8"))
    titles = {str(o.order_id): o.title for o in orders}
    report, spent = {}, 0.0
    for model in [m.strip() for m in args.models.split(",") if m.strip()]:
        if model not in yandex_budget.PRICES:
            print(f"{model}: нет цены в yandex_budget.PRICES, пропуск")
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
    print(f"\nПотрачено всего: {spent:.2f} ₽")

    out_dir = ROOT / "eval_results"
    out_dir.mkdir(exist_ok=True)
    out = out_dir / f"yandex_{datetime.now():%Y%m%d_%H%M}.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"Результат: {out} - пришли его Claude для разбора")


if __name__ == "__main__":
    main()
