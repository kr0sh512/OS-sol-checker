from __future__ import annotations

from psycopg import sql

# Column grants deliberately separate publication, browser acceptance and scheduler finalization.
WEB_GRANTS = (
    ("SELECT", "checker_state"), ("SELECT", "sessions"), ("SELECT", "session_last_submissions"),
    ("SELECT", "queue_jobs"), ("SELECT", "submissions"), ("SELECT", "result_cache"),
    ("INSERT", "sessions"), ("INSERT", "queue_jobs"), ("INSERT", "session_last_submissions"),
    ("INSERT (id, session_id, author_name, author_group, task_id, context, generation, policy_digest, "
     "sha256, byte_size, source, original_filename, created_at, started_at, finished_at, expires_at, "
     "status, result, proven_result, cache_hit, queue_job_id, state_reason)", "submissions"),
    ('UPDATE (name, "group", token_hash, last_seen_at, expires_at)', "sessions"),
    ("UPDATE (submission_id, updated_at)", "session_last_submissions"),
    ("DELETE", "session_last_submissions"),
)
WORKER_GRANTS = (
    ("SELECT", "checker_state"), ("UPDATE (policy, policy_digest)", "checker_state"),
    ("SELECT, UPDATE, DELETE", "queue_jobs"), ("SELECT, UPDATE, DELETE", "submissions"),
    ("SELECT, INSERT, DELETE", "result_cache"), ("SELECT, INSERT, UPDATE, DELETE", "worker_status"),
    ("SELECT, DELETE", "catalog_snapshots"), ("SELECT, DELETE", "session_last_submissions"),
    ("SELECT (id, expires_at)", "sessions"), ('UPDATE (token_hash, name, "group")', "sessions"),
    ("DELETE", "sessions"),
)
CATALOG_GRANTS = (
    ("SELECT", "checker_state"), ("UPDATE (catalog, generation, tests_digest)", "checker_state"),
    ("SELECT, INSERT", "catalog_snapshots"),
    ("SELECT (state)", "queue_jobs"),
    ("UPDATE (state, stage, source, result, finished_at, state_reason)", "queue_jobs"),
    ("SELECT (status)", "submissions"),
    ("UPDATE (status, result, proven_result, source, state_reason, finished_at, expires_at)", "submissions"),
    ("DELETE", "submissions"), ("DELETE", "result_cache"),
)
TRANSIENT_TABLES = (
    "checker_state", "catalog_snapshots", "sessions", "queue_jobs", "submissions",
    "session_last_submissions", "result_cache", "worker_status",
)
ARCHIVE_TABLES = ("blobs", "accepted_solutions", "success_receipts")


async def grant_roles(connection, kind: str, runtime_role: str | None, *,
                      web_role: str | None, worker_role: str | None, catalog_role: str | None) -> None:
    schema = (await (await connection.execute("SELECT current_schema() AS name")).fetchone())["name"]
    schema_id = sql.Identifier(schema)
    await connection.execute(sql.SQL("REVOKE CREATE ON SCHEMA {} FROM PUBLIC").format(schema_id))
    await connection.execute(sql.SQL("REVOKE ALL ON ALL TABLES IN SCHEMA {} FROM PUBLIC").format(schema_id))
    if kind == "archive" and any((web_role, worker_role, catalog_role)):
        raise ValueError("service-specific transient roles cannot target archive")
    if runtime_role and any((web_role, worker_role, catalog_role)):
        raise ValueError("choose either a broad transient role or distinct service roles")
    permissions = []
    if runtime_role:
        tables = TRANSIENT_TABLES if kind == "transient" else ARCHIVE_TABLES
        privilege = "SELECT, INSERT, UPDATE, DELETE" if kind == "transient" else "SELECT, INSERT"
        permissions.append((runtime_role, tuple((privilege, table) for table in tables)))
    permissions.extend((role, grants) for role, grants in (
        (web_role, WEB_GRANTS), (worker_role, WORKER_GRANTS), (catalog_role, CATALOG_GRANTS),
    ) if role)
    if len({role for role, _ in permissions}) != len(permissions):
        raise ValueError("web, worker and catalog roles must be distinct")
    # Explicitly revoke old column grants too: REVOKE ALL ON TABLE alone doesn't remove those.
    columns = await (await connection.execute(
        """SELECT table_name, array_agg(column_name ORDER BY ordinal_position) AS names
           FROM information_schema.columns WHERE table_schema = %s GROUP BY table_name""", (schema,),
    )).fetchall()
    for role_name, grants in permissions:
        exists = await (await connection.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role_name,))).fetchone()
        if not exists:
            raise ValueError(f"runtime role does not exist: {role_name}")
        role = sql.Identifier(role_name)
        await connection.execute(sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(schema_id, role))
        await connection.execute(sql.SQL("REVOKE ALL ON ALL TABLES IN SCHEMA {} FROM {}").format(schema_id, role))
        for table in columns:
            names = sql.SQL(", ").join(sql.Identifier(name) for name in table["names"])
            privileges = sql.SQL(", ").join(sql.SQL("{} ({})").format(sql.SQL(privilege), names)
                                             for privilege in ("SELECT", "INSERT", "UPDATE", "REFERENCES"))
            await connection.execute(sql.SQL("REVOKE {} ON {} FROM {}").format(
                privileges, sql.Identifier(schema, table["table_name"]), role,
            ))
        for privilege, table in (*grants, ("SELECT", "checker_schema_migrations")):
            await connection.execute(sql.SQL("GRANT {} ON {} TO {}").format(
                sql.SQL(privilege), sql.Identifier(schema, table), role,
            ))
