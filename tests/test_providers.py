"""Тесты цепочки бесплатных AI-провайдеров (groq, cerebras, mistral, openrouter). Без сети.

Запуск из корня проекта: python -m unittest tests.test_providers -v
"""
import json
import unittest
from types import SimpleNamespace
from unittest import mock

from freelance import ai_scorer, pipeline
from health import HealthMonitor

GOOD = json.dumps({"fit": 8, "category": "tg_bot",
                   "components": [{"name": "бот", "hours": 3}], "summary": "x"})
ORDER = SimpleNamespace(title="Бот", price_text="5000 ₽", body="Нужен телеграм-бот")


def resp(status=200, content=GOOD, headers=None, text="", extra=None):
    r = mock.Mock()
    r.status_code = status
    r.headers = headers or {}
    r.text = text or (content if status == 200 and isinstance(content, str) else "")
    payload = {"choices": [{"message": {"content": content}}]}
    payload.update(extra or {})
    r.json.return_value = payload
    return r


def env(**extra):
    """Ключи у всех четырёх провайдеров, нулевые интервалы."""
    base = {}
    for p in ("GROQ", "CEREBRAS", "MISTRAL", "OPENROUTER"):
        base[f"{p}_API_KEY"] = "test-key"
        base[f"{p}_MIN_INTERVAL_SEC"] = "0"
    base.update(extra)
    return base


class FakeApi:
    """requests.post: ответ по хосту из URL; ведёт журнал вызовов."""

    def __init__(self, routes):
        self.routes = routes  # подстрока URL -> ответ или список ответов (по очереди)
        self.calls = []

    def __call__(self, url, **kwargs):
        self.calls.append((url, kwargs))
        for part, value in self.routes.items():
            if part in url:
                if isinstance(value, list):
                    return value.pop(0) if len(value) > 1 else value[0]
                return value
        raise AssertionError(f"неожиданный запрос {url}")

    def hosts(self):
        return [u.split("/")[2] for u, _ in self.calls]


class ProviderTestBase(unittest.TestCase):
    def setUp(self):
        self.env = env()
        self._patches = [
            mock.patch.object(ai_scorer, "GEMINI_KEY", ""),
            mock.patch.object(ai_scorer, "YANDEX_ENABLED", False),
            mock.patch.object(ai_scorer, "CHAIN", ["groq", "cerebras", "mistral", "openrouter"]),
            mock.patch.object(ai_scorer, "_or_next_check", float("inf")),  # без похода в /models
            mock.patch.object(ai_scorer, "_or_paid", set()),
            mock.patch("time.sleep"),
        ]
        for p in self._patches:
            p.start()
            self.addCleanup(p.stop)

    def use(self, **extra):
        self.env.update(extra)
        providers = ai_scorer._build_providers(self.env)
        patcher = mock.patch.object(ai_scorer, "PROVIDERS", providers)
        patcher.start()
        self.addCleanup(patcher.stop)
        return providers

    def run_order(self, routes):
        api = FakeApi(routes)
        with mock.patch("requests.post", side_effect=api):
            result = ai_scorer.score_order(ORDER)
        return result, api


