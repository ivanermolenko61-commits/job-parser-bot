"""Тесты цепочки бесплатных AI-провайдеров (groq, cerebras, mistral, openrouter). Без сети.

Запуск из корня проекта: python -m unittest tests.test_providers -v
"""
import json
import time
import unittest
from datetime import datetime, timezone
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
            # счётчики за сутки не должны трогать реальную БД
            mock.patch.object(ai_scorer, "_load_count"),
            mock.patch.object(ai_scorer, "_save_count"),
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
                         ["gemini", "cloudflare", "openrouter", "github", "groq", "cerebras", "mistral"])

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
            "nvidia/nemotron-3-super-120b-a12b:free", "openrouter/free"])
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
        self.assertEqual(chain, ["gemini", "cloudflare", "openrouter", "github", "groq", "cerebras", "mistral"])

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


class NewProvidersTest(unittest.TestCase):
    """GitHub Models и Cloudflare: URL, ключи, аккаунт Cloudflare."""

    def test_github_spec(self):
        p = ai_scorer._build_providers({"GITHUB_API_KEY": " tok "})["github"]
        self.assertEqual(p.base_url, "https://models.github.ai/inference")
        self.assertEqual(p.key, "tok")
        self.assertEqual(p.models, ["openai/gpt-4.1-mini", "openai/gpt-4o-mini"])

    def test_cloudflare_account_in_url(self):
        p = ai_scorer._build_providers({"CLOUDFLARE_API_KEY": "tok",
                                        "CLOUDFLARE_ACCOUNT_ID": "abc123"})["cloudflare"]
        self.assertEqual(p.base_url, "https://api.cloudflare.com/client/v4/accounts/abc123/ai/v1")
        self.assertEqual(p.key, "tok")
        self.assertTrue(p.shared_daily)

    def test_cloudflare_without_account_disabled(self):
        for account in ("", "../evil", "a b"):
            p = ai_scorer._build_providers({"CLOUDFLARE_API_KEY": "tok",
                                            "CLOUDFLARE_ACCOUNT_ID": account})["cloudflare"]
            self.assertEqual(p.key, "", account)
            self.assertNotIn("{", p.base_url)

    def test_default_chain_order(self):
        self.assertEqual(ai_scorer._parse_chain(None)[:4], ["gemini", "cloudflare", "openrouter", "github"])
        self.assertNotIn("yandex", ai_scorer._parse_chain(None))


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


def raw_resp(payload, status=200, text=None):
    """Ответ 200 с произвольным JSON-телом (для content_filter и пустых choices)."""
    r = mock.Mock()
    r.status_code = status
    r.headers = {}
    r.text = text if text is not None else json.dumps(payload)
    r.json.return_value = payload
    return r


class EmptyAnswerTest(ProviderTestBase):
    def test_content_filter_goes_next(self):
        prov = self.use(GROQ_MODELS="m1")
        payload = {"choices": [{"finish_reason": "content_filter", "message": {"content": None}}]}
        api = FakeApi({"groq.com": raw_resp(payload)})
        with mock.patch("requests.post", side_effect=api):
            outcome = ai_scorer._ask_provider_model(prov["groq"], "m1", ORDER)
        self.assertEqual(outcome, "next")
        self.assertIn("отказ модерации", prov["groq"].last_error)
        self.assertIn("отказ модерации", ai_scorer._last_error)

    def test_empty_choices(self):
        prov = self.use(GROQ_MODELS="m1")
        api = FakeApi({"groq.com": raw_resp({"choices": []})})
        with mock.patch("requests.post", side_effect=api):
            outcome = ai_scorer._ask_provider_model(prov["groq"], "m1", ORDER)
        self.assertEqual(outcome, "next")
        self.assertEqual(prov["groq"].last_error, "groq:m1: нет choices")

    def test_empty_content_has_finish_reason(self):
        prov = self.use(GROQ_MODELS="m1")
        payload = {"choices": [{"finish_reason": "length", "message": {"content": None}}]}
        api = FakeApi({"groq.com": raw_resp(payload)})
        with mock.patch("requests.post", side_effect=api):
            ai_scorer._ask_provider_model(prov["groq"], "m1", ORDER)
        self.assertEqual(prov["groq"].last_error, "groq:m1: пустой ответ (finish_reason=length)")

    def test_last_error_per_provider_and_cleared_by_success(self):
        prov = self.use(GROQ_MODELS="m1", CEREBRAS_MODELS="c1")
        api = FakeApi({"groq.com": resp(500), "cerebras.ai": resp(200)})
        with mock.patch("requests.post", side_effect=api):
            ai_scorer._ask_provider_model(prov["groq"], "m1", ORDER)
            self.assertEqual(prov["groq"].last_error, "groq:m1: HTTP 500")
            self.assertEqual(prov["cerebras"].last_error, "")
            self.assertEqual(ai_scorer.status()["providers"]["groq"]["last_error"],
                             "groq:m1: HTTP 500")
        api = FakeApi({"groq.com": resp(200)})
        with mock.patch("requests.post", side_effect=api):
            ai_scorer._ask_provider_model(prov["groq"], "m1", ORDER)
        self.assertEqual(prov["groq"].last_error, "")


