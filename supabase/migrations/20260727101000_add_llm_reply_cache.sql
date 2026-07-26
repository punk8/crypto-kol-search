create table if not exists kol_search.llm_usage_daily (
    provider text not null,
    usage_date text not null,
    request_count integer not null default 0,
    updated_at text not null,
    primary key (provider, usage_date)
);

create table if not exists kol_search.llm_reply_drafts (
    cache_key text primary key,
    platform text not null,
    content_id text not null,
    model text not null,
    draft text not null,
    generated_at text not null,
    expires_at text not null,
    updated_at text not null
);

create index if not exists idx_llm_reply_drafts_expiry
    on kol_search.llm_reply_drafts(expires_at);