class FallbackTest(ProviderTestBase):
    def test_429_pauses_only_this_model(self):
        prov = self.use(GROQ_MODELS="m1,m2")
        result, api = self.run_order({
            "groq.com": [resp(429, headers={"retry-after": "30"}), resp(200)],
        })
        self.assertEqual(result["model"], "groq:m2")
        paused = prov["groq"].paused
        self.assertEqual(set(paused), {"m1"})
        # остальные провайдеры не тронуты
        self.assertFalse(prov["cerebras"].paused)
        self.assertEqual(api.hosts(), ["api.groq.com", "api.groq.com"])

    def test_429_all_models_goes_to_next_provider(self):
        prov = self.use(GROQ_MODELS="m1", CEREBRAS_MODELS="c1")
        result, api = self.run_order({
            "groq.com": resp(429, headers={"retry-after": "10"}),
            "cerebras.ai": resp(200),
        })
        self.assertEqual(result["model"], "cerebras:c1")
        self.assertIn("m1", prov["groq"].paused)
        # заказ дальше: модель m1 на паузе, groq пропускается без запроса
        result, api = self.run_order({"cerebras.ai": resp(200)})
        self.assertEqual(api.hosts(), ["api.cerebras.ai"])

    def test_401_disables_only_this_provider(self):
        prov = self.use(GROQ_MODELS="m1,m2", CEREBRAS_MODELS="c1")
        result, api = self.run_order({"groq.com": resp(401), "cerebras.ai": resp(200)})
        self.assertEqual(result["model"], "cerebras:c1")
        self.assertEqual(api.hosts(), ["api.groq.com", "api.cerebras.ai"])  # m2 не пробовали
        self.assertGreater(prov["groq"].blocked_until, 0)
        self.assertIn("groq", prov["groq"].key_error)
        self.assertEqual(prov["cerebras"].key_error, "")
        self.assertEqual(prov["cerebras"].blocked_until, 0.0)
        st_keys = {n: p.key_error for n, p in prov.items() if p.key_error}
        self.assertEqual(list(st_keys), ["groq"])
        _, api = self.run_order({"cerebras.ai": resp(200)})
        self.assertEqual(api.hosts(), ["api.cerebras.ai"])

    def test_402_disables_provider(self):
        prov = self.use(GROQ_MODELS="m1,m2", CEREBRAS_MODELS="c1")
        with self.assertLogs(level="ERROR"):
            result, api = self.run_order({"groq.com": resp(402), "cerebras.ai": resp(200)})
        self.assertEqual(result["model"], "cerebras:c1")
        self.assertTrue(prov["groq"].dead)
        self.assertFalse(prov["cerebras"].dead)
        _, api = self.run_order({"cerebras.ai": resp(200)})
        self.assertEqual(api.hosts(), ["api.cerebras.ai"])

    def test_404_disables_model_only(self):
        prov = self.use(GROQ_MODELS="m1,m2")
        result, _ = self.run_order({"groq.com": [resp(404), resp(200)]})
        self.assertEqual(result["model"], "groq:m2")
        self.assertEqual(prov["groq"].disabled, {"m1"})

    def test_no_json_goes_to_next_provider(self):
        self.use(GROQ_MODELS="m1", CEREBRAS_MODELS="c1")
        result, api = self.run_order({
            "groq.com": resp(200, content="Извините, я не могу ответить в JSON"),
            "cerebras.ai": resp(200),
        })
        self.assertEqual(result["model"], "cerebras:c1")
        self.assertEqual(api.hosts(), ["api.groq.com", "api.cerebras.ai"])

    def test_nobody_answered_returns_none(self):
        self.use(GROQ_MODELS="m1", CEREBRAS_MODELS="c1", MISTRAL_MODELS="s1",
                 OPENROUTER_MODELS="o1:free")
        result, _ = self.run_order({
            "groq.com": resp(500), "cerebras.ai": resp(429), "mistral.ai": resp(500),
            "openrouter.ai": resp(500),
        })
        self.assertIsNone(result)

    def test_status_reports_providers(self):
        self.use(GROQ_MODELS="m1")
        self.run_order({"groq.com": resp(200)})
        st = ai_scorer.status()
        self.assertTrue(st["providers"]["groq"]["enabled"])
        self.assertEqual(st["providers"]["groq"]["today"], 1)
        self.assertEqual(st["chain"], ["groq", "cerebras", "mistral", "openrouter"])
        self.assertTrue(st["enabled"])


