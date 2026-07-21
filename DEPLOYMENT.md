# Vercel + Supabase + Mac Worker

The production topology deliberately separates the short-lived web runtime from
browser automation:

- Vercel runs `app.py` as the authenticated FastAPI dashboard and API.
- Supabase PostgreSQL stores jobs, KOL data, audit events, and worker heartbeats.
- The Mac runs `kol-search worker`, APScheduler, OpenCLI, Chrome, and persistent
  browser profiles.
- Platform API keys, OpenCLI profiles, Postiz credentials, and browser state stay
  on the Mac. Vercel only receives database and dashboard authentication secrets.

There is no Vercel Cron in this deployment. The current Hobby plan only supports
daily schedules, while signal scans need shorter intervals; the persistent Mac
scheduler creates those jobs directly in PostgreSQL.

## Environment split

The production Mac keeps its password-bearing URL in the login Keychain. Its
`.env` only selects the Keychain item:

```dotenv
KOL_DATABASE_URL_KEYCHAIN_SERVICE=kol-search-database-url
KOL_RUNTIME_MODE=worker
KOL_WORKER_ID=mac-worker
```

Local development may still set `KOL_DATABASE_URL` directly instead. Keep all
existing `X_*`, `TWITTERAPI_IO_*`, `OPENCLI_*`, `POSTIZ_*`, and
`OPENAI_*` values only in that local file.

Set only these values in Vercel Production and Preview:

```dotenv
KOL_DATABASE_URL=postgresql://...
KOL_RUNTIME_MODE=web
KOL_WEB_READ_ONLY=false
KOL_ADMIN_USERNAME=admin
KOL_ADMIN_PASSWORD=<strong unique password>
KOL_SESSION_SECRET=<random 32+ byte secret>
```

The dashboard may create or approve durable jobs, but it never executes OpenCLI
or platform writes. The Mac worker claims and executes them.

## Database migration

The canonical schema is in `supabase/migrations/`. Validate the existing SQLite
database and an empty Supabase schema first:

```bash
.venv/bin/python scripts/migrate_sqlite_to_postgres.py \
  --database-url "$KOL_DATABASE_URL"
```

Re-run with `--apply` to copy the data. The script refuses a non-empty destination,
creates a timestamped SQLite recovery copy, verifies every table count, resets
identity sequences, and releases stale `running`/`executing` leases.

## Mac worker service

After the database URL is present in `.env`, install the checked-in launch agent:

```bash
mkdir -p output "$HOME/Library/LaunchAgents"
cp config/launchd/com.kol-search.worker.plist \
  "$HOME/Library/LaunchAgents/com.kol-search.worker.plist"
launchctl bootstrap "gui/$(id -u)" \
  "$HOME/Library/LaunchAgents/com.kol-search.worker.plist"
```

The service starts at login, restarts after failures, and writes logs under
`output/`. Its heartbeat appears on `/health` and the platform center.

To stop it safely:

```bash
launchctl bootout "gui/$(id -u)/com.kol-search.worker"
```
