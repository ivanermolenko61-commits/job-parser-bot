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
import math
import os
import re
import sqlite3
import statistics
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from config import (
    DB_PATH, DUPLICATE_WINDOW_DAYS, FREELANCE_JOURNAL_DAYS, FREELANCE_MIN_FIT, SENT_HISTORY_DAYS,
)
from freelance import journal as jr
from parsers.filters import dedup_key


# Сколько дней хранить заказы фриланса
FREELANCE_KEEP_DAYS = FREELANCE_JOURNAL_DAYS
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

    m = re.match(r"^(\d{1,2})\s+([а-яё]+)(?:\s+(\d{4}))?", text.lower().strip())
    if m:
        day = int(m.group(1))
        month_name = m.group(2)
        month = MONTHS_RU.get(month_name)
        if month:
            now = datetime.now()
            if m.group(3):
                # год указан явно ("23 октября 2025"): берём его как есть
                year = int(m.group(3))
            else:
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
            WHERE COALESCE(
                -- дата публикации; если её нет или она не разбирается ("вчера") - found_at
                datetime(REPLACE(NULLIF(published_at, ''), 'T', ' ')),
                datetime(found_at)
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
        # Технический журнал живёт FREELANCE_JOURNAL_DAYS; активные лиды (сохранён, откликнулся,
        # обсуждаем, договорились) чистка не трогает, как и строки со сделкой и рыночные бюджеты
        active = ",".join("?" * len(jr.LEAD_ACTIVE))
        conn.execute(
            f"""
            DELETE FROM freelance_orders
            WHERE seen_at < datetime('now', ?)
              AND COALESCE(lead, '') NOT IN ({active})
              AND NOT (COALESCE(budget, 0) > 0 AND COALESCE(fit, 0) >= ? AND seen_at >= datetime('now', ?))
              AND NOT (COALESCE(deal_price, 0) > 0 AND seen_at >= datetime('now', ?))
            """,
            (f'-{FREELANCE_KEEP_DAYS} days', *jr.LEAD_ACTIVE, FREELANCE_MIN_FIT,
             f'-{FREELANCE_MARKET_DAYS} days', f'-{FREELANCE_DEAL_DAYS} days'),
        )
        # события удалённых заказов не остаются; события активных лидов живут, пока жив заказ
        conn.execute("DELETE FROM freelance_manual_inputs WHERE order_id NOT IN (SELECT order_id FROM freelance_orders)")
        conn.execute("DELETE FROM freelance_lead_events "
                     "WHERE order_id NOT IN (SELECT order_id FROM freelance_orders)")
        conn.execute("DELETE FROM freelance_funnel_daily WHERE day < date('now', ?)",
                     (f'-{FREELANCE_KEEP_DAYS * 2} days',))
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
    # Журнал увиденных заказов (freelance/journal.py): три независимых измерения вместо одного status
    for col, ddl in (
        ("origin", "TEXT"), ("manual_key", "TEXT"), ("title_key", "TEXT"), ("content_hash", "TEXT"), ("snippet", "TEXT"),
        ("pub_unreliable", "INTEGER DEFAULT 0"), ("tech", "TEXT"), ("queue_reason", "TEXT"),
        ("reject_reason", "TEXT"), ("verdict", "TEXT"), ("dup_of", "INTEGER"), ("delivery", "TEXT"),
        ("assessed_at", "TEXT"), ("delivered_at", "TEXT"), ("card_deleted_at", "TEXT"),
        ("stale_at", "TEXT"), ("stale_reason", "TEXT"), ("desc_changed", "INTEGER DEFAULT 0"),
        ("lead", "TEXT"), ("lead_reason", "TEXT"), ("lead_at", "TEXT"),
        # Ф08: следующее действие, напоминание (UTC), отправленное напоминание, автоподборка необработанных
        ("next_action", "TEXT"), ("remind_at", "TEXT"), ("reminded_at", "TEXT"), ("offered_at", "TEXT"), ("offer_claimed_at", "TEXT"),
    ):
        if col not in cols:
            conn.execute(f"ALTER TABLE freelance_orders ADD COLUMN {col} {ddl}")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_manual_key ON freelance_orders(manual_key) WHERE manual_key IS NOT NULL")
    conn.execute("""CREATE TABLE IF NOT EXISTS freelance_manual_inputs (
        message_id INTEGER PRIMARY KEY, order_id INTEGER NOT NULL, note TEXT NOT NULL)""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_manual_inputs_order ON freelance_manual_inputs(order_id)")
    _backfill_journal(conn)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_fl_title_key ON freelance_orders(title_key)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_fl_tech ON freelance_orders(tech)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_fl_lead ON freelance_orders(lead)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_fl_remind ON freelance_orders(remind_at)")
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
        CREATE TABLE IF NOT EXISTS freelance_funnel_daily (
            day TEXT, site TEXT, reason TEXT, n INTEGER DEFAULT 0,
            PRIMARY KEY (day, site, reason)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS freelance_assessments (
            order_id INTEGER NOT NULL, content_hash TEXT NOT NULL, assessed_at TEXT, fit INTEGER,
            model TEXT, ai_json TEXT, notified INTEGER DEFAULT 0,
            PRIMARY KEY (order_id, content_hash)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS freelance_reassess (
            order_id INTEGER PRIMARY KEY, content_hash TEXT NOT NULL, similarity REAL, significant INTEGER DEFAULT 0,
            status TEXT DEFAULT 'pending', attempts INTEGER DEFAULT 0, old_fit INTEGER, old_price REAL,
            created_at TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS freelance_actuals (
            order_id INTEGER PRIMARY KEY, hours REAL, expenses REAL, updated_at TEXT
        )
    """)
    # История действий по лиду (Ф08): основа для отмены; событие хранит состояние ДО действия
    conn.execute("""
        CREATE TABLE IF NOT EXISTS freelance_lead_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id INTEGER NOT NULL,
            at TEXT NOT NULL,
            kind TEXT NOT NULL,
            from_lead TEXT, to_lead TEXT,
            prev_reason TEXT, prev_next TEXT, prev_remind TEXT, prev_deal_price REAL,
            reason TEXT,
            undone INTEGER DEFAULT 0
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_fl_lead_ev ON freelance_lead_events(order_id, id)")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS kv (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)


def _backfill_journal(conn) -> None:
    """Старые строки (до журнала) получают новые измерения из status. Без выдумывания истории:
    время оценки и удаления карточки неизвестно (NULL / 'unknown'), «rejected» с высоким fit - это
    сводка «ещё N» либо дубль оценённого, различить нельзя (delivery='unknown'), «rejected» без
    оценки - tech='legacy' с причиной legacy_unknown. Идемпотентно: трогает только tech IS NULL."""
    if conn.execute("SELECT 1 FROM freelance_orders WHERE tech IS NULL LIMIT 1").fetchone() is None:
        return
    conn.execute(
        """
        UPDATE freelance_orders SET
          origin = CASE WHEN order_id >= 20000000000 THEN 'kwork'
                        WHEN order_id >= 10000000000 THEN 'flru' ELSE 'alot' END,
          tech = CASE
            WHEN status = 'pending' AND fit IS NULL THEN 'queued'
            WHEN status = 'expired' AND sent_at IS NULL THEN 'stale'
            WHEN status = 'rejected' AND fit IS NULL THEN 'legacy'
            WHEN status IN ('pending', 'sent', 'taken', 'dismissed', 'expired', 'rejected') THEN 'assessed'
            ELSE 'legacy' END,
          queue_reason = CASE WHEN status = 'pending' AND fit IS NULL
                              THEN CASE WHEN COALESCE(attempts, 0) > 0 THEN 'ai_error' ELSE 'ai_wait' END END,
          verdict = CASE WHEN fit IS NULL THEN CASE WHEN sent_at IS NOT NULL THEN 'no_ai' END
                         WHEN fit < ? THEN 'low_fit' ELSE 'ok' END,
          delivery = CASE WHEN sent_at IS NOT NULL THEN 'card'
                          WHEN status = 'pending' AND fit IS NOT NULL THEN 'pending'
                          WHEN status = 'rejected' AND fit >= ? THEN 'unknown'
                          ELSE 'none' END,
          delivered_at = sent_at,
          lead = CASE status WHEN 'taken' THEN 'agreed' WHEN 'dismissed' THEN 'lost' END,
          lead_reason = CASE WHEN status = 'dismissed' THEN 'не подходит' END,
          card_deleted_at = CASE WHEN status = 'expired' AND sent_at IS NOT NULL THEN 'unknown' END,
          stale_reason = CASE WHEN status = 'expired' AND sent_at IS NULL THEN 'legacy_expired' END,
          reject_reason = CASE WHEN status = 'rejected' AND fit IS NULL THEN 'legacy_unknown' END
        WHERE tech IS NULL
        """,
        (FREELANCE_MIN_FIT, FREELANCE_MIN_FIT),
    )


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


def _now_sql() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def freelance_dup_candidates(title_key: str, days: int, exclude_id: int | None = None) -> list[dict]:
    """Оригиналы, с которыми можно сравнить новый заказ: та же биржа и название (title_key),
    увиденные за последние days дней, сами не дубли и не отсеянные правилом. Для сравнения
    текста нужен snippet; строки старой схемы без него ничего не совпадут."""
    if not title_key:
        return []
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT order_id, origin, snippet, COALESCE(budget, 0) AS price, published_at,
                   COALESCE(pub_unreliable, 0) AS unreliable, content_hash
            FROM freelance_orders
            WHERE title_key = ? AND seen_at >= datetime('now', ?)
              AND tech IN ('queued', 'assessed', 'stale') AND order_id != ?
            ORDER BY seen_at
            """,
            (title_key, f"-{int(days)} days", exclude_id or -1),
        ).fetchall()
        return [dict(r) for r in rows]


def freelance_known(order_ids: list[int]) -> dict[int, dict]:
    """Уже виденные заказы: {order_id: {content_hash, tech, snippet, desc_changed}} (для обнаружения правки описания)."""
    found: dict[int, dict] = {}
    if not order_ids:
        return found
    with _connect() as conn:
        for i in range(0, len(order_ids), 500):
            chunk = order_ids[i:i + 500]
            marks = ",".join("?" * len(chunk))
            rows = conn.execute(
                f"SELECT order_id, content_hash, tech, snippet, COALESCE(desc_changed, 0) AS desc_changed "
                f"FROM freelance_orders WHERE order_id IN ({marks})", chunk).fetchall()
            found.update((r["order_id"], dict(r)) for r in rows)
    return found


_SAVE_COLUMNS = (
    "site", "title", "url", "price_text", "fit", "ai_json", "published_at", "dedup_key", "attempts",
    "message_id", "category", "budget", "hours", "sent_at", "status", "origin", "title_key",
    "content_hash", "snippet", "pub_unreliable", "tech", "queue_reason", "reject_reason", "verdict",
    "dup_of", "delivery", "assessed_at", "delivered_at", "card_deleted_at", "stale_at", "stale_reason",
    "desc_changed", "lead", "lead_reason", "lead_at",
)


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
    *,
    origin: str | None = None,
    title_key: str | None = None,
    content_hash: str | None = None,
    snippet: str | None = None,
    pub_unreliable: bool | None = None,
    tech: str | None = None,
    queue_reason: str | None = None,
    reject_reason: str | None = None,
    verdict: str | None = None,
    dup_of: int | None = None,
    delivery: str | None = None,
    stale_reason: str | None = None,
) -> None:
    """Запоминает заказ (upsert) и ведёт три независимых измерения (freelance/journal.py).

    Состояния можно задать явно (tech/delivery/verdict/queue_reason/reject_reason/dup_of/stale_reason);
    не заданные берутся из старого status (pending, sent, taken, dismissed, expired, rejected) -
    совместимость. Однажды достигнутое не откатывается: доставка (card/digest), этап сделки (lead),
    оценка (assessed не возвращается в queued), устаревание (stale не возвращается в очередь);
    message_id и sent_at, однажды записанные, сохраняются. Время оценки и доставки пишется один раз.
    category/budget/hours - для статистики рынка и ставки (None не затирает записанное).
    Поле status - зеркало для старых запросов (jr.legacy_status).
    """
    guess = jr.status_to_states(status, fit, attempts)
    now = _now_sql()
    with _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")   # чтение-слияние-запись без гонки между потоками оценки
        row = conn.execute("SELECT * FROM freelance_orders WHERE order_id = ?", (order_id,)).fetchone()
        old = dict(row) if row else {}
        new_tech = tech or guess["tech"] or jr.LEGACY
        old_tech = old.get("tech")
        if old_tech == jr.ASSESSED and new_tech in (jr.QUEUED, jr.FILTERED, jr.DUPLICATE, jr.STALE):
            new_tech = old_tech               # оценка уже получена и стоила квоты
        elif old_tech == jr.STALE:
            new_tech = old_tech               # устаревший терминален: запоздавшая оценка не возвращает его в обработку
        new_delivery = delivery or guess["delivery"]
        if old.get("delivery") in (jr.D_CARD, jr.D_DIGEST) and new_delivery in (
                None, jr.D_NONE, jr.D_PENDING, jr.D_UNKNOWN):
            new_delivery = old["delivery"]    # доставленное не откатывается
        elif old_tech == jr.STALE and new_delivery not in (jr.D_CARD, jr.D_DIGEST):
            new_delivery = old.get("delivery") or jr.D_NONE   # оценка сохраняется, очередь доставки не воскресает
        new_delivery = new_delivery or old.get("delivery") or jr.D_NONE
        new_lead = old.get("lead") or guess["lead"]
        new_verdict = verdict or guess["verdict"] or old.get("verdict")
        values = {
            "site": site, "title": title, "url": url, "price_text": price_text,
            "fit": fit if fit is not None else old.get("fit"),
            "ai_json": ai_json if ai_json != "" else (old.get("ai_json") or ""),
            "published_at": published_at, "dedup_key": dedup, "attempts": attempts,
            "message_id": message_id if message_id is not None else old.get("message_id"),
            "category": category if category is not None else old.get("category"),
            "budget": budget if budget is not None else old.get("budget"),
            "hours": hours if hours is not None else old.get("hours"),
            "sent_at": old.get("sent_at") or (now if new_delivery == jr.D_CARD else None),
            "origin": origin or old.get("origin"),
            "title_key": title_key or old.get("title_key"),
            "content_hash": content_hash or old.get("content_hash"),
            "snippet": snippet if snippet is not None else old.get("snippet"),
            "pub_unreliable": int(pub_unreliable) if pub_unreliable is not None
            else int(old.get("pub_unreliable") or 0),
            "tech": new_tech,
            "queue_reason": (queue_reason or guess["queue_reason"]) if new_tech == jr.QUEUED else None,
            "reject_reason": reject_reason or old.get("reject_reason"),
            "verdict": new_verdict,
            "dup_of": dup_of if dup_of is not None else old.get("dup_of"),
            "delivery": new_delivery,
            "assessed_at": old.get("assessed_at") or (now if new_tech == jr.ASSESSED else None),
            "delivered_at": old.get("delivered_at") or (
                now if new_delivery in (jr.D_CARD, jr.D_DIGEST) else None),
            "card_deleted_at": old.get("card_deleted_at"),
            "stale_at": old.get("stale_at") or (now if new_tech == jr.STALE else None),
            "stale_reason": stale_reason or old.get("stale_reason"),
            "desc_changed": int(old.get("desc_changed") or 0),
            "lead": new_lead,
            "lead_reason": old.get("lead_reason"),
            "lead_at": old.get("lead_at") or (now if new_lead else None),
        }
        values["status"] = jr.legacy_status(new_tech, new_delivery, new_lead,
                                            bool(values["card_deleted_at"]))
        cols = ("order_id", *_SAVE_COLUMNS)
        marks = ",".join("?" * len(cols))
        updates = ",".join(f"{c} = excluded.{c}" for c in _SAVE_COLUMNS)
        conn.execute(
            f"INSERT INTO freelance_orders ({','.join(cols)}) VALUES ({marks}) "
            f"ON CONFLICT(order_id) DO UPDATE SET {updates}",
            (order_id, *(values[c] for c in _SAVE_COLUMNS)),
        )


