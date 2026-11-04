#!/usr/bin/env bash
# Restore of a Pharos database dump made by scripts/backup.sh (ADR-0022).
#
#   scripts/restore.sh DUMPFILE
#
# Use it on a NEW installation, before anything else is started:
#   1. Put deploy/.env back (the same root secret, or the audit chain and the tokens cannot be read).
#   2. docker compose ... up -d postgres        (a new volume: deploy/postgres/init/01-roles.sh runs
#                                                once and creates the roles and the vector extension)
#   3. scripts/restore.sh DUMPFILE
#   4. docker compose ... up -d                 (the migration step finds the schema at head)
#
# A DUMP IS CODE. pg_restore runs what the dump says as the database owner, and the owner owns every
# table, so a dump that someone changed can switch row-level security off, add a policy or a grant,
# or add a function or an administrator. Restore only a dump whose origin you trust.
#
# The SHA-256 file beside the dump (made by backup.sh, DUMPFILE.sha256) must hold exactly one line,
# `<hash>  <the dump's own file name>`. The dump is copied once into a private directory and the
# hash is taken from that copy; the checks and the restore all read the copy, so the file cannot be
# swapped between them. The hash catches damage; it catches a changed dump only if whoever changed
# the dump could not change the file beside it, so keep the checksums, or the dumps encrypted with
# authenticated encryption, where the keepers of the dumps cannot rewrite them. There is no way to
# skip the check: a dump you made by hand needs its checksum file made by hand (sha256sum).
#
# It refuses to touch a database that already has tables, one whose roles are missing (a dump holds
# the GRANTs to pharos_app and pharos_worker but not the roles) and one whose roles are not plain
# login roles without memberships. The extension entry is left out of the restore (the init script
# made it as a superuser; the owner cannot make it again). After the restore it looks for two things
# a good dump never makes: settings on the database or on a role, and schemas other than public.
# That is all it looks for: it does NOT compare policies, grants or functions with what Pharos ships.
#
# PHAROS_COMPOSE names the Compose command (default: this repository's file and deploy/.env).
set -euo pipefail
CALLER=$PWD
cd "$(dirname "$0")/.."

umask 077
read -r -a COMPOSE <<<"${PHAROS_COMPOSE:-docker compose -f deploy/docker-compose.yml --env-file deploy/.env}"
compose() { "${COMPOSE[@]}" "$@"; }
die() { echo "restore: $*" >&2; exit 1; }
# As the owner role through the container's local socket; no password on any command line.
psql_owner() { compose exec -T postgres psql --username=pharos_owner --dbname=pharos --no-psqlrc --tuples-only --no-align "$@"; }

