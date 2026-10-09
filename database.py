"""Работа с SQLite: хранение уже отправленных вакансий.

Две таблицы:
  • vacancies     — вакансии для /list, чистятся через CLEANUP_DAYS.
  • sent_history  — только «что уже отправляли» (source + vacancy_id
                    и dedup_key — нормализованные название+компания),
                    хранится SENT_HISTORY_DAYS. Нужна, чтобы вакансия без
                    даты публикации (DreamJob) не пришла повторно после того,
                    как её удалили из vacancies, а на сайте она всё ещё висит.
"""
import json
import os
import re
import sqlite3
import statistics
from contextlib import contextmanager
from datetime import datetime, timedelta

from config import DB_PATH, DUPLICATE_WINDOW_DAYS, FREELANCE_MIN_FIT, SENT_HISTORY_DAYS
from parsers.filters import dedup_key


# Сколько дней хранить заказы фриланса
FREELANCE_KEEP_DAYS = 14
# Строки с бюджетом заказчика нужны для статистики рынка - хранятся дольше
FREELANCE_MARKET_DAYS = 90
# Строки со сделкой (deal_price) - для ставки и точности цен
FREELANCE_DEAL_DAYS = 365

MONTHS_RU = {
    "января": 1, "февраля": 2, "марта": 3, "апреля": 4,
    "мая": 5, "июня": 6, "июля": 7, "августа": 8,
    "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12,
}


@contextmanager
def _connect():
    """Открывает соединение с базой и гарантированно закрывает его.

    Обычный `with sqlite3.connect(...)` только коммитит транзакцию,
    но НЕ закрывает соединение — поэтому оборачиваем сами.
    """
    # timeout: джобы вакансий и фриланса пишут параллельно — ждём, а не падаем
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def normalize_date(text: str) -> str:
    """Приводит дату к ISO (без timezone) для корректной сортировки.

    Публичная функция — используется также в parser_manager для проверки
    свежести вакансии.
    """
    if not text:
        return ""

    if re.match(r"^\d{4}-\d{2}-\d{2}", text):
        m = re.match(r"^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}(?::\d{2})?)", text)
        if m:
            return f"{m.group(1)}T{m.group(2)}"
        return text

    m = re.match(r"^(\d{1,2})\s+([а-яё]+)", text.lower().strip())
    if m:
        day = int(m.group(1))
        month_name = m.group(2)
        month = MONTHS_RU.get(month_name)
        if month:
            now = datetime.now()
            year = now.year
            # Если месяц в будущем относительно текущего — значит это
            # прошлый год. Но если месяц тот же, а день ещё не наступил
            # в этом году — тоже прошлый год.
            if month > now.month or (month == now.month and day > now.day):
                year -= 1
            return f"{year}-{month:02d}-{day:02d}T00:00:00"

    return text


def is_fresh(published_at: str, max_age_days: int) -> bool:
    """Проверяет, что вакансия не старше max_age_days.

    Если published_at пустой — считаем вакансию свежей (пропускаем, чтобы
    не терять валидные вакансии без указанной даты).
    """
    if not published_at:
        return True

    normalized = normalize_date(published_at)
    if not normalized:
        return True

    # Парсим нормализованную дату
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(normalized[:19], fmt)
            break
        except ValueError:
            continue
    else:
        # Не смогли распарсить — не блокируем вакансию
        return True

    threshold = datetime.now() - timedelta(days=max_age_days)
    return dt >= threshold