class FailureSummaryTest(ProviderTestBase):
    NOW = datetime(2026, 10, 11, 10, 0, tzinfo=timezone.utc)   # 13:00 МСК

    def setUp(self):
        super().setUp()
        self.prov = self.use(GROQ_MODELS="m1", CEREBRAS_MODELS="c1", MISTRAL_MODELS="s1",
                             OPENROUTER_MODELS="x:free")

    def pause(self, name, seconds):
        p = self.prov[name]
        for m in p.models:
            p.paused[m] = ai_scorer.time.monotonic() + seconds

    def test_all_paused(self):
        self.pause("groq", 3630)
        self.pause("cerebras", 7230)
        self.pause("mistral", 5430)
        self.pause("openrouter", 3630)
        text = ai_scorer.failure_summary(self.NOW)
        self.assertTrue(text.startswith("кончились бесплатные квоты, оценка возобновится ~14:00 МСК"))
        self.assertIn("cerebras: на паузе до 15:00 МСК", text)

    def test_all_blocked_by_daily_cap(self):
        for p in self.prov.values():
            p.day, p.count = "2026-10-11", p.max_per_day
        text = ai_scorer.failure_summary(self.NOW)
        self.assertIn("оценка возобновится ~03:00 МСК", text)
        self.assertIn("лимит 900/900 за сутки", text)

    def test_mixed(self):
        self.pause("groq", 3630)
        self.prov["cerebras"].last_error = "cerebras:c1: HTTP 500"
        self.prov["mistral"].key_error = "ключ mistral отклонён (HTTP 401)"
        text = ai_scorer.failure_summary(self.NOW)
        self.assertNotIn("кончились", text)
        self.assertIn("groq: на паузе до 14:00 МСК", text)
        self.assertIn("cerebras:c1: HTTP 500", text)
        self.assertNotIn("cerebras: cerebras:", text)
        self.assertIn("mistral: ключ mistral отклонён (HTTP 401)", text)
        self.assertIn("openrouter: нет ответа", text)
        self.assertIn(" · ", text)

    def test_alert_has_no_runtime_error_prefix(self):
        monitor = HealthMonitor()
        res = pipeline.CycleResult(ai_attempted=3, ai_ok=0)
        st = {"key_errors": {}, "paid_errors": {}, "providers": {}}
        with mock.patch.object(pipeline, "monitor", monitor), \
                mock.patch.object(pipeline.ai_scorer, "status", return_value=st), \
                mock.patch.object(pipeline.ai_scorer, "failure_summary", return_value="groq: нет ответа"), \
                mock.patch.object(pipeline, "kv_get", return_value="2"), \
                mock.patch.object(pipeline, "kv_set"):
            for _ in range(3):   # порог монитора - 3 плохих цикла подряд
                pipeline._report_ai_health(res)
        alerts = monitor.pop_alerts()
        self.assertEqual(len(alerts), 1)
        self.assertIn("groq: нет ответа", alerts[0])
        self.assertNotIn("RuntimeError", alerts[0])