class ChainOrderTest(ProviderTestBase):
    def test_parse_chain(self):
        with self.assertLogs(level="WARNING"):
            chain = ai_scorer._parse_chain("mistral, bogus ,groq,mistral")
        self.assertEqual(chain, ["mistral", "groq"])
        self.assertEqual(ai_scorer._parse_chain(None),
                         ["gemini", "groq", "cerebras", "mistral", "openrouter"])

    def test_order_follows_ai_providers(self):
        self.use(GROQ_MODELS="g1", CEREBRAS_MODELS="c1", MISTRAL_MODELS="s1")
        with mock.patch.object(ai_scorer, "CHAIN", ["mistral", "groq"]):
            result, api = self.run_order({"groq.com": resp(200), "mistral.ai": resp(200)})
        self.assertEqual(result["model"], "mistral:s1")
        self.assertEqual(api.hosts(), ["api.mistral.ai"])

    def test_provider_without_key_is_skipped(self):
        self.use(GROQ_API_KEY="", CEREBRAS_MODELS="c1")
        result, api = self.run_order({"cerebras.ai": resp(200)})
        self.assertEqual(result["model"], "cerebras:c1")
        self.assertEqual(api.hosts(), ["api.cerebras.ai"])

    def test_not_in_chain_not_used(self):
        self.use(CEREBRAS_MODELS="c1")
        with mock.patch.object(ai_scorer, "CHAIN", ["cerebras"]):
            _, api = self.run_order({"cerebras.ai": resp(200)})
        self.assertEqual(api.hosts(), ["api.cerebras.ai"])


class LimitsTest(ProviderTestBase):
    def test_daily_cap(self):
        prov = self.use(GROQ_MODELS="g1", GROQ_MAX_PER_DAY="1", CEREBRAS_MODELS="c1")
        result, _ = self.run_order({"groq.com": resp(200)})
        self.assertEqual(result["model"], "groq:g1")
        result, api = self.run_order({"groq.com": resp(200), "cerebras.ai": resp(200)})
        self.assertEqual(result["model"], "cerebras:c1")
        self.assertEqual(api.hosts(), ["api.cerebras.ai"])
        self.assertEqual(prov["groq"].count, 1)

    def test_daily_counter_resets_next_day(self):
        prov = self.use(GROQ_MODELS="g1", GROQ_MAX_PER_DAY="1")
        self.run_order({"groq.com": resp(200)})
        with mock.patch.object(ai_scorer, "_today", return_value="2999-01-01"):
            result, _ = self.run_order({"groq.com": resp(200)})
        self.assertEqual(result["model"], "groq:g1")
        self.assertEqual(prov["groq"].count, 1)

    def test_long_queue_skips_provider(self):
        prov = self.use(GROQ_MODELS="g1", CEREBRAS_MODELS="c1")
        prov["groq"].next_at = ai_scorer.time.monotonic() + 1000
        result, api = self.run_order({"cerebras.ai": resp(200)})
        self.assertEqual(result["model"], "cerebras:c1")
        self.assertEqual(api.hosts(), ["api.cerebras.ai"])

    def test_pause_parsing(self):
        pause, daily = ai_scorer._provider_pause(resp(429, headers={"Retry-After": "45"}))
        self.assertEqual((pause, daily), (45, False))
        # исчерпана дневная корзина: Cerebras
        pause, daily = ai_scorer._provider_pause(resp(429, headers={
            "x-ratelimit-remaining-requests-day": "0",
            "x-ratelimit-reset-requests-day": "7200.5",
            "x-ratelimit-remaining-tokens-minute": "5000",
            "x-ratelimit-reset-tokens-minute": "12"}))
        self.assertEqual((pause, daily), (7201, True))
        # Groq: длительность строкой, корзина запросов не исчерпана - её сброс не берём
        pause, daily = ai_scorer._provider_pause(resp(429, headers={
            "x-ratelimit-remaining-requests": "900",
            "x-ratelimit-reset-requests": "20h",
            "x-ratelimit-remaining-tokens": "0",
            "x-ratelimit-reset-tokens": "1m30.5s"}))
        self.assertEqual((pause, daily), (91, False))
        # без сроков: обычный лимит - минута, дневной по тексту - 3 часа
        self.assertEqual(ai_scorer._provider_pause(resp(429)), (60, False))
        self.assertEqual(
            ai_scorer._provider_pause(resp(429, text="Rate limit exceeded: free-models-per-day")),
            (3 * 3600, True))
        # потолок 6 часов
        pause, _ = ai_scorer._provider_pause(resp(429, headers={"retry-after": "999999"}))
        self.assertEqual(pause, 6 * 3600)

    def test_daily_429_pauses_all_openrouter_models(self):
        prov = self.use(OPENROUTER_MODELS="a:free,b:free")
        with mock.patch.object(ai_scorer, "CHAIN", ["openrouter"]):
            result, _ = self.run_order({"openrouter.ai": resp(
                429, text="Rate limit exceeded: free-models-per-day")})
        self.assertIsNone(result)
        self.assertEqual(set(prov["openrouter"].paused), {"a:free", "b:free"})


