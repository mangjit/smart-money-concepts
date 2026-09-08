# Deploy SMC Desk on Render

This guide deploys the single FastAPI web service in this repository. The dashboard, API, and Telegram webhook deliberately run as one service because the browser uses same-origin `/api` requests. There is no broker/exchange execution component.

The repository includes [`render.yaml`](../render.yaml), so Render can create the base service as a Blueprint. It declares a Python web service, installs `requirements-webui.txt`, starts Uvicorn on Render's injected `$PORT`, and checks `GET /api/health`.

> **Choose an always-on plan for Telegram.** A sleeping or cold-starting service is unsuitable for a time-sensitive webhook. The supplied Blueprint uses `starter`; change the plan only if you understand its sleep, request, and cost behavior.

## Before you start

1. Push this branch to GitHub and ensure `render.yaml` is present at repository root.
2. Create accounts/keys only for services you intend to use:
   - **MongoDB Atlas** is optional but recommended for durable recent-chat memory.
   - A Telegram bot token is required only for Telegram.
   - At least one LLM API key is optional; the built-in educational assistant works without one.
3. Do **not** put `.env`, bot tokens, API keys, or MongoDB URIs in Git. `.env` is ignored; `.env.example` is a blank template only.
4. Decide whether the Telegram bot is private. For private use, collect your numeric Telegram chat ID and use `TELEGRAM_ALLOWED_CHAT_IDS`.

## 1. Create the Render Web Service

1. Sign in to Render and select **New → Blueprint**.
2. Connect the GitHub repository and select the branch containing this work.
3. Render detects `render.yaml`. Review the service named `smc-desk`.
4. Keep `runtime: python`, the provided build command, and start command:

   ```text
   pip install --upgrade pip && pip install -r requirements-webui.txt
   uvicorn webui.main:app --host 0.0.0.0 --port $PORT
   ```

5. Select an appropriate region; the supplied `ohio` region is a reasonable low-latency starting point for an Ontario-based operator. Region availability and pricing are Render-account dependent.
6. Create the Blueprint and wait for the deploy to finish. The health check should call:

   ```text
   https://YOUR-SERVICE.onrender.com/api/health
   ```