def init_db() -> None:
    """Создаёт таблицу, если её нет, и делает миграции."""
    db_dir = os.path.dirname(DB_PATH)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)
    with _connect() as conn:
        # WAL: чтение не блокирует запись (две джобы работают параллельно)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS vacancies (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT NOT NULL,
                vacancy_id TEXT NOT NULL,
                title TEXT,
                company TEXT,
                url TEXT NOT NULL,
                found_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(source, vacancy_id)
            )
        """)

        try:
            conn.execute("ALTER TABLE vacancies ADD COLUMN published_at TEXT DEFAULT ''")
        except sqlite3.OperationalError:
            pass

        conn.execute("""
            CREATE TABLE IF NOT EXISTS sent_history (
                source TEXT NOT NULL,
                vacancy_id TEXT NOT NULL,
                sent_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (source, vacancy_id)
            )
        """)
        cols = [r["name"] for r in conn.execute("PRAGMA table_info(sent_history)")]
        if "dedup_key" not in cols:
            conn.execute("ALTER TABLE sent_history ADD COLUMN dedup_key TEXT DEFAULT ''")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_sent_dedup ON sent_history(dedup_key)"
        )
        # Миграция: всё, что уже есть в vacancies, считаем отправленным
        conn.execute("""
            INSERT OR IGNORE INTO sent_history (source, vacancy_id, sent_at)
            SELECT source, vacancy_id, found_at FROM vacancies
        """)
        # Миграция: ключ дубля для старых записей (берём из vacancies)
        rows = conn.execute("""
            SELECT v.source, v.vacancy_id, v.title, v.company
            FROM vacancies v
            JOIN sent_history s USING (source, vacancy_id)
            WHERE s.dedup_key IS NULL OR s.dedup_key = ''
        """).fetchall()
        conn.executemany(
            "UPDATE sent_history SET dedup_key = ? WHERE source = ? AND vacancy_id = ?",
            [(dedup_key(r["title"], r["company"]), r["source"], r["vacancy_id"])
             for r in rows],
        )

        _init_freelance(conn)
        conn.commit()


def is_sent(source: str, vacancy_id: str) -> bool:
    """Проверяет, отправляли ли уже эту вакансию (за SENT_HISTORY_DAYS)."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM sent_history WHERE source = ? AND vacancy_id = ?",
            (source, vacancy_id),
        ).fetchone()
        return row is not None


def is_duplicate(title: str, company: str) -> bool:
    """Проверяет, отправляли ли уже такую вакансию с другой площадки.

    Одна и та же вакансия бывает и на hh.ru, и на Dream Job, и на GeekJob:
    vacancy_id разные, а название+компания совпадают после нормализации
    (регистр, кавычки, ООО/АО — см. dedup_key). Смотрим в sent_history,
    а не в vacancies: та чистится через 5 дней, и дубль без даты (DreamJob)
    пришёл бы снова. Окно — DUPLICATE_WINDOW_DAYS: у крупных работодателей
    бывают разные вакансии с одним названием, за 60 дней они бы глохли.
    """
    key = dedup_key(title, company)
    if not key:
        return False

    with _connect() as conn:
        row = conn.execute(
            """
            SELECT 1 FROM sent_history
            WHERE dedup_key = ? AND sent_at >= datetime('now', ?)
            LIMIT 1
            """,
            (key, f"-{DUPLICATE_WINDOW_DAYS} days"),
        ).fetchone()
        return row is not None


def mark_sent(
    source: str,
    vacancy_id: str,
    title: str = "",
    company: str = "",
    url: str = "",
    published_at: str = "",
) -> None:
    """Помечает вакансию как отправленную."""
    normalized_date = normalize_date(published_at)

    with _connect() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO vacancies
                (source, vacancy_id, title, company, url, published_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (source, vacancy_id, title, company, url, normalized_date),
        )
        conn.execute(
            """
            INSERT OR IGNORE INTO sent_history (source, vacancy_id, dedup_key)
            VALUES (?, ?, ?)
            """,
            (source, vacancy_id, dedup_key(title, company)),
        )
        conn.commit()