def _update_states(order_id: int, _guard=None, **fields) -> bool:
    """Точечная правка состояний заказа с пересчётом зеркала status. True - заказ найден.
    _guard(row) -> bool проверяется в той же транзакции, что и запись: False - строка не меняется
    (конкурентная доставка или устаревание не затираются); заказ при этом считается найденным."""
    with _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM freelance_orders WHERE order_id = ?", (order_id,)).fetchone()
        if row is None:
            return False
        if _guard is not None and not _guard(dict(row)):
            return True
        merged = {**dict(row), **fields}
        merged["status"] = jr.legacy_status(
            merged.get("tech"), merged.get("delivery"), merged.get("lead"), bool(merged.get("card_deleted_at")))
        names = [*fields, "status"]
        conn.execute(
            f"UPDATE freelance_orders SET {','.join(f'{n} = ?' for n in names)} WHERE order_id = ?",
            (*(merged[n] for n in names), order_id),
        )
        return True


def freelance_mark_stale(order_id: int, reason: str) -> bool:
    """Заказ устарел (не оценён/не доставлен до предела очереди): состояние с причиной, без удаления.
    Уже доставленный, дубль и отсеянный остаются как есть."""
    # оценка (fit, assessed_at) остаётся: в отчёте видно, что заказ оценили, но не доставили;
    # проверка и запись - одна транзакция: доставка, состоявшаяся между ними, не затирается
    return _update_states(
        order_id,
        lambda r: r.get("delivery") not in (jr.D_CARD, jr.D_DIGEST)
        and r.get("tech") not in (jr.STALE, jr.DUPLICATE, jr.FILTERED),
        tech=jr.STALE, delivery=jr.D_NONE, stale_reason=reason, stale_at=_now_sql(), queue_reason=None)


