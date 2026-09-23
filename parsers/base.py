"""Базовый класс для всех парсеров вакансий."""
from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class Vacancy:
    """Универсальное представление вакансии."""
    source: str
    vacancy_id: str
    title: str
    company: str
    url: str
    location: str = ""
    salary: str = ""
    is_remote: bool = False

    def format_message(self) -> str:
        """Форматирует вакансию для отправки в Telegram (HTML)."""
        remote_mark = "🌍 " if self.is_remote else ""
        lines = [f"🔍 {remote_mark}<b>{self.title}</b>"]
        lines.append(f"🏢 Компания: {self.company}")
        if self.location:
            lines.append(f"📍 Локация: {self.location}")
        if self.salary:
            lines.append(f"💰 Зарплата: {self.salary}")
        lines.append(f"🔗 <a href='{self.url}'>Открыть вакансию</a>")
        return "\n".join(lines)


class BaseParser(ABC):
    """Абстрактный парсер. Все парсеры наследуются от него."""

    source_name: str = "base"

    @abstractmethod
    def fetch(self) -> list[Vacancy]:
        raise NotImplementedError