def stats() -> dict:
    """Статистика: сколько всего вакансий, по источникам, последняя."""
    with _connect() as conn:
        total = conn.execute("SELECT COUNT(*) FROM vacancies").fetchone()[0]

        by_source = conn.execute(
            "SELECT source, COUNT(*) AS cnt FROM vacancies GROUP BY source"
        ).fetchall()

        last = conn.execute(
            "SELECT found_at FROM vacancies ORDER BY found_at DESC, id DESC LIMIT 1"
        ).fetchone()

        return {
            "total": total,
            "by_source": {row["source"]: row["cnt"] for row in by_source},
            "last_found": last["found_at"] if last else None,
        }


def get_recent_vacancies(limit: int = 20) -> list[dict]:
    """Возвращает последние N вакансий.

    Сортировка:
      1. Записи с датой публикации — сначала (по убыванию published_at)
      2. Записи без даты публикации — в конце (по убыванию found_at)
    """
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT source, vacancy_id, title, company, url, found_at, published_at
            FROM vacancies
            ORDER BY
                CASE WHEN published_at IS NULL OR published_at = '' THEN 1 ELSE 0 END,
                published_at DESC,
                found_at DESC,
                id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]


def cleanup_old(days: int = 5) -> int:
    """Удаляет вакансии старше N дней. Возвращает число удалённых.

    Важно: published_at хранится как ISO-строка с 'T' (2026-09-19T14:30:00),
    а found_at — в формате SQLite с пробелом (2026-09-19 14:30:00).
    Чтобы сравнение работало корректно, приводим обе даты к формату,
    который понимает datetime() в SQLite: REPLACE(..., 'T', ' ').
    """
    with _connect() as conn:
        cursor = conn.execute(
            """
            DELETE FROM vacancies
            WHERE datetime(
                REPLACE(
                    COALESCE(NULLIF(published_at, ''), found_at),
                    'T', ' '
                )
            ) < datetime('now', ?)
            """,
            (f'-{days} days',),
        )
        deleted = cursor.rowcount
        # История отправок живёт дольше — защита от повторной отправки
        conn.execute(
            "DELETE FROM sent_history WHERE sent_at < datetime('now', ?)",
            (f'-{SENT_HISTORY_DAYS} days',),
        )
        # строки, оставленные ради статистики рынка, не хранят тяжёлый ai_json
        conn.execute(
            """
            UPDATE freelance_orders SET ai_json = ''
            WHERE seen_at < datetime('now', ?)
              AND COALESCE(budget, 0) > 0 AND COALESCE(deal_price, 0) <= 0
              AND ai_json != ''
            """,
            (f'-{FREELANCE_KEEP_DAYS} days',),
        )
        conn.execute(
            """
            DELETE FROM freelance_orders
            WHERE seen_at < datetime('now', ?)
              AND NOT (COALESCE(budget, 0) > 0 AND COALESCE(fit, 0) >= ? AND seen_at >= datetime('now', ?))
              AND NOT (COALESCE(deal_price, 0) > 0 AND seen_at >= datetime('now', ?))
            """,
            (f'-{FREELANCE_KEEP_DAYS} days', FREELANCE_MIN_FIT, f'-{FREELANCE_MARKET_DAYS} days',
             f'-{FREELANCE_DEAL_DAYS} days'),
        )
        conn.commit()
        return deleted


def reset_db() -> int:
    """Полностью очищает базу (вакансии и историю отправок).

    История тоже чистится, иначе «загрузить заново» ничего бы не прислал.
    Возвращает число удалённых вакансий.
    """
    with _connect() as conn:
        cursor = conn.execute("DELETE FROM vacancies")
        deleted = cursor.rowcount
        conn.execute("DELETE FROM sent_history")
        conn.commit()
        return deleted


# ---------- Фриланс ----------

