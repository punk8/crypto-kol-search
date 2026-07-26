create table if not exists kol_search.x_kol_domain_memberships (
    domain_key text not null,
    domain_name text not null,
    domain_query text not null,
    account_id text not null references kol_search.x_accounts(id),
    status text not null default 'candidate',
    score double precision not null default 0,
    confidence text not null default 'Low',
    reasons_json text not null default '[]',
    consecutive_qualified integer not null default 0,
    consecutive_missed integer not null default 0,
    enrolled_at text,
    last_evaluated_at text not null,
    updated_at text not null,
    primary key (domain_key, account_id),
    constraint x_kol_domain_membership_status
        check (status in ('candidate', 'active', 'paused', 'rejected'))
);

create index if not exists idx_x_domain_memberships_status_score
    on kol_search.x_kol_domain_memberships(domain_key, status, score desc);
