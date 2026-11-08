from __future__ import annotations

from psycopg import Error
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool


class PostgresStore:
    """Explicit pool lifecycle; schema changes are an operator-only operation."""

    database_kind: str

    def __init__(self, dsn: str):
        if not dsn:
            raise ValueError(f"{self.database_kind} database URL is required")
        self.dsn = dsn
        self.pool = AsyncConnectionPool(
            dsn, min_size=1, max_size=8, open=False,
            kwargs={"row_factory": dict_row, "autocommit": True},
            check=AsyncConnectionPool.check_connection,
        )

    async def open(self) -> None:
        await self.pool.open(wait=True)

    async def close(self) -> None:
        await self.pool.close()

    async def health(self) -> bool:
        try:
            async with self.pool.connection() as connection:
                await connection.execute("SELECT 1 FROM checker_schema_migrations LIMIT 1")
            return True
        except (Error, OSError, TimeoutError):
            return False

    async def migrate(self) -> None:
        from .migrate import migrate_database

        await migrate_database(self.dsn, self.database_kind)