def _init_freelance(conn) -> None:
    """Таблицы раздела «Фриланс». Вызывается из init_db()."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS freelance_orders (
            order_id INTEGER PRIMARY KEY,
            site TEXT,
            title TEXT,
            url TEXT,
            price_text TEXT,
            fit INTEGER,
            ai_json TEXT,
            published_at TEXT,
            dedup_key TEXT,
            seen_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            sent_at TIMESTAMP
        )
    """)
    # Миграция: новые колонки добавляем в существующую БД без потери данных
    cols = {r[1] for r in conn.execute("PRAGMA table_info(freelance_orders)")}
    if "attempts" not in cols:
        conn.execute("ALTER TABLE freelance_orders ADD COLUMN attempts INTEGER DEFAULT 0")
    if "message_id" not in cols:
        conn.execute("ALTER TABLE freelance_orders ADD COLUMN message_id INTEGER")
    if "status" not in cols:
        conn.execute("ALTER TABLE freelance_orders ADD COLUMN status TEXT")
        conn.execute(
            "UPDATE freelance_orders SET status = "
            "CASE WHEN sent_at IS NOT NULL THEN 'sent' ELSE 'rejected' END"
        )
    for col, ddl in (("category", "TEXT"), ("budget", "REAL"),
                     ("hours", "REAL"), ("deal_price", "REAL")):
        if col not in cols:
            conn.execute(f"ALTER TABLE freelance_orders ADD COLUMN {col} {ddl}")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_fl_dedup ON freelance_orders(dedup_key)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_fl_category ON freelance_orders(category)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_fl_status ON freelance_orders(status)"
    )
    conn.execute("""
        CREATE TABLE IF NOT EXISTS kv (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)


def kv_get(key: str, default: str | None = None) -> str | None:
    with _connect() as conn:
        row = conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default


def kv_set(key: str, value) -> None:
    with _connect() as conn:
        conn.execute(
            "INSERT INTO kv (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, str(value)),
        )


def freelance_seen_ids(order_ids: list[int]) -> set[int]:
    """Какие из order_id уже есть в БД (отправлены или отброшены)."""
    if not order_ids:
        return set()
    found: set[int] = set()
    with _connect() as conn:
        for i in range(0, len(order_ids), 500):
            chunk = order_ids[i:i + 500]
            marks = ",".join("?" * len(chunk))
            rows = conn.execute(
                f"SELECT order_id FROM freelance_orders WHERE order_id IN ({marks})",
                chunk,
            ).fetchall()
            found.update(r["order_id"] for r in rows)
    return found


def freelance_dup_keys(keys: list[str], days: int, sent_only: bool = False,
                       exclude_ids: list[int] | None = None) -> set[str]:
    """Какие из dedup_key уже встречались за последние N дней.

    Учитываются только отправленные и оценённые заказы: запись-дубль
    (без sent_at и без fit) не должна «съедать» оригинал.
    sent_only - только реально отправленные (sent_at задан); exclude_ids -
    не считать записи с этими order_id (для проверки pending-заказов).
    """
    keys = [k for k in keys if k]
    if not keys:
        return set()
    exclude = list(exclude_ids or [])
    cond = "sent_at IS NOT NULL" if sent_only else "(sent_at IS NOT NULL OR fit IS NOT NULL)"
    excl_sql = f" AND order_id NOT IN ({','.join('?' * len(exclude))})" if exclude else ""
    found: set[str] = set()
    with _connect() as conn:
        for i in range(0, len(keys), 500):
            chunk = keys[i:i + 500]
            marks = ",".join("?" * len(chunk))
            rows = conn.execute(
                f"SELECT DISTINCT dedup_key FROM freelance_orders "
                f"WHERE dedup_key IN ({marks}) AND seen_at >= datetime('now', ?) "
                f"AND {cond}{excl_sql}",
                [*chunk, f"-{days} days", *exclude],
            ).fetchall()
            found.update(r["dedup_key"] for r in rows)
    return found


def freelance_save(
    order_id: int,
    site: str,
    title: str,
    url: str,
    price_text: str,
    published_at: str,
    dedup: str,
    fit: int | None,
    ai_json: str,
    status: str,
    attempts: int = 0,
    message_id: int | None = None,
    category: str | None = None,
    budget: float | None = None,
    hours: float | None = None,
) -> None:
    """Запоминает заказ (upsert).

    status: pending (ждёт оценки/отправки), sent, taken, dismissed, expired,
    rejected (просмотрен, но не отправлен: низкий fit, дубль, сводка).
    Уже закрытый пользователем/очисткой статус (taken/dismissed/expired) повторной
    записью не затирается. message_id и sent_at, однажды записанные, сохраняются.
    category/budget/hours - для статистики рынка и ставки (None не затирает записанное).
    """
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO freelance_orders
                (order_id, site, title, url, price_text, fit, ai_json,
                 published_at, dedup_key, status, attempts, message_id,
                 category, budget, hours, sent_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    CASE WHEN ? IN ('sent', 'taken', 'dismissed')
                         THEN CURRENT_TIMESTAMP ELSE NULL END)
            ON CONFLICT(order_id) DO UPDATE SET
                site = excluded.site, title = excluded.title, url = excluded.url,
                price_text = excluded.price_text,
                fit = COALESCE(excluded.fit, freelance_orders.fit),
                ai_json = CASE WHEN excluded.ai_json != '' THEN excluded.ai_json
                               ELSE freelance_orders.ai_json END,
                published_at = excluded.published_at,
                dedup_key = excluded.dedup_key,
                attempts = excluded.attempts,
                message_id = COALESCE(excluded.message_id, freelance_orders.message_id),
                category = COALESCE(excluded.category, freelance_orders.category),
                budget = COALESCE(excluded.budget, freelance_orders.budget),
                hours = COALESCE(excluded.hours, freelance_orders.hours),
                sent_at = COALESCE(freelance_orders.sent_at, excluded.sent_at),
                status = CASE
                    WHEN freelance_orders.status IN ('taken', 'dismissed', 'expired')
                         AND excluded.status IN ('sent', 'pending', 'rejected')
                    THEN freelance_orders.status
                    ELSE excluded.status END
            """,
            (order_id, site, title, url, price_text, fit, ai_json,
             published_at, dedup, status, attempts, message_id,
             category, budget, hours, status),
        )


