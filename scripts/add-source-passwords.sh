#!/usr/bin/env bash
# Add <SOURCE>_OWNER_PASSWORD and <SOURCE>_READER_PASSWORD to the existing Secret `lab-data-store`.
# Usage: add-source-passwords.sh <source> [namespace]   (namespace default: lab)
# Skips keys that already exist, so reruns never rotate a live password. Never prints a value.
set -euo pipefail

source_name="${1:?usage: add-source-passwords.sh <source> [namespace]}"
ns="${2:-lab}"
name="lab-data-store"

if ! [[ "$source_name" =~ ^[a-z][a-z0-9_]*$ ]]; then
  echo "source must match ^[a-z][a-z0-9_]*$" >&2
  exit 2
fi
upper="$(printf '%s' "$source_name" | tr '[:lower:]' '[:upper:]')"

existing="$(kubectl -n "$ns" get secret "$name" -o jsonpath='{.data}' | tr ',' '\n' | cut -d'"' -f2)"
patch_items=()
for kind in OWNER READER; do
  key="${upper}_${kind}_PASSWORD"
  if grep -qx "$key" <<<"$existing"; then
    echo "$key already present; leaving it unchanged"
    continue
  fi
  value="$(openssl rand -hex 24 | tr -d '\n' | base64 | tr -d '\n')"
  patch_items+=("\"$key\":\"$value\"")
done

if [ "${#patch_items[@]}" -eq 0 ]; then
  exit 0
fi

body="$(IFS=,; echo "${patch_items[*]}")"
kubectl -n "$ns" patch secret "$name" --type merge -p "{\"data\":{${body}}}" >/dev/null
echo "added ${#patch_items[@]} key(s) for source $source_name to secret $name"
echo "next: add the env vars to the ingest-api Deployment in lab-gitops and register the source in ingest-sources"
