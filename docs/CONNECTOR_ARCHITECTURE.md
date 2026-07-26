# Discover Connector Architecture

## Stable frontend contract

The frontend consumes only the versioned `/api/discover/v1` contract. Every response uses platform-native IDs, normalized public metrics, provider provenance, observation time, attempted-provider metadata, and non-secret warnings. Browser code never receives a platform credential.

The standalone server entrypoint is `backend_app.py` (or `kol_search.backend.app:app`) and can be started with `kol-search backend`. The Vercel entrypoint remains `app.py`; the two applications do not need to share a process.

```text
Vercel frontend / BFF
        │ authenticated HTTPS
        ▼
Discover API (/api/discover/v1)
        │
        ▼
ConnectorRegistry ── x ── XConnector
        │                    ├── GetXAPI adapter
        │                    ├── FxEmbed adapter
        │                    ├── twscrape adapter
        │                    ├── official X adapter
        │                    └── TwitterAPI.io / vendor adapter
        ├── youtube ── future YouTubeConnector
        ├── reddit ─── future RedditConnector
        └── instagram ─ future InstagramConnector
```

The registry is the platform boundary. A connector owns platform semantics. Provider adapters translate external response shapes into the platform domain models. Replacing GetXAPI with the official X API therefore changes configuration or one adapter, not frontend routes or response models.

The frontend uses `DiscoverGateway` as its BFF boundary. When `KOL_BACKEND_URL` is configured, it calls the standalone backend with `KOL_BACKEND_API_TOKEN`. When the URL is empty on loopback, it calls the connector registry in-process but still consumes the same normalized response objects. Trend Radar reads native X trends, Hot Content reads public X search results, KOL Discover finds platform-local accounts for a domain, and Watchlist reads per-account timelines only for configured or explicitly enrolled X handles.

## Short-term internal X KOL discovery

`GET /api/discover/v1/platforms/x/accounts/search` combines direct account search when the selected provider supports it with authors found through high-engagement domain content. It hydrates each profile, samples recent public posts, and returns an explainable deterministic score. The current score version is `x-kol-internal-v1`: relevance 35%, authority 20%, public engagement 20%, consistency 15%, and freshness 10%. Sparse evidence caps the result, so a large follower count alone cannot produce a high-confidence KOL.

Every result preserves its platform-native account ID, provider, observation time, five component scores, evidence links, and sampled recent content. Score snapshots are persisted by domain and score version. Discovery only creates a `candidate`; it does not start ongoing tracking. `POST /api/discover/v1/platforms/x/accounts/{native_id}/tracking` is the explicit enrollment boundary used by Add to Watchlist.

The backend persists identical trend, content, account-discovery, timeline, and
content-lookup responses in PostgreSQL (or SQLite in local development). Cache
TTLs are configured independently; five minutes is the default for list
queries and one hour for a single-content lookup. Because snapshots are shared
and durable, a page refresh or backend restart does not repeat the provider
call. If all providers fail after expiry, a snapshot may be served stale for up
to `KOL_X_SNAPSHOT_STALE_SECONDS` and is labeled explicitly in response
warnings.

On a single managed backend, `KOL_PROVIDER_STATE_DB_PATH` may point to a durable
server-side SQLite file for these non-canonical snapshots and the provider
budget ledger. Canonical trends, accounts, content, and score history continue
to use `KOL_DATABASE_URL`. Multi-instance backends must leave this override
empty and apply the shared PostgreSQL migration.

## X provider chain

`KOL_X_PROVIDER_CHAIN` is an ordered, comma-separated list. The connector tries only providers explicitly listed there and only when the requested capability is declared. A provider error is recorded as a sanitized warning and the next adapter is tried.

Internal test default:

```dotenv
KOL_X_PROVIDER_CHAIN=fxembed,getxapi
GET_X_API_KEY=...
GET_X_API_BASE_URL=https://api.getxapi.com
GET_X_API_DAILY_CALL_LIMIT=120
GET_X_API_MIN_CREDITS=0.05
GET_X_API_BALANCE_CACHE_SECONDS=300
GET_X_API_ESTIMATED_CREDITS_PER_CALL=0.001
```

Future official transition:

```dotenv
KOL_X_PROVIDER_CHAIN=official,twitterapi_io
X_BEARER_TOKEN=...
TWITTERAPI_IO_API_KEY=...
```

FxEmbed is the credential-free first reader for internal use. GetXAPI is the
paid fallback when FxEmbed cannot serve a capability or returns an error. Both
remain replaceable adapters and neither should be represented as an
availability-guaranteed production source.

The Starter-safe default caps GetXAPI at 120 calls per UTC day and preserves
0.05 credits. Over 31 possible UTC date buckets this is at most 3,720 standard
calls, below a 4,500-call Starter allowance. Identical trend, content, KOL, and
timeline requests remain cached for 30–60 minutes before either provider is
contacted again.

Before every billable GetXAPI call, the adapter atomically reserves one unit in
the UTC daily ledger. The free account endpoint is cached and supplies the
credit monitor. Between account checks, the backend conservatively subtracts
`GET_X_API_ESTIMATED_CREDITS_PER_CALL` from the last observed balance so a burst
cannot overrun the configured reserve. `GET /api/discover/v1/platforms/x/providers/usage` exposes only
sanitized counts, balance fields, and status to the authenticated frontend; it
never returns the API key or account identity.

## Adding another platform

1. Implement the `PlatformConnector` protocol in `src/kol_search/backend/connectors/`.
2. Keep all provider-specific payload parsing in that platform's adapters.
3. Normalize results to `TrendItem`, `ContentItem`, or `AccountItem` while preserving the platform native ID and provider provenance.
4. Register the connector in `build_connector_registry` only when the platform is enabled.
5. Add contract, fallback, authentication, and response-normalization tests using simulated upstream responses.

Do not add platform-specific branches to frontend routes. If a new platform needs a genuinely different capability, extend the versioned API model explicitly instead of hiding the difference in an existing field.

## Service authentication

Loopback development may call the API without a token. Any non-loopback backend bind requires `KOL_BACKEND_API_TOKEN`; the frontend BFF sends it as a Bearer credential. The token, X cookies, provider keys, database URL, and future LLM keys stay on the backend and must not appear in browser bundles, logs, or API error details.