def freelance_set_status(order_id: int, status: str) -> bool:
    """Меняет статус заказа. True, если заказ найден."""
    with _connect() as conn:
        cur = conn.execute(
            "UPDATE freelance_orders SET status = ? WHERE order_id = ?",
            (status, order_id),
        )
        return cur.rowcount > 0


def freelance_set_deal_price(order_id: int, price: float | None) -> bool:
    """Записывает цену, о которой договорились (None - сбросить). True, если заказ найден."""
    with _connect() as conn:
        cur = conn.execute(
            "UPDATE freelance_orders SET deal_price = ? WHERE order_id = ?",
            (price, order_id),
        )
        return cur.rowcount > 0


def market_stats(category: str, days: int = 90, min_points: int = 5,
                 report_small: bool = False) -> dict | None:
    """Рынок категории по реальным бюджетам заказчиков (budget > 0) за days дней:
    {"median", "p25", "p75", "n"}. Берутся только заказы с fit >= FREELANCE_MIN_FIT и
    одна строка на dedup_key (кросспосты не искажают медиану). Меньше min_points
    точек - None (якорь не применяется), а с report_small - {"n": N} для подписи «мало данных»."""
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT MAX(budget) AS budget FROM freelance_orders
            WHERE category = ? AND budget > 0 AND seen_at >= datetime('now', ?)
              AND fit >= ?
            GROUP BY COALESCE(NULLIF(dedup_key, ''), CAST(order_id AS TEXT))
            """,
            (category, f"-{days} days", FREELANCE_MIN_FIT),
        ).fetchall()
    values = sorted(float(r["budget"]) for r in rows)
    if len(values) < min_points:
        return {"n": len(values)} if report_small else None
    q = statistics.quantiles(values, n=4, method="inclusive")
    return {"median": statistics.median(values), "p25": q[0], "p75": q[2], "n": len(values)}


def freelance_deals(category: str) -> list[tuple[float, float, float]]:
    """Сделки категории: [(deal_price, hours, risk)] - для ставки по вашим реальным ценам.
    risk - коэффициент риска из ai_json (нет - 1.0). Сделки, где цена была ограничена
    бюджетом заказчика (budget_capped), не берутся: они не отражают нашу ставку."""
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT deal_price, hours, ai_json FROM freelance_orders
            WHERE category = ? AND deal_price > 0 AND hours > 0
            """,
            (category,),
        ).fetchall()
    result = []
    for r in rows:
        try:
            ai = json.loads(r["ai_json"] or "{}")
        except ValueError:
            ai = {}
        if not isinstance(ai, dict) or ai.get("budget_capped"):
            continue
        try:
            risk = float(ai.get("price_risk") or 1.0)
        except (TypeError, ValueError):
            risk = 1.0
        result.append((float(r["deal_price"]), float(r["hours"]), risk if risk > 0 else 1.0))
    return result


