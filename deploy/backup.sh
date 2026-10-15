#!/bin/sh
# Scheduled PostgreSQL backups for the production stack.
#
#   sh backup.sh          dump now, then every BACKUP_INTERVAL_SECONDS, forever
#   sh backup.sh once     dump now and exit (make prod-backup)
#
# Connection settings come from libpq's own variables (PGHOST, PGUSER,
# PGPASSWORD, PGDATABASE), so the password never appears on a command line.
#
# The custom format (-Fc) is compressed and restores selectively with
# pg_restore. The whole database is dumped, LangGraph's checkpoint tables
# included, so a restore brings back conversations *and* their graph state.
set -eu

dir=${BACKUP_DIR:-/backups}
interval=${BACKUP_INTERVAL_SECONDS:-86400}
keep_days=${BACKUP_KEEP_DAYS:-14}

# Dumps hold every account and conversation: readable by their owner only.
umask 077
mkdir -p "$dir"

log() {
    # One JSON object per line, like the API's logs, so one collector reads both.
    printf '{"timestamp":"%s","event":"%s"%s}\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$1" "${2:-}"
}

dump() {
    stamp=$(date -u +%Y%m%dT%H%M%SZ)
    final="$dir/app-$stamp.dump"
    partial="$dir/.app-$stamp.dump.partial"
    started=$(date +%s)
    # Written under a temporary name and renamed only when complete: a dump
    # cut short by a crash or a full disk never looks like a good backup.
    if pg_dump --format=custom --no-owner --no-privileges --file="$partial"; then
        mv "$partial" "$final"
        size=$(wc -c < "$final" | tr -d ' ')
        log backup_completed ",\"file\":\"$(basename "$final")\",\"bytes\":$size,\"seconds\":$(( $(date +%s) - started ))"
    else
        rm -f "$partial"
        log backup_failed
        return 1
    fi
    # Retention. Only finished dumps match the pattern, never a partial one.
    find "$dir" -name 'app-*.dump' -type f -mtime +"$keep_days" -print -delete |
        while read -r old; do log backup_pruned ",\"file\":\"$(basename "$old")\""; done
}

if [ "${1:-}" = "once" ]; then
    dump
    exit
fi

while true; do
    # A failed dump is logged and retried next interval; the health check
    # turns the container unhealthy once no recent dump exists.
    dump || true
    sleep "$interval"
done
