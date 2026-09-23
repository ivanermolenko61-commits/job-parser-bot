"""Базовый класс для всех парсеров вакансий.

Каждый источник (hh.ru, Dream Job, Хабр Карьера и т.д.) наследует этот класс
и реализует метод `fetch()`. Это даёт единый интерфейс для менеджера парсеров.
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class Vacancy:
    """Универсальное представление вакансии."""
    source: str          # "hh", "dreamjob", "habr"
    vacancy_id: str      # уникальный ID внутри источника
    title: str
    company: str
    url: str
    location: str = ""
    salary: str = ""

    def format_message(self) -> str:
        """Форматирует вакансию для отправки в Telegram (HTML)."""
        lines = [f"🔍 <b>{self.title}</b>"]
        lines.append(f"🏢 Компания: {self.company}")
        if self.location:
            lines.append(f"📍 Локация: {self.location}")
        if self.salary:
            lines.append(f"💰 Зарплата: {self.salary}")
        lines.append(f"🔗 <a href='{self.url}'>Открыть вакансию</a>")
        return "\n".join(lines)


class BaseParser(ABC):
    """Абстрактный парсер. Все парсеры наследуются от него."""

    #: Имя источника (короткое, латиницей). Используется в БД.
    source_name: str = "base"

    @abstractmethod
    def fetch(self) -> list[Vacancy]:
        """Возвращает список вакансий из источника.

        Должен возвращать только те, что подходят под фильтры.
        Дедупликация и хранение — задача `parser_manager`.
        """
        raise NotImplementedError