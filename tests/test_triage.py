"""Быстрый триаж заказов, экономия токенов и строка расхода AI в сводке. Без сети.

Запуск из корня проекта: python -m unittest tests.test_triage -v
"""
import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest import mock

import bot
import database
from freelance import alot_client, ai_scorer, pipeline

GOOD = json.dumps({"fit": 8, "category": "tg_bot",
                   "components": [{"name": "бот", "hours": 3}], "summary": "x"})
ORDER = SimpleNamespace(title="Бот", price_text="5000 ₽", body="Нужен телеграм-бот")


def make_order(oid, title="Создать телеграм бота"):
    return alot_client.FreelanceOrder(oid, "youdoru", title, "нужен бот", "5000 ₽", 5000.0, [],
                                      datetime.now(timezone.utc), f"https://x/{oid}")


def full_result(fit):
    return {"fit": fit, "difficulty": "легко", "hours": 4.0, "category": "tg_bot",
            "components": [{"name": "бот", "hours": 4}], "unknowns": [], "clarity": "ясно",
            "client_budget_ok": True, "summary": "s", "risks": "", "questions": [],
            "model": "test:m"}


def http(status=200, content='{"fit": 1}'):
    r = mock.Mock()
    r.status_code = status
    r.headers = {}
    r.text = content
    r.json.return_value = {"choices": [{"message": {"content": content}}]}
    return r


