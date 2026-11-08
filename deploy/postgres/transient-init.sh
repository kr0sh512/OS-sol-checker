#!/bin/sh
set -eu
IFS= read -r web_password < /run/secrets/web_password
IFS= read -r worker_password < /run/secrets/worker_password
IFS= read -r catalog_password < /run/secrets/catalog_password
psql --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" --set ON_ERROR_STOP=1 \
  --set web_password="$web_password" --set worker_password="$worker_password" --set catalog_password="$catalog_password" <<'SQL'
REVOKE ALL ON DATABASE checker_transient FROM PUBLIC;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
CREATE ROLE checker_web LOGIN PASSWORD :'web_password';
CREATE ROLE checker_worker LOGIN PASSWORD :'worker_password';
CREATE ROLE checker_catalog LOGIN PASSWORD :'catalog_password';
GRANT CONNECT ON DATABASE checker_transient TO checker_web, checker_worker, checker_catalog;
SQL
