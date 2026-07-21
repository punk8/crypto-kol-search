#!/usr/bin/env python3
"""Copy KOL Growth OS data from SQLite into an empty PostgreSQL schema."""

from __future__ import annotations

import argparse
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import psycopg
from psycopg import sql


TABLES = (
    "automation_platform_connections",
    "automation_jobs",
    "automation_opportunities",
    "automation_actions",
    "automation_policy_decisions",
    "automation_channel_controls",
    "automation_audit_events",
    "automation_brand_config",
    "automation_account_quotas",
    "automation_conversations",
    "automation_worker_heartbeats",
    "x_accounts",
    "x_kols",
    "x_tweets",
    "x_tweet_metrics",
    "x_discovery_evidence",
    "x_trends",
    "x_trend_tweets",
    "xhs_users",
    "xhs_kols",
    "xhs_notes",
    "xhs_note_metrics",
    "xhs_comments",
    "xhs_discovery_evidence",
    "xhs_trends",
)

IDENTITY_TABLES = (
    "automation_platform_connections",
    "automation_jobs",
    "automation_opportunities",
    "automation_actions",
    "automation_policy_decisions",
    "automation_audit_events",
    "x_discovery_evidence",
    "xhs_discovery_evidence",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sqlite", type=Path, default=Path("data/kol_search.db"))
    parser.add_argument("--database-url", required=True, help="Supabase PostgreSQL URL")
    parser.add_argument("--schema", default="kol_search")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Perform the copy. Without this flag the command only validates and reports.",
    )
    return parser.parse_args()


def sqlite_tables(connection: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }


def postgres_columns(connection: psycopg.Connection, schema: str, table: str) -> list[str]:
    rows = connection.execute(
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema=%s AND table_name=%s
        ORDER BY ordinal_position
        """,
        (schema, table),
    ).fetchall()
    return [str(row[0]) for row in rows]


def source_columns(connection: sqlite3.Connection, table: str) -> list[str]:
    return [str(row[1]) for row in connection.execute(f'PRAGMA table_info("{table}")')]


def count_postgres(connection: psycopg.Connection, schema: str, table: str) -> int:
    statement = sql.SQL("SELECT COUNT(*) FROM {}.{}").format(
        sql.Identifier(schema), sql.Identifier(table)
    )
    return int(connection.execute(statement).fetchone()[0])


def copy_table(
    source: sqlite3.Connection,
    destination: psycopg.Connection,
    schema: str,
    table: str,
) -> int:
    target_columns = postgres_columns(destination, schema, table)
    columns = [name for name in source_columns(source, table) if name in target_columns]
    if not columns:
        raise RuntimeError(f"No shared columns found for {table}")
    selected = ", ".join(f'"{name}"' for name in columns)
    rows = source.execute(f'SELECT {selected} FROM "{table}"').fetchall()
    if not rows:
        return 0
    insert = sql.SQL("INSERT INTO {}.{} ({}) VALUES ({})").format(
        sql.Identifier(schema),
        sql.Identifier(table),
        sql.SQL(", ").join(map(sql.Identifier, columns)),
        sql.SQL(", ").join(sql.Placeholder() for _ in columns),
    )
    with destination.cursor() as cursor:
        cursor.executemany(insert, [tuple(row) for row in rows])
    return len(rows)


def reset_identity_sequences(
    connection: psycopg.Connection, schema: str, tables: tuple[str, ...]
) -> None:
    for table in tables:
        qualified = f"{schema}.{table}"
        sequence = connection.execute(
            "SELECT pg_get_serial_sequence(%s, 'id')", (qualified,)
        ).fetchone()[0]
        if not sequence:
            continue
        maximum = connection.execute(
            sql.SQL("SELECT MAX(id) FROM {}.{}").format(
                sql.Identifier(schema), sql.Identifier(table)
            )
        ).fetchone()[0]
        if maximum is None:
            connection.execute("SELECT setval(%s, 1, false)", (sequence,))
        else:
            connection.execute("SELECT setval(%s, %s, true)", (sequence, maximum))


def release_stale_leases(connection: psycopg.Connection, schema: str) -> None:
    jobs = sql.SQL(
        """
        UPDATE {}.automation_jobs
        SET status='queued', progress=0, phase='queued', locked_by=NULL,
            locked_at=NULL, started_at=NULL
        WHERE status='running'
        """
    ).format(sql.Identifier(schema))
    actions = sql.SQL(
        """
        UPDATE {}.automation_actions
        SET status='scheduled', locked_by=NULL, locked_at=NULL, started_at=NULL
        WHERE status='executing'
        """
    ).format(sql.Identifier(schema))
    heartbeats = sql.SQL(
        "UPDATE {}.automation_worker_heartbeats SET status='offline'"
    ).format(sql.Identifier(schema))
    connection.execute(jobs)
    connection.execute(actions)
    connection.execute(heartbeats)


def main() -> int:
    args = parse_args()
    source_path = args.sqlite.expanduser().resolve()
    if not source_path.is_file():
        raise SystemExit(f"SQLite database not found: {source_path}")

    source = sqlite3.connect(f"file:{source_path}?mode=ro", uri=True)
    source.row_factory = sqlite3.Row
    existing_source = sqlite_tables(source)

    with psycopg.connect(args.database_url) as destination:
        missing_target = [
            table
            for table in TABLES
            if not postgres_columns(destination, args.schema, table)
        ]
        if missing_target:
            raise SystemExit(
                "PostgreSQL migration is not installed; missing: "
                + ", ".join(missing_target)
            )

        counts = {
            table: int(source.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
            for table in TABLES
            if table in existing_source
        }
        total = sum(counts.values())
        print(f"Validated {len(counts)} source tables with {total} rows.")
        for table, count in counts.items():
            if count:
                print(f"  {table}: {count}")

        occupied = {
            table: count_postgres(destination, args.schema, table)
            for table in TABLES
            if count_postgres(destination, args.schema, table)
        }
        if occupied:
            details = ", ".join(f"{table}={count}" for table, count in occupied.items())
            raise SystemExit(f"Destination must be empty; found {details}")

        if not args.apply:
            print("Dry run complete. Re-run with --apply to migrate these rows.")
            return 0

        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup = source_path.with_name(f"{source_path.stem}.pre-postgres-{timestamp}.db")
        source.close()
        shutil.copy2(source_path, backup)
        source = sqlite3.connect(f"file:{source_path}?mode=ro", uri=True)
        source.row_factory = sqlite3.Row
        print(f"Created recovery copy: {backup}")

        try:
            copied = {}
            for table in TABLES:
                if table in existing_source:
                    copied[table] = copy_table(
                        source, destination, args.schema, table
                    )
            release_stale_leases(destination, args.schema)
            reset_identity_sequences(destination, args.schema, IDENTITY_TABLES)
            for table, expected in copied.items():
                actual = count_postgres(destination, args.schema, table)
                if actual != expected:
                    raise RuntimeError(
                        f"Row-count mismatch for {table}: expected {expected}, got {actual}"
                    )
            destination.commit()
        except Exception:
            destination.rollback()
            raise

    source.close()
    print(f"Migration complete: {sum(copied.values())} rows copied and verified.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
