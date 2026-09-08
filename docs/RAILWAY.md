# Railway deployment

## Why the initial Railpack build failed

Railpack did not recognize this repository as Python because the FastAPI entrypoint lived at `webui/main.py` and dependencies were named `requirements-webui.txt`. Railpack detects Python projects from a root-level `main.py`, `requirements.txt`, `pyproject.toml`, or similar standard marker.

The repository now provides all three Railway-specific pieces:

| File | Purpose |
| --- | --- |
| [`main.py`](../main.py) | Root ASGI shim exposing `webui.main:app`. |
| [`requirements.txt`](../requirements.txt) | Standard Railpack manifest; includes the existing `requirements-webui.txt`. |
| [`railway.toml`](../railway.toml) | Explicit Railpack builder, build/start command, health check, and restart policy. |

## Redeploy the fixed branch

1. In Railway, open the service that failed.
2. Open **Settings → Service Source** and confirm its branch is:

   ```text
   arena/01a08156-smart-money-concepts
   ```

3. Ensure Railway detects the root-level [`railway.toml`](../railway.toml). In the service’s Deploy/Build settings, use these values if a previously saved dashboard setting overrides config-as-code:

   ```text
   Build Command: pip install -r requirements.txt
   Start Command: python -m uvicorn main:app --host 0.0.0.0 --port $PORT
   Healthcheck Path: /api/health
   ```

4. Click **Deploy** / **Redeploy**. If Railway shows staged changes, choose **Deploy Changes**.
5. When the build finishes, use **Networking → Generate Domain**.
6. Verify:

   ```text
   https://YOUR-RAILWAY-DOMAIN/api/health
   ```

   Expected response:

   ```json
   {"status":"ok","memory":"in-memory","telegram_configured":false,"twelve_data_configured":false,"oanda":{"practice_configured":false,"live_configured":false,"access_protected":false,"read_only":true}}
   ```

Railway Config as Code supports a `railway.toml`/`railway.json` start command and health-check path, while Railpack recognizes Python projects from a root `main.py` or `requirements.txt`. See Railway’s [Config as Code reference](https://docs.railway.com/config-as-code/reference) and [Railpack Python detection](https://railpack.com/languages/python/).

## If the same error remains

The service is probably still building an old commit or an old source configuration.

1. Open the latest deployment details and confirm the commit begins with the latest commit shown on GitHub for the deployment branch.
2. In **Settings → Service Source**, reconnect the repository and select the branch again.
3. Check the service Root Directory is blank or `/`. Do **not** set it to `webui`, because Railway must see root `requirements.txt`, `main.py`, and `railway.toml`.
4. Trigger **Redeploy** after the latest GitHub push.
5. If custom dashboard commands exist and do not match the commands above, remove/replace them and deploy staged changes.

## Environment variables

At minimum add:

```text
APP_ENV=production
```

Optional variables are listed in [`.env.example`](../.env.example): MongoDB, Telegram, LLM-provider keys, Twelve Data, and protected OANDA account access. Add them in Railway’s Variables panel, never in the repository. For Twelve Data and OANDA, also add a separate `DASHBOARD_ACCESS_TOKEN`; it is not an OANDA API token and is required before account data can be displayed. See [`docs/OANDA_TWELVE_DATA.md`](OANDA_TWELVE_DATA.md) for the full read-only setup.

## Telegram note

After Railway generates the public domain, set:

```text
PUBLIC_WEBHOOK_BASE_URL=https://YOUR-RAILWAY-DOMAIN
TELEGRAM_BOT_TOKEN=...
TELEGRAM_WEBHOOK_SECRET=...
TELEGRAM_ALLOWED_CHAT_IDS=your_numeric_chat_id
```

Then, from a trusted local terminal with those values in a local ignored `.env`, run:

```bash
python -m pip install -r requirements.txt
python scripts/set_telegram_webhook.py
```

## Performance note

This project calls its configured market-data provider on demand. With `TWELVE_DATA_API_KEY`, Twelve Data is the primary chart/analysis feed and the closed-candle cache defaults to 15 seconds. If the Railway service itself is slow before it reaches the dashboard, inspect provider/deployment/runtime logs and the available compute plan; the build fix above addresses the current `Railpack could not determine how to build the app` error.
