"""Работа с SQLite: хранение уже отправленных вакансий."""
import sqlite3
from datetime import datetime
from typing import Optional

from config import DB_PATH


def _connect() -> sqlite3.Connection:
    """Создаёт соединение с базой."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


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


def mark_sent(
    source: str,
    vacancy_id: str,
    title: str = "",
    company: str = "",
    url: str = "",
    published_at: str = "",
) -> None:
    """Помечает вакансию как отправленную."""
    with _connect() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO vacancies
                (source, vacancy_id, title, company, url, published_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (source, vacancy_id, title, company, url, published_at),
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
      1. Записи с датой публикации — сначала (в порядке убывания даты)
      2. Записи без даты публикации — в конце (в порядке found_at)

    ISO-формат published_at ('2026-09-23T14:27:10+03:00') корректно
    сортируется лексикографически — год/месяц/день/час/минута идут
    в правильном порядке.
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


def cleanup_old(days: int = 30) -> int:
    """Удаляет вакансии старше N дней. Возвращает число удалённых."""
    with _connect() as conn:
        cursor = conn.execute(
            f"DELETE FROM vacancies WHERE found_at < datetime('now', '-{days} days')"
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