class JsonModeTest(ProviderTestBase):
    def test_retry_without_response_format_on_400(self):
        prov = self.use(GROQ_MODELS="g1")
        bad = resp(400, text='{"error": "response_format json_object is not supported"}')
        with mock.patch.object(ai_scorer, "CHAIN", ["groq"]):
            result, api = self.run_order({"groq.com": [bad, resp(200)]})
            self.assertEqual(result["model"], "groq:g1")
            self.assertIn("response_format", api.calls[0][1]["json"])
            self.assertNotIn("response_format", api.calls[1][1]["json"])
            self.assertEqual(prov["groq"].no_json, {"g1"})
            # запомнено: следующий заказ сразу без response_format
            _, api2 = self.run_order({"groq.com": resp(200)})
        self.assertEqual(len(api2.calls), 1)
        self.assertNotIn("response_format", api2.calls[0][1]["json"])

    def test_request_format(self):
        self.use(GROQ_MODELS="g1")
        with mock.patch.object(ai_scorer, "CHAIN", ["groq"]):
            _, api = self.run_order({"groq.com": resp(200)})
        url, kw = api.calls[0]
        self.assertEqual(url, "https://api.groq.com/openai/v1/chat/completions")
        self.assertEqual(kw["json"]["response_format"], {"type": "json_object"})
        self.assertEqual(kw["json"]["temperature"], 0.2)
        self.assertEqual([m["role"] for m in kw["json"]["messages"]], ["system", "user"])
        self.assertIn("timeout", kw)
        self.assertEqual(kw["headers"]["Authorization"], "Bearer test-key")


class OpenRouterFreeOnlyTest(ProviderTestBase):
    def test_non_free_models_dropped_from_env(self):
        with self.assertLogs(level="WARNING") as cm:
            prov = self.use(OPENROUTER_MODELS="a/b:free, paid/model ,openrouter/free,openai/gpt-4o")
        self.assertEqual(prov["openrouter"].models, ["a/b:free", "openrouter/free"])
        self.assertTrue(any("paid/model" in line for line in cm.output))

    def test_defaults_are_free(self):
        prov = self.use()
        self.assertEqual(prov["openrouter"].models, [
            "nvidia/nemotron-3-super-120b-a12b:free", "google/gemma-4-31b-it:free",
            "openrouter/free"])
        self.assertEqual(prov["openrouter"].max_per_day, 45)

    def _models_payload(self):
        return {"data": [
            {"id": "x:free", "pricing": {"prompt": "0", "completion": "0"}},
            {"id": "y:free", "pricing": {"prompt": "0.000001", "completion": "0"}},
            {"id": "z:free", "pricing": {"prompt": "0", "completion": "0.0000002"}},
            {"id": "openrouter/free", "pricing": {"prompt": "0", "completion": "0"}},
        ]}

    def test_paid_models_excluded_by_models_endpoint(self):
        prov = self.use(OPENROUTER_MODELS="x:free,y:free,z:free,openrouter/free")
        listing = mock.Mock()
        listing.json.return_value = self._models_payload()
        listing.raise_for_status.return_value = None
        with mock.patch.object(ai_scorer, "_or_next_check", 0.0), \
                mock.patch("requests.get", return_value=listing) as get:
            models = ai_scorer._provider_models(prov["openrouter"])
            ai_scorer._provider_models(prov["openrouter"])  # повторно в 6 ч не ходим
        self.assertEqual(models, ["x:free", "openrouter/free"])
        self.assertEqual(get.call_count, 1)

    def test_models_endpoint_failure_falls_back_to_suffix(self):
        prov = self.use(OPENROUTER_MODELS="x:free,openrouter/free")
        with mock.patch.object(ai_scorer, "_or_next_check", 0.0), \
                mock.patch("requests.get", side_effect=OSError("нет сети")):
            models = ai_scorer._provider_models(prov["openrouter"])
        self.assertEqual(models, ["x:free", "openrouter/free"])

    def test_request_has_no_paid_features(self):
        self.use(OPENROUTER_MODELS="x:free")
        with mock.patch.object(ai_scorer, "CHAIN", ["openrouter"]):
            result, api = self.run_order({"openrouter.ai": resp(200)})
        self.assertEqual(result["model"], "openrouter:x:free")
        body = api.calls[0][1]["json"]
        for forbidden in ("plugins", "tools", "models", "route", "provider", "web_search_options"):
            self.assertNotIn(forbidden, body)
        self.assertEqual(body["model"], "x:free")
        self.assertEqual(api.calls[0][1]["headers"]["X-Title"], "job-parser-bot")


