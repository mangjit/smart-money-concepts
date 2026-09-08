"""Focused tests for deterministic WebUI signal invariants and local fallback memory."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import math
import unittest

from starlette.testclient import TestClient

from webui.analytics import build_signal
from webui.config import Settings
from webui.llm import ModelRouter
from webui.market_data import MarketDataService
from webui.memory import InMemoryConversationMemory, MemoryMessage
from webui.models import Candle, Market, SignalAction


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
