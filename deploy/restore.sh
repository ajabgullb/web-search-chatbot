#!/bin/sh
# Restore a dump made by backup.sh into the database named by PGDATABASE.
#
#   sh restore.sh /backups/app-20261011T020000Z.dump
#
# Destructive: every object in the dump is dropped and recreated, so whatever
# was written since the dump is lost. Stop the API first (make prod-restore
# does), or requests will write into a half-restored database.
set -eu

file=${1:?usage: restore.sh <dump file>}
[ -f "$file" ] || { echo "restore: no such file: $file" >&2; exit 1; }

# Refuse anything that is not a complete pg_dump archive before touching data.
pg_restore --list "$file" > /dev/null

# --single-transaction: a failure part-way leaves the database as it was,
# not half old and half new.
pg_restore --clean --if-exists --no-owner --no-privileges \
    --single-transaction --exit-on-error --dbname="$PGDATABASE" "$file"

echo "restore: $PGDATABASE restored from $(basename "$file")" >&2