class YandexPaidTest(ProviderTestBase):
    def test_default_chain_has_no_yandex(self):
        self.assertNotIn("yandex", ai_scorer.DEFAULT_CHAIN.split(","))

    def test_yandex_not_called_without_enabled(self):
        # ключи есть, yandex в цепочке, но YANDEX_ENABLED выключен - не вызывается
        self.use()
        with mock.patch.object(ai_scorer, "CHAIN", ["yandex"]), \
                mock.patch.object(ai_scorer, "API_KEY", "k"), \
                mock.patch.object(ai_scorer, "FOLDER_ID", "f"), \
                mock.patch.object(ai_scorer, "_ask_yandex_lite") as ya, \
                mock.patch.object(ai_scorer, "_ask") as ask:
            self.assertFalse(ai_scorer.is_enabled())
            self.assertIsNone(ai_scorer.score_order(ORDER))
        ya.assert_not_called()
        ask.assert_not_called()

    def test_yandex_called_when_enabled_and_in_chain(self):
        self.use()
        with mock.patch.object(ai_scorer, "CHAIN", ["yandex"]), \
                mock.patch.object(ai_scorer, "YANDEX_ENABLED", True), \
                mock.patch.object(ai_scorer, "_ask_yandex_lite", return_value={"fit": 5}) as ya:
            self.assertEqual(ai_scorer.score_order(ORDER), {"fit": 5})
        ya.assert_called_once()


class ChainResolveTest(ProviderTestBase):
    def test_typo_falls_back_to_default(self):
        self.use()
        with self.assertLogs(level="ERROR"):
            chain = ai_scorer._resolve_chain("grokk,yandx")
        self.assertEqual(chain, ["gemini", "groq", "cerebras", "mistral", "openrouter"])

    def test_chain_without_keys_falls_back(self):
        self.use()
        # yandex без YANDEX_ENABLED ключей не даёт, а у groq и др. они есть
        with self.assertLogs(level="ERROR"):
            chain = ai_scorer._resolve_chain("yandex")
        self.assertIn("groq", chain)

    def test_good_chain_kept(self):
        self.use()
        self.assertEqual(ai_scorer._resolve_chain("mistral,groq"), ["mistral", "groq"])

    def test_default_when_unset(self):
        self.use()
        self.assertEqual(ai_scorer._resolve_chain(None)[0], "gemini")


