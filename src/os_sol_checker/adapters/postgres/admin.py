from __future__ import annotations

from ...config import Settings
from .base import PostgresStore


class AdminArchiveStore(PostgresStore):
    """Read-only archive queries for the authenticated admin page."""

    database_kind = "archive"

    def __init__(self, settings: Settings):
        super().__init__(settings.admin_archive_database_url)

    async def correct_solutions(self) -> list[dict]:
        async with self.pool.connection() as connection:
            rows = await (await connection.execute(
                """SELECT a.task_id, a.sha256, a.byte_size, b.source,
                          min(a.first_accepted_at) AS first_accepted_at,
                          count(r.submission_id) AS successful_submissions
                   FROM accepted_solutions a
                   JOIN blobs b ON (b.sha256, b.byte_size) = (a.sha256, a.byte_size)
                   LEFT JOIN success_receipts r ON r.accepted_solution_id = a.id
                   GROUP BY a.task_id, a.sha256, a.byte_size, b.source
                   ORDER BY a.task_id, min(a.first_accepted_at), a.sha256, a.byte_size"""
            )).fetchall()
        return [
            {**row, "source": bytes(row["source"]), "successful_submissions": row["successful_submissions"]}
            for row in rows
        ]
