#!/bin/sh
set -eu
IFS= read -r archive_password < /run/secrets/archive_worker_password
psql --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" --set ON_ERROR_STOP=1 \
  --set archive_password="$archive_password" <<'SQL'
REVOKE ALL ON DATABASE checker_archive FROM PUBLIC;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
CREATE ROLE checker_archive_runtime LOGIN PASSWORD :'archive_password';
GRANT CONNECT ON DATABASE checker_archive TO checker_archive_runtime;
SQL
