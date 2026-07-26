# Repository Guidelines

## Product Mission

This repository builds a frontend-and-backend Trend Tracking + KOL Tracking service. Discovery is an input to tracking, not the final product.

Production uses a split deployment topology:

- The frontend is deployed to Vercel and owns the user-facing web experience, authentication/session handling, and calls to the backend API.
- The backend is deployed to the user-managed server described by the local `.deployment` file. It owns data acquisition, normalization, classification, scoring, tracking jobs, persistence, and LLM processing.
- Local development may run both layers in one FastAPI process, but code must preserve the API boundary so they can be deployed independently.

The two core product capabilities are:

1. **Trend Tracking**: continuously capture the newest trends, organize them by a defined category taxonomy, and preserve enough snapshots to explain when a trend appeared, accelerated, persisted, or cooled.
2. **KOL Tracking**:
   - discover and rank high-scoring KOLs for a user-defined domain;
   - track the newest public content published by selected KOLs.

Users must be able to inspect source evidence, freshness, category, score reasons, and tracking state without running a local companion process.

Keep platform-native meaning intact:

- A native trend comes from a platform API and retains that platform's rank and volume semantics.
- A web trend is inferred from indexed public web sources and must be labeled as a web trend.
- KOL scores remain platform-specific. Do not invent a cross-platform influence score.
- Every discovered trend or KOL must retain traceable source evidence.

## Core Tracking Contracts

### Trend Tracking

- A trend run accepts one or more categories, locales, languages, and time windows.
- Categories come from an explicit, configurable taxonomy. Do not silently invent or rename categories per request.
- Every observation records at least: stable trend key, display name, category, provider, source scope, source URL when available, observed rank or volume when available, `first_seen_at`, `last_seen_at`, `observed_at`, and retrieval time.
- Preserve successive observations instead of overwriting the only copy. Trend state and velocity must be reproducible from snapshots.
- Distinguish `new`, `rising`, `persistent`, and `cooling` using documented rules and comparable observations from the same source. Missing observations are not automatically proof that a trend ended.
- Deduplicate aliases conservatively. Store the original provider label and explain any canonical merge.
- Keep native and web trend semantics separate. Never compare or add incompatible ranks as if they shared one scale.

### KOL Domain Discovery and Scoring

- A KOL run starts from a defined domain containing queries, keywords, exclusions, languages, locales, and optional seed accounts.
- A KOL run selects exactly one platform before discovery. KOL candidates, rankings, scores, metrics, and watchlists are platform-account records; do not aggregate the same person across platforms.
- Candidates remain candidates until a platform HTTP API resolves a stable native account ID.
- Score within each platform using explicit evidence such as domain relevance, recent relevant output, engagement quality, audience/relationship evidence, consistency, and safety risk.
- Do not rank by follower count alone. Persist score components, reasons, evidence timestamps, and scoring-version metadata.
- A high-scoring KOL must satisfy both a configurable score threshold and minimum evidence requirements. Insufficient evidence lowers confidence or routes the account to review.

### KOL Content Tracking

- Track content only for explicitly enrolled KOLs and only from public, permitted sources.
- Enroll and track each platform account independently. A combined content feed may mix platforms, but it must preserve platform provenance and must not imply a merged KOL identity.
- Fetch incrementally with provider cursors, `since_id`, or time windows where available. Do not re-fetch full histories on every run.
- Deduplicate by platform-native content ID and make persistence idempotent.
- Store author native ID, content native ID, canonical URL, text/title, publication time, retrieval time, provider, metrics snapshot, and topic/category matches.
- Preserve publication time separately from retrieval time. A newly observed old post is not “newly published.”
- Advance a cursor only after the corresponding content has been stored successfully. Partial provider failures must not skip unseen content.

## Deployment Topology and Runtime Constraints

### Frontend on Vercel

- Use `app.py` as the Vercel FastAPI entrypoint while the current Jinja frontend remains in place.
- Keep frontend request handlers short-lived and stateless. Do not run platform ingestion, long polling, tracking schedulers, queues, or LLM jobs inside Vercel request handlers.
- The browser must call this service's frontend/BFF endpoints, and the frontend must call the backend over authenticated HTTPS. Templates and browser JavaScript must never call platform or LLM APIs with private credentials.
- Do not depend on Vercel process memory or filesystem for durable state. Filesystem use is limited to packaged read-only assets and disposable `/tmp` data.
- Treat Vercel function duration and payload limits as frontend/BFF constraints. Bound backend calls and return clear pending, partial, or failed states.

