CREATE TABLE IF NOT EXISTS kol_search.discover_response_snapshots (
    cache_key text PRIMARY KEY,
    platform text NOT NULL,
    operation text NOT NULL,
    provider text NOT NULL,
    payload_json text NOT NULL,
    observed_at text NOT NULL,
    expires_at text NOT NULL,
    updated_at text NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_discover_snapshots_expiry
ON kol_search.discover_response_snapshots(expires_at);

CREATE TABLE IF NOT EXISTS kol_search.provider_usage_daily (
    provider text NOT NULL,
    usage_date text NOT NULL,
    request_count integer NOT NULL DEFAULT 0,
    updated_at text NOT NULL,
    PRIMARY KEY(provider, usage_date)
);

CREATE TABLE IF NOT EXISTS kol_search.provider_account_status (
    provider text PRIMARY KEY,
    credits_remaining double precision,
    credits_used double precision,
    upstream_total_requests integer,
    observed_at text NOT NULL,
    updated_at text NOT NULL
);