class GeminiRegionTest(ProviderTestBase):
    NOW = datetime(2026, 10, 11, 10, 0, tzinfo=timezone.utc)

    def setUp(self):
        super().setUp()
        for name, value in (("GEMINI_KEY", "k"), ("CHAIN", ["gemini"]), ("GEMINI_MODELS", ["g1", "g2"]),
                            ("_config_blocked_until", 0.0), ("_config_error", ""),
                            ("_region_error", ""), ("_paused_until", {}), ("_disabled", set())):
            p = mock.patch.object(ai_scorer, name, value)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(ai_scorer, "_throttle")
        p.start()
        self.addCleanup(p.stop)

    def test_region_blocks_all_gemini(self):
        body = ('{"error": {"code": 400, "message": "User location is not supported for the API use.", '
                '"status": "FAILED_PRECONDITION"}}')
        api = FakeApi({"googleapis.com": resp(400, text=body)})
        with mock.patch("requests.post", side_effect=api):
            self.assertIsNone(ai_scorer.score_order(ORDER))
            self.assertIsNone(ai_scorer.score_order(ORDER))   # блокировка: сети нет
        self.assertEqual(len(api.calls), 1)                   # вторая модель не вызывалась
        st = ai_scorer.status()
        self.assertEqual(st["key_errors"], {})
        self.assertIn("регион", st["region_error"])
        text = ai_scorer.failure_summary(self.NOW)
        self.assertIn("регион сервера не поддерживается, повтор в", text)
        self.assertNotIn("кончились", text)   # регион - не квота

    def test_success_clears_region(self):
        ai_scorer._region_error = "x"
        api = FakeApi({"googleapis.com": mock.Mock(
            status_code=200, text="ok", headers={},
            json=mock.Mock(return_value={"candidates": [{"content": {"parts": [{"text": GOOD}]}}]}))})
        with mock.patch("requests.post", side_effect=api):
            self.assertIsNotNone(ai_scorer.score_order(ORDER))
        self.assertEqual(ai_scorer._region_error, "")


class NonJsonTest(ProviderTestBase):
    def non_json(self, ctype, text):
        r = mock.Mock()
        r.status_code = 200
        r.headers = {"content-type": ctype}
        r.text = text
        r.json.side_effect = ValueError("no json")
        return r

    def test_html_pauses_all_models(self):
        prov = self.use(GROQ_MODELS="m1,m2")
        api = FakeApi({"groq.com": self.non_json("text/html; charset=utf-8", "<html>\nblocked</html>")})
        with mock.patch("requests.post", side_effect=api):
            outcome = ai_scorer._ask_provider_model(prov["groq"], "m1", ORDER)
        self.assertEqual(outcome, "next")
        self.assertEqual(set(prov["groq"].paused), {"m1", "m2"})
        self.assertIn("ответ не JSON (text/html; charset=utf-8): «<html> blocked</html>»",
                      prov["groq"].last_error)

    def test_plain_text_no_pause(self):
        prov = self.use(GROQ_MODELS="m1,m2")
        api = FakeApi({"groq.com": self.non_json("text/plain", "oops")})
        with mock.patch("requests.post", side_effect=api):
            ai_scorer._ask_provider_model(prov["groq"], "m1", ORDER)
        self.assertFalse(prov["groq"].paused)
        self.assertIn("(text/plain)", prov["groq"].last_error)

    def test_empty_body_pauses(self):
        prov = self.use(GROQ_MODELS="m1,m2")
        api = FakeApi({"groq.com": self.non_json("application/json", "")})
        with mock.patch("requests.post", side_effect=api):
            ai_scorer._ask_provider_model(prov["groq"], "m1", ORDER)
        self.assertEqual(set(prov["groq"].paused), {"m1"})   # пустое тело - только эта модель


class ScoredFromRowTest(unittest.TestCase):
    ROW = {"order_id": 7, "site": "kworkru", "title": "Бот", "url": "https://e.com/7",
           "price_text": "5000 ₽", "published_at": "2026-10-11T10:00:00+00:00",
           "dedup_key": "k7", "ai_json": json.dumps({"fit": 8, "summary": "s"})}

    def test_restores_order_and_ai(self):
        sc = pipeline.scored_from_row(self.ROW)
        self.assertEqual(sc.order.order_id, 7)
        self.assertEqual(sc.order.title, "Бот")
        self.assertEqual(sc.order.url, "https://e.com/7")
        self.assertEqual(sc.order.published_at.hour, 10)
        self.assertEqual(sc.fit, 8)
        self.assertEqual(sc.dedup, "k7")

    def test_broken_ai_json(self):
        for bad in ("{oops", "", None, "[1]"):
            sc = pipeline.scored_from_row(dict(self.ROW, ai_json=bad))
            self.assertIsNone(sc.ai, bad)
            self.assertIsNone(sc.fit)


