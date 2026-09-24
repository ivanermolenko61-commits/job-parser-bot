"""Работа с SQLite: хранение уже отправленных вакансий."""
import re
import sqlite3
from datetime import datetime, timedelta

from config import DB_PATH


MONTHS_RU = {
    "января": 1, "февраля": 2, "марта": 3, "апреля": 4,
    "мая": 5, "июня": 6, "июля": 7, "августа": 8,
    "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12,
}


def _connect() -> sqlite3.Connection:
    """Создаёт соединение с базой."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


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
    with _connect() as conn:
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

        conn.commit()


def is_sent(source: str, vacancy_id: str) -> bool:
    """Проверяет, отправляли ли уже эту вакансию."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM vacancies WHERE source = ? AND vacancy_id = ?",
            (source, vacancy_id),
        ).fetchone()
        return row is not None


def is_duplicate(title: str, company: str) -> bool:
    """Проверяет, есть ли уже вакансия с таким названием и компанией.

    Это ловит дубли между источниками: одна и та же вакансия может
    быть и на hh.ru, и на Dream Job, и на GeekJob. vacancy_id у них разные,
    но title + company совпадают.
    """
    if not title or not company:
        return False

    with _connect() as conn:
        row = conn.execute(
            """
            SELECT 1 FROM vacancies
            WHERE LOWER(TRIM(title)) = LOWER(TRIM(?))
              AND LOWER(TRIM(company)) = LOWER(TRIM(?))
            LIMIT 1
            """,
            (title, company),
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
        conn.commit()
        return cursor.rowcount


def reset_db() -> int:
    """Полностью очищает таблицу vacancies. Возвращает число удалённых."""
    with _connect() as conn:
        cursor = conn.execute("DELETE FROM vacancies")
        conn.commit()
        return cursor.rowcount


if __name__ == "__main__":
    init_db()
    print("База инициализирована:", DB_PATH)
    print("Статистика:", stats())