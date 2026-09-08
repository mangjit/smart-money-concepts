"""Read-only OANDA v20 account and pricing adapter.

This module deliberately implements GET-only account/position/pricing calls. It has
no order creation, modification, trade-close, or account-mutation method. A separate
DASHBOARD_ACCESS_TOKEN protects responses because brokerage account information must
not be exposed by a public dashboard.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import httpx

from .config import Settings
from .models import OandaAccountSnapshot, OandaConfiguration, OandaEnvironment, OandaPendingOrder, OandaPosition, OandaQuote, OandaTrade


class OandaNotConfigured(RuntimeError):
    """Raised when credentials for the requested account environment are absent."""


class OandaUnavailable(RuntimeError):
    """Raised when OANDA rejects or cannot complete a read-only request."""


@dataclass(frozen=True)
class OandaCredentials:
    base_url: str
    token: str
    account_id: str


class OandaReadOnlyService:
    _PRACTICE_BASE_URL = "https://api-fxpractice.oanda.com"
    _LIVE_BASE_URL = "https://api-fxtrade.oanda.com"
    _FOREX_CURRENCIES = frozenset({"AUD", "CAD", "CHF", "CNH", "CZK", "DKK", "EUR", "GBP", "HKD", "HUF", "JPY", "MXN", "NOK", "NZD", "PLN", "SEK", "SGD", "THB", "TRY", "USD", "ZAR"})

    def __init__(self, settings: Settings):
        self._settings = settings
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(settings.request_timeout_seconds),
            headers={"User-Agent": "smc-desk/0.1 (read-only account dashboard)"},
        )

    async def close(self) -> None:
        await self._client.aclose()

    @property
    def configuration(self) -> OandaConfiguration:
        return OandaConfiguration(
            practice_configured=bool(self._settings.oanda_practice_api_token and self._settings.oanda_practice_account_id),
            live_configured=bool(self._settings.oanda_live_api_token and self._settings.oanda_live_account_id),
            access_protected=bool(self._settings.dashboard_access_token),
            read_only=True,
        )

    def credentials_for(self, environment: OandaEnvironment) -> OandaCredentials:
        if environment is OandaEnvironment.PRACTICE:
            token = self._settings.oanda_practice_api_token
            account_id = self._settings.oanda_practice_account_id
            base_url = self._PRACTICE_BASE_URL
        else:
            token = self._settings.oanda_live_api_token
            account_id = self._settings.oanda_live_account_id
            base_url = self._LIVE_BASE_URL
        if not token or not account_id:
            raise OandaNotConfigured(f"OANDA {environment.value} account is not configured on this server")
        return OandaCredentials(base_url=base_url, token=token, account_id=account_id)

    async def snapshot(self, *, environment: OandaEnvironment, instrument: str | None = None) -> OandaAccountSnapshot:
        credentials = self.credentials_for(environment)
        summary_task = self._get(credentials, f"/v3/accounts/{credentials.account_id}/summary")
        positions_task = self._get(credentials, f"/v3/accounts/{credentials.account_id}/openPositions")
        trades_task = self._get(credentials, f"/v3/accounts/{credentials.account_id}/openTrades")
        orders_task = self._get(credentials, f"/v3/accounts/{credentials.account_id}/pendingOrders")
        quote_task = self._price(credentials, instrument) if instrument else None
        if quote_task is None:
            summary, positions_response, trades_response, orders_response = await asyncio.gather(summary_task, positions_task, trades_task, orders_task)
            quote = None
        else:
            summary, positions_response, trades_response, orders_response, quote = await asyncio.gather(summary_task, positions_task, trades_task, orders_task, quote_task)

        account = summary.get("account")
        if not isinstance(account, dict):
            raise OandaUnavailable("OANDA returned an invalid account summary")
        positions = positions_response.get("positions", [])
        trades = trades_response.get("trades", [])
        orders = orders_response.get("orders", [])
        if not isinstance(positions, list) or not isinstance(trades, list) or not isinstance(orders, list):
            raise OandaUnavailable("OANDA returned an invalid account detail response")

        return OandaAccountSnapshot(
            environment=environment,
            currency=str(account.get("currency", "")),
            balance=self._number(account, "balance"),
            nav=self._number(account, "NAV"),
            margin_available=self._number(account, "marginAvailable"),
            margin_used=self._number(account, "marginUsed"),
            unrealized_pl=self._number(account, "unrealizedPL"),
            open_trade_count=int(account.get("openTradeCount", 0)),
            open_position_count=int(account.get("openPositionCount", 0)),
            pending_order_count=int(account.get("pendingOrderCount", 0)),
            positions=[
                OandaPosition(
                    instrument=str(position.get("instrument", "")),
                    long_units=self._nested_number(position, "long", "units"),
                    short_units=self._nested_number(position, "short", "units"),
                    unrealized_pl=self._number(position, "unrealizedPL"),
                )
                for position in positions
            ],
            open_trades=[
                OandaTrade(
                    id=str(trade.get("id", "")),
                    instrument=str(trade.get("instrument", "")),
                    current_units=self._number(trade, "currentUnits"),
                    price=self._number(trade, "price"),
                    unrealized_pl=self._number(trade, "unrealizedPL"),
                )
                for trade in trades
            ],
            pending_orders=[
                OandaPendingOrder(
                    id=str(order.get("id", "")),
                    instrument=str(order.get("instrument", "")),
                    order_type=str(order.get("type", "")),
                    units=self._number(order, "units"),
                    price=float(order["price"]) if order.get("price") is not None else None,
                )
                for order in orders
            ],
            quote=quote,
            read_only=True,
        )

    async def _price(self, credentials: OandaCredentials, instrument: str) -> OandaQuote | None:
        response = await self._get(
            credentials,
            f"/v3/accounts/{credentials.account_id}/pricing",
            params={"instruments": self.normalize_instrument(instrument), "includeHomeConversions": "false"},
        )
        prices = response.get("prices", [])
        if not prices:
            return None
        price = prices[0]
        if not isinstance(price, dict):
            raise OandaUnavailable("OANDA returned an invalid pricing response")
        bids = price.get("bids", [])
        asks = price.get("asks", [])
        if not bids or not asks:
            return None
        return OandaQuote(
            instrument=str(price.get("instrument", instrument)),
            timestamp=str(price.get("time", "")),
            bid=float(bids[0]["price"]),
            ask=float(asks[0]["price"]),
            closeout_bid=float(price["closeoutBid"]) if price.get("closeoutBid") is not None else None,
            closeout_ask=float(price["closeoutAsk"]) if price.get("closeoutAsk") is not None else None,
        )

    async def _get(self, credentials: OandaCredentials, path: str, params: dict[str, str] | None = None) -> dict[str, Any]:
        endpoint_name = path.rsplit("/", 1)[-1]
        try:
            response = await self._client.get(
                credentials.base_url + path,
                params=params,
                headers={"Authorization": f"Bearer {credentials.token}", "Accept-Datetime-Format": "RFC3339"},
            )
            response.raise_for_status()
            payload = response.json()
        except httpx.HTTPStatusError as error:
            # Do not reflect OANDA URLs: they contain the account ID.
            raise OandaUnavailable(f"OANDA {endpoint_name} request failed with HTTP {error.response.status_code}") from error
        except httpx.HTTPError as error:
            raise OandaUnavailable(f"OANDA {endpoint_name} request could not be completed") from error
        except ValueError as error:
            raise OandaUnavailable(f"OANDA {endpoint_name} response was not valid JSON") from error
        if not isinstance(payload, dict):
            raise OandaUnavailable("OANDA returned an invalid response")
        return payload

    @staticmethod
    def normalize_instrument(symbol: str) -> str:
        cleaned = symbol.upper().replace("/", "").replace("_", "").replace("-", "")
        if len(cleaned) != 6 or not cleaned.isalpha() or cleaned[:3] not in OandaReadOnlyService._FOREX_CURRENCIES or cleaned[3:] not in OandaReadOnlyService._FOREX_CURRENCIES:
            raise OandaUnavailable("OANDA pricing context is limited to a six-letter Forex pair, for example EURUSD")
        return f"{cleaned[:3]}_{cleaned[3:]}"

    @staticmethod
    def _number(record: dict[str, Any], key: str) -> float:
        try:
            return float(record.get(key, 0))
        except (TypeError, ValueError) as error:
            raise OandaUnavailable(f"OANDA returned a non-numeric {key}") from error

    @classmethod
    def _nested_number(cls, record: dict[str, Any], outer_key: str, inner_key: str) -> float:
        nested = record.get(outer_key, {})
        return cls._number(nested, inner_key) if isinstance(nested, dict) else 0.0