class ProbeTest(ProviderTestBase):
    """/aitest: по одному запросу на провайдера, понятная причина у каждого."""

    def test_probe_reports_each_provider(self):
        providers = self.use(OPENROUTER_API_KEY="")
        mistral = providers["mistral"]
        mistral.day, mistral.count = ai_scorer._today(), mistral.max_per_day
        providers["groq"].paused[providers["groq"].models[0]] = float("inf")  # пауза обходится
        html_page = resp(200, text="<html>blocked</html>", headers={"content-type": "text/html"})
        html_page.json.side_effect = ValueError("not json")
        api = FakeApi({"groq.com": resp(), "cerebras.ai": html_page})
        with mock.patch("requests.post", side_effect=api):
            results = {r["provider"]: r for r in ai_scorer.probe()}
        self.assertTrue(results["groq"]["ok"])
        self.assertEqual(results["groq"]["fit"], 8)
        self.assertFalse(results["cerebras"]["ok"])
        self.assertIn("не JSON", results["cerebras"]["error"])
        self.assertIn("потолок", results["mistral"]["error"])
        self.assertFalse(results["openrouter"]["configured"])
        self.assertEqual(sorted(api.hosts()), ["api.cerebras.ai", "api.groq.com"])  # mistral не вызывался


class SlotReturnTest(ProviderTestBase):
    def count_after(self, response=None, exc=None):
        prov = self.use(GROQ_MODELS="m1")
        api = FakeApi({"groq.com": response}) if response is not None else None
        with mock.patch("requests.post", side_effect=exc or api):
            ai_scorer._ask_provider_model(prov["groq"], "m1", ORDER)
        return prov["groq"].count

    def test_returned_when_no_provider_answer(self):
        self.assertEqual(self.count_after(resp(500)), 0)
        self.assertEqual(self.count_after(resp(503)), 0)
        self.assertEqual(self.count_after(exc=OSError("net")), 0)
        bad = mock.Mock(status_code=200, headers={"content-type": "text/plain"}, text="oops")
        bad.json.side_effect = ValueError("no json")
        self.assertEqual(self.count_after(bad), 0)

    def test_timeout_keeps_slot(self):
        import requests
        # при таймауте провайдер мог засчитать запрос: слот остаётся
        self.assertEqual(self.count_after(exc=requests.Timeout("slow")), 1)

    def test_kept_when_provider_answered(self):
        self.assertEqual(self.count_after(resp(429, headers={"retry-after": "5"})), 1)
        self.assertEqual(self.count_after(resp(400, text="bad")), 1)
        self.assertEqual(self.count_after(resp(200, content="не json")), 1)   # ответ был, не разобран
        self.assertEqual(self.count_after(resp(200)), 1)


class CountPersistenceTest(unittest.TestCase):
    def setUp(self):
        self.store = {}
        for p in (mock.patch("database.kv_get", side_effect=lambda k, d=None: self.store.get(k, d)),
                  mock.patch("database.kv_set", side_effect=lambda k, v: self.store.__setitem__(k, str(v))),
                  mock.patch("time.sleep")):
            p.start()
            self.addCleanup(p.stop)

    def ask(self, provider):
        with mock.patch("requests.post", side_effect=FakeApi({"groq.com": resp(200)})):
            ai_scorer._ask_provider_model(provider, "m1", ORDER)

    def provider(self):
        return ai_scorer._build_providers(env(GROQ_MODELS="m1"))["groq"]

    def test_counter_survives_restart(self):
        today = ai_scorer._today()
        p = self.provider()
        self.ask(p)
        self.ask(p)
        self.assertEqual(self.store["ai_count_groq"], f"{today}:2")
        p2 = self.provider()                      # «рестарт»: новые объекты провайдеров
        self.ask(p2)
        self.assertEqual(p2.count, 3)
        self.assertEqual(self.store["ai_count_groq"], f"{today}:3")

    def test_load_blocks_concurrent_reserve(self):
        import threading
        today = ai_scorer._today()
        self.store["ai_count_groq"] = f"{today}:7"
        started = threading.Event()

        def slow_get(key, default=None):
            started.set()
            threading.Event().wait(0.3)      # медленное чтение (time.sleep замокан)
            return self.store.get(key, default)

        p = self.provider()
        with mock.patch("database.kv_get", side_effect=slow_get):
            loader = threading.Thread(target=ai_scorer._load_count, args=(p,))
            loader.start()
            started.wait(2)
            reserver = threading.Thread(target=ai_scorer._reserve_slot, args=(p,))
            reserver.start()                 # должен дождаться загрузки, а не занять слот раньше
            loader.join(5)
            reserver.join(5)
        self.assertEqual(p.count, 8)
        self.assertEqual(self.store["ai_count_groq"], f"{today}:8")

    def test_foreign_values_ignored(self):
        for raw in ("garbage", "2020-01-01:5", f"{ai_scorer._today()}:abc", ":", ""):
            self.store["ai_count_groq"] = raw
            p = self.provider()
            self.ask(p)
            self.assertEqual(p.count, 1, raw)

    def test_db_error_works_in_memory(self):
        with mock.patch("database.kv_get", side_effect=OSError("db")), \
                mock.patch("database.kv_set", side_effect=OSError("db")):
            p = self.provider()
            self.ask(p)
        self.assertEqual(p.count, 1)


