"""Focused tests for deterministic WebUI signal invariants and local fallback memory."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import math
import unittest

import httpx
from starlette.testclient import TestClient

from webui.analytics import build_signal
from webui.config import Settings
from webui.llm import ModelRouter
from webui.market_data import MarketDataResult, MarketDataService, MarketDataUnavailable
from webui.memory import InMemoryConversationMemory, MemoryMessage
from webui.models import Candle, Market, OandaEnvironment, SignalAction
from webui.oanda import OandaReadOnlyService
from webui.overlays import build_smc_overlays


def bullish_break_fixture() -> list[Candle]:
    """Create a deterministic, confirmed bullish-break sequence with non-extreme RSI."""
    candles: list[Candle] = []
    for index in range(100):
        close = 100 + index * 0.05 + 2 * math.sin(index / 2)
        candles.append(
            Candle(
                timestamp=1_700_000_000_000 + index * 3_600_000,
                open=close - 0.05,
                high=close + 0.25,
                low=close - 0.30,
                close=close,
                volume=100 + index % 4,
            )
        )
    # The last completed bar closes through the preceding, fully confirmed pivot high.
    break_close = 107.79718174482353
    candles[-1] = Candle(
        timestamp=candles[-1].timestamp,
        open=break_close - 0.05,
        high=break_close + 0.20,
        low=break_close - 0.30,
        close=break_close,
        volume=150,
    )
    return candles


class TestSignalEngine(unittest.TestCase):
    def test_buy_has_atr_stop_minimum_two_r_target_and_full_reasoning(self) -> None:
        signal = build_signal(
            bullish_break_fixture(),
            symbol="BTCUSDT",
            market=Market.CRYPTO,
            timeframe="1h",
            data_source="fixture",
        )
        self.assertEqual(signal.action, SignalAction.BUY)
        self.assertIsNotNone(signal.entry_price)
        self.assertIsNotNone(signal.stop_loss)
        self.assertIsNotNone(signal.take_profit)
        self.assertGreater(signal.entry_price, signal.stop_loss)
        self.assertAlmostEqual((signal.entry_price - signal.stop_loss) / signal.technicals.atr, 1.5, delta=0.001)
        self.assertGreaterEqual((signal.take_profit - signal.entry_price) / (signal.entry_price - signal.stop_loss), 2.0)
        self.assertGreaterEqual(len(signal.reasoning), 10)
        self.assertLessEqual(len(signal.reasoning), 15)
        self.assertEqual(len(signal.exit_plan), 3)

    def test_missing_structure_break_returns_no_signal_without_levels(self) -> None:
        candles = bullish_break_fixture()
        # Keep last close below the confirmed swing high: the mandatory break gate fails.
        previous = candles[-1]
        candles[-1] = Candle(
            timestamp=previous.timestamp,
            open=103.0,
            high=103.2,
            low=102.7,
            close=103.0,
            volume=150,
        )
        signal = build_signal(
            candles,
            symbol="BTCUSDT",
            market=Market.CRYPTO,
            timeframe="1h",
            data_source="fixture",
        )
        self.assertEqual(signal.action, SignalAction.NO_SIGNAL)
        self.assertIsNone(signal.entry_price)
        self.assertIsNone(signal.stop_loss)
        self.assertIsNone(signal.take_profit)
        self.assertIsNone(signal.risk_reward)
        self.assertEqual(len(signal.reasoning), 11)


class TestMarketDataPolicy(unittest.TestCase):
    def test_provider_in_progress_bar_is_excluded(self) -> None:
        candles = [
            Candle(timestamp=1_700_000_000_000 + index * 60_000, open=10, high=11, low=9, close=10, volume=1)
            for index in range(81)
        ]
        result = MarketDataService._closed_result(candles, source="fixture", limit=100)
        self.assertEqual(len(result.candles), 80)
        self.assertEqual(result.candles[-1].timestamp, candles[-2].timestamp)


class TestSmcOverlayAdapter(unittest.TestCase):
    def test_closed_candle_overlay_includes_smc_studies_without_unconfirmed_tail_swings(self) -> None:
        candles = bullish_break_fixture()
        overlays = build_smc_overlays(
            candles=candles,
            symbol="BTCUSDT",
            market=Market.CRYPTO,
            timeframe="1h",
            source="fixture",
            session="London",
        )
        self.assertTrue(any(marker.text in {"SH", "SL"} for marker in overlays.markers))
        self.assertTrue(any(marker.text.startswith("BOS") for marker in overlays.markers))
        self.assertTrue({"fvg", "order_block"}.issubset({zone.kind for zone in overlays.zones}))
        self.assertTrue(all(marker.timestamp <= candles[-6].timestamp or marker.text.startswith(("BOS", "R ")) or marker.text == "London" for marker in overlays.markers))
        summary_labels = {item.label for item in overlays.summary}
        self.assertTrue({"FVG", "Order blocks", "Swings", "Retracement", "London"}.issubset(summary_labels))
        self.assertTrue(any(label.startswith("Prev 1D") for label in summary_labels))


class TestProviderAdapters(unittest.TestCase):
    def test_twelve_data_is_primary_normalizes_crypto_and_discards_newest_bar(self) -> None:
        async def scenario() -> None:
            requests: list[httpx.Request] = []
            values = [
                {
                    "datetime": (datetime(2024, 1, 1, tzinfo=timezone.utc) + timedelta(hours=index)).strftime("%Y-%m-%d %H:%M:%S"),
                    "open": str(10 + index),
                    "high": str(11 + index),
                    "low": str(9 + index),
                    "close": str(10.5 + index),
                    "volume": str(index + 1),
                }
                for index in range(81)
            ]

            def handler(request: httpx.Request) -> httpx.Response:
                requests.append(request)
                self.assertEqual(request.url.host, "api.twelvedata.com")
                self.assertEqual(request.url.params["symbol"], "BTC/USDT")
                self.assertEqual(request.url.params["interval"], "1h")
                self.assertEqual(request.url.params["apikey"], "twelve-test-key")
                return httpx.Response(200, json={"values": values})

            settings = replace(Settings.from_environment(), twelve_data_api_key="twelve-test-key")
            service = MarketDataService(settings)
            original_client = service._client
            service._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
            await original_client.aclose()
            try:
                result = await service.candles(symbol="BTCUSDT", market=Market.CRYPTO, timeframe="1h", limit=80)
            finally:
                await service.close()
            self.assertEqual(len(requests), 1)
            self.assertEqual(result.source, "Twelve Data market data")
            self.assertEqual(len(result.candles), 80)
            self.assertEqual(result.candles[0].close, 10.5)
            self.assertEqual(result.candles[-1].close, 89.5)

        asyncio.run(scenario())

    def test_unavailable_twelve_crypto_pair_falls_back_without_changing_symbol(self) -> None:
        async def scenario() -> None:
            calls: list[httpx.Request] = []
            rows = [
                [str(1_700_000_000 + index * 3_600), "10", "11", "9", "10.5", "10.4", "1", "4"]
                for index in range(81)
            ]

            def handler(request: httpx.Request) -> httpx.Response:
                calls.append(request)
                if request.url.host == "api.twelvedata.com":
                    self.assertEqual(request.url.params["symbol"], "BTC/USDT")
                    return httpx.Response(404)
                self.assertEqual(request.url.host, "api.kraken.com")
                self.assertEqual(request.url.params["pair"], "XBTUSDT")
                return httpx.Response(200, json={"error": [], "result": {"XBTUSDT": rows, "last": rows[-1][0]}})

            settings = replace(Settings.from_environment(), twelve_data_api_key="twelve-test-key")
            service = MarketDataService(settings)
            original_client = service._client
            service._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
            await original_client.aclose()
            try:
                result = await service.candles(symbol="BTCUSDT", market=Market.CRYPTO, timeframe="1h", limit=80)
            finally:
                await service.close()
            self.assertEqual([request.url.host for request in calls], ["api.twelvedata.com", "api.kraken.com"])
            self.assertEqual(result.source, "Kraken public spot market data (Twelve Data fallback)")
            self.assertEqual(len(result.candles), 80)

        asyncio.run(scenario())

    def test_crypto_prefers_kraken_before_binance(self) -> None:
        async def scenario() -> None:
            calls: list[httpx.Request] = []
            rows = [
                [str(1_700_000_000 + index * 3_600), "10", "11", "9", "10.5", "10.4", "1", "4"]
                for index in range(81)
            ]

            def handler(request: httpx.Request) -> httpx.Response:
                calls.append(request)
                self.assertEqual(request.url.host, "api.kraken.com")
                self.assertEqual(request.url.params["pair"], "XBTUSDT")
                self.assertEqual(request.url.params["interval"], "60")
                return httpx.Response(200, json={"error": [], "result": {"XBTUSDT": rows, "last": rows[-1][0]}})

            service = MarketDataService(Settings.from_environment())
            original_client = service._client
            service._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
            await original_client.aclose()
            try:
                result = await service.candles(symbol="BTCUSDT", market=Market.CRYPTO, timeframe="1h", limit=80)
            finally:
                await service.close()
            self.assertEqual(len(calls), 1)
            self.assertEqual(result.source, "Kraken public spot market data")
            self.assertEqual(result.candles[-1].timestamp, int(rows[-2][0]) * 1000)

        asyncio.run(scenario())

    def test_crypto_uses_bybit_when_kraken_is_unavailable(self) -> None:
        async def scenario() -> None:
            calls: list[httpx.Request] = []
            rows = [
                [str(1_700_000_000_000 + index * 3_600_000), str(10 + index), str(11 + index), str(9 + index), str(10.5 + index), str(index + 1)]
                for index in range(81)
            ]

            def handler(request: httpx.Request) -> httpx.Response:
                calls.append(request)
                if request.url.host in {"api.kraken.com", "api.exchange.coinbase.com"}:
                    return httpx.Response(451)
                self.assertEqual(request.url.host, "api.bybit.com")
                self.assertEqual(request.url.params["category"], "spot")
                self.assertEqual(request.url.params["symbol"], "BTCUSDT")
                self.assertEqual(request.url.params["interval"], "60")
                return httpx.Response(200, json={"retCode": 0, "result": {"list": list(reversed(rows))}})

            service = MarketDataService(Settings.from_environment())
            original_client = service._client
            service._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
            await original_client.aclose()
            try:
                result = await service.candles(symbol="BTCUSDT", market=Market.CRYPTO, timeframe="1h", limit=80)
            finally:
                await service.close()
            self.assertEqual([request.url.host for request in calls], ["api.kraken.com", "api.exchange.coinbase.com", "api.bybit.com"])
            self.assertEqual(result.source, "Bybit public spot market data")
            self.assertEqual(len(result.candles), 80)
            self.assertEqual(result.candles[-1].timestamp, int(rows[-2][0]))

        asyncio.run(scenario())

    def test_futures_use_the_bybit_linear_contract_feed(self) -> None:
        async def scenario() -> None:
            rows = [
                [str(1_700_000_000_000 + index * 3_600_000), "10", "11", "9", "10.5", "1"]
                for index in range(81)
            ]

            def handler(request: httpx.Request) -> httpx.Response:
                self.assertEqual(request.url.host, "api.bybit.com")
                self.assertEqual(request.url.params["category"], "linear")
                return httpx.Response(200, json={"retCode": 0, "result": {"list": list(reversed(rows))}})

            service = MarketDataService(Settings.from_environment())
            original_client = service._client
            service._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
            await original_client.aclose()
            try:
                result = await service.candles(symbol="BTCUSDT.P", market=Market.FUTURES, timeframe="1h", limit=80)
            finally:
                await service.close()
            self.assertEqual(result.source, "Bybit public futures data")
            self.assertEqual(len(result.candles), 80)

        asyncio.run(scenario())

    def test_twelve_data_errors_do_not_reflect_the_api_key(self) -> None:
        async def scenario() -> None:
            settings = replace(Settings.from_environment(), twelve_data_api_key="twelve-secret-must-not-leak")
            service = MarketDataService(settings)
            original_client = service._client
            service._client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(429)))
            await original_client.aclose()
            try:
                with self.assertRaisesRegex(MarketDataUnavailable, "HTTP 429") as raised:
                    await service.candles(symbol="EURUSD", market=Market.FOREX, timeframe="1h", limit=80)
            finally:
                await service.close()
            self.assertNotIn("twelve-secret-must-not-leak", str(raised.exception))

        asyncio.run(scenario())

    def test_oanda_snapshot_uses_only_practice_get_endpoints_and_keeps_account_id_out_of_payload(self) -> None:
        async def scenario() -> None:
            calls: list[httpx.Request] = []

            def handler(request: httpx.Request) -> httpx.Response:
                calls.append(request)
                self.assertEqual(request.method, "GET")
                self.assertEqual(request.headers["authorization"], "Bearer practice-test-token")
                path = request.url.path
                if path.endswith("/summary"):
                    return httpx.Response(200, json={"account": {"alias": "Demo", "currency": "CAD", "balance": "1000", "NAV": "1010", "marginAvailable": "800", "marginUsed": "210", "unrealizedPL": "10", "openTradeCount": 1, "openPositionCount": 1, "pendingOrderCount": 1}})
                if path.endswith("/openPositions"):
                    return httpx.Response(200, json={"positions": [{"instrument": "EUR_USD", "long": {"units": "100"}, "short": {"units": "0"}, "unrealizedPL": "4.5"}]})
                if path.endswith("/openTrades"):
                    return httpx.Response(200, json={"trades": [{"id": "42", "instrument": "EUR_USD", "currentUnits": "100", "price": "1.08", "unrealizedPL": "4.5"}]})
                if path.endswith("/pendingOrders"):
                    return httpx.Response(200, json={"orders": [{"id": "99", "instrument": "EUR_USD", "type": "LIMIT", "units": "50", "price": "1.07"}]})
                if path.endswith("/pricing"):
                    self.assertEqual(request.url.params["instruments"], "EUR_USD")
                    return httpx.Response(200, json={"prices": [{"instrument": "EUR_USD", "time": "2024-01-01T00:00:00Z", "bids": [{"price": "1.081"}], "asks": [{"price": "1.082"}], "closeoutBid": "1.0809", "closeoutAsk": "1.0821"}]})
                return httpx.Response(404, json={"errorMessage": "unexpected request"})

            settings = replace(
                Settings.from_environment(),
                oanda_practice_api_token="practice-test-token",
                oanda_practice_account_id="practice-account-id-must-not-leak",
            )
            service = OandaReadOnlyService(settings)
            original_client = service._client
            service._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
            await original_client.aclose()
            try:
                snapshot = await service.snapshot(environment=OandaEnvironment.PRACTICE, instrument="EURUSD")
            finally:
                await service.close()
            self.assertEqual(len(calls), 5)
            self.assertTrue(snapshot.read_only)
            self.assertEqual(snapshot.balance, 1000)
            self.assertEqual(snapshot.positions[0].instrument, "EUR_USD")
            self.assertEqual(snapshot.open_trades[0].id, "42")
            self.assertEqual(snapshot.pending_orders[0].id, "99")
            self.assertEqual(snapshot.quote.ask, 1.082)
            self.assertNotIn("practice-account-id-must-not-leak", snapshot.model_dump_json())

        asyncio.run(scenario())


class TestRuleBasedAssistant(unittest.TestCase):
    def test_follow_up_uses_recent_context_without_an_external_model(self) -> None:
        async def scenario() -> None:
            router = ModelRouter(Settings.from_environment())
            try:
                reply = await router.respond(
                    model_id="rule-based",
                    message="How does ATR affect the stop?",
                    history=[MemoryMessage("user", "Explain fair value gaps", datetime.now(timezone.utc))],
                    signal=None,
                )
            finally:
                await router.close()
            self.assertIn("Following your earlier question", reply)
            self.assertIn("ATR measures recent range", reply)

        asyncio.run(scenario())


class TestWebUiRoutes(unittest.TestCase):
    def test_dashboard_health_and_model_catalog_are_available_without_secrets(self) -> None:
        # No market endpoint is called here; this verifies the offline-safe dashboard shell.
        from webui.main import app

        with TestClient(app) as client:
            dashboard = client.get("/")
            self.assertEqual(dashboard.status_code, 200)
            self.assertIn("SMC Desk", dashboard.text)
            health = client.get("/api/health")
            self.assertEqual(health.status_code, 200)
            self.assertEqual(health.json()["memory"], "in-memory")
            self.assertTrue(health.json()["oanda"]["read_only"])
            self.assertFalse(health.json()["oanda"]["access_protected"])
            class FixtureMarketData:
                async def candles(self, **_kwargs):
                    return MarketDataResult(candles=bullish_break_fixture(), source="fixture")

            client.app.state.market_data = FixtureMarketData()
            overlays = client.get("/api/market/overlays?symbol=BTCUSDT&market=crypto&timeframe=1h&session=London&limit=100")
            self.assertEqual(overlays.status_code, 200)
            self.assertTrue(overlays.json()["markers"])
            self.assertTrue(overlays.json()["zones"])
            models = client.get("/api/models")
            self.assertEqual(models.status_code, 200)
            model_ids = {item["id"] for item in models.json()}
            self.assertTrue({"rule-based", "openai-gpt4", "openai-gpt5", "gemini", "claude-sonnet", "claude-opus", "deepseek", "qwen", "groq", "ollama"}.issubset(model_ids))
            first_reply = client.post(
                "/api/chat",
                json={"session_id": "test-web-session", "message": "What is an FVG?", "model_id": "rule-based"},
            )
            self.assertEqual(first_reply.status_code, 200)
            self.assertIn("fair value gap", first_reply.json()["reply"].lower())
            follow_up = client.post(
                "/api/chat",
                json={"session_id": "test-web-session", "message": "How does ATR affect risk?", "model_id": "rule-based"},
            )
            self.assertEqual(follow_up.status_code, 200)
            self.assertIn("Following your earlier question", follow_up.json()["reply"])
            # A broker route is not publicly readable even before account credentials are supplied.
            client.app.state.settings = replace(client.app.state.settings, dashboard_access_token="dashboard-test-token")
            protected = client.get("/api/oanda/accounts/practice")
            self.assertEqual(protected.status_code, 403)
            self.assertIn("access token", protected.json()["detail"].lower())
            authenticated_but_unconfigured = client.get(
                "/api/oanda/accounts/practice", headers={"X-SMC-Access-Token": "dashboard-test-token"}
            )
            self.assertEqual(authenticated_but_unconfigured.status_code, 503)
            self.assertIn("not configured", authenticated_but_unconfigured.json()["detail"].lower())


class TestConversationMemory(unittest.TestCase):
    def test_in_memory_recent_context_is_bounded_and_ordered(self) -> None:
        async def scenario() -> None:
            memory = InMemoryConversationMemory()
            for index in range(45):
                await memory.add("session", "user", f"message-{index}")
            items = await memory.recent("session", 50)
            self.assertEqual(len(items), 40)
            self.assertEqual(items[0].content, "message-5")
            self.assertEqual(items[-1].content, "message-44")

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