class TriageClientTest(unittest.TestCase):
    def setUp(self):
        providers = ai_scorer._build_providers({"CLOUDFLARE_API_KEY": "k", "CLOUDFLARE_ACCOUNT_ID": "abc123"})
        for p in (mock.patch.object(ai_scorer, "PROVIDERS", providers),
                  mock.patch.object(ai_scorer, "CHAIN", ["cloudflare", "openrouter"]),
                  mock.patch.object(ai_scorer, "_triage_day", ""),
                  mock.patch.object(ai_scorer, "_triage_count", 0),
                  mock.patch.object(ai_scorer, "_triage_paused_until", 0.0),
                  mock.patch.object(ai_scorer, "_triage_disabled", False),
                  mock.patch.object(ai_scorer, "_triage_loaded", True),
                  mock.patch.object(ai_scorer, "_triage_save_count")):
            p.start()
            self.addCleanup(p.stop)
        self.save = ai_scorer._triage_save_count
        self.cf = providers["cloudflare"]

    def test_returns_fit_and_does_not_spend_main_quota(self):
        with mock.patch("requests.post", return_value=http(content='{"fit": 1}')) as post:
            self.assertEqual(ai_scorer.triage_order(ORDER), 1)
        body = post.call_args.kwargs["json"]
        self.assertEqual(body["model"], ai_scorer.TRIAGE_MODEL)
        self.assertLessEqual(body["max_tokens"], 100)
        self.assertEqual(self.cf.count, 0)                 # слоты основной оценки не тронуты
        self.assertEqual(ai_scorer.triage_today(), 1)

    def test_unparseable_and_errors_give_none(self):
        for response in (http(content="не знаю"), http(500), http(401)):
            with mock.patch("requests.post", return_value=response):
                self.assertIsNone(ai_scorer.triage_order(ORDER))
        with mock.patch("requests.post", side_effect=OSError("net")):
            self.assertIsNone(ai_scorer.triage_order(ORDER))

    def test_429_pauses_triage_only(self):
        with mock.patch("requests.post", return_value=http(429)) as post:
            self.assertIsNone(ai_scorer.triage_order(ORDER))
            self.assertIsNone(ai_scorer.triage_order(ORDER))
        self.assertEqual(post.call_count, 1)               # вторая попытка - на паузе
        self.assertEqual(self.cf.paused, {})               # основная оценка Cloudflare не затронута

    def test_404_disables_until_restart(self):
        with mock.patch("requests.post", return_value=http(404)) as post:
            self.assertIsNone(ai_scorer.triage_order(ORDER))
            self.assertIsNone(ai_scorer.triage_order(ORDER))
        self.assertEqual(post.call_count, 1)

    def test_daily_cap(self):
        with mock.patch.object(ai_scorer, "TRIAGE_MAX_PER_DAY", 2), \
                mock.patch("requests.post", return_value=http()) as post:
            results = [ai_scorer.triage_order(ORDER) for _ in range(3)]
        self.assertEqual(results, [1, 1, None])
        self.assertEqual(post.call_count, 2)

    def test_no_cloudflare_key_or_not_in_chain(self):
        with mock.patch.object(ai_scorer, "CHAIN", ["openrouter"]), \
                mock.patch("requests.post") as post:
            self.assertIsNone(ai_scorer.triage_order(ORDER))
        post.assert_not_called()

    def test_contacts_masked_in_request(self):
        order = SimpleNamespace(title="Бот, +7 999 123-45-67", price_text="",
                                body="пишите ivan@private-company.ru")
        with mock.patch("requests.post", return_value=http()) as post:
            ai_scorer.triage_order(order)
        sent = json.dumps(post.call_args.kwargs["json"], ensure_ascii=False)
        self.assertNotIn("private-company", sent)
        self.assertNotIn("999", sent)

    def test_fit_parsed_as_json_not_by_prefix(self):
        cases = {'{"fit": 1e1}': 10, '```json\n{"fit": 3}\n```': 3, '{"fit": 2.4}': 2,
                 '{"fit": 42}': None, '{"fit": -1}': None, '{"fit": "1"}': None,
                 '{"fit": true}': None, '{"fit": null}': None, '[1]': None, 'fit: 1': None,
                 '[{"fit": 1}]': None, 'ответ: {"fit": 1}': None, '{"fit": 1} и ещё {"fit": 9}': None,
                 '{"fit": 1' + '0' * 400 + '}': None, '{"fit": NaN}': None}
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(ai_scorer._parse_triage_fit(text), expected)

    def test_counter_persisted_and_restored_after_restart(self):
        with mock.patch("requests.post", return_value=http()):
            ai_scorer.triage_order(ORDER)
        today = ai_scorer._today()
        self.save.assert_called_with(f"{today}:1")
        # «рестарт»: память пуста, в kv - 300 за сегодня -> потолок достигнут
        with mock.patch.object(ai_scorer, "_triage_loaded", False), \
                mock.patch.object(ai_scorer, "_triage_day", ""), \
                mock.patch.object(ai_scorer, "_triage_count", 0), \
                mock.patch("database.kv_get", return_value=f"{today}:{ai_scorer.TRIAGE_MAX_PER_DAY}"), \
                mock.patch("requests.post") as post:
            self.assertIsNone(ai_scorer.triage_order(ORDER))
        post.assert_not_called()

    def test_db_read_failure_skips_triage_and_retries_load(self):
        today = ai_scorer._today()
        with mock.patch.object(ai_scorer, "_triage_loaded", False), \
                mock.patch.object(ai_scorer, "_triage_day", ""), \
                mock.patch.object(ai_scorer, "_triage_count", 0), \
                mock.patch("database.kv_get", side_effect=[OSError("db"), f"{today}:7"]), \
                mock.patch("requests.post", return_value=http()) as post:
            self.assertIsNone(ai_scorer.triage_order(ORDER))     # чтение не удалось: запроса нет
            post.assert_not_called()
            self.assertEqual(ai_scorer.triage_order(ORDER), 1)   # второе чтение удалось
            self.assertEqual(ai_scorer._triage_count, 8)         # расход 7 не потерян
        self.save.assert_called_with(f"{today}:8")

    def test_save_happens_under_lock(self):
        held = []
        self.save.side_effect = lambda value: held.append(ai_scorer._triage_lock.locked())
        with mock.patch("requests.post", return_value=http()):
            ai_scorer.triage_order(ORDER)
        self.assertEqual(held, [True])

    def test_stale_day_in_kv_ignored(self):
        with mock.patch.object(ai_scorer, "_triage_loaded", False), \
                mock.patch.object(ai_scorer, "_triage_day", ""), \
                mock.patch.object(ai_scorer, "_triage_count", 0), \
                mock.patch("database.kv_get", return_value="2000-01-01:300"), \
                mock.patch("requests.post", return_value=http()):
            self.assertEqual(ai_scorer.triage_order(ORDER), 1)