### Backend on the User-Managed Server

- Deploy data acquisition, normalization, category classification, scoring, KOL tracking, content tracking, and LLM processing to the user-managed server.
- The backend may run persistent workers, a scheduler, polling loops, or a durable queue when those components are explicitly implemented and supervised. Do not use ad hoc background threads launched by web requests.
- Keep jobs bounded, idempotent, observable, and resumable through durable job records and cursors. A process restart must not lose canonical state or silently skip content.
- Expose an authenticated, versioned HTTPS API to the frontend. Keep provider credentials, database credentials, and LLM credentials on the backend only.
- Use PostgreSQL through `KOL_DATABASE_URL` for durable production state. Do not use repository files or local SQLite as the canonical production store.
- A single backend instance may use a persistent SQLite file only for rebuildable provider snapshots, balance observations, and the atomic spend ledger. Multi-instance deployments must use the shared PostgreSQL provider-state migration.
- Bound every provider and LLM call with a timeout, retry policy, concurrency limit, and cost or token budget where applicable.
- Keep paid-provider budgets and balance observations in durable backend storage. Browser refreshes must reuse normalized backend snapshots rather than calling an upstream provider again.
- Provide lightweight health/readiness checks that do not run discovery or LLM generation as part of the probe.

### Local Deployment Configuration

- `.deployment` is a local-only, Git-ignored source of deployment target and connection metadata for the user-managed server.
- Never commit, quote, copy, or expose `.deployment` contents in source, documentation, tests, screenshots, command output, logs, or chat responses.
- Read `.deployment` only when a deployment or server inspection is explicitly requested. Its existence does not authorize a deployment.
- Keep application runtime secrets in environment variables or a server-side secret manager; do not make the application read `.deployment` as runtime configuration.

## Data Source Policy

Production discovery must use backend-accessible network APIs.

Allowed production source categories:

- Official platform APIs, such as the X API.
- Server-to-server data providers, such as GetXAPI, TwitterAPI.io, or another configured HTTP vendor.
- Explicit web-search APIs, such as OpenAI Web Search, Brave Search, Serper, or Tavily, subject to storage and redistribution terms.
- Other providers only when they expose a documented network API, support server-side credentials, and work without an interactive browser session.

Test-only source:

- `mock`, enabled explicitly with `KOL_ENABLE_MOCK_BACKEND=true`.

Forbidden production dependencies:

- OpenCLI.
- Chrome, Chromium, Playwright, Selenium, Browser Bridge, browser profiles, cookies, or interactive login sessions.
- `subprocess` calls to local data collectors.
- macOS Keychain, launchd, or machine-local credential stores.
- Stateful scraping clients that require private account/session files, unless the user explicitly approves an isolated experimental connector after legal, reliability, and account-risk review.

Do not reintroduce OpenCLI as a primary source or fallback. Moving the backend to a server does not grant permission to add OpenCLI, Chrome automation, browser profiles, or silent scraping fallbacks. Unofficial providers such as public Nitter instances may be used only as explicitly enabled, non-canonical experimental sources; they must never be the sole evidence for stable identity, complete content history, or production availability.

## Platform Availability Rules

Only advertise or enable a platform when the server backend has a tested reader that satisfies the relevant capability contracts.

- X may be enabled with `official`, `getxapi`, `twitterapi_io`, or another tested HTTP provider.
- Small Red Book / Xiaohongshu must remain disabled in production until a backend provider implements its trend, KOL identity, and incremental content-tracking contracts.
- A disabled platform should produce a clear unavailable/configuration state, not an empty successful discovery result.
- Platform manifests must declare only capabilities that the configured HTTP provider can actually execute.

Keep provider-specific behavior behind the existing platform and Twitter reader interfaces. Shared web routes and discovery services must not branch on vendor names when a capability check can express the same decision.

## Web Search Semantics

Web search is a valid default or fallback discovery source when integrated through an explicit server-to-server search API.

For web trend discovery:

- Label results as `web` rather than `native`.
- Store the query, provider, source URL, source title, publication or observation time, and retrieval time.
- Derive trend strength from transparent signals such as source count, domain diversity, recency, and repeated mentions.
- Do not present inferred volume as a platform post count or native platform rank.

For web KOL discovery:

- Treat people or accounts found in search results as candidates until identity is verified.
- Preserve the evidence URL and the text that supports the candidate's relevance.
- Do not fabricate follower counts, verification state, native IDs, or engagement metrics from missing snippets.
- Mark incomplete identities and lower their confidence instead of coercing a local hash into a native platform ID.

Web Search may recall a candidate or topic, but it cannot by itself verify native trend rank, account identity, current platform metrics, or a complete latest-content feed. Use platform APIs or licensed HTTP providers for those claims.

## Project Structure

Application code uses the `src/kol_search/` layout:

- `web.py`: frontend/BFF FastAPI routes and request-scoped orchestration. During the transition it may host backend routes locally, but new work must preserve a separable API boundary.
- `backend/`: backend API contracts, provider catalog, acquisition, normalization, data processing, and future LLM services consumed by the frontend. Keep acquisition credentials and provider payload handling here; do not let templates call platform APIs directly.
- `cli.py`: local development and one-shot maintenance commands; it is not the production worker supervisor.
- `settings.py`: environment-backed configuration.
- `automation/`: durable job records and shared orchestration primitives.
- `platforms/`: platform contracts and native models.
- `platform_modules/`: platform repositories, projections, and web routers.
- `twitter/`: X HTTP reader implementations.
- `templates/` and `static/`: Jinja UI and browser assets.
- `fixtures/`: deterministic offline samples.
- `tests/`: pytest coverage.
- `supabase/migrations/`: PostgreSQL schema migrations.

Runtime databases and exports belong in ignored `data/` and `output/` directories and must never be committed.

## Configuration Rules

Frontend secrets belong in Vercel environment variables. Backend secrets belong in the server environment or its secret manager. Neither belongs in source control.

Expected frontend settings include:

```dotenv
KOL_ADMIN_USERNAME=admin
KOL_ADMIN_PASSWORD=...
KOL_SESSION_SECRET=...
KOL_ENABLED_PLATFORMS=x
KOL_BACKEND_URL=https://backend.example.com
KOL_BACKEND_API_TOKEN=...
```

Expected backend settings include:

```dotenv
KOL_DATABASE_URL=postgresql://...
KOL_PROVIDER_STATE_DB_PATH=/var/lib/signalscope/provider_state.db
KOL_X_PROVIDER_CHAIN=fxembed,getxapi
GET_X_API_KEY=...
GET_X_API_BASE_URL=https://api.getxapi.com
GET_X_API_DAILY_CALL_LIMIT=120
GET_X_API_MIN_CREDITS=0.05
GET_X_API_BALANCE_CACHE_SECONDS=300
GET_X_API_ESTIMATED_CREDITS_PER_CALL=0.001
KOL_BACKEND_API_TOKEN=...
OPENAI_API_KEY=...
```

For the internal X version, GetXAPI is the primary HTTP provider and FxEmbed is the best-effort fallback. Keep `twscrape` isolated as an explicitly approved experiment and retain the ability to switch to official or licensed providers by configuration. Do not add provider, database, cookie, or LLM secrets to browser-exposed environment variables. Do not add `OPENCLI_*`, browser-profile, or Keychain settings to either production layer.

When changing defaults:

- Both production layers must be deployable without Chrome.
- Missing credentials must fail with a concise configuration message.
- Health checks must be network-safe and quick; they must not spawn commands, probe a local browser, run discovery, or invoke an LLM.
- Preview and Production may use different backend URLs, service tokens, provider keys, and databases, but both must obey the same provider restrictions.

## Database and Request Safety

- Use PostgreSQL/Supabase or server-managed PostgreSQL for production. Preserve connection-pool behavior appropriate for the backend deployment model.
- Keep canonical writes in the backend. The frontend must not connect directly to the production database from browser code.
- Keep schema migrations explicit and reviewable. Never run destructive migrations implicitly during a web request.
- Make all trend observations, KOL score snapshots, and KOL content writes idempotent.
- Protect frontend-to-backend and scheduler-triggered endpoints with service authentication and network controls where available.
- Bound each run by category count, KOL count, pages per provider, and wall-clock budget. Resume through durable cursors rather than an unbounded request.
- A failed request or worker restart must not leave a job permanently in `queued` or `running`.
- Preserve SSRF protections and the public-source-only policy when fetching evidence URLs.
- Never log API keys, password-bearing database URLs, cookies, authorization headers, or raw provider secrets.

## Build and Development Commands

