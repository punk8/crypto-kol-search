do $provision$
begin
    if not exists (select 1 from pg_roles where rolname = 'kol_app') then
        create role kol_app nologin;
    end if;
end
$provision$;

alter role kol_app set search_path = kol_search, public;
grant connect on database postgres to kol_app;
grant usage on schema kol_search to kol_app;
grant select, insert, update, delete on all tables in schema kol_search to kol_app;
grant usage, select, update on all sequences in schema kol_search to kol_app;

alter default privileges in schema kol_search
    grant select, insert, update, delete on tables to kol_app;
alter default privileges in schema kol_search
    grant usage, select, update on sequences to kol_app;