Render supports repository-root `render.yaml` infrastructure-as-code, server-injected environment variables, and health-check paths for FastAPI services. See Render’s FastAPI deployment guidance for the corresponding production pattern [here](https://render.com/articles/fastapi-production-deployment-best-practices).

## 2. Set environment variables in Render

Open the service’s **Environment** page. The base Blueprint provides non-secret application values. Add the following as needed; values marked **secret** must be entered in Render and never committed.

| Variable | Required? | What to enter |
| --- | --- | --- |
| `APP_ENV` | Yes | `production` |
| `MONGODB_URI` | Recommended | **Secret.** Atlas connection string for a dedicated, least-privilege database user. Leave unset only if ephemeral memory is acceptable. |
| `MONGODB_DATABASE` | Optional | `smc_assistant` (default) |
| `MONGODB_COLLECTION` | Optional | `conversations` (default) |
| `OPENAI_API_KEY` | Optional | **Secret.** Enables GPT-5/GPT-4 selection. |
| `GEMINI_API_KEY` | Optional | **Secret.** Enables Gemini. |
| `ANTHROPIC_API_KEY` | Optional | **Secret.** Enables Claude Sonnet/Opus. |
| `DEEPSEEK_API_KEY` | Optional | **Secret.** Enables DeepSeek. |
| `DASHSCOPE_API_KEY` | Optional | **Secret.** Enables Qwen. |
| `GROQ_API_KEY` | Optional | **Secret.** Enables Groq. |
| `OLLAMA_ENABLED` | Optional | Keep `false` on this single service. Render cannot reach `127.0.0.1` on your laptop. See the Ollama note below. |

After saving, Render redeploys the service. Check `https://YOUR-SERVICE.onrender.com/api/models`: configured providers show `"configured": true`; the endpoint does not reveal values.

### MongoDB Atlas checklist

1. Create a dedicated database user restricted to the SMC database—do not reuse an Atlas owner credential.
2. Require TLS and use an Atlas connection string in `MONGODB_URI`.
3. Configure Atlas network access for the Render service according to your Atlas/Render networking plan. Avoid a broad allowlist where a more restricted private-network option is available.
4. The application creates a retrieval index and a 30-day TTL index. Review that retention period against your privacy obligations before enabling public chat.

## 3. Verify the application

Open the service URL and verify:

```bash
curl -fsS https://YOUR-SERVICE.onrender.com/api/health
curl -fsS https://YOUR-SERVICE.onrender.com/api/models
```

Expected baseline health response:

```json
{"status":"ok","memory":"in-memory","telegram_configured":false}
```

Use the dashboard to request a market. The application explicitly drops the newest provider candle and does not invent a signal if Binance or Yahoo Finance is unavailable. Public feed limits, blocked symbols, delayed data, and provider terms are operational concerns; monitor them before relying on the dashboard.

## 4. Enable Telegram safely

Do this **after** Render has assigned the service’s public HTTPS URL.

1. In BotFather, create the bot and copy its token.
2. Generate a webhook secret locally:

   ```bash
   openssl rand -hex 32
   ```

3. In Render → Environment, add these **secret** variables:

   ```text
   TELEGRAM_BOT_TOKEN=...
   TELEGRAM_WEBHOOK_SECRET=<output of openssl command>
   TELEGRAM_ALLOWED_CHAT_IDS=123456789       # strongly recommended for a private bot
   PUBLIC_WEBHOOK_BASE_URL=https://YOUR-SERVICE.onrender.com
   ```

   `TELEGRAM_ALLOWED_CHAT_IDS` can contain multiple comma-separated numeric IDs. An empty value allows any chat that reaches the bot, so do not leave it empty for a private assistant.

4. Let Render redeploy. Then open the **Shell** tab for the running service and register the webhook:

   ```bash
   python scripts/set_telegram_webhook.py
   ```

   The script registers `https://YOUR-SERVICE.onrender.com/api/telegram/webhook` and passes the configured secret to Telegram. It never exposes the token in the browser.

5. In Telegram, send `/help`, then try:

   ```text
   /signal BTCUSDT 1h crypto
   /signal EURUSD 4h forex
   /models
   /model rule-based
   ```

6. Check Render logs for the `POST /api/telegram/webhook` response. A `403` means the Telegram secret header did not match; re-register the webhook after correcting `TELEGRAM_WEBHOOK_SECRET`.

The app rejects unverified webhooks in production. The handler returns quickly and sends the bot reply as a background task; use logs/alerts to monitor delivery failures.

## 5. Ollama on Render vs. local Ollama

`OLLAMA_BASE_URL=http://127.0.0.1:11434` works only when the FastAPI app and Ollama daemon run on the **same** machine. A Render web service cannot reach your desktop’s loopback interface.

Use one of these approaches instead:

- Keep Ollama for local development and set `OLLAMA_ENABLED=false` in Render.
- Host an Ollama-compatible service in a trusted, network-reachable environment, then set `OLLAMA_BASE_URL` to its HTTPS/private URL and `OLLAMA_ENABLED=true`.
- Deploy a separate private inference service with suitable compute, storage, authentication, and network policy. Do not expose an unauthenticated Ollama endpoint to the public internet.

For a public production assistant, managed LLM APIs are usually operationally simpler. In every case, the model is explanatory only; entry, stop, target, and reward-to-risk remain deterministic code.

## 6. After deployment

- Add a custom domain in Render if needed; Render provides HTTPS for its managed service URL/custom domains.
- Set alerting for failed deploys and service errors. Review webhook and provider-error logs routinely.
- Add app-level authentication and rate limiting before making the chat endpoint a public multi-user product.
- Use a separate staging service/branch for model-prompt and risk-engine changes; do not change production rules without tests and documented versioning.
- Re-run the test suite before every deployment:

  ```bash
  python -m unittest tests/unit_tests.py tests/test_webui.py -v
  ```

## Troubleshooting

| Symptom | Likely cause / fix |
| --- | --- |
| Render says no port detected | Confirm the exact start command ends with `--port $PORT`, not a hard-coded port. |
| Health check fails | Open `/api/health` in the service URL and inspect deploy logs. Confirm `healthCheckPath: /api/health` remained in the Blueprint. |
| WebUI loads but chart errors | Public market provider is unavailable, rate-limited, blocked, or has a symbol/timeframe limitation. The service intentionally returns an error rather than stale/synthetic prices. |
| Every model reads “configure server key” | Add the corresponding secret in Render’s Environment page, save, and wait for redeploy. Check `/api/models`. |
| Telegram has no response | Verify public HTTPS URL, bot token, secret, `TELEGRAM_ALLOWED_CHAT_IDS`, then rerun `python scripts/set_telegram_webhook.py` in the Render shell. |
| Telegram webhook returns 403 | The secret entered in Telegram does not equal `TELEGRAM_WEBHOOK_SECRET`; correct it and re-register. |
| Conversations disappear after redeploy | Set a working `MONGODB_URI`; in-memory fallback is intentionally non-durable. |

## Financial-risk reminder

The deployed service is educational paper-trading research software, not investment advice or an automated execution system. Public data can be delayed or revised; ATR stops and 2R targets do not address position sizing, gaps, slippage, leverage, spreads, liquidation, or suitability. Validate independently and do not make real-money decisions solely from this application.
