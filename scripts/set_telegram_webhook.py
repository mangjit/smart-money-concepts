"""Register the SMC Desk Telegram webhook from a trusted server shell.

Usage:
  cp .env.example .env  # configure TELEGRAM_* and PUBLIC_WEBHOOK_BASE_URL
  python scripts/set_telegram_webhook.py
"""

from __future__ import annotations

import asyncio

from webui.config import Settings
from webui.telegram_bot import set_webhook


async def main() -> None:
    callback = await set_webhook(Settings.from_environment())
    print(f"Telegram webhook registered: {callback}")


if __name__ == "__main__":
    asyncio.run(main())