def freelance_mark_filtered(order_id: int, reason: str) -> bool:
    """Отсеян правилом (например, после смены фильтра ожидающий заказ больше не проходит)."""
    return _update_states(
        order_id, lambda r: r.get("tech") in (jr.QUEUED, jr.ASSESSED)
        and r.get("delivery") not in (jr.D_CARD, jr.D_DIGEST),
        tech=jr.FILTERED, reject_reason=reason, queue_reason=None)


def freelance_mark_card_deleted(order_id: int) -> bool:
    """Карточка удалена из чата по сроку. Ни технический статус, ни этап сделки не меняются."""
    return _update_states(order_id, card_deleted_at=_now_sql())


def freelance_mark_desc_changed(order_id: int, content_hash: str, snippet: str | None = None) -> bool:
    """Заказчик поправил описание уже оценённого заказа: оценка не повторяется, пометка остаётся
    (в карточке и отчёте); отпечаток обновлён, чтобы не срабатывать каждый цикл."""
    fields: dict = {"desc_changed": 1, "content_hash": content_hash}
    if snippet is not None:
        fields["snippet"] = snippet
    return _update_states(order_id, **fields)


def freelance_requeue(order_id: int, content_hash: str, snippet: str, queue_reason: str = jr.Q_WAIT) -> bool:
    """Отсеянный заказ получил новое описание: снова в очередь на оценку."""
    return _update_states(order_id, lambda r: r.get("tech") == jr.FILTERED,
                          tech=jr.QUEUED, queue_reason=queue_reason, reject_reason=None,
                          content_hash=content_hash, snippet=snippet, desc_changed=1)


