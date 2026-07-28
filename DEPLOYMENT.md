# Split Frontend + Backend Deployment

Trend & KOL Discover uses two deployable services. The user-facing FastAPI/Jinja
frontend runs on Vercel. Platform acquisition, normalization, persistence,
scheduled tracking, and LLM reply generation run on the user-managed server.

## Deployment topology

```text
Browser
  -> Vercel frontend/BFF (app.py)
  -> authenticated HTTPS /api/discover/v1
  -> backend service (backend_app.py)
  -> platform connectors / PostgreSQL / future LLM providers
```

The browser never calls X, another platform, or an LLM provider directly. X
cookies, platform API keys, database credentials, and LLM keys belong only on
the backend. Neither service depends on Chrome, OpenCLI, a browser profile, or
a local worker process.

For local development, leaving `KOL_BACKEND_URL` empty keeps a combined-process
mode that calls the same connector and response models directly. This shortcut
is for loopback development and tests; split deployment uses authenticated HTTP.

## Backend server

Install the application and start the standalone API:

```bash
pip install -e ".[dev,twscrape]"
kol-search backend
```

Minimum backend environment:

```dotenv
KOL_BACKEND_HOST=0.0.0.0
KOL_BACKEND_PORT=8780
KOL_BACKEND_API_TOKEN=<random service token>
KOL_DATABASE_URL=postgresql://...
# Single-server least-privilege option for non-canonical cache/budget state:
KOL_PROVIDER_STATE_DB_PATH=/var/lib/signalscope/provider_state.db
KOL_X_PROVIDER_CHAIN=fxembed,getxapi
GET_X_API_KEY=...
GET_X_API_BASE_URL=https://api.getxapi.com
GET_X_API_DAILY_CALL_LIMIT=120
GET_X_API_MIN_CREDITS=0.05
GET_X_API_BALANCE_CACHE_SECONDS=300
GET_X_API_ESTIMATED_CREDITS_PER_CALL=0.001
KOL_X_KOL_DISCOVERY_CACHE_SECONDS=3600
KOL_X_TREND_CACHE_SECONDS=1800
KOL_X_CONTENT_CACHE_SECONDS=1800
KOL_X_TIMELINE_CACHE_SECONDS=1800
KOL_X_CONTENT_LOOKUP_CACHE_SECONDS=3600
KOL_X_SNAPSHOT_STALE_SECONDS=86400
KOL_DISCOVERY_DOMAINS_JSON='[{"key":"ai","label":"AI","query":"AI agents OR LLM OR machine learning"},{"key":"crypto","label":"Crypto","query":"crypto OR bitcoin OR ethereum OR DeFi"},{"key":"financial","label":"Financial","query":"financial markets OR investing OR macroeconomics"}]'
KOL_AUTO_WATCHLIST_ENABLED=true
KOL_AUTO_WATCHLIST_MIN_SCORE=75
KOL_AUTO_WATCHLIST_MIN_EVIDENCE=3
KOL_AUTO_WATCHLIST_QUALIFYING_RUNS=2
KOL_HOT_CONTENT_REFRESH_INTERVAL_MINUTES=30
KOL_HOT_CONTENT_WINDOW_HOURS=48
KOL_HOT_CONTENT_MIN_ENGAGEMENT=100
DEEP_SEEK_API_KEY=...
DEEP_SEEK_BASE_URL=https://api.deepseek.com
DEEP_SEEK_MODEL=deepseek-reasoner
KOL_LLM_REPLY_DAILY_LIMIT=50
KOL_LLM_REPLY_CACHE_SECONDS=600
```

Put the service behind TLS and a process supervisor. Restrict inbound traffic
to the Vercel frontend where practical. FxEmbed is attempted first for
supported public lookups and GetXAPI fills the search/discovery gaps; FxEmbed
is not an availability-guaranteed production source. The daily GetXAPI call
limit resets at UTC midnight. GetXAPI's account endpoint does not expose active
subscription-plan credits, so a zero wallet balance must not pre-block paid
reads. The backend marks the provider balance exhausted only after an actual
billable request returns HTTP 402; the daily call limit remains the
authoritative local spend guard. `GET_X_API_MIN_CREDITS` is retained only for
configuration compatibility.

Choose one provider-state deployment mode:

- One backend instance: set `KOL_PROVIDER_STATE_DB_PATH` to a persistent,
  backed-up server path. This stores only rebuildable snapshots and the spend
  ledger in SQLite; canonical product data remains in PostgreSQL.
- Multiple backend instances: leave that setting empty and apply
  `supabase/migrations/20260726223000_add_provider_budget_and_snapshots.sql`
  with the migration/admin role before restarting the backend.

The backend never creates PostgreSQL tables during a web request.

Apply these migrations before starting the new scheduler:

```text
20260726223000_add_provider_budget_and_snapshots.sql
20260727100000_add_domain_kol_memberships.sql
20260727101000_add_llm_reply_cache.sql
```

Run one bounded smoke refresh, then enable the supervised scheduler:

```bash
kol-search backend-scheduler --once
sudo cp config/systemd/signalscope-discover-worker.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now signalscope-discover-worker
```

The API and scheduler must use the same `KOL_DATABASE_URL` and provider-state
configuration. Only one scheduler instance should be active per deployment.

## Vercel frontend/BFF

The frontend needs only the backend endpoint and the matching service token:

```dotenv
KOL_BACKEND_URL=https://backend.example.com
KOL_BACKEND_API_TOKEN=<same random service token>
KOL_BACKEND_REQUEST_TIMEOUT_SECONDS=30
KOL_ADMIN_USERNAME=admin
KOL_ADMIN_PASSWORD=<strong unique password>
KOL_SESSION_SECRET=<random 32+ byte secret>
```

Do not configure `GET_X_API_KEY`, `TWSCRAPE_*`, X provider credentials, database credentials, or
LLM keys on Vercel. The service token is used by server-side BFF code and must
never be embedded in HTML or browser JavaScript.

Optional X product settings belong on the frontend because they select what the
UI asks the backend to retrieve:

```dotenv
KOL_X_HOT_CONTENT_QUERY=(AI OR crypto OR technology OR startups) min_faves:100 -filter:replies lang:en
KOL_X_HOT_CONTENT_LIMIT=12
KOL_X_WATCH_HANDLES=OpenAI,AnthropicAI
KOL_X_WATCH_POSTS_PER_ACCOUNT=3
KOL_X_KOL_DISCOVERY_DOMAIN=AI agents
KOL_X_KOL_DISCOVERY_LIMIT=8
KOL_X_KOL_CANDIDATE_SAMPLE_SIZE=20
# Five is recommended; autonomous enrollment always samples at least three.
KOL_X_KOL_RECENT_POSTS_PER_ACCOUNT=5
KOL_X_KOL_MIN_ENGAGEMENT=25
```

`KOL_X_WATCH_HANDLES` is only a bootstrap. Once X KOL enrollment is persisted,
the frontend also loads tracked handles from the X KOL repository.

## Database migration

PostgreSQL migrations live in `supabase/migrations/`. Before moving existing
SQLite data, run a read-only report:

```bash
.venv/bin/python scripts/migrate_sqlite_to_postgres.py \
  --database-url "$KOL_DATABASE_URL"
```

Apply only after reviewing the report by adding `--apply`. Local SQLite rebuilds
remain available through `kol-search db rebuild --backup`.

## Verification

```bash
pytest
curl http://127.0.0.1:8780/health
curl -H "Authorization: Bearer $KOL_BACKEND_API_TOKEN" \
  "http://127.0.0.1:8780/api/discover/v1/platforms/x/trends?limit=3"
curl -H "Authorization: Bearer $KOL_BACKEND_API_TOKEN" \
  "http://127.0.0.1:8780/api/discover/v1/platforms/x/providers/usage"
```

Before switching frontend traffic, verify from the deployed Vercel frontend:

- Trend Radar shows an X provider and native trend links.
- Hot Content shows canonical X links and current public metrics.
- AI, Crypto, and Financial boards refresh automatically and require repeated
  qualification before an account enters Watchlist.
- Watchlist shows configured or enrolled X accounts, or an explicit empty state.
- Generate Reply enters a loading state, returns an editable DeepSeek draft,
  and opens the native X reply composer with the draft prefilled.
- A repeated identical request is served from a backend snapshot and does not
  increment `requests_today`.
- Browser responses and logs contain no provider cookies or platform API keys.
