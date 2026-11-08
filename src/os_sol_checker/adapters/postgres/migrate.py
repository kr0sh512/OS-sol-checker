from __future__ import annotations

import argparse
import asyncio
import os
from hashlib import sha256
from importlib.resources import files
from pathlib import Path

from psycopg import AsyncConnection
from psycopg.rows import dict_row

from ...config import Settings
from .permissions import grant_roles

MIGRATION_LOCK = 0x4F53430004


def _scripts(kind: str) -> list[tuple[str, str]]:
    if kind not in {"transient", "archive"}:
        raise ValueError("unknown database kind")
    packaged = files("os_sol_checker.adapters.postgres").joinpath("sql", kind)
    # Hatch force-includes the canonical root migrations in wheels. A checkout uses that same source.
    directory = packaged if packaged.is_dir() else Path(__file__).resolve().parents[4] / "migrations" / kind
    scripts = sorted((entry.name, entry.read_text(encoding="utf-8")) for entry in directory.iterdir()
                     if entry.name.endswith(".sql"))
    if not scripts:
        raise RuntimeError(f"no packaged {kind} migrations found")
    return scripts


async def migrate_database(
    dsn: str, kind: str, runtime_role: str | None = None, *,
    web_role: str | None = None, worker_role: str | None = None, catalog_role: str | None = None,
) -> None:
    if not dsn:
        raise ValueError(f"explicit {kind} migration database URL is required")
    scripts = _scripts(kind)
    async with (
        await AsyncConnection.connect(dsn, autocommit=True, row_factory=dict_row) as connection,
        connection.transaction(),
    ):
        await connection.execute("SELECT pg_advisory_xact_lock(%s)", (MIGRATION_LOCK,))
        await connection.execute(
            """CREATE TABLE IF NOT EXISTS checker_schema_migrations (
                version text PRIMARY KEY, checksum text NOT NULL,
                applied_at timestamptz NOT NULL DEFAULT clock_timestamp())"""
        )
        applied = {row["version"]: row["checksum"] for row in await (await connection.execute(
            "SELECT version, checksum FROM checker_schema_migrations"
        )).fetchall()}
        known = {version for version, _ in scripts}
        if applied.keys() - known:
            raise RuntimeError("database contains migrations unknown to this application")
        for version, content in scripts:
            checksum = sha256(content.encode()).hexdigest()
            if version in applied:
                if applied[version] != checksum:
                    raise RuntimeError(f"applied migration checksum changed: {kind}/{version}")
                continue
            await connection.execute(content, prepare=False)
            await connection.execute(
                "INSERT INTO checker_schema_migrations(version, checksum) VALUES (%s, %s)", (version, checksum),
            )
        await grant_roles(connection, kind, runtime_role, web_role=web_role,
                          worker_role=worker_role, catalog_role=catalog_role)


async def _run(arguments) -> None:
    settings = Settings()
    targets = ("transient", "archive") if arguments.database == "both" else (arguments.database,)
    for kind in targets:
        dsn = os.environ.get(f"{kind.upper()}_MIGRATION_DATABASE_URL") or getattr(settings, f"{kind}_database_url")
        service_roles = {"web_role": arguments.web_role, "worker_role": arguments.worker_role,
                             "catalog_role": arguments.catalog_role} if kind == "transient" else {}
        await migrate_database(dsn, kind, getattr(arguments, f"{kind}_role"), **service_roles)


def main() -> None:
    parser = argparse.ArgumentParser(description="Apply versioned PostgreSQL migrations with owner DSNs.")
    parser.add_argument("--database", choices=("both", "transient", "archive"), default="both")
    parser.add_argument("--transient-role", help="explicit broad transient role, instead of service-specific grants")
    parser.add_argument("--archive-role", help="existing append-only archive runtime group role")
    parser.add_argument("--web-role", help="existing web LOGIN/group role for session and acceptance operations")
    parser.add_argument("--worker-role", help="existing scheduler role for lifecycle, policy and cleanup")
    parser.add_argument("--catalog-role", help="existing catalog role for publication and invalidation")
    asyncio.run(_run(parser.parse_args()))


if __name__ == "__main__":
    main()
