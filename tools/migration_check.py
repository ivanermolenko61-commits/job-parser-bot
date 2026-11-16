"""Проверка миграции БД этапа 1 на КОПИИ: перенос данных, повторный запуск, откат на прежний код, резервная копия.

Боевая БД не открывается на запись: источник (--db) снимается через sqlite3.backup во временную папку.
Без --db строится синтетическая прежняя БД: её создаёт код старой ревизии (--old-ref, по умолчанию 110afe6 -
версия, которая сейчас на BotHost), с заказами во всех старых статусах.

Проверяет по шагам:
  1. новый код мигрирует копию прежней БД; ни одна строка и ни одно старое поле не изменились;
  2. повторный запуск ничего не меняет (идемпотентность);
  3. журнал заполнен: у каждого заказа есть состояние tech (старые записи без выдуманных причин);
  4. ОТКАТ: прежний код (старая ревизия) открывает уже мигрированную БД, читает, пишет и закрывает заказ;
     затем новый код снова открывает её, а строка, записанная прежним кодом, получает состояние журнала;
  5. резервная копия (sqlite3.backup): PRAGMA integrity_check, те же данные, восстановленная копия открывается
     новым и прежним кодом.

  python -m tools.migration_check [--db path/to/vacancies.db] [--old-ref 110afe6]
Код возврата 0 - все проверки прошли, 1 - есть расхождения.
"""
import argparse
import hashlib
import io
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Столбцы freelance_orders, существовавшие до этапа 1: по ним сравниваем «до» и «после»
OLD_ORDER_COLS = ("order_id", "site", "title", "url", "price_text", "published_at", "dedup_key", "fit", "ai_json",
                  "status", "attempts", "message_id", "category", "budget", "hours", "deal_price")

SEED = r'''
import json, os, sys
sys.path.insert(0, os.getcwd())
import database
database.init_db()
rows = [
    (1, "flru", "Выгрузка из WB в таблицу", "pending", 7), (2, "flru", "Бот для заявок", "sent", 8),
    (3, "kwork", "Парсер цен", "taken", 9), (4, "alot", "Скрипт отчёта", "dismissed", 6),
    (5, "flru", "Автоматизация отчётов", "expired", 7), (6, "kwork", "Сайт-визитка", "rejected", 2),
    (7, "alot", "Интеграция API с таблицей", "sent", 8),
]
for oid, site, title, status, fit in rows:
    database.freelance_save(oid, site, title, f"https://x/{oid}", "5000 ₽", "2026-11-01T10:00:00", f"{site}|{title}",
                            fit, json.dumps({"fit": fit, "category": "PROG_OTHER"}), status, 0, 100 + oid,
                            "PROG_OTHER", 5000.0, 10.0)
database.freelance_set_deal_price(3, 12000.0)
database.kv_set("alot_last_id", 12345)
print("seeded")
'''

OLD_PROBE = r'''
import json, os, sys
sys.path.insert(0, os.getcwd())
import database
database.init_db()
database.freelance_stats(); database.freelance_recent(5); database.freelance_pending(10)
database.freelance_day_summary(24); database.freelance_deals("PROG_OTHER"); database.freelance_deal_rows()
database.freelance_save(901, "flru", "Запись прежнего кода после отката", "https://x/901", "1000 ₽",
                        "2026-11-02T10:00:00", "flru|rollback", 7, "{}", "pending", 0, None)
database.freelance_set_status(901, "taken")
database.freelance_set_status(1, "sent")
print(json.dumps(database.freelance_get(901)["status"]))
'''

NEW_ROWS = r'''
import os, sys
sys.path.insert(0, os.getcwd())
import database
database.init_db()
def save(oid, status, **kw):
    database.freelance_save(oid, "flru", f"Новый заказ {oid}", f"https://x/{oid}", "3000 ₽", "2026-11-10T10:00:00",
                            f"flru|n{oid}", None, "", status, 0, None, **kw)
save(701, "rejected", tech="filtered", reject_reason="no_profile_signal")
save(702, "rejected", tech="duplicate", dup_of=1)
save(703, "pending", tech="queued", queue_reason="no_quota")
save(704, "expired", tech="stale", stale_reason="queue_expired")
save(705, "sent", tech="assessed", verdict="ok", delivery="card")
database.freelance_set_lead(705, "applied")
print("ok")
'''

NEW_INIT = r'''
import os, sys
sys.path.insert(0, os.getcwd())
import database
database.init_db()
print("ok")
'''


def run_code(tree: str, code: str, db: str) -> str:
    env = dict(os.environ, DB_PATH=db, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
    r = subprocess.run([sys.executable, "-c", code], cwd=tree, env=env, capture_output=True, text=True, timeout=300)
    if r.returncode:
        raise RuntimeError((r.stderr or r.stdout).strip()[-600:])
    return r.stdout.strip()


def snapshot(db: str) -> dict:
    conn = sqlite3.connect(db)
    try:
        out = {}
        for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' "
                                    "ORDER BY name"):
            out[name] = conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
        cols = {r[1] for r in conn.execute("PRAGMA table_info(freelance_orders)")}
        use = [c for c in OLD_ORDER_COLS if c in cols]
        # таблицы может не быть (старая БД без фриланса): тогда «до» пусто, миграция её создаст
        rows = conn.execute(f"SELECT {', '.join(use)} FROM freelance_orders ORDER BY order_id").fetchall() if use else []
        out["_orders_hash"] = hashlib.sha256(json.dumps(rows, default=str, ensure_ascii=False).encode()).hexdigest()
        kv = conn.execute("SELECT key, value FROM kv ORDER BY key").fetchall() if "kv" in out else []
        out["_kv_hash"] = hashlib.sha256(json.dumps(kv, ensure_ascii=False).encode()).hexdigest()
        return out
    finally:
        conn.close()


