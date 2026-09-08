"""FastAPI entrypoint for the Smart WebUI and Telegram webhook."""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Query, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .assistant_service import TradingAssistantService
from .config import Settings
from .llm import LlmConfigurationError, LlmRequestError, ModelRouter
from .market_data import MarketDataService, MarketDataUnavailable
from .memory import ConversationMemory, build_memory
from .models import CandleResponse, ChatRequest, ChatResponse, HealthResponse, Market, ModelStatus, SignalRequest, SignalResponse
from .telegram_bot import TelegramService

ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = Settings.from_environment()
    memory = await build_memory(settings)
    market_data = MarketDataService(settings)
    model_router = ModelRouter(settings)
    assistant = TradingAssistantService(
        data=market_data,
        memory=memory,
        models=model_router,
        memory_turns=settings.memory_turns,
    )
    app.state.settings = settings
    app.state.memory = memory
    app.state.market_data = market_data
    app.state.models = model_router
    app.state.assistant = assistant
    app.state.telegram = TelegramService(settings, assistant)
    try:
        yield
    finally:
        await app.state.telegram.close()
        await model_router.close()
        await market_data.close()
        await memory.close()


app = FastAPI(
    title="SMC Desk",
    version="0.1.0",
    description="Confirmation-first market research dashboard and Telegram education assistant. No broker execution.",
    lifespan=lifespan,
)

# The dashboard is served by this app and uses relative URLs. CORS is opt-in for an
# intentionally separate frontend deployment; credentials are never enabled.
_boot_settings = Settings.from_environment()
if _boot_settings.cors_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(_boot_settings.cors_origins),
        allow_credentials=False,
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type"],
    )

app.mount("/static", StaticFiles(directory=STATIC), name="static")


def _services(request: Request) -> tuple[Settings, ConversationMemory, MarketDataService, ModelRouter, TradingAssistantService, TelegramService]:
    return (
        request.app.state.settings,
        request.app.state.memory,
        request.app.state.market_data,
        request.app.state.models,
        request.app.state.assistant,
        request.app.state.telegram,
    )


@app.get("/", include_in_schema=False)
async def dashboard() -> FileResponse:
    return FileResponse(STATIC / "index.html")


@app.get("/api/health", response_model=HealthResponse)
async def health(request: Request) -> HealthResponse:
    settings, memory, *_unused, telegram = _services(request)
    return HealthResponse(
        status="ok",
        memory="mongo" if memory.enabled else "in-memory",
        telegram_configured=telegram.configured,
    )


@app.get("/api/models", response_model=list[ModelStatus])
async def models(request: Request) -> list[ModelStatus]:
    *_prefix, model_router, _assistant, _telegram = _services(request)
    return model_router.statuses()


@app.get("/api/market/candles", response_model=CandleResponse)
async def candles(
    request: Request,
    symbol: str = Query("BTCUSDT", min_length=2, max_length=24),
    market: Market = Query(Market.CRYPTO),
    timeframe: Literal["1m", "5m", "15m", "1h", "4h", "1d"] = Query("1h"),
    limit: int = Query(300, ge=80, le=1000),
) -> CandleResponse:
    _, _, market_data, *_rest = _services(request)
    normalized = symbol.strip().upper().replace("/", "").replace("-", "")
    try:
        result = await market_data.candles(symbol=normalized, market=market, timeframe=timeframe, limit=limit)
    except MarketDataUnavailable as error:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(error)) from error
    return CandleResponse(symbol=normalized, market=market, timeframe=timeframe, source=result.source, candles=result.candles)


@app.post("/api/signals/analyze", response_model=SignalResponse)
async def analyze(request: Request, payload: SignalRequest) -> SignalResponse:
    *_, assistant, _telegram = _services(request)
    try:
        return await assistant.analyze(
            symbol=payload.symbol,
            market=payload.market,
            timeframe=payload.timeframe,
            limit=payload.limit,
        )
    except MarketDataUnavailable as error:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(error)) from error


@app.post("/api/chat", response_model=ChatResponse)
async def chat(request: Request, payload: ChatRequest) -> ChatResponse:
    *_, assistant, _telegram = _services(request)
    signal: SignalResponse | None = None
    if payload.symbol:
        try:
            signal = await assistant.analyze(symbol=payload.symbol, market=payload.market, timeframe=payload.timeframe, limit=300)
        except (MarketDataUnavailable, ValueError):
            # Preserve access to general education/chat when public market data is down.
            signal = None
    try:
        return await assistant.chat(
            session_id=payload.session_id,
            message=payload.message,
            model_id=payload.model_id,
            signal=signal,
        )
    except LlmConfigurationError as error:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(error)) from error
    except LlmRequestError as error:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(error)) from error


@app.post("/api/telegram/webhook", status_code=status.HTTP_204_NO_CONTENT, include_in_schema=False)
async def telegram_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
) -> None:
    settings, *_prefix, telegram = _services(request)
    if not telegram.configured:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Telegram is not configured")
    # Require a verification secret outside development. A missing secret should never
    # silently create an unauthenticated production command surface.
    if settings.telegram_webhook_secret:
        if x_telegram_bot_api_secret_token != settings.telegram_webhook_secret:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid Telegram webhook secret")
    elif settings.app_env.lower() != "development":
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Telegram webhook secret is required outside development")

    update = await request.json()
    if not isinstance(update, dict):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Invalid Telegram update")
    background_tasks.add_task(telegram.handle_update, update)