def freelance_set_queue_reason(order_id: int, reason: str, attempts: int | None = None) -> bool:
    """Причина, по которой заказ ещё не оценён (нет квоты, ошибка AI, резерв, ждёт очереди)."""
    with _connect() as conn:
        row = conn.execute("SELECT tech FROM freelance_orders WHERE order_id = ?", (order_id,)).fetchone()
    if row is None or row["tech"] != jr.QUEUED:
        return False
    fields: dict = {"queue_reason": reason}
    if attempts is not None:
        fields["attempts"] = attempts
    return _update_states(order_id, **fields)


def freelance_set_lead(order_id: int, lead: str | None, reason: str | None = None) -> bool:
    """Этап сделки (решение пользователя). Не зависит от доставки и удаления карточки."""
    if lead is not None and lead not in jr.LEAD_STAGES:
        raise ValueError(f"неизвестный этап сделки: {lead}")
    return _update_states(order_id, lead=lead, lead_reason=reason, lead_at=_now_sql() if lead else None)


def freelance_get(order_id: int) -> dict | None:
    """Заказ из БД по id (None, если нет)."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM freelance_orders WHERE order_id = ?", (order_id,)
        ).fetchone()
        return dict(row) if row else None


def freelance_set_status(order_id: int, status: str) -> bool:
    """Совместимость со старым API статусов (True, если заказ найден). taken/dismissed - это этап
    сделки (lead), expired - карточка удалена из чата (или заказ устарел, если не доставлялся);
    остальные старые статусы выводятся из tech/delivery и напрямую не ставятся."""
    if status == "taken":
        return freelance_set_lead(order_id, jr.LEAD_AGREED)
    if status == "dismissed":
        return freelance_set_lead(order_id, jr.LEAD_LOST, "не подходит")
    if status == "expired":
        row = freelance_get(order_id)
        if row is None:
            return False
        if row.get("delivery") == jr.D_CARD:
            return freelance_mark_card_deleted(order_id)
        return freelance_mark_stale(order_id, jr.S_QUEUE_EXPIRED)
    return freelance_get(order_id) is not None


def freelance_set_deal_price(order_id: int, price: float | None, *, require_price_stage: bool = False) -> bool:
    """Цена сделки (None - сброс). require_price_stage атомарно отклоняет ответ на раннем этапе.
    Без флага сохраняет совместимость старого API; True означает, что строка обновлена."""
    stage_guard = " AND lead IN ('agreed', 'done')" if require_price_stage else ""
    with _connect() as conn:
        cur = conn.execute(
            "UPDATE freelance_orders SET deal_price = ? WHERE order_id = ?" + stage_guard,
            (price, order_id),
        )
        return cur.rowcount > 0


# --- Ф08: этапы сделки с историей, отменой, следующим действием и напоминанием. Только аддитивно:
# freelance_set_lead / freelance_set_status (старый API) работают как раньше и событий не пишут ---

_KEEP = object()   # «поле не менять» (None - это «очистить»)
_LEAD_COLS = ("order_id, site, title, url, fit, lead, lead_reason, lead_at, next_action, remind_at, "
              "reminded_at, deal_price, delivered_at, card_deleted_at, message_id")
_LEAD_STATE = ("lead", "lead_reason", "next_action", "remind_at", "deal_price")
LEAD_LOST_REASONS = (*jr.LOST_REASONS, jr.LOST_LEGACY)


def _lead_snapshot(row: dict) -> dict:
    return {k: row.get(k) for k in _LEAD_STATE}


def _lead_write(conn, row: dict, kind: str, changes: dict, event_reason: str | None, now: str) -> dict:
    """Запись изменения лида и события в ТЕКУЩЕЙ транзакции (вызывающий уже сделал BEGIN IMMEDIATE)."""
    merged = {**row, **changes}
    merged["status"] = jr.legacy_status(merged.get("tech"), merged.get("delivery"), merged.get("lead"),
                                        bool(merged.get("card_deleted_at")))
    names = [*changes, "status"]
    conn.execute(
        f"UPDATE freelance_orders SET {','.join(f'{n} = ?' for n in names)} WHERE order_id = ?",
        (*(merged[n] for n in names), row["order_id"]),
    )
    cur = conn.execute(
        "INSERT INTO freelance_lead_events (order_id, at, kind, from_lead, to_lead, prev_reason, prev_next, "
        "prev_remind, prev_deal_price, reason) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (row["order_id"], now, kind, row.get("lead"), merged.get("lead"), row.get("lead_reason"),
         row.get("next_action"), row.get("remind_at"), row.get("deal_price"), event_reason),
    )
    return {"ok": True, "changed": True, "event_id": cur.lastrowid,
            "before": _lead_snapshot(row), "after": _lead_snapshot(merged)}


def _lead_row(conn, order_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM freelance_orders WHERE order_id = ?", (order_id,)).fetchone()
    return dict(row) if row else None


def freelance_lead_stage(order_id: int, lead: str, reason: str | None = None, *, next_action=_KEEP,
                         remind_at=_KEEP, now: str | None = None) -> dict:
    """Перевод лида на этап, одной транзакцией со строкой события (из него работает отмена).
    Неизвестный этап и отказ без причины из списка - ValueError, строка не меняется. Тот же этап и та же
    причина - changed=False без события. При уходе с agreed/done на любой этап, кроме agreed/done (более
    ранний или отказ), deal_price обнуляется (прежнее значение остаётся в prev_deal_price события).
    next_action/remind_at: _KEEP - не менять.
    Возвращает {ok, changed, before, after, event_id} либо {ok: False, reason: 'not_found'}."""
    if lead not in jr.LEAD_STAGES:
        raise ValueError(f"неизвестный этап сделки: {lead}")
    if lead == jr.LEAD_LOST:
        if reason not in LEAD_LOST_REASONS:
            raise ValueError(f"причина отказа не из списка: {reason!r}")
    else:
        reason = None
    now = now or _now_sql()
    with _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = _lead_row(conn, order_id)
        if row is None:
            return {"ok": False, "changed": False, "reason": "not_found"}
        if row.get("lead") == lead and row.get("lead_reason") == reason:
            snap = _lead_snapshot(row)
            return {"ok": True, "changed": False, "before": snap, "after": snap}
        changes = {"lead": lead, "lead_reason": reason, "lead_at": now}
        if next_action is not _KEEP:
            changes["next_action"] = next_action
        if remind_at is not _KEEP:
            changes["remind_at"] = remind_at
        if row.get("lead") in jr.LEAD_PRICE_STAGES and lead not in jr.LEAD_PRICE_STAGES:
            changes["deal_price"] = None   # цена сделки есть только у agreed/done (отказ её не оставляет)
        return _lead_write(conn, row, "stage", changes, reason, now)



def _lead_active_change(order_id: int, kind: str, changes: dict, event_reason: str | None,
                        now: str | None) -> dict:
    now = now or _now_sql()
    with _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = _lead_row(conn, order_id)
        if row is None:
            return {"ok": False, "changed": False, "reason": "not_found"}
        if row.get("lead") not in jr.LEAD_ACTIVE:
            return {"ok": False, "changed": False, "reason": "no_active_lead"}
        if all(row.get(k) == v for k, v in changes.items()):
            snap = _lead_snapshot(row)
            return {"ok": True, "changed": False, "before": snap, "after": snap}
        return _lead_write(conn, row, kind, changes, event_reason, now)


def freelance_lead_next(order_id: int, text: str | None, now: str | None = None) -> dict:
    """Своё следующее действие у активного лида (None - убрать). Срок напоминания не меняется."""
    if text is not None:
        text = " ".join(str(text).split())[:jr.LEAD_TEXT_MAX] or None
    return _lead_active_change(order_id, "next", {"next_action": text}, text, now)


def freelance_lead_remind(order_id: int, remind_at: str | None, now: str | None = None) -> dict:
    """Срок напоминания у активного лида (UTC 'YYYY-MM-DD HH:MM:SS'; None - без напоминания)."""
    return _lead_active_change(order_id, "remind", {"remind_at": remind_at}, remind_at, now)


def freelance_lead_undo(order_id: int, now: str | None = None) -> dict:
    """Отмена последнего действия с лидом: восстанавливает этап, причину, действие и срок; цена сделки - на
    agreed/done прежняя (если своей нет), на более ранних этапах её нет. Повторная отмена идёт на шаг назад.
    Нет событий - {ok: False, reason: 'nothing'}."""
    now = now or _now_sql()
    with _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = _lead_row(conn, order_id)
        if row is None:
            return {"ok": False, "changed": False, "reason": "not_found"}
        ev = conn.execute(
            "SELECT * FROM freelance_lead_events WHERE order_id = ? AND undone = 0 AND kind != 'undo' "
            "ORDER BY id DESC LIMIT 1", (order_id,)).fetchone()
        if ev is None:
            return {"ok": False, "changed": False, "reason": "nothing"}
        ev = dict(ev)
        changes = {"next_action": ev["prev_next"], "remind_at": ev["prev_remind"]}
        if ev["kind"] == "stage":
            target = ev["from_lead"]
            changes.update(lead=target, lead_reason=ev["prev_reason"], lead_at=now if target else None)
            if target in jr.LEAD_PRICE_STAGES:
                changes["deal_price"] = row.get("deal_price") or ev["prev_deal_price"]
            else:
                changes["deal_price"] = None
        conn.execute("UPDATE freelance_lead_events SET undone = 1 WHERE id = ?", (ev["id"],))
        res = _lead_write(conn, row, "undo", changes, ev["kind"], now)
        res["undone_kind"] = ev["kind"]
        return res


def freelance_lead_can_undo(order_id: int) -> bool:
    """Наличие отмены независимо от ограничения отображаемой истории."""
    with _connect() as conn:
        return conn.execute(
            "SELECT 1 FROM freelance_lead_events WHERE order_id = ? AND undone = 0 "
            "AND kind != 'undo' LIMIT 1", (order_id,)).fetchone() is not None


def freelance_lead_label(order_id: int) -> str | None:
    """Отказ по экономике сохраняет последнее решение о профиле, в том числе после отмены."""
    with _connect() as conn:
        row = _lead_row(conn, order_id)
        if row is None:
            return None
        if row.get("lead") != jr.LEAD_LOST or row.get("lead_reason") in (
                jr.LOST_NOT_MY_PROFILE, jr.LOST_LEGACY):
            return jr.lead_label(row.get("lead"), row.get("lead_reason"))
        event = conn.execute(
            "SELECT to_lead, reason FROM freelance_lead_events WHERE order_id = ? "
            "AND kind = 'stage' AND undone = 0 AND (to_lead != 'lost' OR reason IN (?, ?)) "
            "ORDER BY id DESC LIMIT 1", (order_id, jr.LOST_NOT_MY_PROFILE, jr.LOST_LEGACY)).fetchone()
        if event is not None:
            return jr.lead_label(event["to_lead"], event["reason"])
        # Старое решение может предшествовать журналу; исходный снимок сохраняется после отмены.
        first = conn.execute(
            "SELECT from_lead, prev_reason FROM freelance_lead_events WHERE order_id = ? "
            "AND kind = 'stage' ORDER BY id LIMIT 1", (order_id,)).fetchone()
        return jr.lead_label(first["from_lead"], first["prev_reason"]) if first else None


def freelance_lead_events(order_id: int, limit: int = 3) -> list[dict]:
    """Последние события лида (новые первыми)."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM freelance_lead_events WHERE order_id = ? ORDER BY id DESC LIMIT ?",
            (order_id, limit)).fetchall()
        return [dict(r) for r in rows]


