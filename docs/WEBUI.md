# SMC Desk WebUI and Telegram assistant

## What is included

`webui/` is a dark, TradingView-inspired **research dashboard** served by FastAPI. It is intentionally not a TradingView clone and contains no brokerage/exchange trading or order-execution code. Its optional OANDA panel is server-side and strictly **read-only** for protected practice/live account visibility.

| Capability | Implementation |
| --- | --- |
| Chart workspace | Responsive candlestick chart using TradingView Lightweight Charts in a dark terminal-style UI. Browser calls only same-origin `/api/*` endpoints. |
| Chart/analysis candles | Server-side Twelve Data time-series feed for Crypto and Forex whenever `TWELVE_DATA_API_KEY` is set; Forex `EURUSD` becomes `EUR/USD`, Crypto `BTCUSDT` becomes `BTC/USDT`, and the newest in-progress bar is always discarded. Without a key, Crypto tries public Kraken, Coinbase, Bybit, then Binance—each source remains explicitly labeled. |
| Crypto futures candles | Public Bybit linear-contract candles for `BTCUSDT`, `ETHUSDT`, and other supported perpetual symbols (including an optional `.P` suffix). No order endpoint is added. |
| SMC chart overlays | Server-side FVG, confirmed swing high/low, BOS, CHoCH, order block, liquidity, previous higher-timeframe high/low, retracement state, and selected UTC-session annotations, plotted as bounded markers/levels plus a recent-zone map. |
| OANDA account context | Optional protected server-side **GET-only** practice/live summary, positions, open trades, pending orders, and selected-Forex bid/ask quote. Requires a separate `DASHBOARD_ACCESS_TOKEN`; OANDA API tokens never reach the browser. |
| Deterministic signals | `BUY`, `SELL`, or `NO_SIGNAL`, only after EMA(20/50), RSI(14), a fresh **confirmed** swing break, and volume gate align. |
| Risk invariant | For BUY/SELL the stop is 1.5× ATR(14) from the reference close and target is at least 2.0R. A missing gate returns `NO_SIGNAL` and no price levels. |
| Signal explanation | Every response includes 11 short factual reasoning sentences, confidence (capped at 82 to avoid false precision), risk notice, and exit/invalidation plan. |
| LLM switching | Built-in educational mode plus server-configured OpenAI, Gemini, Claude Sonnet/Opus, DeepSeek, Qwen, Groq, and Ollama adapters. LLMs explain snapshots; they cannot create or alter deterministic price/risk levels. |
| Conversation context | Process-local fallback by default; optional MongoDB recent-memory store with 30-day TTL. |
| Telegram | Verified webhook adapter with `/signal`, `/help`, and educational follow-ups. It never sends orders. |

## Quick start

```bash
# From repository root
python -m venv .venv
. .venv/bin/activate                 # Windows: .venv\Scripts\activate
python -m pip install -r requirements-webui.txt
cp .env.example .env                  # leave provider/bot values empty for local offline mode
uvicorn webui.main:app --host 0.0.0.0 --port 8000 --reload
```

Open `http://localhost:8000`. In an Arena preview or reverse-proxy deployment, use the exposed URL; the frontend uses relative `/api` paths and does not call `localhost`.

The built-in assistant works with no model provider configured. Market data still needs its configured provider to be reachable. If a provider is unavailable, the dashboard displays an error rather than fabricating candles or a signal.

For Render, use the repository Blueprint and follow the dedicated [`RENDER.md`](RENDER.md) runbook rather than copying the local development command verbatim. For Twelve Data setup and protected OANDA practice/live visibility, follow [`OANDA_TWELVE_DATA.md`](OANDA_TWELVE_DATA.md).

## Signal policy

A signal is a **paper-trading research classification** for the last completed candle:

1. The app fetches public candles and drops the newest provider bar to avoid reading an in-progress bar.
2. Pivots require five candles on the right side, so they are confirmed rather than retroactively actionable.
3. A fresh close above/below the latest confirmed swing is mandatory.
4. EMA20/EMA50 must align with direction; RSI must be inside the configured momentum band; volume must be at least 0.80× its 20-bar average when volume is available.
5. Only all-four alignment produces BUY/SELL. All other conditions deliberately produce `NO_SIGNAL`.
6. Entry is the last closed price reference—not a guarantee of a live fill. Stop and target are deterministic and are never submitted to OANDA or another broker.

