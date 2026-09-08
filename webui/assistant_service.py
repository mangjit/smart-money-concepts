"""Application service connecting data, deterministic signals, memory, and LLM explanation."""

from __future__ import annotations

from .analytics import build_signal
from .llm import ModelRouter
from .market_data import MarketDataService
from .memory import ConversationMemory
from .models import ChatResponse, Market, ModelStatus, SignalResponse


class TradingAssistantService:
    def __init__(
        self,
        *,
        data: MarketDataService,
        memory: ConversationMemory,
        models: ModelRouter,
        memory_turns: int,
    ) -> None:
        self._data = data
        self._memory = memory
        self._models = models
        self._memory_turns = memory_turns

    def model_statuses(self) -> list[ModelStatus]:
        """Expose labels/configuration flags, never provider credentials."""
        return self._models.statuses()

    async def analyze(self, *, symbol: str, market: Market, timeframe: str, limit: int) -> SignalResponse:
        result = await self._data.candles(symbol=symbol, market=market, timeframe=timeframe, limit=limit)
        return build_signal(
            result.candles,
            symbol=symbol,
            market=market,
            timeframe=timeframe,
            data_source=result.source,
        )

    async def chat(
        self,
        *,
        session_id: str,
        message: str,
        model_id: str,
        signal: SignalResponse | None,
    ) -> ChatResponse:
        history = await self._memory.recent(session_id, self._memory_turns * 2)
        await self._memory.add(session_id, "user", message)
        reply = await self._models.respond(model_id=model_id, message=message, history=history, signal=signal)
        await self._memory.add(session_id, "assistant", reply)
        return ChatResponse(
            session_id=session_id,
            model_id=model_id,
            reply=reply,
            memory_enabled=self._memory.enabled,
            signal=signal,
        )