def freelance_last_deal_price(order_id: int) -> float | None:
    """Последняя обнулённая цена сделки (из событий): подсказка «было N ₽» при повторном «Договорились»."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT prev_deal_price FROM freelance_lead_events WHERE order_id = ? AND prev_deal_price > 0 "
            "ORDER BY id DESC LIMIT 1", (order_id,)).fetchone()
        return float(row["prev_deal_price"]) if row else None


def freelance_lead_get(order_id: int) -> dict | None:
    """Строка лида для карточки /lead_<id>: только из БД (работает после TTL и рестарта)."""
    with _connect() as conn:
        row = conn.execute(f"SELECT {_LEAD_COLS} FROM freelance_orders WHERE order_id = ?",
                           (order_id,)).fetchone()
        return dict(row) if row else None



def freelance_leads_active() -> list[dict]:
    """Активные лиды (saved/applied/discussing/agreed); группировку по этапам делает вызывающий."""
    marks = ",".join("?" * len(jr.LEAD_ACTIVE))
    with _connect() as conn:
        rows = conn.execute(
            f"SELECT {_LEAD_COLS} FROM freelance_orders WHERE lead IN ({marks}) "
            "ORDER BY remind_at IS NULL, remind_at, lead_at", jr.LEAD_ACTIVE).fetchall()
        return [dict(r) for r in rows]


def freelance_leads_closed(days: int = 7) -> list[dict]:
    """Закрытые (done/lost) за последние days дней - для исправления ошибочного закрытия."""
    with _connect() as conn:
        rows = conn.execute(
            f"SELECT {_LEAD_COLS} FROM freelance_orders WHERE lead IN ('done', 'lost') "
            "AND lead_at >= datetime('now', ?) ORDER BY lead_at DESC", (f"-{days} days",)).fetchall()
        return [dict(r) for r in rows]


_UNPROCESSED = ("verdict = 'ok' AND delivery IN ('card', 'digest') AND COALESCE(lead, '') = '' "
                "AND delivered_at >= datetime('now', ?)")


def freelance_unprocessed(days: int = 3, limit: int = 30) -> list[dict]:
    """Необработанные интересные: оценён как подходящий, доставлен карточкой или сводкой «ещё N»
    не раньше days дней назад, решения по лиду нет."""
    with _connect() as conn:
        rows = conn.execute(
            f"SELECT {_LEAD_COLS} FROM freelance_orders WHERE {_UNPROCESSED} "
            "ORDER BY fit DESC, delivered_at DESC LIMIT ?", (f"-{days} days", limit)).fetchall()
        return [dict(r) for r in rows]


def freelance_claim_offers(limit: int = 10, days: int = 3, now: str | None = None) -> tuple[str, list[dict]]:
    """Резервировать строки на десять минут. После сбоя просроченный резерв можно занять снова."""
    now = now or _now_sql()
    with _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        rows = [dict(r) for r in conn.execute(
            f"SELECT {_LEAD_COLS} FROM freelance_orders WHERE {_UNPROCESSED} AND offered_at IS NULL "
            "AND (offer_claimed_at IS NULL OR offer_claimed_at <= datetime(?, '-10 minutes')) "
            "ORDER BY fit DESC, delivered_at DESC LIMIT ?", (f"-{days} days", now, limit)).fetchall()]
        for r in rows:
            conn.execute("UPDATE freelance_orders SET offer_claimed_at = ? WHERE order_id = ?", (now, r["order_id"]))
        return now, rows


def freelance_release_offers(claimed_at: str, order_ids: list[int]) -> int:
    """Подборка не ушла: вернуть заказам право попасть в следующую (только занятые именно этим вызовом)."""
    if not order_ids:
        return 0
    marks = ",".join("?" * len(order_ids))
    with _connect() as conn:
        cur = conn.execute(
            f"UPDATE freelance_orders SET offer_claimed_at = NULL WHERE offer_claimed_at = ? AND order_id IN ({marks})",
            (claimed_at, *order_ids))
        return cur.rowcount


def freelance_confirm_offers(claimed_at: str, order_ids: list[int], day: str) -> None:
    """Зафиксировать доставку и день подборки одной транзакцией после ответа Telegram."""
    with _connect() as conn:
        conn.executemany(
            "UPDATE freelance_orders SET offered_at = ?, offer_claimed_at = NULL "
            "WHERE order_id = ? AND offer_claimed_at = ?",
            [(claimed_at, oid, claimed_at) for oid in order_ids],
        )
        conn.execute("INSERT OR REPLACE INTO kv(key, value) VALUES ('freelance_offer_day', ?)", (day,))


def freelance_reminders_due(now: str | None = None, limit: int = 20) -> list[dict]:
    """Активные лиды, у которых срок напоминания наступил, а последнее напоминание было раньше этого срока."""
    marks = ",".join("?" * len(jr.LEAD_ACTIVE))
    with _connect() as conn:
        rows = conn.execute(
            f"SELECT {_LEAD_COLS} FROM freelance_orders WHERE lead IN ({marks}) AND remind_at IS NOT NULL "
            "AND remind_at <= ? AND (reminded_at IS NULL OR reminded_at < remind_at) "
            "ORDER BY remind_at LIMIT ?", (*jr.LEAD_ACTIVE, now or _now_sql(), limit)).fetchall()
        return [dict(r) for r in rows]


def freelance_mark_reminded(order_id: int, remind_at: str, now: str | None = None) -> bool:
    """Напоминание отправлено. Срок, который успели поменять, не гасится: условие - прежний remind_at."""
    marks = ",".join("?" * len(jr.LEAD_ACTIVE))
    with _connect() as conn:
        cur = conn.execute(
            f"UPDATE freelance_orders SET reminded_at = ? WHERE order_id = ? AND remind_at = ? "
            f"AND lead IN ({marks})", (now or _now_sql(), order_id, remind_at, *jr.LEAD_ACTIVE))
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
    бюджетом заказчика (budget_capped), не берутся: они не отражают нашу ставку.
    Если цену подняли к большому бюджету (budget_raised, а в старых записях - бюджет
    выше price_mid), сделка берётся не дороже нашего расчёта: щедрый бюджет и так
    учитывается при каждой оценке, в ставку его не закладываем."""
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT deal_price, hours, budget, ai_json FROM freelance_orders
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
        try:
            own = float((ai.get("price_own") if ai.get("budget_raised") else ai.get("price_mid")) or 0)
        except (TypeError, ValueError):
            own = 0.0
        deal = float(r["deal_price"])
        raised = ai.get("budget_raised") or float(r["budget"] or 0) > own > 0
        if raised and own > 0:
            deal = min(deal, own)
        result.append((deal, float(r["hours"]), risk if risk > 0 else 1.0))
    return result