A percentage “confidence” is a rule-alignment score, **not** an estimated probability of profit. It is intentionally capped and should never be used for position sizing.

## SMC chart overlays and market coverage

After the closed-candle chart loads, the dashboard calls `GET /api/market/overlays` using the same symbol, timeframe, and candle window. The overlay map displays these repository calculations:

- **FVG:** recent bullish/bearish fair-value-gap boundaries; zones are shown as time-bounded paired chart lines and all recent zones appear in the map.
- **Swing highs/lows:** `SH` / `SL` markers using a five-candle confirmation side. Endpoint swings that need future candles are intentionally hidden.
- **BOS and CHoCH:** broken structural levels and directional markers, calculated from the confirmed swing frame.
- **Order blocks and liquidity:** recent order-block boundaries plus `BSL` / `SSL` liquidity markings. These are descriptive historical calculations, not guaranteed live levels.
- **Previous high/low:** prior `1H`, `1D`, or `1W` levels selected from the chart timeframe.
- **Retracement:** current/deepest percentage and an `R` marker.
- **Session:** the selected UTC session or kill-zone high/low and a session-start marker.

The output is intentionally bounded (recent zones/levels and up to 32 markers) so it remains readable. All overlays are derived from bars already treated as closed by the market-data service; no overlay submits an order. Some SMC definitions require later bars for historical confirmation, so the dashboard labels them as research context rather than immediate signals.

### Symbols and fallback coverage

- **Forex:** enter six-letter pairs such as `EURUSD`, `GBPJPY`, `AUDCAD`, `USDCHF`, `NZDUSD`, or `EURGBP`. With Twelve Data configured, the server requests standard slash notation and supports its available Forex coverage. Without Twelve Data, Yahoo Finance remains the explicit-error fallback.
- **Crypto spot:** `BTCUSDT`, `ETHUSDT`, `SOLUSDT`, and other provider-supported quote pairs work through Twelve Data when configured. Without it, the server tries Kraken first, then Coinbase, Bybit, and Binance. A regional restriction such as Binance `451` therefore no longer ends the request before the other public sources are attempted.
- **Crypto futures:** choose **Crypto futures** and use public Bybit linear symbols such as `BTCUSDT` or `ETHUSDT`; `BTCUSDT.P` is normalized to the same contract symbol. Availability is provider/region dependent.
- **OANDA quote context:** OANDA uses a broader ISO-currency pair normalizer, but the account’s own instrument permissions, region, and OANDA response are authoritative. The panel remains read-only for both practice and live accounts.

No public provider fallback substitutes a different asset (for example, it will not silently turn `BTCUSDT` into `BTCUSD`) or makes up candle data. Configure `TWELVE_DATA_API_KEY` for the most consistent cross-market chart source.

## LLM configuration

Copy `.env.example` to `.env` and configure only providers you intend to use. Keys are read on the server and are never returned by `/api/models`; that endpoint exposes only an identifier, label, capability, and configured flag.

| UI model ID | Required environment variable | Model name variable |
| --- | --- | --- |
| `openai-gpt5`, `openai-gpt4` | `OPENAI_API_KEY` | `OPENAI_GPT5_MODEL`, `OPENAI_GPT4_MODEL` |
| `gemini` | `GEMINI_API_KEY` | `GEMINI_MODEL` |
| `claude-sonnet`, `claude-opus` | `ANTHROPIC_API_KEY` | `ANTHROPIC_SONNET_MODEL`, `ANTHROPIC_OPUS_MODEL` |
| `deepseek` | `DEEPSEEK_API_KEY` | `DEEPSEEK_MODEL` |
| `qwen` | `DASHSCOPE_API_KEY` | `QWEN_MODEL` |
| `groq` | `GROQ_API_KEY` | `GROQ_MODEL` |
| `ollama` | `OLLAMA_ENABLED=true` | `OLLAMA_BASE_URL`, `OLLAMA_MODEL` |

