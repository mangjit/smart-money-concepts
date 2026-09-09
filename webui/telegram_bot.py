"""Minimal Telegram webhook adapter sharing the WebUI's assistant service.

This adapter never places orders. Telegram users can request closed-candle research
signals and ask educational questions using the selected server-side LLM provider.
"""

from __future__ import annotations

from typing import Any

import httpx

from .assistant_service import TradingAssistantService
from .config import Settings
from .llm import LlmConfigurationError, LlmRequestError
from .market_data import MarketDataUnavailable
from .models import Market, SignalAction, SignalResponse


class TelegramService:
    def __init__(self, settings: Settings, assistant: TradingAssistantService):
        self._settings = settings
        self._assistant = assistant
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(settings.request_timeout_seconds))
        # Selection is deliberately process-local. Conversation text may be durable in Mongo,
        # but a provider choice is operational configuration rather than user data.
        self._model_by_chat: dict[int, str] = {}

    @property
    def configured(self) -> bool:
        return bool(self._settings.telegram_bot_token)

    def is_authorized(self, chat_id: int) -> bool:
        allowed = self._settings.telegram_allowed_chat_ids
        return not allowed or str(chat_id) in allowed

    async def close(self) -> None:
        await self._client.aclose()

    async def handle_update(self, update: dict[str, Any]) -> None:
        message = update.get("message")
        if not isinstance(message, dict) or not isinstance(message.get("text"), str):
            return
        chat = message.get("chat")
        if not isinstance(chat, dict) or not isinstance(chat.get("id"), int):
            return
        chat_id = chat["id"]
        if not self.is_authorized(chat_id):
            await self.send_message(chat_id, "This bot is not enabled for this chat.")
            return

        text = message["text"].strip()
        if text.startswith("/start") or text.startswith("/help"):
            await self.send_message(chat_id, self._help_text())
            return
        if text.startswith("/signal"):
            await self._signal_command(chat_id, text)
            return
        if text.startswith("/models"):
            statuses = self._assistant.model_statuses()
            choices = "\n".join(f"• {item.id}: {item.label} ({'ready' if item.configured else 'not configured'})" for item in statuses)
            await self.send_message(chat_id, f"Available educational models:\n{choices}\n\nUse /model <id> to select a ready model for this bot session.")
            return
        if text.startswith("/model"):
            await self._model_command(chat_id, text)
            return

        session_id = f"telegram:{chat_id}"
        try:
            response = await self._assistant.chat(
                session_id=session_id,
                message=text,
                model_id=self._model_by_chat.get(chat_id, "rule-based"),
                signal=None,
            )
            await self.send_message(chat_id, response.reply)
        except (LlmConfigurationError, LlmRequestError, MarketDataUnavailable, ValueError) as error:
            await self.send_message(chat_id, f"I could not process that request: {error}")

    async def _model_command(self, chat_id: int, text: str) -> None:
        parts = text.split(maxsplit=1)
        if len(parts) != 2:
            await self.send_message(chat_id, "Usage: /model rule-based\nUse /models to view server-configured options.")
            return
        model_id = parts[1].strip().lower()
        status_by_id = {item.id: item for item in self._assistant.model_statuses()}
        selected = status_by_id.get(model_id)
        if selected is None:
            await self.send_message(chat_id, "Unknown model. Use /models to view available options.")
            return
        if not selected.configured:
            await self.send_message(chat_id, f"{selected.label} is not configured on this server.")
            return
        self._model_by_chat[chat_id] = model_id
        await self.send_message(chat_id, f"Educational assistant model set to {selected.label} for this bot session. Signal levels remain deterministic.")

    async def _signal_command(self, chat_id: int, text: str) -> None:
        # /signal BTCUSDT 1h crypto  OR  /signal EURUSD 4h forex  OR  /signal BTCUSDT 1h futures
        parts = text.split()
        if len(parts) < 2:
            await self.send_message(chat_id, "Usage: /signal BTCUSDT 1h crypto\nForex: /signal EURUSD 4h forex\nFutures: /signal BTCUSDT 1h futures")
            return
        symbol = parts[1].upper().replace("/", "").replace("-", "")
        timeframe = parts[2].lower() if len(parts) >= 3 else "1h"
        market_value = parts[3].lower() if len(parts) >= 4 else ("forex" if len(symbol) == 6 and symbol.isalpha() else "crypto")
        if timeframe not in {"1m", "5m", "15m", "1h", "4h", "1d"}:
            await self.send_message(chat_id, "Timeframe must be one of 1m, 5m, 15m, 1h, 4h, or 1d.")
            return
        if market_value not in {"crypto", "forex", "futures"}:
            await self.send_message(chat_id, "Market must be crypto, futures, or forex.")
            return
        try:
            signal = await self._assistant.analyze(
                symbol=symbol,
                market=Market(market_value),
                timeframe=timeframe,
                limit=300,
            )
            await self.send_message(chat_id, self._format_signal(signal))
        except (MarketDataUnavailable, ValueError) as error:
            await self.send_message(chat_id, f"Signal unavailable: {error}")

    async def send_message(self, chat_id: int, text: str) -> None:
        if not self._settings.telegram_bot_token:
            return
        # Telegram caps messages at 4096 characters. Keep complete risk information first.
        if len(text) > 3900:
            text = text[:3890] + "…"
        try:
            response = await self._client.post(
                f"https://api.telegram.org/bot{self._settings.telegram_bot_token}/sendMessage",
                json={"chat_id": chat_id, "text": text, "disable_web_page_preview": True},
            )
            response.raise_for_status()
        except httpx.HTTPError:
            # Telegram delivery failure should not crash webhook processing or expose token details.
            return

    def _format_signal(self, signal: SignalResponse) -> str:
        levels = (
            f"Entry: {signal.entry_price}\nStop: {signal.stop_loss}\nTarget: {signal.take_profit}\nR:R: {signal.risk_reward}:1"
            if signal.action is not SignalAction.NO_SIGNAL
            else "No reference entry, stop, or target while the setup is blocked."
        )
        reasons = "\n".join(f"• {reason}" for reason in signal.reasoning[:6])
        exits = "\n".join(f"• {item}" for item in signal.exit_plan)
        return (
            f"{signal.action.value} — {signal.symbol} {signal.timeframe}\n"
            f"Rule confidence: {signal.confidence}%\n{levels}\n\n"
            f"WHY\n{reasons}\n\nEXIT PLAN\n{exits}\n\n"
            f"{signal.risk_notice}"
        )

    @staticmethod
    def _help_text() -> str:
        return (
            "SMC Desk is a paper-trading research assistant, not a broker.\n\n"
            "• /signal BTCUSDT 1h crypto\n"
            "• /signal EURUSD 4h forex\n"
            "• /signal BTCUSDT 1h futures\n"
            "• /models, then /model <id> to switch a configured educational LLM\n"
            "• Ask about ATR, FVGs, BOS/CHoCH, risk process, or the current no-signal gate.\n\n"
            "Signals use closed candles, 1.5×ATR stops, and a minimum 2R target when all deterministic rules align."
        )


async def set_webhook(settings: Settings) -> str:
    """Register the webhook from a trusted shell; never expose this as a public route."""
    if not settings.telegram_bot_token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is required")
    if not settings.public_webhook_base_url:
        raise RuntimeError("PUBLIC_WEBHOOK_BASE_URL is required")
    if not settings.telegram_webhook_secret:
        raise RuntimeError("TELEGRAM_WEBHOOK_SECRET is required")
    callback = settings.public_webhook_base_url.rstrip("/") + "/api/telegram/webhook"
    async with httpx.AsyncClient(timeout=httpx.Timeout(settings.request_timeout_seconds)) as client:
        response = await client.post(
            f"https://api.telegram.org/bot{settings.telegram_bot_token}/setWebhook",
            json={"url": callback, "secret_token": settings.telegram_webhook_secret, "allowed_updates": ["message"]},
        )
        response.raise_for_status()
        payload = response.json()
    if not payload.get("ok"):
        raise RuntimeError(f"Telegram rejected webhook: {payload}")
    return callback
