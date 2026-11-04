#!/usr/bin/env bash
# Create the out-of-band Secret `lab-data-store` with random passwords. Run on a machine with kubectl access.
# Usage: make-secret.sh [namespace]   (default: lab)
# Idempotent: does nothing if the Secret exists. Never prints a value.
set -euo pipefail

ns="${1:-lab}"
name="lab-data-store"

if kubectl -n "$ns" get secret "$name" >/dev/null 2>&1; then
  echo "secret $name already exists in namespace $ns; to add keys use 'kubectl patch' (see add-source-passwords.sh)"
  exit 0
fi

gen() { openssl rand -hex 24; }

postgres_password="$(gen)"
ingest_password="$(gen)"
usage_owner_password="$(gen)"
usage_reader_password="$(gen)"
bank_owner_password="$(gen)"
bank_reader_password="$(gen)"
email_owner_password="$(gen)"
email_reader_password="$(gen)"

kubectl -n "$ns" create secret generic "$name" \
  --from-literal=POSTGRES_PASSWORD="$postgres_password" \
  --from-literal=INGEST_PASSWORD="$ingest_password" \
  --from-literal=USAGE_OWNER_PASSWORD="$usage_owner_password" \
  --from-literal=USAGE_READER_PASSWORD="$usage_reader_password" \
  --from-literal=BANK_OWNER_PASSWORD="$bank_owner_password" \
  --from-literal=BANK_READER_PASSWORD="$bank_reader_password" \
  --from-literal=EMAIL_OWNER_PASSWORD="$email_owner_password" \
  --from-literal=EMAIL_READER_PASSWORD="$email_reader_password" >/dev/null

echo "created secret $name in namespace $ns (8 keys)"
