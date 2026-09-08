"""Environment-backed configuration for the dashboard and bot.

Secrets are intentionally read only on the server. Do not expose this configuration
or API keys to the browser, TradingView, Telegram users, or a client-side bundle.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from typing import FrozenSet

try:  # dotenv is optional at import time; required only for .env convenience.
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - exercised by minimal library installs
    load_dotenv = None


def _csv(name: str) -> FrozenSet[str]:
    return frozenset(value.strip() for value in os.getenv(name, "").split(",") if value.strip())


def _integer(name: str, default: int, minimum: int = 1) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        return default
    return max(minimum, value)


def _boolean(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    app_env: str
    cors_origins: FrozenSet[str]
    request_timeout_seconds: int
    memory_turns: int
    mongo_uri: str | None
    mongo_database: str
    mongo_collection: str
    telegram_bot_token: str | None
    telegram_webhook_secret: str | None
    telegram_allowed_chat_ids: FrozenSet[str]
    public_webhook_base_url: str | None
    openai_api_key: str | None
    openai_gpt5_model: str
    openai_gpt4_model: str
    gemini_api_key: str | None
    gemini_model: str
    anthropic_api_key: str | None
    anthropic_sonnet_model: str
    anthropic_opus_model: str
    deepseek_api_key: str | None
    deepseek_model: str
    dashscope_api_key: str | None
    qwen_model: str
    groq_api_key: str | None
    groq_model: str
    ollama_base_url: str
    ollama_model: str
    ollama_enabled: bool

    @classmethod
    def from_environment(cls) -> "Settings":
        if load_dotenv is not None:
            load_dotenv(override=False)

        def secret(name: str) -> str | None:
            return os.getenv(name) or None

        return cls(
            app_env=os.getenv("APP_ENV", "development"),
            cors_origins=_csv("CORS_ORIGINS"),
            request_timeout_seconds=_integer("REQUEST_TIMEOUT_SECONDS", 12),
            memory_turns=_integer("MEMORY_TURNS", 8, minimum=1),
            mongo_uri=secret("MONGODB_URI"),
            mongo_database=os.getenv("MONGODB_DATABASE", "smc_assistant"),
            mongo_collection=os.getenv("MONGODB_COLLECTION", "conversations"),
            telegram_bot_token=secret("TELEGRAM_BOT_TOKEN"),
            telegram_webhook_secret=secret("TELEGRAM_WEBHOOK_SECRET"),
            telegram_allowed_chat_ids=_csv("TELEGRAM_ALLOWED_CHAT_IDS"),
            public_webhook_base_url=secret("PUBLIC_WEBHOOK_BASE_URL"),
            openai_api_key=secret("OPENAI_API_KEY"),
            openai_gpt5_model=os.getenv("OPENAI_GPT5_MODEL", "gpt-5"),
            openai_gpt4_model=os.getenv("OPENAI_GPT4_MODEL", "gpt-4.1"),
            gemini_api_key=secret("GEMINI_API_KEY"),
            gemini_model=os.getenv("GEMINI_MODEL", "gemini-2.5-pro"),
            anthropic_api_key=secret("ANTHROPIC_API_KEY"),
            anthropic_sonnet_model=os.getenv("ANTHROPIC_SONNET_MODEL", "claude-sonnet-4-5"),
            anthropic_opus_model=os.getenv("ANTHROPIC_OPUS_MODEL", "claude-opus-4-5"),
            deepseek_api_key=secret("DEEPSEEK_API_KEY"),
            deepseek_model=os.getenv("DEEPSEEK_MODEL", "deepseek-chat"),
            dashscope_api_key=secret("DASHSCOPE_API_KEY"),
            qwen_model=os.getenv("QWEN_MODEL", "qwen-plus"),
            groq_api_key=secret("GROQ_API_KEY"),
            groq_model=os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile"),
            ollama_base_url=os.getenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434").rstrip("/"),
            ollama_model=os.getenv("OLLAMA_MODEL", "llama3.2"),
            ollama_enabled=_boolean("OLLAMA_ENABLED"),
        )