class SafetyTest(ProviderTestBase):
    def test_402_does_not_call_second_model(self):
        prov = self.use(GROQ_MODELS="m1,m2", CEREBRAS_MODELS="c1")
        with self.assertLogs(level="ERROR"):
            _, api = self.run_order({"groq.com": resp(402), "cerebras.ai": resp(200)})
        self.assertEqual(api.hosts().count("api.groq.com"), 1)
        self.assertTrue(prov["groq"].paid_reason)
        self.assertIn("groq", ai_scorer.status()["paid_errors"])

    def test_dead_set_by_other_thread_during_wait(self):
        prov = self.use(GROQ_MODELS="m1,m2", CEREBRAS_MODELS="c1")
        prov["groq"].next_at = ai_scorer.time.monotonic() + 5

        def other_thread_got_402(_sec):
            prov["groq"].dead = True

        with mock.patch("time.sleep", side_effect=other_thread_got_402):
            result, api = self.run_order({"groq.com": resp(200), "cerebras.ai": resp(200)})
        self.assertEqual(api.hosts(), ["api.cerebras.ai"])
        self.assertEqual(result["model"], "cerebras:c1")
        self.assertEqual(prov["groq"].count, 0)  # слот возвращён

    def test_403_moderation_does_not_block_key(self):
        prov = self.use(GROQ_MODELS="m1,m2")
        result, _ = self.run_order({"groq.com": [
            resp(403, text='{"error": "input flagged by moderation"}'), resp(200)]})
        self.assertEqual(result["model"], "groq:m2")
        self.assertEqual(prov["groq"].blocked_until, 0.0)
        self.assertEqual(prov["groq"].key_error, "")

    def test_key_error_expires_and_clears_on_success(self):
        prov = self.use(GROQ_MODELS="m1")
        self.run_order({"groq.com": resp(401)})
        self.assertIn("groq", ai_scorer.status()["key_errors"])
        prov["groq"].blocked_until = ai_scorer.time.monotonic() - 1  # срок вышел
        st = ai_scorer.status()
        # таймер снимает только блокировку, предупреждение остаётся до реального ответа
        self.assertEqual(prov["groq"].blocked_until, 0.0)
        self.assertEqual(st["providers"]["groq"]["blocked_sec"], 0)
        self.assertIn("groq", st["key_errors"])
        # ключ снова принят: реальный ответ не 401/403 (запрос ушёл после блокировки) снимает
        self.run_order({"groq.com": resp(200)})
        self.assertEqual(prov["groq"].key_error, "")
        self.assertNotIn("groq", ai_scorer.status()["key_errors"])

    def test_old_request_does_not_clear_block(self):
        prov = self.use(GROQ_MODELS="m1")
        # блокировка поставлена «позже», чем уходит запрос: ответ 200 на него её не стирает
        prov["groq"].key_error = "ключ groq отклонён (HTTP 401)"
        prov["groq"].key_error_at = ai_scorer.time.monotonic() + 1000
        self.run_order({"groq.com": resp(200)})
        self.assertNotEqual(prov["groq"].key_error, "")
        # запрос после блокировки - снимает
        prov["groq"].key_error_at = ai_scorer.time.monotonic() - 10
        self.run_order({"groq.com": resp(200)})
        self.assertEqual(prov["groq"].key_error, "")

    def test_openrouter_paid_response_kills_provider(self):
        prov = self.use(OPENROUTER_MODELS="openrouter/free,x:free")
        with mock.patch.object(ai_scorer, "CHAIN", ["openrouter"]), self.assertLogs(level="ERROR"):
            result, api = self.run_order({"openrouter.ai": resp(
                200, extra={"model": "vendor/paid-model", "usage": {"cost": 0.0004}})})
        self.assertIsNone(result)
        self.assertTrue(prov["openrouter"].dead)
        self.assertEqual(len(api.calls), 1)
        self.assertIn("openrouter", ai_scorer.status()["paid_errors"])

    def test_openrouter_cost_decides(self):
        paid = ai_scorer._openrouter_response_is_paid
        self.assertFalse(paid({"model": "apodex/apodex-1.1-mini:free", "usage": {"cost": 0}}))
        self.assertFalse(paid({"model": "vendor/no-suffix", "usage": {"cost": 0}}))  # cost=0 решает
        self.assertTrue(paid({"model": "vendor/x:free", "usage": {"cost": 0.0001}}))
        self.assertTrue(paid({"model": "vendor/no-suffix"}))                          # нет cost, нет :free
        self.assertFalse(paid({"model": "vendor/x:free"}))                            # нет cost, :free
        self.assertTrue(paid({"usage": {"cost": "abc"}}))
        self.assertFalse(paid({}))

    def test_openrouter_requests_usage_include(self):
        self.use(OPENROUTER_MODELS="x:free")
        with mock.patch.object(ai_scorer, "CHAIN", ["openrouter"]):
            _, api = self.run_order({"openrouter.ai": resp(200)})
        self.assertEqual(api.calls[0][1]["json"]["usage"], {"include": True})
        self.use(GROQ_MODELS="g1")
        with mock.patch.object(ai_scorer, "CHAIN", ["groq"]):
            _, api = self.run_order({"groq.com": resp(200)})
        self.assertNotIn("usage", api.calls[0][1]["json"])

    def test_paid_checked_before_choices_parsed(self):
        prov = self.use(OPENROUTER_MODELS="x:free")
        r = resp(200, extra={"model": "vendor/paid", "usage": {"cost": 0.01}})
        r.json.return_value = {"model": "vendor/paid", "usage": {"cost": 0.01}}  # без choices
        with mock.patch.object(ai_scorer, "CHAIN", ["openrouter"]), self.assertLogs(level="ERROR"):
            result, _ = self.run_order({"openrouter.ai": r})
        self.assertIsNone(result)
        self.assertTrue(prov["openrouter"].dead)

    def test_status_has_paid_reason(self):
        self.use(GROQ_MODELS="m1")
        with self.assertLogs(level="ERROR"):
            self.run_order({"groq.com": resp(402)})
        st = ai_scorer.status()["providers"]["groq"]
        self.assertTrue(st["dead"])
        self.assertIn("402", st["paid_reason"])

    def test_openrouter_free_response_ok(self):
        for extra in ({"model": "vendor/some:free"},
                      {"model": "vendor/x", "usage": {"cost": 0}},
                      {"model": "vendor/some:free", "usage": {"cost": 0}}):
            prov = self.use(OPENROUTER_MODELS="openrouter/free")
            with mock.patch.object(ai_scorer, "CHAIN", ["openrouter"]):
                result, _ = self.run_order({"openrouter.ai": resp(200, extra=extra)})
            self.assertIsNotNone(result, extra)
            self.assertFalse(prov["openrouter"].dead)

    def test_is_paid(self):
        self.assertFalse(ai_scorer._is_paid({"prompt": "0", "completion": "0", "request": "0.0"}))
        self.assertTrue(ai_scorer._is_paid({"prompt": "0", "completion": "0", "web_search": "0.01"}))
        self.assertTrue(ai_scorer._is_paid({"prompt": "0", "image": "0.001"}))
        self.assertTrue(ai_scorer._is_paid({"prompt": "-1"}))
        self.assertTrue(ai_scorer._is_paid({"prompt": "abc"}))
        self.assertTrue(ai_scorer._is_paid(None))
        self.assertTrue(ai_scorer._is_paid("0"))

    def test_json_validate_failed_goes_next_without_no_json(self):
        prov = self.use(GROQ_MODELS="m1,m2")
        bad = resp(400, text='{"error": {"code": "json_validate_failed", '
                             '"message": "response_format json_object: failed to validate"}}')
        result, api = self.run_order({"groq.com": [bad, resp(200)]})
        self.assertEqual(result["model"], "groq:m2")
        self.assertEqual(len(api.calls), 2)
        self.assertEqual(api.calls[0][1]["json"]["model"], "m1")
        self.assertEqual(api.calls[1][1]["json"]["model"], "m2")
        self.assertEqual(prov["groq"].no_json, set())

    def test_generic_json_400_does_not_retry(self):
        prov = self.use(GROQ_MODELS="m1")
        with mock.patch.object(ai_scorer, "CHAIN", ["groq"]):
            _, api = self.run_order({"groq.com": resp(400, text="bad json body")})
        self.assertEqual(len(api.calls), 1)
        self.assertEqual(prov["groq"].no_json, set())

    def test_content_list_is_joined(self):
        self.use(GROQ_MODELS="m1")
        content = [{"type": "text", "text": GOOD[:20]}, {"type": "text", "text": GOOD[20:]}]
        with mock.patch.object(ai_scorer, "CHAIN", ["groq"]):
            result, _ = self.run_order({"groq.com": resp(200, content=content)})
        self.assertEqual(result["model"], "groq:m1")
        self.assertEqual(ai_scorer._content_text(["a", {"text": "b"}, 5]), "ab")

    def test_content_not_text_goes_next(self):
        self.use(GROQ_MODELS="m1", CEREBRAS_MODELS="c1")
        result, _ = self.run_order({"groq.com": resp(200, content=12345),
                                    "cerebras.ai": resp(200)})
        self.assertEqual(result["model"], "cerebras:c1")
        self.assertIsNone(ai_scorer._content_text(None))

    def test_order_deadline(self):
        self.use(GROQ_MODELS="m1")
        with mock.patch.object(ai_scorer, "ORDER_DEADLINE_SEC", 0.0):
            result, api = self.run_order({"groq.com": resp(200)})
        self.assertIsNone(result)
        self.assertEqual(api.calls, [])

    def test_deadline_stops_next_models(self):
        prov = self.use(GROQ_MODELS="m1,m2")
        deadline = ai_scorer.time.monotonic() + 100
        api = FakeApi({"groq.com": resp(500)})
        real = ai_scorer.time.monotonic
        # после первой модели «наступает» дедлайн: m2 уже не начинаем
        clock = {"shift": 0.0}

        def fake_post(url, **kw):
            clock["shift"] = 1000.0
            return api(url, **kw)

        with mock.patch("requests.post", side_effect=fake_post), \
                mock.patch.object(ai_scorer.time, "monotonic", side_effect=lambda: real() + clock["shift"]):
            result = ai_scorer._ask_provider(prov["groq"], ORDER, deadline)
        self.assertIsNone(result)
        self.assertEqual(len(api.calls), 1)


