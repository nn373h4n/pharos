#!/bin/sh
# Runs once, when the PostgreSQL data directory is first created (docker-entrypoint-initdb.d).
# Creates the two login roles described in docs/DATA_MODEL.md#database-roles:
#   pharos_owner  owns the database and runs migrations
#   pharos_app    used by the API and worker; not a superuser, no BYPASSRLS, owns nothing
# The passwords are read by psql from its own environment (\getenv), so they are never part of a
# command line that `ps` could show, and psql quotes them itself (:'name').
set -eu

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<'SQL'
\getenv owner_password PHAROS_OWNER_PASSWORD
\getenv app_password PHAROS_APP_PASSWORD
\getenv database POSTGRES_DB

CREATE ROLE pharos_owner LOGIN PASSWORD :'owner_password';
CREATE ROLE pharos_app LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE PASSWORD :'app_password';

ALTER DATABASE :"database" OWNER TO pharos_owner;
REVOKE ALL ON DATABASE :"database" FROM PUBLIC;
GRANT CONNECT ON DATABASE :"database" TO pharos_app;

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