def freelance_deal_rows() -> list[dict]:
    """Заказы со сделкой: category, deal_price, ai_json (для отчёта о точности цен)."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT category, deal_price, ai_json FROM freelance_orders WHERE deal_price > 0"
        ).fetchall()
        return [dict(r) for r in rows]


def freelance_set_message(order_id: int, message_id: int) -> None:
    """Запоминает id сообщения Telegram с заказом (для автоудаления)."""
    with _connect() as conn:
        conn.execute(
            "UPDATE freelance_orders SET message_id = ? WHERE order_id = ?",
            (message_id, order_id),
        )


def freelance_expired_messages(ttl_hours: int, limit: int = 200) -> list[dict]:
    """Отправленные (status='sent', не «Взял») сообщения старше ttl_hours — под удаление."""
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT order_id, message_id
            FROM freelance_orders
            WHERE status = 'sent' AND message_id IS NOT NULL
              AND sent_at < datetime('now', ?)
            ORDER BY sent_at
            LIMIT ?
            """,
            (f"-{ttl_hours} hours", limit),
        ).fetchall()
        return [dict(r) for r in rows]


def freelance_pending(limit: int = 100) -> list[dict]:
    """Заказы со статусом pending (не оценены или не отправлены), новые первыми."""
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT order_id, attempts, ai_json, published_at, dedup_key
            FROM freelance_orders
            WHERE status = 'pending'
            ORDER BY order_id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]


def freelance_recent(limit: int = 10, ttl_hours: int | None = None) -> list[dict]:
    """Последние отправленные заказы, сообщения которых ещё не удалены по TTL."""
    if ttl_hours is None:
        from config import FREELANCE_MESSAGE_TTL_HOURS
        ttl_hours = FREELANCE_MESSAGE_TTL_HOURS
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT order_id, site, title, url, price_text, fit, published_at
            FROM freelance_orders
            WHERE status = 'sent' AND sent_at >= datetime('now', ?)
            ORDER BY order_id DESC
            LIMIT ?
            """,
            (f"-{ttl_hours} hours", limit),
        ).fetchall()
        return [dict(r) for r in rows]


def freelance_stats() -> dict:
    """Счётчики за последние сутки: прошли префильтр / отправлены."""
    with _connect() as conn:
        seen = conn.execute(
            "SELECT COUNT(*) FROM freelance_orders "
            "WHERE seen_at >= datetime('now', '-1 day')"
        ).fetchone()[0]
        sent = conn.execute(
            "SELECT COUNT(*) FROM freelance_orders "
            "WHERE sent_at >= datetime('now', '-1 day')"
        ).fetchone()[0]
        return {"seen_24h": seen, "sent_24h": sent}


if __name__ == "__main__":
    init_db()
    print("База инициализирована:", DB_PATH)
    print("Статистика:", stats())