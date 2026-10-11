"""Тесты префильтра фриланс-заказов: объявления исполнителей отсекаются.

Запуск из корня проекта: python -m unittest tests.test_filters -v
"""
import unittest
from datetime import datetime, timezone

from freelance.alot_client import FreelanceOrder
from freelance.filters import is_candidate, is_service_offer, priority


def make(title):
    return FreelanceOrder(1, "youdoru", title, "", "1 499 ₽", 1499.0, ["WEB"],
                          datetime.now(timezone.utc), "https://example.com/1")


class ServiceOfferTest(unittest.TestCase):
    OFFERS = [
        "Создам вашего персонального телеграм бота в течении 10 минут",
        "Сделаю сайт под ключ",
        "  🔥 Разработаю лендинг",
        "СОЗДАМ бота",
        "Помогу с настройкой сервера",
    ]
    ORDERS = [
        "Создать сайт", "Сделать бота", "Разработать чат-бот для телеграм",
        "Нужно создать лендинг", "Нужно сделать парсер", "Разработка сайта",
        "Создание бота", "Предлагаю работу: сделать бота для записи",
    ]

    def test_offers(self):
        for title in self.OFFERS:
            self.assertTrue(is_service_offer(title), title)
            self.assertFalse(is_candidate(make(title)), title)

    def test_orders_pass(self):
        for title in self.ORDERS:
            self.assertFalse(is_service_offer(title), title)
            self.assertTrue(is_candidate(make(title)), title)


def make_cat(title, categories, body=""):
    return FreelanceOrder(1, "youdoru", title, body, "", 0.0, categories,
                          datetime.now(timezone.utc), "https://example.com/1")


class PriorityTest(unittest.TestCase):
    def test_groups(self):
        # примеры из эталонной выборки (fit 7-8, кроме последнего)
        self.assertEqual(priority(make_cat("Создать сайт на тильде", ["WEB"])), 1)
        self.assertEqual(priority(make_cat("Написать макрос в excel", ["FINANCE"])), 2)
        self.assertEqual(priority(make_cat("Создать миниапп", ["PROG_OTHER"])), 2)
        self.assertEqual(priority(make_cat("Специалист TapLink", ["SEOSMM"], "нужен сайт")), 3)


if __name__ == "__main__":
    unittest.main()