def full_hash(db: str) -> str:
    conn = sqlite3.connect(db)
    try:
        return hashlib.sha256("\n".join(conn.iterdump()).encode("utf-8")).hexdigest()
    finally:
        conn.close()


def backup_copy(src: str, dst: str) -> None:
    s = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    d = sqlite3.connect(dst)
    try:
        s.backup(d)
    finally:
        d.close()
        s.close()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", help="копия боевой БД (по умолчанию строится синтетическая прежняя)")
    ap.add_argument("--old-ref", default="110afe6", help="ревизия прежнего кода (то, что сейчас на сервере)")
    args = ap.parse_args(argv)

    results: list[tuple[bool, str]] = []

    def check(ok: bool, text: str) -> None:
        results.append((bool(ok), text))
        print(("  ПРОШЛО  " if ok else "  ПРОВАЛ  ") + text)

    tmp = tempfile.mkdtemp(prefix="migration_check_")
    try:
        old_tree = os.path.join(tmp, "old")
        os.makedirs(old_tree)
        arch = subprocess.run(["git", "archive", args.old_ref], cwd=ROOT, capture_output=True, timeout=120)
        if arch.returncode:
            print(f"Не удалось получить ревизию {args.old_ref}: {arch.stderr.decode(errors='replace')[-300:]}")
            return 2
        with tarfile.open(fileobj=io.BytesIO(arch.stdout)) as tf:
            tf.extractall(old_tree, filter="data")

        legacy = os.path.join(tmp, "legacy.db")
        if args.db:
            if not os.path.exists(args.db):
                print(f"БД не найдена: {args.db}")
                return 2
            backup_copy(args.db, legacy)
            print(f"Источник: снимок {args.db} (боевая БД не менялась)")
        else:
            run_code(old_tree, SEED, legacy)
            print(f"Источник: синтетическая БД, созданная кодом {args.old_ref}")
        before = snapshot(legacy)
        print(f"Строк freelance_orders до: {before.get('freelance_orders', 0)}")

        print("1-3. Миграция и повторный запуск (новый код):")
        migrated = os.path.join(tmp, "migrated.db")
        shutil.copy2(legacy, migrated)
        run_code(ROOT, NEW_INIT, migrated)
        after = snapshot(migrated)
        check(after["_orders_hash"] == before["_orders_hash"], "старые поля всех заказов не изменились")
        check(after["_kv_hash"] == before["_kv_hash"], "служебные значения kv не изменились")
        for t, n in before.items():
            if not t.startswith("_"):
                check(after.get(t) == n, f"таблица {t}: строк {n} -> {after.get(t)}")
        h1 = full_hash(migrated)
        run_code(ROOT, NEW_INIT, migrated)
        check(full_hash(migrated) == h1, "повторный запуск ничего не меняет (идемпотентность)")
        conn = sqlite3.connect(migrated)
        try:
            empty = conn.execute("SELECT COUNT(*) FROM freelance_orders WHERE tech IS NULL OR tech = ''").fetchone()[0]
            invented = conn.execute("SELECT COUNT(*) FROM freelance_orders WHERE tech = 'legacy' "
                                    "AND COALESCE(reject_reason, '') != ''").fetchone()[0]
        finally:
            conn.close()
        check(empty == 0, "у каждого заказа есть состояние журнала tech")
        check(invented == 0, "у старых записей причины отсева не выдуманы")

        print("4. Откат: прежний код на мигрированной БД:")
        rolled = os.path.join(tmp, "rolled.db")
        shutil.copy2(migrated, rolled)
        run_code(ROOT, NEW_ROWS, rolled)   # в БД есть и записи, созданные только новым кодом (отсев, дубль, лид...)
        try:
            out = run_code(old_tree, OLD_PROBE, rolled)
            check(out == '"taken"', "прежний код открыл мигрированную БД, прочитал, записал и закрыл заказ")
        except RuntimeError as e:
            check(False, f"прежний код не работает на мигрированной БД: {e}")
        else:
            try:
                run_code(ROOT, NEW_INIT, rolled)
                conn = sqlite3.connect(rolled)
                try:
                    row = conn.execute("SELECT tech FROM freelance_orders WHERE order_id = 901").fetchone()
                finally:
                    conn.close()
                check(row is not None and row[0], "после отката и нового запуска запись прежнего кода получила состояние журнала")
            except RuntimeError as e:
                check(False, f"новый код не открыл БД после отката: {e}")

        print("5. Резервная копия и восстановление:")
        backup = os.path.join(tmp, "backup.db")
        backup_copy(migrated, backup)
        conn = sqlite3.connect(backup)
        try:
            integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        finally:
            conn.close()
        check(integrity == "ok", f"integrity_check копии: {integrity}")
        check(snapshot(backup) == after, "копия содержит те же данные")
        restored = os.path.join(tmp, "restored.db")
        shutil.copy2(backup, restored)
        try:
            run_code(ROOT, NEW_INIT, restored)
            run_code(old_tree, OLD_PROBE, restored)
            check(True, "восстановленная копия открывается новым и прежним кодом")
        except RuntimeError as e:
            check(False, f"восстановленная копия не открылась: {e}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    failed = [t for ok, t in results if not ok]
    print(f"\nИтог: {len(results) - len(failed)} из {len(results)} проверок прошли" + (" - ЕСТЬ ПРОВАЛЫ" if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
