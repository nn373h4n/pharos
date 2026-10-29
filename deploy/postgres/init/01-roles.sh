#!/bin/sh
# Runs once, when the PostgreSQL data directory is first created (docker-entrypoint-initdb.d).
# Creates the three login roles described in docs/DATA_MODEL.md#database-roles:
#   pharos_owner  owns the database and runs migrations
#   pharos_app    used by the API; not a superuser, no BYPASSRLS, owns nothing
#   pharos_worker used by the worker and the scheduler; same restrictions, and grants only on the
#                 ingestion tables (ADR-0012)
# The passwords are read by psql from its own environment (\getenv), so they are never part of a
# command line that `ps` could show, and psql quotes them itself (:'name').
set -eu

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<'SQL'
\getenv owner_password PHAROS_OWNER_PASSWORD
\getenv app_password PHAROS_APP_PASSWORD
\getenv worker_password PHAROS_WORKER_DB_PASSWORD
\getenv database POSTGRES_DB

CREATE ROLE pharos_owner LOGIN PASSWORD :'owner_password';
CREATE ROLE pharos_app LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE PASSWORD :'app_password';

ALTER DATABASE :"database" OWNER TO pharos_owner;
REVOKE ALL ON DATABASE :"database" FROM PUBLIC;
GRANT CONNECT ON DATABASE :"database" TO pharos_app;

-- The worker handles content from outside, so it has its own, much smaller role. Migrations grant it
-- column-level access to the ingestion tables only; it can read nothing about people or sign-in.
CREATE ROLE pharos_worker LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE PASSWORD :'worker_password';
GRANT CONNECT ON DATABASE :"database" TO pharos_worker;

-- Creating an extension needs a superuser, so it happens here and not in a migration.
CREATE EXTENSION IF NOT EXISTS vector;
SQL

# The superuser may only connect through the local socket, never over the network, so a
# compromised service on the data network cannot log in as it even with the password.
# pg_hba.conf is read top to bottom, and the first matching line wins.
hba="$PGDATA/pg_hba.conf"
{
  echo "# Added by Pharos: the superuser is not allowed over TCP."
  echo "host all $POSTGRES_USER all reject"
  cat "$hba"
} > "$hba.new"
chmod 600 "$hba.new"
mv "$hba.new" "$hba"
