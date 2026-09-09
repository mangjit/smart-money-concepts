# Twelve Data and OANDA integration

## Scope and safety boundary

This implementation uses:

- **Twelve Data** as the primary chart/analysis candle source when `TWELVE_DATA_API_KEY` is configured.
- **OANDA v20 practice and live accounts** for protected **read-only** balances, NAV, margin, positions, open trades, pending orders, and optional Forex bid/ask context.

There are intentionally **no OANDA order-creation, order-change, trade-close, or position-close routes** in this repository. `webui/oanda.py` only issues `GET` requests. A live account connection does not grant the dashboard permission to trade it.

## 1. Configure Twelve Data

Create a Twelve Data API key, then set this server-side environment variable:

```text
TWELVE_DATA_API_KEY=...
```

Twelve Data is used for both Forex and Crypto chart/analysis requests. UI symbols are normalized as follows:

| UI input | Twelve Data request symbol |
| --- | --- |
| `EURUSD` | `EUR/USD` |
| `GBPJPY` | `GBP/JPY` |
| `BTCUSDT` | `BTC/USDT` |
| `ETHUSD` | `ETH/USD` |

| UI timeframe | Twelve Data interval |
| --- | --- |
| `1m` | `1min` |
| `5m` | `5min` |
| `15m` | `15min` |
| `1h` | `1h` |
| `4h` | `4h` |
| `1d` | `1day` |

The app calls Twelve Data's time-series endpoint with UTC timestamps, sorts returned candles, and intentionally removes the newest candle before calculating a signal. The 15-second default `MARKET_CACHE_SECONDS` cache reduces duplicate provider calls and API usage while retaining closed-candle-only behavior.

Twelve Data supports Forex and digital-currency symbols such as `EUR/USD` and `BTC/USD` with its time-series API; availability, exchange coverage, entitlements, rate limits, and real-time/delayed status depend on your plan. See the [Twelve Data Python client documentation](https://pypi.org/project/twelvedata/).

## 2. Configure OANDA practice and live accounts

Set only the account type(s) you want to view. These values are server secrets—place them in Render/Railway environment variables, never in the WebUI or Git.

```text
# Practice/demo account
OANDA_PRACTICE_API_TOKEN=...
OANDA_PRACTICE_ACCOUNT_ID=...

# Live account (read-only dashboard view)
OANDA_LIVE_API_TOKEN=...
OANDA_LIVE_ACCOUNT_ID=...

# Separate high-entropy dashboard gate, not an OANDA token.
DASHBOARD_ACCESS_TOKEN=...
```

Generate the dashboard gate independently:

```bash
openssl rand -hex 32
```

The browser asks for `DASHBOARD_ACCESS_TOKEN` only when the user presses **Load account**. It is held in page memory only, transmitted over HTTPS as `X-SMC-Access-Token`, and is not saved to localStorage/sessionStorage. Never type either OANDA API token into the browser.

For each selected environment, the server uses only these read-only OANDA v20 endpoints:

```text
GET /v3/accounts/{accountID}/summary
GET /v3/accounts/{accountID}/openPositions
GET /v3/accounts/{accountID}/openTrades
GET /v3/accounts/{accountID}/pendingOrders
GET /v3/accounts/{accountID}/pricing?instruments=EUR_USD  # Forex context only
```

OANDA provides account-summary and open-position endpoints under `/v3/accounts/{accountID}` in its [v20 account-endpoint reference](https://developer.oanda.com/rest-live-v20/account-ep/). Access only works with a valid token/account combination for the chosen practice or live environment.

## 3. Use the OANDA panel

1. Configure the variables and redeploy the service.
2. Open `GET /api/health`; `oanda.practice_configured` and/or `oanda.live_configured` should be `true`, while `oanda.access_protected` must also be `true`.
3. In the WebUI, choose **Practice** first.
4. Enter the **dashboard access token**—not the OANDA token—then click **Load account**.
5. Switch to **Live** only when you intentionally want a read-only view of its balances/positions. The panel marks it `LIVE · READ ONLY`.
6. When viewing a supported six-letter Forex pair such as `EURUSD`, `GBPJPY`, `AUDCAD`, or `USDCHF`, the same request also includes an OANDA bid/ask context quote. The server accepts a broad set of ISO currency codes, while OANDA/account-region availability remains authoritative.

The protected endpoint is:

```text
GET /api/oanda/accounts/practice
GET /api/oanda/accounts/live
```

It returns `403` if the dashboard access header is absent/wrong, `503` if that account type is not configured, and `502` if OANDA rejects or cannot complete the upstream request. It sets `Cache-Control: no-store` for account responses.

## Deployment checklist

### Render / Railway variables

Add these values in the service’s secret environment-variable panel:

```text
TWELVE_DATA_API_KEY=...
OANDA_PRACTICE_API_TOKEN=...
OANDA_PRACTICE_ACCOUNT_ID=...
OANDA_LIVE_API_TOKEN=...
OANDA_LIVE_ACCOUNT_ID=...
DASHBOARD_ACCESS_TOKEN=...
MARKET_CACHE_SECONDS=15
```

Start with **Twelve Data + OANDA practice only**. Verify the dashboard does not expose account information when a user lacks the separate dashboard token. Only then decide whether to add the live-account read-only variables.

### Protect the public dashboard

`DASHBOARD_ACCESS_TOKEN` is a single-user protective gate, not a substitute for production identity management. Before exposing the app to multiple users, add real authentication/authorization, HTTPS-only access, rate limiting, access logs, and a per-user permission model. Do not share the dashboard token in Telegram, browser screenshots, chat, or source control.

## Operational limitations

- Public/chart data and OANDA executable prices can differ due to source, quote convention, market session, plan entitlement, latency, and spread. The UI labels OANDA values as context; it does not use them to silently alter the deterministic analysis levels.
- This app does not establish whether an instrument is available to your regional OANDA account. Treat OANDA rejection as authoritative and do not infer trade availability from a chart symbol.
- The price panel is read-only and is not a guarantee of a fill.
- Account access and price data are sensitive operational information. Use a practice account first, monitor API access, rotate leaked tokens, and never use this application as the sole basis for a live decision.