class HealthReportTest(unittest.TestCase):
    def run_report(self, statuses, ai_ok=1, attempted=1):
        """statuses - список статусов по циклам; возвращает (монитор, алерты по циклам)."""
        monitor = HealthMonitor()
        res = pipeline.CycleResult(ai_attempted=attempted, ai_ok=ai_ok)
        out = []
        for extra in statuses:
            st = {"key_errors": {}, "paid_errors": {}, "last_error": "", "config_error": "",
                  "providers": {"groq": {}, "openrouter": {}}}
            st.update(extra)
            with mock.patch.object(pipeline, "monitor", monitor), \
                    mock.patch.object(pipeline.ai_scorer, "status", return_value=st), \
                    mock.patch.object(pipeline, "kv_get", return_value="0"), \
                    mock.patch.object(pipeline, "kv_set"):
                pipeline._report_ai_health(res)
            out.append(monitor.pop_alerts())
        return monitor, out

    def test_key_error_is_separate_warning_not_ai_down(self):
        monitor, out = self.run_report([{"key_errors": {"groq": "ключ groq отклонён (HTTP 401)"}}])
        self.assertEqual(len(out[0]), 1)
        self.assertIn("groq", out[0][0])
        self.assertIn("ключ отклонён", out[0][0])
        self.assertEqual(monitor.streak.get("gemini", 0), 0)  # «AI не работает» не поднят

    def test_key_recovered(self):
        _, out = self.run_report([{"key_errors": {"groq": "x"}}, {}])
        self.assertEqual(len(out[0]), 1)
        self.assertEqual(len(out[1]), 1)
        self.assertIn("принят", out[1][0])

    def test_paid_alert_is_immediate(self):
        _, out = self.run_report([{"paid_errors": {"openrouter": "openrouter: HTTP 402 (требуется оплата)"}}])
        self.assertEqual(len(out[0]), 1)
        self.assertIn("openrouter", out[0][0])
        self.assertIn("платный", out[0][0])

    def test_nobody_answered_needs_streak(self):
        monitor, out = self.run_report([{}], ai_ok=0, attempted=3)
        self.assertEqual(out[0], [])  # порог 3 цикла
        self.assertEqual(monitor.streak["gemini"], 1)


if __name__ == "__main__":
    unittest.main()