class DailyCapacityTest(ProviderTestBase):
    def test_sums_left_and_total(self):
        prov = self.use(GROQ_MAX_PER_DAY="10", CEREBRAS_MAX_PER_DAY="20",
                        MISTRAL_MAX_PER_DAY="30", OPENROUTER_MAX_PER_DAY="40")
        today = ai_scorer._today()
        prov["groq"].day, prov["groq"].count = today, 4
        prov["cerebras"].day, prov["cerebras"].count = "2000-01-01", 20   # вчерашний счётчик не в счёт
        prov["mistral"].dead = True                                        # выключен из-за оплаты
        prov["openrouter"].day, prov["openrouter"].count = today, 99       # сверх потолка - не в минус
        self.assertEqual(ai_scorer.daily_capacity(), (6 + 20 + 0, 10 + 20 + 40))

    def test_none_without_capped_providers(self):
        self.use()
        with mock.patch.object(ai_scorer, "CHAIN", ["gemini"]), \
                mock.patch.object(ai_scorer, "GEMINI_KEY", "k"):
            self.assertIsNone(ai_scorer.daily_capacity())

    def test_unavailable_provider_left_not_counted(self):
        prov = self.use(GROQ_MAX_PER_DAY="10", CEREBRAS_MAX_PER_DAY="20",
                        MISTRAL_MAX_PER_DAY="30", OPENROUTER_MAX_PER_DAY="40")
        now = time.monotonic()
        prov["groq"].blocked_until = now + 300                              # ключ отклонён
        prov["cerebras"].paused = {m: now + 3 * 3600 for m in prov["cerebras"].models}  # дневной 429
        prov["mistral"].paused = {m: now + 60 for m in prov["mistral"].models}          # короткая пауза
        self.assertEqual(ai_scorer.daily_capacity(), (30 + 40, 100))

    def test_gemini_available_disables_reserve(self):
        self.use()
        with mock.patch.object(ai_scorer, "CHAIN", ["gemini", "groq"]), \
                mock.patch.object(ai_scorer, "GEMINI_KEY", "k"), \
                mock.patch.object(ai_scorer, "_region_error", ""), \
                mock.patch.object(ai_scorer, "_config_blocked_until", 0.0):
            self.assertIsNone(ai_scorer.daily_capacity())
            with mock.patch.object(ai_scorer, "_region_error", "регион"):
                self.assertIsNotNone(ai_scorer.daily_capacity())

    def test_provider_without_key_not_counted(self):
        self.use(GROQ_API_KEY="", GROQ_MAX_PER_DAY="10")
        self.assertEqual(ai_scorer.daily_capacity()[1], 300 + 1000 + 45)

    def test_cloudflare_default_cap(self):
        prov = ai_scorer._build_providers({"CLOUDFLARE_API_KEY": "k", "CLOUDFLARE_ACCOUNT_ID": "a1"})
        self.assertEqual(prov["cloudflare"].max_per_day, 50)


if __name__ == "__main__":
    unittest.main()