def freelance_deal_rows() -> list[dict]:
    """Заказы со сделкой: category, deal_price, ai_json (для отчёта о точности цен)."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT category, deal_price, ai_json FROM freelance_orders WHERE deal_price > 0"
        ).fetchall()
        return [dict(r) for r in rows]


# --- Версии оценки, повторная оценка и фактические результаты (этап 2). Таблицы новые: freelance_orders не меняется ---

def freelance_record_assessment(order_id: int, content_hash: str, fit: int | None, model: str,
                                ai_json: str) -> bool:
    """Запоминает оценку версии текста. False - такая версия (заказ + отпечаток текста) уже оценена: повторно не пишется."""
    if not content_hash:
        return False
    with _connect() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO freelance_assessments (order_id, content_hash, assessed_at, fit, model, ai_json) "
            "VALUES (?, ?, ?, ?, ?, ?)", (order_id, content_hash, _now_sql(), fit, model or "", ai_json or ""))
        return cur.rowcount > 0


def freelance_assessment_get(order_id: int, content_hash: str) -> dict | None:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM freelance_assessments WHERE order_id = ? AND content_hash = ?",
                           (order_id, content_hash)).fetchone()
        return dict(row) if row else None


def freelance_assessment_history(order_id: int) -> list[dict]:
    """История оценок заказа, новые первыми."""
    with _connect() as conn:
        rows = conn.execute("SELECT * FROM freelance_assessments WHERE order_id = ? "
                            "ORDER BY assessed_at DESC, rowid DESC", (order_id,)).fetchall()
        return [dict(r) for r in rows]


def freelance_claim_assessment_notice(order_id: int, content_hash: str) -> bool:
    """Занимает право на повторное уведомление по версии текста: True получает только первый вызов
    (одно уведомление на версию), дальше False."""
    with _connect() as conn:
        cur = conn.execute("UPDATE freelance_assessments SET notified = 1 "
                           "WHERE order_id = ? AND content_hash = ? AND COALESCE(notified, 0) = 0",
                           (order_id, content_hash))
        return cur.rowcount > 0


def freelance_reassess_queue(order_id: int, content_hash: str, similarity: float, significant: bool,
                             old_fit: int | None, old_price: float | None) -> bool:
    """Правка описания оценённого заказа: запись о повторной оценке. Тот же отпечаток повторно не ставится (False).
    Если прежняя запись ещё не отработана, прежняя оценка (old_fit/old_price) сохраняется: она та, что видел владелец."""
    with _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        old = conn.execute("SELECT * FROM freelance_reassess WHERE order_id = ?", (order_id,)).fetchone()
        if old is not None and old["content_hash"] == content_hash:
            return False
        if old is not None and old["status"] == "pending":
            old_fit, old_price = old["old_fit"], old["old_price"]
        conn.execute(
            "INSERT OR REPLACE INTO freelance_reassess (order_id, content_hash, similarity, significant, status, "
            "attempts, old_fit, old_price, created_at) VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?)",
            (order_id, content_hash, float(similarity), 1 if significant else 0,
             "pending" if significant else "skipped", old_fit, old_price, _now_sql()))
        return True


def freelance_reassess_state(order_id: int) -> dict | None:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM freelance_reassess WHERE order_id = ?", (order_id,)).fetchone()
        return dict(row) if row else None


def freelance_reassess_pending(limit: int = 2, max_attempts: int = 3) -> list[dict]:
    """Существенные правки, ждущие повторной оценки (старые первыми, попыток меньше max_attempts)."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM freelance_reassess WHERE status = 'pending' AND significant = 1 AND attempts < ? "
            "ORDER BY created_at, order_id LIMIT ?", (max_attempts, limit)).fetchall()
        return [dict(r) for r in rows]


