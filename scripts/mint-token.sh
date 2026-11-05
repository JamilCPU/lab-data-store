#!/usr/bin/env bash
# Mint an ingest token for a device. Run on a machine with kubectl access.
# Usage: mint-token.sh <device> <source> [more sources...]
# Prints only the plaintext token on stdout; redirect it to a file:
#   (umask 077; mint-token.sh <device> usage > ~/.config/usage-sync/token)
# store-admin's own messages pass through on stderr.
set -euo pipefail

if [ "$#" -lt 2 ]; then
  echo "usage: mint-token.sh <device> <source> [more sources...]" >&2
  exit 2
fi

device="$1"
shift
args=()
for source in "$@"; do
  args+=(--source "$source")
done

exec kubectl -n "${NAMESPACE:-lab}" exec deploy/ingest-api -c ingest-api -- \
  store-admin token create --device "$device" "${args[@]}"
