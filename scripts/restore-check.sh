#!/usr/bin/env bash
# Prove a backup is restorable: restore a dump into a throwaway TimescaleDB container and compare row counts
# with the live database. Runs on the primary node (needs docker; kubectl only when LIVE_* are unset).
# Usage: restore-check.sh <lab-dump-file> [globals-sql-file]
# Env:   LIVE_EVENTS / LIVE_TOKENS  live row counts of usage.events / store.device_tokens
#        (looked up with kubectl from postgres-0 in namespace `lab` when unset)
#        IMAGE  override the Postgres image (default: the one the cluster runs)
# Exit:  0 on PASS, 1 on FAIL.
set -euo pipefail

dump="${1:?usage: restore-check.sh <lab-dump-file> [globals-sql-file]}"
globals="${2:-}"
image="${IMAGE:-timescale/timescaledb:2.17.2-pg17}"
name="lds-restore"

[ -f "$dump" ] || { echo "dump not found: $dump" >&2; exit 1; }
[ -z "$globals" ] || [ -f "$globals" ] || { echo "globals file not found: $globals" >&2; exit 1; }

live_count() {
  kubectl -n lab exec postgres-0 -c postgres -- psql -U postgres -d lab -Atc "SELECT count(*) FROM $1"
}
live_events="${LIVE_EVENTS:-$(live_count usage.events)}"
live_tokens="${LIVE_TOKENS:-$(live_count store.device_tokens)}"

cleanup() { docker rm -f "$name" >/dev/null 2>&1 || true; }
trap cleanup EXIT
cleanup

pg() { docker exec -u postgres "$name" psql -v ON_ERROR_STOP=1 -X -q "$@"; }

echo "starting throwaway $image as $name"
docker run -d --name "$name" -e POSTGRES_PASSWORD=restore "$image" >/dev/null

# The image restarts the server once during its own init, so wait for two consecutive successes with a pause
# between them to be sure we are talking to the final server.
ready=0
for _ in $(seq 1 60); do
  if docker exec "$name" pg_isready -h 127.0.0.1 -U postgres -q; then
    sleep 3
    if docker exec "$name" pg_isready -h 127.0.0.1 -U postgres -q; then ready=1; break; fi
  fi
  sleep 1
done
[ "$ready" = 1 ] || { echo "postgres did not become ready" >&2; exit 1; }

docker cp "$dump" "$name:/tmp/lab.dump"
if [ -n "$globals" ]; then
  docker cp "$globals" "$name:/tmp/globals.sql"
  # Roles that already exist (postgres) and superuser attributes make psql report errors; they are expected on a
  # fresh server, so this is the ONE step where failure is tolerated. Missing app roles would surface later as
  # restore errors or a count mismatch.
  docker exec -u postgres "$name" psql -X -q -f /tmp/globals.sql >/dev/null 2>&1 || true
fi

docker exec -u postgres "$name" createdb lab

# TimescaleDB restore sequence: extension, pre_restore, pg_restore, post_restore.
pg -d lab -c "CREATE EXTENSION IF NOT EXISTS timescaledb;" -c "SELECT timescaledb_pre_restore();" >/dev/null

# pg_restore can exit 1 with "errors ignored on restore". Capture the code and let the row-count comparison
# below decide; --no-owner/--no-privileges keep the restore independent of the live roles.
set +e
docker exec -u postgres "$name" pg_restore -d lab --no-owner --no-privileges /tmp/lab.dump
restore_rc=$?
set -e
echo "pg_restore exit code: $restore_rc"

pg -d lab -c "SELECT timescaledb_post_restore();" >/dev/null

restored_events="$(pg -d lab -Atc 'SELECT count(*) FROM usage.events')"
restored_tokens="$(pg -d lab -Atc 'SELECT count(*) FROM store.device_tokens')"

verdict=PASS
[ "$restored_events" = "$live_events" ] || verdict=FAIL
[ "$restored_tokens" = "$live_tokens" ] || verdict=FAIL
# Exit 1 is tolerated (warnings); anything higher means pg_restore itself failed.
[ "$restore_rc" -le 1 ] || verdict=FAIL

printf '\n%-24s %8s %8s\n' table live restored
printf '%-24s %8s %8s\n' usage.events "$live_events" "$restored_events"
printf '%-24s %8s %8s\n' store.device_tokens "$live_tokens" "$restored_tokens"
echo "$verdict"
[ "$verdict" = PASS ]