The system prompt requires education, uncertainty, and no modification of the signal engine’s levels. Treat any model output as untrusted explanatory text: models can still be wrong, incomplete, or unavailable.

## MongoDB conversation memory

Set `MONGODB_URI` and optionally database/collection names. The application creates:

* a `{session_id: 1, created_at: -1}` retrieval index;
* a 30-day TTL index on `created_at`.

Only recent role/content/timestamp records are stored. Do not send account numbers, credentials, keys, identity documents, or unnecessary personal information in chat. Use a dedicated MongoDB user with database-only permissions, encrypted connections, backups/retention suitable for your jurisdiction, and an access policy appropriate for Telegram users.

If `MONGODB_URI` is unset, recent messages are held only in the application process and are bounded to 40 messages per session.

## Telegram setup

1. Create a Telegram bot through BotFather and set `TELEGRAM_BOT_TOKEN`.
2. Deploy the API to a public HTTPS URL. Set `PUBLIC_WEBHOOK_BASE_URL` to that base URL, without a trailing `/`.
3. Generate a long random `TELEGRAM_WEBHOOK_SECRET` and optionally set `TELEGRAM_ALLOWED_CHAT_IDS` to a comma-separated allowlist of numeric chat IDs.
4. From a trusted shell—not a public web route—run:

   ```bash
   python scripts/set_telegram_webhook.py
   ```

5. In Telegram, use `/help` or `/signal BTCUSDT 1h crypto`. For forex, use `/signal EURUSD 4h forex`. Use `/models` then `/model <id>` to select any configured education model for the current bot process session.

The webhook checks Telegram’s `X-Telegram-Bot-Api-Secret-Token`. In any environment other than `APP_ENV=development`, startup configuration must provide that secret before the webhook will accept updates. The bot never invokes exchange or broker APIs and is deliberately limited to analysis and educational responses.

## Deployment security checklist

- Put the app behind HTTPS and a reverse proxy with request-size/rate limits.
- Keep `.env` out of Git; rotate provider, OANDA, and Telegram keys if exposed.
- Configure `DASHBOARD_ACCESS_TOKEN` separately from OANDA credentials. Never type/send OANDA tokens to the WebUI, Telegram, or chat; use a practice account before enabling a live read-only view.
- Set `CORS_ORIGINS` only for known, exact separately hosted frontend origins; same-origin mode needs no CORS.
- Set `TELEGRAM_ALLOWED_CHAT_IDS` for a private assistant. An empty allowlist means every chat that reaches the bot may interact with it.
- Add authentication before exposing chat or signals as a multi-user public service. This starter intentionally has no user account system.
- Monitor public data provider terms, outages, delayed feeds, and symbol mapping. No feed is guaranteed suitable for execution.
- Do not use an LLM response or confidence score as an automated order trigger.

## API summary

| Endpoint | Purpose |
| --- | --- |
| `GET /api/health` | Memory and Telegram configuration status (no secrets). |
| `GET /api/models` | Supported model names and whether each is configured. |
| `GET /api/market/candles` | Closed provider candles for the UI; Twelve Data is primary when configured, with labeled public-provider fallbacks. |
| `GET /api/market/overlays` | Bounded closed-candle SMC markers, price levels, zones, retracement/session summary; accepts `session`. |
| `POST /api/signals/analyze` | Deterministic research signal and explanation. |
| `POST /api/chat` | Context-aware educational assistant response, optionally with current snapshot. |
| `GET /api/oanda/accounts/{practice\|live}` | Protected read-only account/position/trade/order snapshot and optional Forex bid/ask context; requires `X-SMC-Access-Token`, never an OANDA token. |
| `POST /api/telegram/webhook` | Telegram webhook only; not a general public command API. |

## Important limitations

This is educational software, not investment advice. Public bars can be delayed, incomplete, revised, unavailable, or have symbol-specific volume semantics. A 1.5×ATR stop and 2R target do not control loss size without a separate, independently determined position size, and neither protects against gaps, liquidation, spread widening, slippage, exchange outages, or leverage. Test any methodology on out-of-sample data and paper trade before considering real capital.
