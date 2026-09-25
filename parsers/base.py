"""Базовый класс для всех парсеров вакансий."""
import html
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
    published_at: str = ""        # дата публикации с сайта
    experience: str = ""          # требуемый опыт
    applications_count: str = ""  # количество откликов (пока только hh.ru)

    def format_message(self) -> str:
        """Форматирует вакансию для отправки в Telegram (HTML).

        Все поля с сайтов экранируются через html.escape: символы & < >
        в названии («R&D», «C++ <junior>») иначе ломают разметку, и Telegram
        отказывается отправлять сообщение.
        """
        esc = html.escape
        remote_mark = "🌍 " if self.is_remote else ""
        lines = [f"🔍 {remote_mark}<b>{esc(self.title)}</b>"]
        lines.append(f"🏢 Компания: {esc(self.company)}")
        if self.location:
            lines.append(f"📍 Локация: {esc(self.location)}")
        if self.salary:
            lines.append(f"💰 Зарплата: {esc(self.salary)}")
        if self.experience:
            lines.append(f"💼 Опыт: {esc(self.experience)}")
        if self.applications_count:
            lines.append(f"📊 Откликов: {esc(self.applications_count)}")
        if self.published_at:
            lines.append(f"🕒 Опубликовано: {esc(self.published_at)}")
        lines.append(f"🔗 <a href=\"{esc(self.url)}\">Открыть вакансию</a>")
        return "\n".join(lines)


class BaseParser(ABC):
    """Абстрактный парсер. Все парсеры наследуются от него."""

    source_name: str = "base"

    @abstractmethod
    def fetch(self) -> list[Vacancy]:
        raise NotImplementedError