def freelance_reassess_attempt(order_id: int, content_hash: str) -> bool:
    """Ещё одна неудачная попытка повторной оценки этой версии текста."""
    with _connect() as conn:
        cur = conn.execute("UPDATE freelance_reassess SET attempts = attempts + 1 "
                           "WHERE order_id = ? AND content_hash = ?", (order_id, content_hash))
        return cur.rowcount > 0


def freelance_reassess_finish(order_id: int, content_hash: str, status: str) -> bool:
    """Закрывает запись версии: done (оценка обновлена) или skipped (заказ изменился или не подходит)."""
    with _connect() as conn:
        cur = conn.execute("UPDATE freelance_reassess SET status = ? WHERE order_id = ? AND content_hash = ?",
                           (status, order_id, content_hash))
        return cur.rowcount > 0


def freelance_replace_assessment(order_id: int, content_hash: str, fit: int | None, ai_json: str,
                                 category: str | None, hours: float | None,
                                 delivery: str | None = None, verdict: str | None = None) -> bool:
    """Заменяет текущую оценку оценкой новой версии текста (одна запись) и снимает пометку «описание изменено».
    Только для оценённого заказа и только если текст с тех пор не менялся (отпечаток совпадает). delivery и
    verdict меняются лишь у не доставленных карточкой заказов. False - запись не изменена."""
    fields: dict = {"fit": fit, "ai_json": ai_json, "category": category, "hours": hours, "desc_changed": 0}

    def guard(row: dict) -> bool:
        if row.get("tech") != jr.ASSESSED or row.get("content_hash") != content_hash:
            return False
        if row.get("delivery") in (jr.D_CARD, jr.D_DIGEST):
            fields.pop("delivery", None)
            fields.pop("verdict", None)
        else:
            if delivery is not None:
                fields["delivery"] = delivery
            if verdict is not None:
                fields["verdict"] = verdict
        return True

    return _replace_guarded(order_id, guard, fields)


def _replace_guarded(order_id: int, guard, fields: dict) -> bool:
    """Запись полей заказа под проверкой guard(row) в той же транзакции; guard может дополнить fields."""
    with _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM freelance_orders WHERE order_id = ?", (order_id,)).fetchone()
        if row is None or not guard(dict(row)):
            return False
        merged = {**dict(row), **fields}
        merged["status"] = jr.legacy_status(
            merged.get("tech"), merged.get("delivery"), merged.get("lead"), bool(merged.get("card_deleted_at")))
        names = [*fields, "status"]
        conn.execute(
            f"UPDATE freelance_orders SET {','.join(f'{n} = ?' for n in names)} WHERE order_id = ?",
            (*(merged[n] for n in names), order_id))
        return True


def freelance_set_actuals(order_id: int, hours: float | None = None, expenses: float | None = None) -> bool:
    """Фактические часы и расходы по заказу (None - значение не менять). False - заказа нет, значения
    отрицательные/нечисловые или не передано ничего."""
    values = {}
    for name, value in (("hours", hours), ("expenses", expenses)):
        if value is None:
            continue
        try:
            number = float(value)
        except (TypeError, ValueError):
            return False
        if not math.isfinite(number) or number < 0:
            return False
        values[name] = number
    if not values or freelance_get(order_id) is None:
        return False
    with _connect() as conn:
        conn.execute(
            "INSERT INTO freelance_actuals (order_id, hours, expenses, updated_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(order_id) DO UPDATE SET hours = COALESCE(?, hours), expenses = COALESCE(?, expenses), "
            "updated_at = ?",
            (order_id, values.get("hours"), values.get("expenses"), _now_sql(),
             values.get("hours"), values.get("expenses"), _now_sql()))
    return True


