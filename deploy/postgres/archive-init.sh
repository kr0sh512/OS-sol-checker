#!/bin/sh
set -eu
IFS= read -r archive_password < /run/secrets/archive_worker_password
IFS= read -r admin_password < /run/secrets/archive_admin_password
psql --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" --set ON_ERROR_STOP=1 \
  --set archive_password="$archive_password" --set admin_password="$admin_password" <<'SQL'
REVOKE ALL ON DATABASE checker_archive FROM PUBLIC;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
SELECT format('CREATE ROLE checker_archive_runtime LOGIN PASSWORD %L', :'archive_password')
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'checker_archive_runtime') \gexec
SELECT format('CREATE ROLE checker_admin LOGIN PASSWORD %L', :'admin_password')
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'checker_admin') \gexec
ALTER ROLE checker_archive_runtime LOGIN PASSWORD :'archive_password';
ALTER ROLE checker_admin LOGIN PASSWORD :'admin_password';
GRANT CONNECT ON DATABASE checker_archive TO checker_archive_runtime, checker_admin;
SQL
