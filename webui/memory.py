"""Recent conversation memory with an optional MongoDB implementation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol

from .config import Settings


@dataclass(frozen=True)
class MemoryMessage:
    role: str
    content: str
    created_at: datetime


class ConversationMemory(Protocol):
    enabled: bool

    async def recent(self, session_id: str, limit: int) -> list[MemoryMessage]: ...

    async def add(self, session_id: str, role: str, content: str) -> None: ...

    async def close(self) -> None: ...


class InMemoryConversationMemory:
    """Process-local fallback. It is intentionally not durable and has bounded history."""

    enabled = False

    def __init__(self) -> None:
        self._messages: dict[str, list[MemoryMessage]] = {}

    async def recent(self, session_id: str, limit: int) -> list[MemoryMessage]:
        return self._messages.get(session_id, [])[-limit:]

    async def add(self, session_id: str, role: str, content: str) -> None:
        messages = self._messages.setdefault(session_id, [])
        messages.append(MemoryMessage(role=role, content=content, created_at=datetime.now(timezone.utc)))
        # Keep bounded server memory even when a browser creates many messages.
        del messages[:-40]

    async def close(self) -> None:
        return None


class MongoConversationMemory:
    """Durable, TTL-limited recent-chat memory.

    Mongo is only selected when MONGODB_URI is configured. Each record includes a
    session id, role, text, and UTC timestamp; no tokens or market-provider secrets
    are stored. Configure MongoDB access controls and a retention policy for your use.
    """

    enabled = True

    def __init__(self, settings: Settings) -> None:
        try:
            from motor.motor_asyncio import AsyncIOMotorClient
        except ImportError as error:  # pragma: no cover - depends on optional deployment dependency
            raise RuntimeError("MongoDB memory requires the motor package") from error
        if not settings.mongo_uri:
            raise ValueError("MONGODB_URI is required for MongoConversationMemory")
        self._client = AsyncIOMotorClient(settings.mongo_uri, serverSelectionTimeoutMS=3000)
        self._collection = self._client[settings.mongo_database][settings.mongo_collection]

    async def initialize(self) -> None:
        await self._collection.create_index([("session_id", 1), ("created_at", -1)])
        # 30-day retention prevents an unbounded conversational data store.
        await self._collection.create_index("created_at", expireAfterSeconds=30 * 24 * 60 * 60)

    async def recent(self, session_id: str, limit: int) -> list[MemoryMessage]:
        cursor = self._collection.find({"session_id": session_id}).sort("created_at", -1).limit(limit)
        records = [record async for record in cursor]
        records.reverse()
        return [
            MemoryMessage(
                role=str(record["role"]),
                content=str(record["content"]),
                created_at=record["created_at"],
            )
            for record in records
        ]

    async def add(self, session_id: str, role: str, content: str) -> None:
        await self._collection.insert_one(
            {
                "session_id": session_id,
                "role": role,
                "content": content,
                "created_at": datetime.now(timezone.utc),
            }
        )

    async def close(self) -> None:
        self._client.close()


async def build_memory(settings: Settings) -> ConversationMemory:
    if not settings.mongo_uri:
        return InMemoryConversationMemory()
    memory = MongoConversationMemory(settings)
    await memory.initialize()
    return memory
