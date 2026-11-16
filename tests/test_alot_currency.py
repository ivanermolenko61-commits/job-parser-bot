"""Валюта бюджета alot.pro: знак из поля currency дописывается к сумме, чтобы бюджет не считался рублями по умолчанию.

Запуск из корня проекта: python -m unittest tests.test_alot_currency -v
"""
import unittest

from freelance import alot_client
from freelance.budget import parse_budget


def item(price, currency=None):
    out = {"internalId": 1, "date": 1_700_000_000_000, "site": "flru", "title": "Заказ", "body": "Текст",
           "price": price, "alotCategories": [], "url": "https://alot.pro/projects/1/"}
    if currency is not None:
        out["currency"] = currency
    return out


class AlotCurrencyTest(unittest.TestCase):
    def test_known_currency_field_is_added_to_amount(self):
        for code, sign in (("RUB", "₽"), ("UAH", "₴"), ("usd", "$"), ("EUR", "€")):
            o = alot_client._parse_item(item("5 000 - 10 000", code))
            self.assertTrue(o.price_text.endswith(sign), (code, o.price_text))
            self.assertEqual(parse_budget(o.price_text)["currency"], code.upper())

    def test_missing_or_unknown_currency_stays_unknown(self):
        for cur in (None, "", "XYZ"):
            o = alot_client._parse_item(item("5 000", cur))
            self.assertEqual(o.price_text, "5 000")
            self.assertEqual(parse_budget(o.price_text)["currency"], "")

    def test_existing_sign_in_text_is_kept(self):
        o = alot_client._parse_item(item("100 $", "RUB"))
        self.assertEqual(o.price_text, "100 $")           # знак в самом тексте важнее поля
        self.assertEqual(parse_budget(o.price_text)["currency"], "USD")

    def test_unknown_foreign_sign_is_not_overridden_by_field(self):
        for text in ("100 £", "5000 zł", "до 100 £"):
            o = alot_client._parse_item(item(text, "RUB"))
            self.assertEqual(o.price_text, text)
            self.assertEqual(parse_budget(o.price_text)["currency"], "")

    def test_words_around_amount_do_not_block_currency(self):
        o = alot_client._parse_item(item("от 5 тыс", "RUB"))
        self.assertEqual(parse_budget(o.price_text)["currency"], "RUB")

    def test_empty_price_stays_empty(self):
        self.assertEqual(alot_client._parse_item(item("", "RUB")).price_text, "")


if __name__ == "__main__":
    unittest.main()