class TokenSavingTest(unittest.TestCase):
    def test_body_limit_default_is_smaller(self):
        self.assertEqual(ai_scorer.BODY_LIMIT, 2000)
        prompt = ai_scorer._user_prompt(SimpleNamespace(title="t", price_text="", body="x" * 5000))
        self.assertLess(len(prompt), 2200)

    def test_reasoning_effort_only_for_gpt_oss_and_when_set(self):
        order = ORDER
        with mock.patch.object(ai_scorer, "REASONING_EFFORT", "low"):
            on = ai_scorer._provider_body("@cf/openai/gpt-oss-120b", order, True, reasoning=True)
            off = ai_scorer._provider_body("@cf/openai/gpt-oss-120b", order, True, reasoning=False)
        self.assertEqual(on["reasoning_effort"], "low")
        self.assertNotIn("reasoning_effort", off)
        self.assertNotIn("reasoning_effort", ai_scorer._provider_body("m", order, True))
        p = SimpleNamespace(no_reasoning=set())
        with mock.patch.object(ai_scorer, "REASONING_EFFORT", "low"):
            self.assertTrue(ai_scorer._wants_reasoning(p, "@cf/openai/gpt-oss-120b"))
            self.assertFalse(ai_scorer._wants_reasoning(p, "llama-3.3-70b"))
            p.no_reasoning.add("@cf/openai/gpt-oss-120b")
            self.assertFalse(ai_scorer._wants_reasoning(p, "@cf/openai/gpt-oss-120b"))

    def test_400_on_reasoning_retries_without_it(self):
        providers = ai_scorer._build_providers({"CLOUDFLARE_API_KEY": "k", "CLOUDFLARE_ACCOUNT_ID": "abc123",
                                                "CLOUDFLARE_MIN_INTERVAL_SEC": "0"})
        cf = providers["cloudflare"]
        bad = http(400, "unknown parameter: reasoning_effort")
        calls = []

        def fake(url, **kw):
            calls.append(kw["json"])
            return bad if "reasoning_effort" in kw["json"] else http(200, GOOD)

        with mock.patch.object(ai_scorer, "REASONING_EFFORT", "low"), \
                mock.patch.object(ai_scorer, "PROVIDERS", providers), \
                mock.patch.object(ai_scorer, "_load_count"), mock.patch.object(ai_scorer, "_save_count"), \
                mock.patch("time.sleep"), mock.patch("requests.post", side_effect=fake):
            result = ai_scorer._ask_provider_model(cf, "@cf/openai/gpt-oss-120b", ORDER)
        self.assertEqual(result["fit"], 8)
        self.assertEqual(len(calls), 2)
        self.assertIn("@cf/openai/gpt-oss-120b", cf.no_reasoning)
        self.assertNotIn("@cf/openai/gpt-oss-120b", cf.disabled)   # модель не выключена


class PipelineTriageTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_path = database.DB_PATH
        database.DB_PATH = os.path.join(self.tmp.name, "t.db")
        database.init_db()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(setattr, database, "DB_PATH", self.old_path)
        pipeline._triage_cache.clear()
        for p in (mock.patch.object(pipeline.monitor, "record"),
                  mock.patch.object(pipeline, "_report_ai_health"),
                  mock.patch.object(pipeline, "_fetch_feed_orders", return_value=[]),  # FL.ru и Kwork без сети
                  mock.patch.object(pipeline.ai_scorer, "is_enabled", return_value=True),
                  mock.patch.object(pipeline.ai_scorer, "daily_capacity", return_value=None),
                  mock.patch.object(pipeline.ai_scorer, "quota_exhausted", return_value=None)):
            p.start()
            self.addCleanup(p.stop)
        database.kv_set(pipeline.LAST_ID_KEY, 100)

    def cycle(self, order, triage, mode, full=None):
        scan = alot_client.ScanResult(orders=[order], max_id=order.order_id, raw_count=1)
        with mock.patch.object(pipeline, "TRIAGE_MODE", mode), \
                mock.patch.object(pipeline, "fetch_new_orders", return_value=scan), \
                mock.patch.object(pipeline.ai_scorer, "triage_order", return_value=triage) as tri, \
                mock.patch.object(pipeline.ai_scorer, "score_order",
                                  return_value=full or full_result(8)) as score:
            res = pipeline._prepare_cycle()
        return res, tri, score

    def test_on_rejects_without_full_scoring(self):
        res, tri, score = self.cycle(make_order(201), 1, "on")
        score.assert_not_called()
        self.assertEqual([s.order.order_id for s in res.rejected], [201])
        self.assertEqual(res.triaged, 1)
        self.assertEqual(res.ai_attempted, 0)       # «здоровье» полной оценки не затронуто
        self.assertTrue(res.rejected[0].ai["triage"])
        self.assertEqual(database.freelance_get(201)["fit"], 1)

    def test_on_passes_good_to_full_scoring(self):
        res, tri, score = self.cycle(make_order(202), 6, "on")
        self.assertEqual(score.call_count, 1)
        self.assertEqual([s.order.order_id for s in res.to_send], [202])

    def test_on_triage_failure_falls_back_to_full_scoring(self):
        res, tri, score = self.cycle(make_order(203), None, "on")
        self.assertEqual(score.call_count, 1)
        self.assertEqual([s.order.order_id for s in res.to_send], [203])

    def test_shadow_never_rejects_and_logs_comparison(self):
        with self.assertLogs(level="INFO") as logs:
            res, tri, score = self.cycle(make_order(204), 1, "shadow", full_result(8))
        self.assertEqual(score.call_count, 1)
        self.assertEqual([s.order.order_id for s in res.to_send], [204])
        text = "\n".join(logs.output)
        self.assertIn("[TRIAGE] заказ 204: быстрая 1, полная 8", text)
        self.assertIn("ПРОПУСТИЛ БЫ ХОРОШИЙ", text)

    def test_off_does_not_call_triage(self):
        res, tri, score = self.cycle(make_order(205), 1, "off")
        tri.assert_not_called()
        self.assertEqual(score.call_count, 1)

    def test_triage_result_is_cached_between_cycles(self):
        order = make_order(206)
        sc = pipeline.ScoredOrder(order, None, "k")
        with mock.patch.object(pipeline.ai_scorer, "triage_order", return_value=5) as tri:
            self.assertEqual(pipeline._triage(sc), 5)
            self.assertEqual(pipeline._triage(sc), 5)
        self.assertEqual(tri.call_count, 1)

    def test_retried_order_is_not_triaged(self):
        sc = pipeline.ScoredOrder(make_order(207), None, "k", attempts=1)
        with mock.patch.object(pipeline.ai_scorer, "triage_order") as tri:
            self.assertIsNone(pipeline._triage(sc))
        tri.assert_not_called()


class UsageLineTest(unittest.TestCase):
    def test_line_lists_enabled_providers_and_triage(self):
        st = {"providers": {
            "cloudflare": {"enabled": True, "limit": 50, "today": 50},
            "openrouter": {"enabled": True, "limit": 45, "today": 30},
            "groq": {"enabled": False, "limit": 900, "today": 0}},
            "triage_today": 90}
        self.assertEqual(bot._ai_usage_line(st),
                         "AI за сутки: cloudflare 50/50, openrouter 30/45, быстрых оценок 90")

    def test_empty_when_nothing_to_show(self):
        self.assertEqual(bot._ai_usage_line({"providers": {}}), "")


if __name__ == "__main__":
    unittest.main()