def freelance_actuals_rows() -> list[dict]:
    """Заказы с фактическими часами: category, hours_est (оценка часов), hours_actual, expenses - для калибровки."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT o.order_id, o.category, o.hours AS hours_est, a.hours AS hours_actual, a.expenses "
            "FROM freelance_actuals a JOIN freelance_orders o ON o.order_id = a.order_id "
            "WHERE a.hours IS NOT NULL").fetchall()
        return [dict(r) for r in rows]


def freelance_set_message(order_id: int, message_id: int) -> None:
    """Запоминает id сообщения Telegram с заказом (для автоудаления)."""
    with _connect() as conn:
        conn.execute(
            "UPDATE freelance_orders SET message_id = ? WHERE order_id = ?",
            (message_id, order_id),
        )


def freelance_expired_messages(ttl_hours: int, limit: int = 200) -> list[dict]:
    """Карточки старше ttl_hours - под удаление из чата. Карточки активных лидов (сохранён,
    откликнулся, обсуждаем, договорились) остаются; сам лид удаление карточки не закрывает."""
    marks = ",".join("?" * len(jr.LEAD_ACTIVE))
    with _connect() as conn:
        rows = conn.execute(
            f"""
            SELECT order_id, message_id
            FROM freelance_orders
            WHERE delivery = 'card' AND message_id IS NOT NULL AND card_deleted_at IS NULL
              AND COALESCE(lead, '') NOT IN ({marks})
              AND sent_at < datetime('now', ?)
            ORDER BY sent_at
            LIMIT ?
            """,
            (*jr.LEAD_ACTIVE, f"-{ttl_hours} hours", limit),
        ).fetchall()
        return [dict(r) for r in rows]


def freelance_pending(limit: int = 100, id_ranges: list[tuple[int, int]] | None = None,
                      exclude_ranges: bool = False) -> list[dict]:
    """Ожидающие заказы (не оценены - tech=queued, либо оценены и ждут доставки), новые первыми
    по времени публикации (order_id разных источников несравнимы).

    id_ranges - полуинтервалы [от, до) order_id: по умолчанию выбираются только заказы из них,
    с exclude_ranges=True - все, кроме них (так лимит не занимают заказы неактивных источников)."""
    where, params = "(tech = 'queued' OR (tech = 'assessed' AND delivery = 'pending'))", []
    where += " AND NOT (COALESCE(origin, '')='manual' AND COALESCE(queue_reason, '')='no_text')"
    if id_ranges:
        cond = " OR ".join("(order_id >= ? AND order_id < ?)" for _ in id_ranges)
        where += f" AND {'NOT ' if exclude_ranges else ''}({cond})"
        params = [v for r in id_ranges for v in r]
    with _connect() as conn:
        rows = conn.execute(
            f"""
            SELECT order_id, attempts, ai_json, published_at, dedup_key, seen_at, queue_reason, origin, title, site, snippet, url, price_text,
                   tech, delivery, COALESCE(pub_unreliable, 0) AS pub_unreliable
            FROM freelance_orders
            WHERE {where}
            ORDER BY COALESCE(published_at,seen_at) DESC, order_id DESC
            LIMIT ?
            """,
            (*params, limit),
        ).fetchall()
        return [dict(r) for r in rows]


def freelance_recent(limit: int = 10, ttl_hours: int | None = None) -> list[dict]:
    """Последние заказы, доставленные карточкой (карточка не удалена по TTL, лид не закрыт отказом)."""
    if ttl_hours is None:
        from config import FREELANCE_MESSAGE_TTL_HOURS
        ttl_hours = FREELANCE_MESSAGE_TTL_HOURS
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT order_id, site, title, url, price_text, fit, published_at
            FROM freelance_orders
            WHERE delivery = 'card' AND card_deleted_at IS NULL AND COALESCE(lead, '') != 'lost'
              AND sent_at >= datetime('now', ?)
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


def freelance_day_summary(hours: int = 24, near_miss: int = 3) -> dict:
    """Итог фриланс-сводки за последние hours часов по журналу. Измерения не смешиваются:
    техническая обработка (filtered/duplicate/queued/assessed/stale), итог оценки (ok/low_fit/...),
    доставка (card/digest/pending). Заказы из сводки «ещё N» - оценены хорошо и доставлены строкой,
    отвергнутыми AI они не считаются. near_miss - лучшие из отсеянных AI (fit ниже порога)."""
    since = f"-{int(hours)} hours"
    with _connect() as conn:
        c = {"total": 0, "filtered": 0, "duplicate": 0, "queued": 0, "assessed": 0, "stale": 0, "legacy": 0,
             "low_fit": 0, "good": 0, "card": 0, "digest": 0, "delivery_pending": 0, "delivery_unknown": 0}
        for tech, verdict, delivery, n in conn.execute(
                "SELECT tech, verdict, delivery, COUNT(*) FROM freelance_orders "
                "WHERE seen_at >= datetime('now', ?) GROUP BY tech, verdict, delivery", (since,)):
            c["total"] += n
            c[tech if tech in c else "legacy"] += n
            if tech == jr.ASSESSED:
                if verdict in (jr.V_LOW, jr.V_TRIAGE):
                    c["low_fit"] += n
                else:
                    c["good"] += n
                if delivery == jr.D_CARD:
                    c["card"] += n
                elif delivery == jr.D_DIGEST:
                    c["digest"] += n
                elif delivery == jr.D_PENDING:
                    c["delivery_pending"] += n
                elif delivery == jr.D_UNKNOWN:
                    c["delivery_unknown"] += n
        c["excluded"] = conn.execute(
            "SELECT COALESCE(SUM(n), 0) FROM freelance_funnel_daily WHERE day >= date('now', ?)",
            (f"-{max(1, int(hours) // 24)} days",)).fetchone()[0]
        # «Отправлено» считается по времени отправки карточки (как в /status), остальное - по seen_at
        c["sent"] = conn.execute(
            "SELECT COUNT(*) FROM freelance_orders WHERE sent_at >= datetime('now', ?)", (since,)
        ).fetchone()[0]
        best = conn.execute(
            "SELECT title, url, fit FROM freelance_orders "
            "WHERE seen_at >= datetime('now', ?) AND tech = 'assessed' AND verdict IN ('low_fit', 'triage_low') "
            "AND fit IS NOT NULL AND sent_at IS NULL "
            "ORDER BY fit DESC, published_at DESC LIMIT ?",
            (since, near_miss),
        ).fetchall()
        c["near_miss"] = [{"title": r[0], "url": r[1], "fit": r[2]} for r in best]
        return c


def freelance_count_excluded(day: str, counts: dict[str, int]) -> None:
    """Счётчики заказов, отсеянных по площадке (вакансионные доски): строк журнала для них нет,
    но воронка должна знать, сколько их было. counts: {site: n}."""
    with _connect() as conn:
        for site, n in counts.items():
            if n > 0:
                conn.execute(
                    "INSERT INTO freelance_funnel_daily (day, site, reason, n) VALUES (?, ?, 'excluded_site', ?) "
                    "ON CONFLICT(day, site, reason) DO UPDATE SET n = n + excluded.n",
                    (day, site, int(n)),
                )


if __name__ == "__main__":
    init_db()
    print("База инициализирована:", DB_PATH)
    print("Статистика:", stats())