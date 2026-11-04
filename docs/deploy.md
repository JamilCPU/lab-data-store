# Deploy runbook

The store runs on the lab Kubernetes cluster in namespace `lab`, deployed by Flux from the `lab-gitops` repo (`apps/lab-data-store/`). The manifests live there; the image, the node directories and the Secret are the out-of-band parts covered here. Commands that talk to the cluster run on the primary node (or any machine with `kubectl` access).

## What gets deployed

| Object | Purpose |
| --- | --- |
| StatefulSet `postgres` + headless Service `postgres` | Postgres with TimescaleDB, pinned to the node labelled `role=primary-worker`, data on a hostPath |
| CronJob `postgres-backup` | Nightly at 03:00 UTC: database dump, role dump, files archive; keeps 14 days |
| ConfigMap `ingest-sources` | The source registry (`sources.yaml`) |
| Deployment `ingest-api`, Service `ingest-api`, Ingress `ingest-api` | The write path, served at `https://ingest.lab` with a certificate from the lab CA |

## Prerequisites on the primary node (once)

```bash
sudo mkdir -p /data/lab/lab-data-store/postgres /data/lab/lab-data-store/files /srv/lab-data-store/backups
sudo chown 70:70 /data/lab/lab-data-store/postgres /srv/lab-data-store/backups   # postgres uid
sudo chown 10001:10001 /data/lab/lab-data-store/files                            # ingest-api uid
```

`/srv/lab-data-store/backups` should be on a different disk from `/data`, so a failure of the data disk does not take the backups with it.

## Create the Secret (once)

```bash
scripts/make-secret.sh          # namespace lab; does nothing if the Secret exists
kubectl -n lab get secret lab-data-store -o jsonpath='{.data}' | tr ',' '\n' | cut -d'"' -f2   # key names only
```

It generates random passwords for Postgres, the `ingest` role and each source's owner and reader roles. Values are never printed.

## Deploy

Merge the change in lab-gitops. Flux applies it within about a minute; to skip the wait:

```bash
flux reconcile source git flux-system
flux reconcile kustomization apps --with-source
flux get kustomizations
```

The `apps` Kustomization waits for every workload to be Ready, so a pod that never becomes Ready blocks all apps. If it stalls, look at `kubectl -n lab describe pod <pod>` and `kubectl -n lab logs deploy/ingest-api`.

## Verify

```bash
kubectl -n lab get pods,svc,ingress -l app.kubernetes.io/part-of=lab-data-store
kubectl -n lab get certificate ingest-lab-tls
curl --cacert ~/lab-tls/lab-ca.pem https://ingest.lab/healthz        # {"ok":true}
curl --cacert ~/lab-tls/lab-ca.pem -o /dev/null -w '%{http_code}\n' https://ingest.lab/readyz   # 200
kubectl -n lab exec postgres-0 -- psql -U postgres -d lab -c '\dn'   # store, usage, bank, email
kubectl -n lab logs deploy/ingest-api | tail
```

If the node cannot resolve `ingest.lab`, add `--resolve ingest.lab:443:<node-ip>` to the curl commands.

To run a backup now: `kubectl -n lab create job --from=cronjob/postgres-backup backup-manual-1`, then `ls -la /srv/lab-data-store/backups` on the node.

## Bump the image

1. On the primary node, from this repo: `scripts/build-push.sh` and note the printed `TAG=<tag>`.
2. In lab-gitops, set `newTag` for `registry.lab/lab/ingest-api` in `apps/lab-data-store/kustomization.yaml`.
3. Merge. The pod is recreated; on start it applies pending store migrations.

## Add a tenant

Follow [tenants.md](tenants.md): register the source, run `scripts/add-source-passwords.sh <source>`, add the env entries and the ConfigMap entry in lab-gitops, merge, then run the tenant's migrations as `<source>_owner`.

## Mint a device token

The plaintext token is printed once, on stdout; redirect it to a file so it never lands in terminal history:

```bash
kubectl -n lab exec deploy/ingest-api -- store-admin token create --device <name> --source usage > <name>.token
chmod 600 <name>.token
kubectl -n lab exec deploy/ingest-api -- store-admin token list
```

Copy the file to the device's configuration and delete the local copy. `store-admin --help` lists the other token commands, including revoke.

## Restore

Dumps are in `/srv/lab-data-store/backups`: `lab-<date>.dump` (custom format, restore with `pg_restore`), `globals-<date>.sql` (roles) and `files-<date>.tgz`. Restore the roles first so ownership is preserved, then the database into an empty one.
