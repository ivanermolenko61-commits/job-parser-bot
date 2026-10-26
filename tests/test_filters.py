"""Тесты префильтра фриланс-заказов: объявления исполнителей отсекаются.

Запуск из корня проекта: python -m unittest tests.test_filters -v
"""
import unittest
from datetime import datetime, timezone

from freelance.alot_client import FreelanceOrder
from freelance.filters import is_candidate, is_service_offer, priority, reject_reason


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


def make_cat(title, categories=(), body="", site="youdoru", suspicious=False):
    return FreelanceOrder(2, site, title, body, "1 499 ₽", 1499.0, list(categories),
                          datetime.now(timezone.utc), "https://example.com/2",
                          is_suspicious=suspicious)


class RejectReasonTest(unittest.TestCase):
    """Причина отсева префильтра: по ней видно, куда делся заказ."""

    def test_telegram_bot_titles_pass(self):
        for title in ["Создание тг бота", "Нужен ТГ-бот для магазина",
                      "Telegram бот для записи клиентов", "tg bot для рассылки"]:
            self.assertIsNone(reject_reason(make_cat(title)), title)
            self.assertTrue(is_candidate(make_cat(title)), title)

    def test_tg_in_body_only_passes(self):
        self.assertIsNone(reject_reason(make_cat("Нужна помощь", body="Нужен тг канал с автопостингом")))

    def test_tg_is_whole_word(self):
        self.assertEqual(reject_reason(make_cat("Помогите с tgz архивом")), "no_include")

    def test_reasons(self):
        self.assertEqual(reject_reason(make_cat("Сайт", site="hhru")), "excluded_site")
        self.assertEqual(reject_reason(make_cat("Сайт", suspicious=True)), "reports")
        self.assertEqual(reject_reason(make_cat("Создам бота за час")), "service_offer")
        self.assertEqual(reject_reason(make_cat("Купить диван")), "no_include")

    def test_hard_stop_word_still_rejects_bot(self):
        why = reject_reason(make_cat("Бот для накрутки подписчиков", ["PROG_OTHER"]))
        self.assertTrue(why.startswith("stop_word:"), why)

    def test_soft_stop_word_does_not_kill_dev_order(self):
        for title in ["Бот для менеджера по продажам", "Парсер отзывов с Wildberries",
                      "Python скрипт для аналитика", "Телеграм бот для перевода текста"]:
            self.assertIsNone(reject_reason(make_cat(title)), title)

    def test_soft_stop_word_rejects_non_dev(self):
        for title in ["Менеджер по продажам", "Перевод документа", "Аналитик данных"]:
            why = reject_reason(make_cat(title, ["PROG_OTHER"]))
            self.assertTrue(why and why.startswith("stop_word:"), title)

    def test_is_candidate_matches_reason(self):
        for title in ["Создание тг бота", "Купить диван", "Менеджер"]:
            o = make_cat(title)
            self.assertEqual(is_candidate(o), reject_reason(o) is None)


class PrefilterLogTest(unittest.TestCase):
    """pipeline._prefilter: причины отсева видны в логе, Youdo логируется один раз на заказ."""

    def test_logs_youdo_reject_once_and_keeps_passed(self):
        from freelance import pipeline
        pipeline._prefilter_logged.clear()
        good = make_cat("Создание тг бота")
        bad = FreelanceOrder(7, "youdoru", "Купить диван", "", "", 0.0, [],
                             datetime.now(timezone.utc), "https://example.com/7")
        with self.assertLogs(level="INFO") as cm:
            self.assertEqual(pipeline._prefilter([good, bad]), [good])
            pipeline._prefilter([bad])
        lines = [m for m in cm.output if "префильтр: no_include" in m]
        self.assertEqual(len(lines), 1)
        self.assertTrue(any("префильтр отсеял" in m for m in cm.output))