Requires Python 3.12 or newer.

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env
kol-search web --reload
kol-search discover "DeFi researcher" --backend mock --limit 30
pytest
pytest --cov=kol_search
```

Tests must not require live credentials, Chrome, OpenCLI, or network access.

## Testing Requirements

Use pytest and name tests `test_<behavior>`.

- Use `tmp_path` for local database tests.
- Use mock HTTP transports or fixtures for provider behavior.
- Add regression coverage for provider timeouts, partial results, job completion, category classification, snapshot history, cursor advancement, deduplication, scoring, and web-route changes.
- Add a test proving that production configuration cannot select OpenCLI or a browser-backed fallback.
- Add a test proving that unsupported platforms are disabled clearly.
- Add tests proving that trend snapshots preserve `first_seen_at`/`last_seen_at`, category filters do not leak results, and incompatible source ranks are not merged.
- Add tests proving that KOL content tracking resumes incrementally, does not duplicate native content IDs, and does not advance cursors after failed persistence.
- For UI changes, verify desktop and mobile layouts and include screenshots in the PR.
- Run the full suite before deployment or PR handoff.

## Deployment Workflow

Do not deploy unless the user explicitly requests deployment.

### Frontend Deployment to Vercel

For normal frontend work, use Vercel Git integration:

- Feature branches and pull requests create Preview deployments.
- Validate the Preview URL before promotion.
- Promote the already-tested Preview artifact to Production rather than rebuilding when possible.

For custom CI:

1. Pin the Vercel CLI version.
2. Run `vercel pull --yes` for the target environment.
3. Run the complete test suite.
4. Run `vercel build`.
5. Deploy with `vercel deploy --prebuilt`.

Keep `VERCEL_TOKEN`, `VERCEL_ORG_ID`, and `VERCEL_PROJECT_ID` in CI secrets. Never commit `.vercel/project.json` credentials or tokens.

### Backend Deployment to the User-Managed Server

- Resolve the target only from the local `.deployment` file after the user explicitly requests a deployment. Never print or persist its contents elsewhere.
- Prefer a versioned container image or versioned release directory managed by a service supervisor. Keep the previous healthy release available for rollback.
- Run database migrations as an explicit, reviewed deployment step. Back up affected data before destructive or irreversible migrations.
- Configure TLS, frontend origin restrictions, service authentication, process supervision, log rotation, resource limits, and restart behavior.
- Start or restart only the exact backend services in scope. Never infer permission to modify unrelated services on the host.
- Verify readiness and a representative authenticated API request before switching frontend traffic or reporting success.
- Keep frontend and backend API contracts backward-compatible during rollout, or deploy them in an explicitly coordinated order.

Before declaring a deployment ready, verify:

- The Vercel frontend can reach the configured backend over authenticated HTTPS without exposing the service token to browser code.
- Frontend `/health` is request-scoped; backend health and readiness report the deployed service and dependency state without performing live discovery.
- The canonical database is PostgreSQL, not local SQLite.
- No enabled platform resolves to OpenCLI, Chrome, or a local executable.
- Backend workers and schedulers are supervised, bounded, and recover jobs after restart.
- Provider and LLM credentials exist only on the backend and are absent from browser bundles and logs.
- Trend Tracking captures the latest results for requested categories and persists reproducible snapshots.
- KOL domain discovery returns scored accounts with score reasons and stable native IDs.
- KOL content tracking stores newly published content once and resumes from a durable cursor.
- Failed provider requests terminate cleanly without stale queued/running jobs.
- Frontend Preview logs and backend service logs contain no missing credential, import, timeout, database, or API-contract errors.

## Coding Style

Use four-space indentation, type annotations, `snake_case` for functions and modules, and `PascalCase` for classes and Pydantic models. Keep functions small and explicit. Group imports as standard library, third party, then local. Match surrounding style; no formatter or linter is currently enforced.

## Commit and Pull Request Guidelines

Use short, imperative, sentence-case commit subjects. Keep each commit scoped to one change.

PRs must describe:

- User-visible behavior.
- Data providers added, removed, or disabled.
- Frontend and backend environment-variable changes.
- Deployment impact for Vercel and the user-managed server.
- Database or migration impact.
- Verification performed, including Preview results when applicable.

Never describe the split deployment as production-ready while the frontend depends on local-only state, while backend secrets can reach browser code, or while an enabled production path can reach OpenCLI, Chrome, or another unapproved local executable.