[ $# -eq 1 ] || { echo "usage: $0 DUMPFILE" >&2; exit 2; }
dump=$1
case $dump in -*) die "a path may not start with '-': $dump" ;; esac
case $dump in /*) ;; *) dump="$CALLER/$dump" ;; esac
[ -f "$dump" ] && [ -r "$dump" ] || die "$dump is not a readable file"
[ -f "$dump.sha256" ] && [ -r "$dump.sha256" ] ||
  die "$dump has no SHA-256 file ($dump.sha256); restore only a dump whose origin you trust"

# One private copy of the dump and of its checksum file; everything below reads these. It goes to
# PHAROS_RESTORE_TMP, else TMPDIR, else /tmp: on many hosts /tmp is memory (tmpfs), so for a large
# dump name a directory on a real disk. The free space is checked first.
base=${PHAROS_RESTORE_TMP:-${TMPDIR:-/tmp}}
[ -d "$base" ] || die "$base (PHAROS_RESTORE_TMP or TMPDIR) is not a directory"
need=$(stat -c %s -- "$dump")
free=$(df -P -B1 -- "$base" | awk 'NR == 2 { print $4 }')
[[ $free =~ ^[0-9]+$ ]] || die "cannot tell how much space $base has"
[ "$free" -ge $((need + need / 100 + 1048576)) ] ||
  die "$base has $free bytes free and the dump needs $need for its private copy; set PHAROS_RESTORE_TMP to a directory with room"
work=$(mktemp -d "$base/pharos-restore.XXXXXXXX")
list_made=""
# The copy is always removed; the list file in the container only if one was made (a refusal before
# that never touches Docker).
trap 'rm -r -- "$work"; if [ -n "$list_made" ]; then compose exec -T postgres rm -f /tmp/pharos-restore.list >/dev/null 2>&1 || true; fi' EXIT
cp -- "$dump" "$work/dump"
cp -- "$dump.sha256" "$work/sums"

name=$(basename -- "$dump")
mapfile -t lines <"$work/sums"
[ "${#lines[@]}" -eq 1 ] || die "$dump.sha256 must hold exactly one line"
[[ ${lines[0]} =~ ^([0-9a-f]{64})\ \ (.+)$ ]] || die "$dump.sha256 is not in the form '<hash>  <file name>'"
want=${BASH_REMATCH[1]}
named=${BASH_REMATCH[2]}
[ "$named" = "$name" ] || die "$dump.sha256 is the checksum of a different file ($(printf '%q' "$named")), not of $(printf '%q' "$name")"
actual=$(sha256sum <"$work/dump")
actual=${actual%% *}
[ "$actual" = "$want" ] || die "$dump does not match its SHA-256 file; it is damaged or was changed. Do not restore it"

listing=$(compose exec -T postgres pg_restore --list <"$work/dump") || die "$dump is not a dump pg_restore understands"
grep -q ' alembic_version ' <<<"$listing" || die "$dump has no alembic_version table; it is not a Pharos dump"

roles=$(psql_owner --command "SELECT count(*) FROM pg_catalog.pg_roles WHERE rolname IN ('pharos_app', 'pharos_worker')") ||
  die "cannot ask the database for its roles (is postgres up?)"
[ "$roles" = 2 ] || die "the roles pharos_app and pharos_worker do not both exist; start postgres on a NEW volume so that 01-roles.sh creates them"
plain=$(psql_owner --command "SELECT count(*) FROM pg_catalog.pg_roles WHERE rolname IN ('pharos_app', 'pharos_worker') AND NOT rolsuper AND NOT rolbypassrls AND NOT rolcreaterole AND NOT rolcreatedb AND NOT rolreplication") ||
  die "cannot ask the database for the attributes of its roles"
[ "$plain" = 2 ] || die "pharos_app and pharos_worker must not be superuser, BYPASSRLS, CREATEROLE, CREATEDB or REPLICATION"
members=$(psql_owner --command "SELECT count(*) FROM pg_catalog.pg_auth_members m JOIN pg_catalog.pg_roles r ON r.oid = m.member WHERE r.rolname IN ('pharos_app', 'pharos_worker')") ||
  die "cannot ask the database for the memberships of its roles"
[ "$members" = 0 ] || die "pharos_app and pharos_worker must not be members of any role"

# Everything that is not the system's: any schema, whoever owns it (information_schema.tables would
# show only what the owner may read, and only in public).
tables=$(psql_owner --command "SELECT count(*) FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace WHERE c.relkind IN ('r', 'p', 'v', 'm', 'S', 'f') AND n.nspname NOT IN ('pg_catalog', 'information_schema') AND n.nspname NOT LIKE 'pg\_toast%' AND n.nspname NOT LIKE 'pg\_temp%'") ||
  die "cannot ask the database for its tables"
[ "$tables" = 0 ] || die "the database already has $tables tables, views or sequences; restore only into a new, empty database"

# The list of what to restore, without the extension entries, kept in the container's tmpfs.
list_made=1
grep -v ' EXTENSION ' <<<"$listing" | compose exec -T postgres sh -c 'cat > /tmp/pharos-restore.list'

compose exec -T postgres pg_restore --username=pharos_owner --dbname=pharos \
  --exit-on-error --single-transaction --use-list=/tmp/pharos-restore.list <"$work/dump" ||
  die "pg_restore failed; nothing was restored (it ran in one transaction)"

# What a good dump never makes. If the dump did, the database is not to be trusted: remove its volume.
strange=$(psql_owner --command "SELECT (SELECT count(*) FROM pg_catalog.pg_db_role_setting WHERE setdatabase IN (0, (SELECT oid FROM pg_catalog.pg_database WHERE datname = current_database()))) + (SELECT count(*) FROM pg_catalog.pg_namespace WHERE nspname NOT IN ('public', 'information_schema') AND nspname NOT LIKE 'pg\_%')") ||
  die "cannot check the restored database"
[ "$strange" = 0 ] || die "the dump set database or role settings or made schemas that a Pharos dump never makes. Do NOT start the stack: remove the postgres volume and investigate where the dump came from"

restored=$(psql_owner --command "SELECT count(*) FROM information_schema.tables WHERE table_schema = 'public'")
echo "restore: done, $restored tables. Start the rest of the stack now (docker compose ... up -d)."
